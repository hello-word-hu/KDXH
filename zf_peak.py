"""正方教务 高峰期抢课引擎（纯 HTTP，不走浏览器）。

为什么要单独一份
----------------
原有 `GrabEngine` 是**点浏览器按钮**提交的：一次要点、要等弹窗、要解析 DOM，
一轮下来好几秒。高峰期那几分钟，这几秒就是名额。
而正方的提交接口本身很轻（一个 POST 就完事），所以真正快的做法是：

    先读一次页面把目标课的 jxb_id 拿到手
        ↓
    之后不再读页面，直接对着提交接口高频投

这和南审那套是同一个思路。另外这里把"故障种类"拆得更细 —— 高峰期最常见
的几种情况必须分开对待，混在一起处理必然出错：

    5xx / 网关错误  -> 请求根本没到应用，"确定没送到"，立刻重投
    超时 / 连接断开 -> 结果不明！服务器可能已经选上了，绝不能当失败
    "人数已满"      -> 这门没戏
    "已经选过"      -> 反而是**我们自己**刚才那次超时成功了
    "不在选课时间"  -> 还没开放，继续投，节奏绝不退让（退让就错过开闸瞬间）
    "登录超时"      -> 会话掉了，用账号密码自动重登（正方不需要验证码）
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from zf_client import Config, LoginError, OpenZfClient, StopError, ZfError

# --------------------------------------------------------------------------
# 反馈归类
# --------------------------------------------------------------------------
OK_MARKS = ("选课成功", "选定成功")
ALREADY_MARKS = ("已选过", "已经选过", "重复选课", "已选该课程", "已选定",
                 "已经选中")
FULL_MARKS = ("已满", "人数已满", "容量已满", "余量不足", "超出容量",
              "已选满", "剩余容量不足", "教学班人数已满")
NOT_OPEN_MARKS = ("不在选课时间", "未到选课时间", "选课未开始", "未开放",
                  "不属于选课阶段", "没有选课")
SESSION_MARKS = ("登录超时", "会话已过期", "未登录", "请重新登录",
                 "登录已失效", "会话失效")
FATAL_MARKS = ("无选课权限", "不允许选课", "选课已结束", "选课时间已过",
               "不在选课范围")


def classify_submit(text: str, status: int = 200) -> str:
    """把一次提交响应归类。

    顺序很关键：先判"已经选过"（它里面也含"已选"），再判"已满"，
    最后才轮到"成功"。跟南审那次踩的坑一模一样。
    """
    if status == 0:
        return "unknown"          # 完全没拿到响应 —— 不可知
    if status >= 500:
        return "gateway"          # 网关错误，请求没到应用
    t = (text or "").strip()
    if not t:
        return "unknown"
    if re.search(r"csrftoken|login_slogin", t) and "<form" in t:
        return "session"
    for m in ALREADY_MARKS:
        if m in t:
            return "already"
    for m in FULL_MARKS:
        if m in t:
            return "full"
    for m in NOT_OPEN_MARKS:
        if m in t:
            return "not_open"
    for m in SESSION_MARKS:
        if m in t:
            return "session"
    for m in FATAL_MARKS:
        if m in t:
            return "stop"
    if t.lstrip().startswith("<"):
        # 一坨 HTML，但既不像成功也不像已知错误 —— 端点或页面变了
        if "选课成功" in t:
            return "ok"
        return "unknown"
    try:
        js = json.loads(t)
    except Exception:
        js = None
    if isinstance(js, dict):
        msg = str(js.get("msg") or js.get("message") or js.get("info") or "")
        flag = str(js.get("flag") or js.get("status") or js.get("code") or "")
        positive = flag in ("1", "success", "true", "200") and "失败" not in msg
        if msg:
            for m in ALREADY_MARKS:
                if m in msg:
                    return "already"
            for m in FULL_MARKS:
                if m in msg:
                    return "full"
            for m in NOT_OPEN_MARKS:
                if m in msg:
                    return "not_open"
            for m in SESSION_MARKS:
                if m in msg:
                    return "session"
        if positive:
            return "ok"
        if msg:
            return "error"
    if any(m in t for m in OK_MARKS):
        return "ok"
    return "error"


@dataclass
class PeakCourse:
    jxb_id: str = ""
    kch_id: str = ""
    name: str = ""
    teacher: str = ""
    raw: str = ""
    index: int = 0

    def __str__(self) -> str:
        bits = [self.name or self.jxb_id]
        if self.teacher:
            bits.append(self.teacher)
        return " | ".join(bits)

    # 下面这些是为了能直接喂给界面的课程表格（它要的字段名不一样）
    @property
    def cells(self) -> List[str]:
        return [c.strip() for c in (self.raw or "").split("|")]

    @property
    def kch(self) -> str:
        return self.kch_id

    @property
    def kcmc(self) -> str:
        return self.name

    @property
    def time_text(self) -> str:
        c = self.cells
        return c[2] if len(c) > 2 else ""

    @property
    def capacity(self) -> str:
        c = self.cells
        return c[4] if len(c) > 4 else ""

    @property
    def already(self) -> bool:
        r = self.raw or ""
        return "已选" in r and "选课" not in r


@dataclass
class PeakResult:
    ok: bool = False
    kind: str = "unknown"
    message: str = ""
    course: Optional[PeakCourse] = None
    status: int = 200
    raw: str = ""


def parse_courses(page: str) -> List[PeakCourse]:
    """从选课页解析课程（复用 zf_client 的解析，再补上课程名/教师）。"""
    out: List[PeakCourse] = []
    for i, d in enumerate(OpenZfClient.parse_courses(page)):
        text = d.get("text", "")
        cells = [c.strip() for c in text.split("|")]
        name = cells[0] if cells else ""
        teacher = cells[1] if len(cells) > 1 else ""
        out.append(PeakCourse(jxb_id=d.get("jxb_id", ""),
                              kch_id=d.get("kch_id", ""),
                              name=name, teacher=teacher, raw=text,
                              index=i))
    return out


def match(courses: List[PeakCourse], keyword: str) -> List[PeakCourse]:
    kw = (keyword or "").strip()
    if not kw:
        return list(courses)
    parts = [p for p in re.split(r"[\s,，、;；]+", kw) if p]
    hit = []
    for c in courses:
        blob = " ".join([c.name, c.teacher, c.jxb_id, c.kch_id,
                         c.raw]).lower()
        if all(p.lower() in blob for p in parts):
            hit.append(c)
    return hit


_TIME_RE = re.compile(
    r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?")


def parse_open_time(page: str) -> Optional[float]:
    """从选课页里读出"本轮选课开始时间"。

    知道确切开放时刻就能：离开放还早时待命（不白刷几小时），
    到点前几秒才开始猛投（不错过开闸那一瞬间）。
    读不到也没关系 —— 那就持续投。
    """
    text = re.sub(r"<script.*?</script>", " ", page or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    # 只在提到"选课时间/开始时间/开放时间"的附近找，避免抓到无关日期
    anchors = ("选课时间", "开始时间", "开放时间", "选课日期", "本轮选课")
    for a in anchors:
        i = text.find(a)
        if i < 0:
            continue
        seg = text[i:i + 120]
        m = _TIME_RE.search(seg)
        if not m:
            continue
        y, mo, d, h, mi = (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                           int(m.group(4)), int(m.group(5)))
        s = int(m.group(6) or 0)
        try:
            return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# 抢课执行
# --------------------------------------------------------------------------
class PeakGrabber:
    """登录之后，用 HttpClient 直接对着提交接口投。"""

    def __init__(self, client: OpenZfClient,
                 log: Callable[[str], None] = print):
        self.c = client
        self.log = log
        self.page_path: Optional[str] = None
        self.courses: List[PeakCourse] = []
        self.open_at: Optional[float] = None
        self.endpoint: Optional[str] = None     # 已经确认可用的提交地址
        self.submits = 0
        self.gateway_errors = 0
        self.unknowns = 0
        self.relogins = 0
        self._stop_hint: Optional[Callable[[], bool]] = None

    # ---------------- 读 ----------------
    def find_page(self, attempts: int = 3) -> Tuple[str, str]:
        """定位选课页。

        高峰期探测失败是常态，所以：
          1. 已经知道地址就直接用（快路径）—— 不要每次都从候选列表
             第一个开始扫，否则一次网络抖动就白扫一圈，几十秒就没了；
          2. 不知道才去逐个探测，并且失败后重试而不是直接放弃。
        """
        last = ""
        for i in range(max(1, attempts)):
            if self._stop_hint and self._stop_hint():
                raise ZfError("已停止")
            # 快路径：上次确认过的地址
            if self.page_path:
                try:
                    html = self.c.fetch(self.page_path)
                    if not self.c._LOGIN_FORM_RE.search(html):
                        return self.page_path, html
                except Exception as exc:
                    last = "%s: %s" % (type(exc).__name__, exc)
            try:
                found = self.c.find_select_page()
            except Exception as exc:
                last = "%s: %s" % (type(exc).__name__, exc)
                found = None
            if found:
                self.page_path, html = found
                return self.page_path, html
            last = last or "所有候选地址都没返回选课页"
            time.sleep(0.3 + random.random() * 0.5)
        raise ZfError("找不到选课页（可能不在选课期或页面改版）：%s" % last)

    def read_courses(self) -> List[PeakCourse]:
        """读一次课程表。高峰期断连/502 是常态，所以自己重试几次。"""
        if not self.page_path:
            self.find_page()
        last = ""
        html = ""
        for i in range(3):
            try:
                html = self.c.fetch(self.page_path)
                break
            except Exception as exc:
                last = "%s: %s" % (type(exc).__name__, exc)
                time.sleep(0.25 + random.random() * 0.4)
        else:
            raise ZfError("读选课页失败：%s" % last)
        if self.c._LOGIN_FORM_RE.search(html):
            raise ZfError("会话已失效")
        not_open = self.c.detect_not_open(html)
        self.courses = parse_courses(html)
        t = parse_open_time(html)
        if t:
            self.open_at = t
        if not_open and not self.courses:
            self.log("选课页提示：%s" % not_open)
        return self.courses

    def is_selected(self, course: PeakCourse) -> bool:
        """回查：这一行是不是已经变成"已选"了。"""
        try:
            html = self.c.fetch(self.page_path)
        except Exception:
            return False
        for c in parse_courses(html):
            if c.jxb_id == course.jxb_id:
                blob = c.raw
                return ("已选" in blob and "选课" not in blob) or "退选" in blob
        return False

    # ---------------- 写 ----------------
    def submit(self, course: PeakCourse) -> PeakResult:
        """提交一次选课。

        刻意**不自动重试**：一旦超时，服务器可能已经处理了；盲目重发会
        收到"已满"，把胜利误判成失败。超时的返回 kind="unknown"，
        由调用方决定回查还是继续投。
        """
        if not self.endpoint:
            return self._discover_endpoint(course)
        self.submits += 1
        try:
            final, body, status = self.c._request(
                self.endpoint,
                data={"jxb_ids": course.jxb_id, "jxb_id": course.jxb_id},
                ajax=True, referer=self._referer(), timeout=8.0)
        except LoginError:
            return PeakResult(False, "session", "会话已失效", course)
        except ZfError as exc:
            # 连接层失败：可能是超时/断开 —— 结果不明
            self.unknowns += 1
            return PeakResult(False, "unknown", "网络不通：%s" % exc, course, 0)
        kind = classify_submit(body, status)
        if kind == "gateway":
            self.gateway_errors += 1
        elif kind == "unknown":
            self.unknowns += 1
        return PeakResult(kind in ("ok", "already"), kind,
                          _short(body) or kind, course, status, body)

    def _referer(self) -> str:
        mod = (self.page_path or "/xsxk/x").strip("/").split("/")[0]
        return "%s/%s/xsxk_list.html?gnmkdm=%s" % (
            self.c.cfg.base_url, mod, self.c.cfg.gnmkdm)

    def _discover_endpoint(self, course: PeakCourse) -> PeakResult:
        """第一次提交时挨个试候选端点，记住能用的那个。

        只有这一步会慢；确定之后后面全是直投。
        """
        cfg = self.c.cfg
        self.log("正在探测可用的选课提交端点（只做一次）…")
        unknowns: List[str] = []
        for mod in cfg.modules:
            for leaf in self.c._SELECT_LEAVES:
                path = "/%s/%s.html?gnmkdm=%s" % (mod, leaf, cfg.gnmkdm)
                self.submits += 1
                try:
                    final, body, status = self.c._request(
                        path,
                        data={"jxb_ids": course.jxb_id,
                              "jxb_id": course.jxb_id},
                        ajax=True, referer=self._referer(), timeout=8.0)
                except LoginError:
                    return PeakResult(False, "session", "会话已失效", course)
                except ZfError as exc:
                    unknowns.append("%s/%s: %s" % (mod, leaf, exc))
                    continue
                kind = classify_submit(body, status)
                if kind in ("ok", "already", "full", "not_open", "stop",
                            "error", "session"):
                    # 服务端认得这个地址（回的是业务错误而不是"页面不存在"）
                    if kind == "unknown":
                        unknowns.append("%s/%s" % (mod, leaf))
                        continue
                    self.endpoint = path
                    self.log("提交端点已确定：%s" % path, )
                    return PeakResult(kind in ("ok", "already"), kind,
                                      _short(body) or kind, course, status,
                                      body)
                unknowns.append("%s/%s: %s" % (mod, leaf, kind))
        raise ZfError("找不到可用的选课提交端点：%s"
                      % "; ".join(unknowns[:4]))

    # ---------------- 会话 ----------------
    def relogin(self) -> bool:
        """正方不需要验证码，直接用账号密码重登。"""
        try:
            self.c.logged_in = False
            self.c._public_key = None
            self.c.login(max_retries=3)
            self.relogins += 1
            self.page_path = None
            return True
        except Exception as exc:
            self.log("自动重登失败：%s" % exc)
            return False


def _short(s: str, n: int = 120) -> str:
    t = " ".join((s or "").split())
    return t[:n]


# --------------------------------------------------------------------------
# 抢课线程
# --------------------------------------------------------------------------
class PeakRunner(threading.Thread):
    """后台抢课：定位 -> 待命 -> 高频直投。

    和南审那套一样的关键设计：
      · 定位是关键路径，失败要**立刻**重试，不能傻等间隔
      · 有目标就永远优先提交；页面读不动了也要继续投
      · 待命期间探活，趁没开放把会话续好
      · 5xx = 确定没送到 -> 立刻重投；超时 = 结果不明 -> 再投一次看是否"已选过"
      · "不在选课时间" 不退避
    """

    def __init__(self, grabber: PeakGrabber, keywords: List[str],
                 interval: float = 1.0, max_interval: float = 8.0,
                 submit_interval: float = 0.5,
                 prewarm: float = 5.0,
                 refresh_after: float = 60.0,
                 keepalive_every: float = 6.0,
                 on_event: Optional[Callable[[str], None]] = None,
                 on_courses: Optional[Callable[[List[PeakCourse]], None]] = None,
                 on_hit: Optional[Callable[[PeakResult], None]] = None,
                 on_status: Optional[Callable[[Dict[str, Any]], None]] = None,
                 stop_on_success: bool = True):
        super().__init__(daemon=True)
        self.g = grabber
        self.keywords = [k for k in keywords if k and k.strip()]
        self.interval = max(0.15, float(interval))
        self.max_interval = max(self.interval, float(max_interval))
        self.submit_interval = max(0.05, float(submit_interval))
        self.prewarm = max(0.0, float(prewarm))
        self.refresh_after = max(5.0, float(refresh_after))
        self.keepalive_every = max(1.0, float(keepalive_every))
        self.on_event = on_event or (lambda m: None)
        self.on_courses = on_courses or (lambda c: None)
        self.on_hit = on_hit or (lambda r: None)
        self.on_status = on_status or (lambda d: None)
        self.stop_on_success = stop_on_success

        self._stop = threading.Event()
        self.wins: List[PeakResult] = []
        self.rounds = 0
        self.targets: List[PeakCourse] = []
        self.located_at = 0.0
        self.first_located_at = 0.0
        self.last_keepalive = 0.0
        self.rot = 0
        self.phase = "扫描"
        self.last_message = ""
        self.need_login = False

    # ---- 工具 ----
    def stop(self) -> None:
        self._stop.set()

    def say(self, msg: str, keep: bool = True) -> None:
        if keep:
            self.last_message = msg
        try:
            self.on_event(msg)
        except Exception:
            pass
        self.emit_status()

    def emit_status(self) -> None:
        try:
            self.on_status({
                "phase": self.phase,
                "rounds": self.rounds,
                "submits": self.g.submits,
                "gateway": self.g.gateway_errors,
                "unknown": self.g.unknowns,
                "relogins": self.g.relogins,
                "interval": self.interval,
                "targets": [str(c) for c in self.targets],
                "last": self.last_message,
            })
        except Exception:
            pass

    # ---- 会话 ----
    def recover(self) -> bool:
        self.say("会话掉了，正在用账号密码自动重登（正方不需要验证码）…")
        for attempt in range(3):
            if self._stop.is_set():
                return False
            if self.g.relogin():
                self.say("重登成功，继续抢")
                self.phase = "抢课"
                return True
            self._stop.wait(1.0 + attempt)
        self.need_login = True
        self.phase = "等重新登录"
        self.say("！！自动重登失败，请在界面上重新登录")
        return False

    def keepalive(self) -> None:
        """待命期间探活：趁还没开放把会话续好。

        不探活的话，会话会在等待期间悄悄过期，脚本到开放前几秒才发现，
        然后忙着重登 —— 而那几秒正是名额被抢光的时候。
        """
        now = time.time()
        if now - self.last_keepalive < self.keepalive_every:
            return
        self.last_keepalive = now
        try:
            courses = self.g.read_courses()
            self.located_at = now
            if courses:
                hits = self._hits(courses)
                if hits:
                    self.targets = hits
        except ZfError as exc:
            if "会话" in str(exc) or "登录" in str(exc):
                self.recover()
        except Exception:
            pass

    # ---- 定位 ----
    def _hits(self, courses: List[PeakCourse]) -> List[PeakCourse]:
        uniq: Dict[str, PeakCourse] = {}
        for kw in (self.keywords or [""]):
            for c in match(courses, kw):
                uniq[c.jxb_id] = c
        return list(uniq.values())

    def discover(self) -> bool:
        self.phase = "扫描"
        self.emit_status()
        try:
            courses = self.g.read_courses()
        except ZfError as exc:
            msg = str(exc)
            if "会话" in msg or "登录" in msg:
                if not self.recover():
                    return False
                self.g.page_path = None
                return False
            # 页面读不动：已经知道地址就别丢掉它，下次直接用
            raise
        if not self.g.page_path:
            raise ZfError("还没定位到选课页")
        self.last_message = ""
        try:
            self.on_courses(courses)
        except Exception:
            pass
        if not courses:
            self.say("页面正常，但暂时没有课程（多半还没开放），继续等…")
            return False
        hits = self._hits(courses)
        if not hits:
            self.say("读到 %d 门课，还没有匹配「%s」的"
                     % (len(courses), " / ".join(self.keywords)))
            return False
        self.targets = hits
        self.located_at = time.time()
        if not self.first_located_at:
            self.first_located_at = self.located_at
        self.say("目标已就位：%s（共 %d 门匹配）"
                 % ("、".join(str(h) for h in hits[:4]), len(hits)))
        return True

    # ---- 提交 ----
    def pump(self) -> bool:
        """投一轮。返回 True 表示抢到了。"""
        self.phase = "抢课"
        self.emit_status()
        # 知道开放时间且还没到 —— 待命，但期间要探活
        while (self.g.open_at and not self._stop.is_set()
               and time.time() < self.g.open_at - self.prewarm):
            left = self.g.open_at - time.time()
            if left > 1:
                self.say("选课 %s 开放（还有 %.0f 秒），先待命并保持会话"
                         % (time.strftime("%H:%M:%S",
                                          time.localtime(self.g.open_at)),
                            left), keep=False)
            self._stop.wait(min(max(left - self.prewarm, 0.2), 2.0))
            if self._stop.is_set():
                return False
            if time.time() >= self.g.open_at - self.prewarm:
                break
            self.keepalive()
        if self.g.open_at and time.time() < self.g.open_at - self.prewarm:
            return False

        n = len(self.targets)
        if n == 0:
            return False
        if n > 1:
            order = [self.targets[(self.rot + i) % n] for i in range(n)]
            self.rot = (self.rot + 1) % n
        else:
            order = list(self.targets)

        for c in order:
            if self._stop.is_set():
                return False
            r = self.g.submit(c)
            if self._stop.is_set():
                return False

            if r.kind in ("ok", "already"):
                self.wins.append(r)
                try:
                    self.on_hit(r)
                except Exception:
                    pass
                self.say("★ 抢到了：%s —— %s" % (c, r.message))
                self.phase = "已抢到"
                self.emit_status()
                return True

            if r.kind == "session":
                if not self.recover():
                    return False
                self.g.page_path = None
                return False

            if r.kind == "gateway":
                # 502/504：请求根本没到应用，立刻重投，不用回查也不用等
                if self.g.submits % 25 == 1:
                    self.say("网关报错，立刻重投…", keep=False)
                self._stop.wait(0.05)
                return False

            if r.kind == "not_open":
                if self.g.submits % 20 == 1:
                    self.say("还没开放（%s），继续投…" % r.message, keep=False)
                self._stop.wait(self.submit_interval
                                * (0.7 + random.random() * 0.6))
                return False

            if r.kind == "unknown":
                # 结果不明：先别读页面（那要一两秒），再投一次。
                # 服务器对重复提交会回"已经选过" —— 那本身就是成功的证据。
                self.g.unknowns += 1
                if self.g.submits % 25 == 1:
                    self.say("提交结果不明（%s），再投一次看看…" % r.message,
                             keep=False)
                self._stop.wait(self.submit_interval * 0.5)
                return False

            if r.kind == "full":
                self.say("「%s」已满（%s），换别的" % (c.name, r.message))
                self.targets = [x for x in self.targets
                                if x.jxb_id != c.jxb_id]
                if not self.targets:
                    self.g.page_path = None
                return False

            if r.kind == "stop":
                self.say("服务端说：%s —— 停止" % r.message)
                self._stop.set()
                return False

            self.say("提交返回：%s —— %s" % (r.message[:60], c))
            self._stop.wait(self.submit_interval)
            return False
        return False

    # ---- 主循环 ----
    def run(self) -> None:  # noqa: C901
        self.g._stop_hint = self._stop.is_set
        try:
            while not self._stop.is_set():
                self.rounds += 1
                try:
                    if not self.targets:
                        if not self.discover():
                            self._stop.wait(0.2 + random.random() * 0.3)
                            continue
                    if self.pump():
                        if self.stop_on_success:
                            return
                    if (self.targets
                            and time.time() - self.located_at
                            > self.refresh_after):
                        self.discover()
                except ZfError as exc:
                    self.say("扫描受阻：%s" % str(exc)[:90], keep=False)
                    if not self.targets:
                        self._stop.wait(0.2 + random.random() * 0.3)
                        continue
                    self._stop.wait(min(self.max_interval, self.interval))
                except Exception as exc:
                    self.say("出错：%s: %s" % (type(exc).__name__,
                                              str(exc)[:110]), keep=False)
                    self._stop.wait(min(self.max_interval, self.interval))
        finally:
            self.phase = "已停止"
            self.emit_status()
            self.say("已停止（共 %d 轮，提交 %d 次）"
                     % (self.rounds, self.g.submits))
