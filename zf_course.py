#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
课程数据获取与抢课

设计原则（重要）：
    本校的「自主选课」数据接口是正方内部的 jqGrid 接口，参数复杂且未公开。
    实测所有候选端点在非选课期都返回同一个通用空响应，无法验证真伪。

    因此这里采用「已证明可用」的机制：
      1. 浏览课程列表：优先用浏览器读取真实渲染后的 DOM（一定准）；
         同时尝试 jqGrid JSON 接口（快），谁有数据用谁。
      2. 提交选课：一律通过浏览器点击学校自己的「选课」按钮 ——
         那是学校前端代码，一定能工作，不依赖任何猜测的接口。

    这样在选课高峰期（服务器慢、接口可能变）也能稳定工作。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["Course", "SelectResult", "CourseSource", "GrabEngine",
           "GrabRunner", "SELECT_TEXT_HINTS", "classify_feedback",
           "match_courses"]

# 页面上「选课」按钮可能出现的文字
SELECT_TEXT_HINTS = ("选课", "选择", "选定", "报名", "我要选", "选 课")
# 表示已经选过
DONE_TEXT_HINTS = ("退选", "退课", "已选", "已选定", "取消", "已报名")

# 课程列表 jqGrid 接口（正方内部）；不带参数会返回通用空响应是对的
JQGRID_PATHS = (
    "/xsxk/zzxkyzb_cxZzxkYzb.html",
    "/xsxk/zzxkyzb_cxZzxkYzbList.html",
)
# 选课页上的「学年/学期」控件 id（用于必要时触发一次查询）
TERM_INPUT_IDS = ("xkxnm", "xkxqm", "xnm", "xqm")


def discover_grid_url(driver) -> Optional[str]:
    """从活着的页面上读出 jqGrid 真正使用的数据地址。

    为什么这样做：选课页的表格初始化代码只在选课开放时才注入，
    静态抓 JS 拿不到地址。但页面一旦有了 grid，它的配置就在实例里，
    可以直接问出来 —— 这样无需猜测，永远和学校前端保持一致。
    """
    if driver is None:
        return None
    js = r"""
    try {
      if (typeof jQuery === 'undefined') return null;
      var out = null;
      // 1) 直接的 jqGrid 实例
      jQuery('table.ui-jqgrid-btable, table[id]').each(function(){
        if (out) return;
        var $t = jQuery(this);
        try {
          if (typeof $t.jqGrid !== 'function') return;
          var u = $t.jqGrid('getGridParam', 'url');
          if (u) out = String(u);
        } catch(e) {}
      });
      if (out) return out;
      // 2) 退一步：从全局变量里找
      for (var k in window) {
        try {
          var v = window[k];
          if (v && typeof v === 'object' && typeof v.url === 'string'
              && v.url.indexOf('.html') >= 0) { return v.url; }
        } catch(e) {}
      }
      return null;
    } catch(e) { return null; }
    """
    try:
        u = driver.execute_script(js)
        return str(u) if u else None
    except Exception:
        return None


def prime_select_page(driver) -> List[str]:
    """确保选课页已触发一次查询（有些学校要选了学年学期才出课程）。

    返回做过的动作说明，便于日志排查。
    """
    actions: List[str] = []
    if driver is None:
        return actions
    js = r"""
    var done = [];
    try {
      if (typeof jQuery === 'undefined') return done;

      // 1) 学年/学期下拉：如果没选，选中第一个非空值
      ['xnm','xqm','xkxnm','xkxqm'].forEach(function(id){
        var el = document.getElementById(id);
        if (!el || el.tagName !== 'SELECT') return;
        if (el.value) return;
        for (var i = 0; i < el.options.length; i++) {
          if (el.options[i].value) {
            el.value = el.options[i].value;
            try { jQuery(el).trigger('change'); } catch(e) {}
            done.push('选择了 ' + id + '=' + el.value);
            break;
          }
        }
      });

      // 2) 点一次「查询」按钮（文案匹配）
      var texts = ['查询','查 询','搜索','检索','确定'];
      var clicked = false;
      jQuery('a,button,input[type=button],input[type=submit]').each(function(){
        if (clicked) return;
        var t = (jQuery(this).text() || jQuery(this).val() || '').trim();
        if (texts.indexOf(t) >= 0 && jQuery(this).is(':visible')) {
          try { jQuery(this).trigger('click'); clicked = true;
                done.push('点击了「' + t + '」'); } catch(e) {}
        }
      });

      // 3) 如果 jqGrid 已存在但没数据，强制 reload
      if (!clicked) {
        jQuery('table.ui-jqgrid-btable').each(function(){
          try {
            jQuery(this).jqGrid('setGridParam',
              {page:1}).trigger('reloadGrid');
            done.push('触发了表格重新加载');
          } catch(e) {}
        });
      }
    } catch(e) {}
    return done;
    """
    try:
        res = driver.execute_script(js)
        if isinstance(res, list):
            actions = [str(x) for x in res]
    except Exception:
        pass
    return actions

SELECT_PAGE = "/xsxk/zzxkyzb_cxZzxkYzbIndex.html?gnmkdm=N253512"


@dataclass
class Course:
    """一门课（一个教学班）。"""
    index: int = -1                       # 在页面表格里的行号（点按用的）
    kch: str = ""                         # 课程代码
    kcmc: str = ""                        # 课程名称
    teacher: str = ""                     # 教师
    time_text: str = ""                   # 上课时间
    room: str = ""                        # 地点
    weeks: str = ""                       # 周次
    capacity: str = ""                    # 容量/已选
    jxb_id: str = ""                      # 教学班 ID
    button_text: str = ""                 # 按钮文字
    already: bool = False                 # 是否已选
    cells: List[str] = field(default_factory=list)

    def text(self) -> str:
        return " | ".join(c for c in self.cells if c)

    def __str__(self) -> str:
        return self.text()[:160]


@dataclass
class SelectResult:
    """一次选课尝试的结果。"""
    ok: bool = False
    message: str = ""
    course: Optional[Course] = None


# 服务端反馈分类
_OK_MARKS = ("选课成功", "选定成功", "成功", "已选定")
_RETRY_MARKS = ("已满", "人数已满", "容量已满", "余量不足", "超出容量",
                "已被选完", "已选满", "剩余容量不足", "超过容量")
_STOP_MARKS = ("已选过", "已经选过", "重复选课", "已选该课程",
               "不在选课时间内", "选课时间已过", "未到选课时间", "未开放",
               "无选课权限", "不允许选课", "与已选课程冲突", "上课时间冲突")


def classify_feedback(text: str) -> str:
    """把服务端反馈归类：ok / already / retry / stop / unknown。"""
    t = text or ""
    for k in ("已选过", "已经选过", "重复选课", "已选该课程"):
        if k in t:
            return "already"
    for k in _STOP_MARKS:
        if k in t:
            return "stop"
    for k in _RETRY_MARKS:
        if k in t:
            return "retry"
    for k in _OK_MARKS:
        if k in t:
            return "ok"
    return "unknown"


class GrabEngine:
    """抢课引擎。

    特点：
      · 浏览器点击提交（学校自己的前端逻辑，最可靠）
      · 支持多目标课程，可多线程并发尝试
      · 自动重试，遇到「已满」继续，遇到「已选过」「不在选课期」停止
      · 用事件回调把进度实时推给界面
    """

    def __init__(self, source: CourseSource, driver,
                 log: Callable[[str], None] = print,
                 stop_flag=None):
        self.source = source
        self.driver = driver
        self.log = log
        self.stop_flag = stop_flag

    # ---------------- 提交一次选课 ----------------
    def submit(self, course: Course) -> SelectResult:
        """点击这一行的「选课」按钮，并读取服务端反馈。"""
        res = SelectResult(course=course)
        if self.driver is None:
            res.message = "没有浏览器可用"
            return res

        from zf_grab import BrowserGrab
        bg = BrowserGrab(self.driver, self.source.base_url, log=self.log)

        # 先清掉可能残留的弹窗，否则点击会被挡住
        try:
            bg.handle_dialogs()
        except Exception:
            pass

        try:
            bg._enter_content_frame()
        except Exception:
            pass

        clicked, msg = bg.click_select(course.index)
        if not clicked:
            res.message = msg
            return res

        # 轮询等待反馈：原生弹窗可能马上出现，也可能等一会儿
        feedback_parts: List[str] = []
        for i in range(8):
            time.sleep(0.3 if i < 4 else 0.6)
            try:
                got = [d for d in bg.handle_dialogs() if d]
            except Exception:
                got = []
            for g in got:
                if g not in feedback_parts:
                    feedback_parts.append(g)
            # 拿到能判定的反馈就可以停了
            if feedback_parts:
                joined = " ".join(feedback_parts)
                if classify_feedback(joined) in ("ok", "already", "retry", "stop"):
                    break

        feedback = " ".join(feedback_parts).strip() or msg
        kind = classify_feedback(feedback)
        res.message = feedback[:200]
        res.ok = (kind == "ok")
        return res


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------
def match_courses(courses: List[Course], code: str = "", name: str = "",
                  teacher: str = "", course_type: str = "") -> List[Course]:
    """按条件筛选课程（填了的条件是「且」关系，均为包含匹配）。"""
    def norm(s):
        return (s or "").strip().lower()

    out = []
    for c in courses:
        blob = " ".join(c.cells) if c.cells else c.text()
        blobl = blob.lower()
        if code and norm(code) not in norm(c.kch) and norm(code) not in blobl:
            continue
        if name and norm(name) not in norm(c.kcmc) and norm(name) not in blobl:
            continue
        if teacher and norm(teacher) not in norm(c.teacher) and \
                norm(teacher) not in blobl:
            continue
        if course_type and course_type != "不限" and \
                norm(course_type) not in blobl:
            continue
        out.append(c)
    return out


class GrabRunner:
    """抢课循环（与界面解耦，便于测试和复用）。

    核心行为：
      · 每轮重新拉课程列表（数据随时可能出现）
      · 页面可读但没课 -> 保持 interval 节奏（关键：不能退避，否则错过开放瞬间）
      · 读取失败       -> 逐步退避，最多 max_cool 秒
      · 匹配到目标     -> 提交；「已选过」算成功；「已满」继续；「停止类」记录后继续监测
      · 全部目标抢到 / 手动停止 -> 结束
    """

    def __init__(self, source: CourseSource, engine: GrabEngine,
                 code: str = "", name: str = "", teacher: str = "",
                 course_type: str = "", interval: float = 1.5,
                 max_cool: float = 8.0, max_rounds: int = 0,
                 on_event: Optional[Callable[[str, str], None]] = None,
                 stop_flag=None,
                 page_loader: Optional[Callable[[], bool]] = None,
                 reload_every: int = 25,
                 on_courses: Optional[Callable[[List[Course]], None]] = None):
        self.source = source
        self.engine = engine
        self.code = code
        self.name = name
        self.teacher = teacher
        self.course_type = course_type
        self.interval = max(0.3, float(interval))
        self.max_cool = max(self.interval, float(max_cool))
        self.max_rounds = max_rounds
        self.on_event = on_event
        self.stop_flag = stop_flag
        # page_loader: 重新加载选课页（应对"页面在开放前打开，成为死页面"）
        self.page_loader = page_loader
        self.reload_every = max(5, int(reload_every))
        # on_courses: 每次成功读到课程列表时回调（界面用它刷新表格）
        self.on_courses = on_courses
        self.rounds = 0
        self.reloads = 0
        self.cool = self.interval

    # ---- 工具 ----
    def emit(self, tag: str, text: str) -> None:
        if self.on_event:
            try:
                self.on_event(tag, text)
            except Exception:
                pass

    def stopped(self) -> bool:
        return bool(self.stop_flag and self.stop_flag.is_set())

    # ---- 主循环 ----
    def run(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"success": [], "failed": [], "rounds": 0,
                                  "reason": ""}
        self.emit("hl", "════════════ 开始抢课 ════════════")
        parts = []
        if self.code:
            parts.append(f"课程代码={self.code}")
        if self.name:
            parts.append(f"课程名称含「{self.name}」")
        if self.teacher:
            parts.append(f"教师含「{self.teacher}」")
        self.emit("info", f"  目标：{' 且 '.join(parts) if parts else '（未设置）'}")
        self.emit("info", f"  轮询间隔：{self.interval} 秒")
        self.emit("hl", "══════════════════════════════════")

        opened = False
        notified_waiting = False

        while not self.stopped():
            if self.max_rounds and self.rounds >= self.max_rounds:
                result["reason"] = "reached_max_rounds"
                break
            self.rounds += 1

            try:
                courses, how = self.source.fetch()
            except Exception as exc:
                self.emit("warn", f"第 {self.rounds} 轮：读取异常 {exc}")
                self.cool = min(self.max_cool, self.cool * 1.5)
                self._sleep(self.cool)
                continue

            # --- 没有课程 ---
            if not courses:
                if self.source.last_ok:
                    # 页面正常，只是还没开课 -> 保持节奏
                    if not notified_waiting:
                        notified_waiting = True
                        self.emit("warn", "页面可访问，但还没有课程数据（选课未开始）")
                        self.emit("warn", f"  将以 {self.interval} 秒间隔持续监测，"
                                          f"开放瞬间即开抢")
                    elif self.rounds % 40 == 0:
                        self.emit("info", f"第 {self.rounds} 轮：仍未开放"
                                          f"（已监测 {self.rounds} 轮）")
                    self.cool = self.interval
                    #
                    # ★ 关键：如果选课页是"开放前"打开的，它不会自己长出表格。
                    #   定期重新加载页面，才能在新一轮开放时拿到数据。
                    #
                    if (self.page_loader
                            and self.rounds - self.reloads * self.reload_every
                            >= self.reload_every):
                        self.reloads += 1
                        self.emit("info", f"  重新加载选课页（第 {self.reloads} 次），"
                                          f"确保页面处于最新状态…")
                        try:
                            if self.page_loader():
                                self.emit("info", "  页面已刷新")
                                # 刷新后重新尝试发现接口
                                self.source.grid_url = None
                                self.source._grid_miss = 0
                                self.source._primed = False
                        except Exception as exc:
                            self.emit("warn", f"  刷新失败：{exc}")
                else:
                    if self.rounds == 1 or self.rounds % 10 == 0:
                        self.emit("warn", f"第 {self.rounds} 轮：服务器读取失败，稍后重试")
                    self.cool = min(self.max_cool, self.cool * 1.3)
                self._sleep(self.cool)
                continue

            # --- 有课程了 ---
            if not opened:
                opened = True
                self.emit("ok", f"★ 检测到课程数据（{how}），共 {len(courses)} 门")
            self.cool = self.interval

            # 让界面刷新课程表格
            if self.on_courses:
                try:
                    self.on_courses(courses)
                except Exception:
                    pass

            matched = match_courses(courses, code=self.code, name=self.name,
                                    teacher=self.teacher,
                                    course_type=self.course_type)
            if not matched:
                if self.rounds == 1 or self.rounds % 10 == 0:
                    self.emit("info", f"第 {self.rounds} 轮：没找到匹配的课"
                                      f"（共 {len(courses)} 门）")
                self._sleep(self.cool)
                continue

            todo = [c for c in matched if not c.already]
            if not todo:
                self.emit("ok", f"第 {self.rounds} 轮：目标课程都已是「已选」状态")
                result["success"] = [c.text() for c in matched]
                result["reason"] = "all_already"
                break

            # --- 逐个提交 ---
            for c in todo:
                if self.stopped():
                    break
                self.emit("hl", f"第 {self.rounds} 轮：提交 → {c.text()[:80]}")
                try:
                    res = self.engine.submit(c)
                except Exception as exc:
                    self.emit("warn", f"  提交异常：{exc}")
                    result["failed"].append(str(exc))
                    continue

                kind = classify_feedback(res.message)
                if res.ok or kind == "ok":
                    result["success"].append(c.text())
                    self.emit("ok", f"  ✅ 抢课成功！{res.message[:80]}")
                elif kind == "already":
                    result["success"].append(c.text())
                    self.emit("ok", f"  ✓ 已在课表中：{c.kcmc}")
                elif kind == "stop":
                    result["failed"].append(res.message)
                    self.emit("err", f"  ✗ 服务端拒绝：{res.message[:80]}")
                else:
                    result["failed"].append(res.message)
                    self.emit("warn", f"  ✗ 没抢到：{res.message[:80] or '无反馈'}")

            if result["success"]:
                result["reason"] = "grabbed"
                break
            self._sleep(self.cool)

        if self.stopped():
            result["reason"] = "stopped"
        result["rounds"] = self.rounds
        return result

    def _sleep(self, sec: float) -> None:
        """可中断的等待。"""
        end = time.time() + sec
        while time.time() < end:
            if self.stopped():
                return
            time.sleep(min(0.2, max(0.0, end - time.time())))


class CourseSource:
    """课程数据来源：JSON 接口 + 浏览器 DOM 双通道。"""

    def __init__(self, http_client=None, driver=None, base_url="",
                 log: Callable[[str], None] = print):
        self.http = http_client
        self.driver = driver
        self.base_url = base_url.rstrip("/")
        self.log = log
        # 上一次 fetch 是否"成功连上了服务器"（哪怕返回 0 门课）
        # 用来区分「服务器读不到」和「页面正常但还没开课」—— 前者该退避，
        # 后者必须保持节奏轮询，否则会错过选课开放的那一瞬间。
        self.last_ok = False
        # 从页面发现的真实接口地址（优先使用，避免猜测）
        self.grid_url: Optional[str] = None
        self._primed = False
        self._grid_miss = 0

    # ------------------------------------------------------------------
    # 通道 1：jqGrid JSON 接口（快，但非选课期返回空）
    # ------------------------------------------------------------------
    def fetch_json(self) -> Tuple[List[Course], bool]:
        """返回 (课程, 接口是否可读)。

        「接口可读但 0 门课」= 还没开课（正常），调用方不该退避。
        「接口全读不到」= 服务器问题，调用方该退避。

        优先用「从页面发现的真实接口」；发现失败时退回到候选地址。
        """
        if self.http is None:
            return [], False

        # 每轮尝试一次：从页面读出真实的表格接口（开放后页面才会有 grid）
        if self.grid_url is None and self.driver is not None and self._grid_miss < 3:
            u = discover_grid_url(self.driver)
            if u:
                self.grid_url = u
                self.log(f"发现真实表格接口：{u[:90]}")
            else:
                self._grid_miss += 1

        paths = ([self.grid_url] if self.grid_url else []) + list(JQGRID_PATHS)
        reachable = False
        seen = set()
        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            for xnm, xqm in self._terms():
                sep = "&" if "?" in path else "?"
                url = f"{path}{sep}gnmkdm=N253512&xnm={xnm}&xqm={xqm}"
                try:
                    _, raw, status = self.http._request(url, ajax=True, timeout=12)
                except Exception:
                    continue
                raw = (raw or "").strip()
                if not raw or "Page Not Found" in raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                reachable = True
                rows = d.get("pageData") or d.get("rows") or d.get("list") or []
                if rows:
                    return ([self._from_json(r, i) for i, r in enumerate(rows)],
                            True)
                if "totalResult" in d or "queryModel" in d:
                    # 拿到结构化空响应 -> 还没开课，不用再试其它学期
                    return [], True
        return [], reachable

    def _terms(self) -> List[Tuple[str, str]]:
        """学年学期组合，从选课页读当前值。"""
        years: List[str] = []
        if self.http is not None:
            try:
                html = self.http.fetch(SELECT_PAGE)
                for pat in (r'name="xkxnm"[^>]*value="(\d{4})"',
                            r'id="xkxnm"[^>]*value="(\d{4})"',
                            r'name="xnm"[^>]*value="(\d{4})"'):
                    years.extend(re.findall(pat, html))
            except Exception:
                pass
        if not years:
            years = [str(time.localtime().tm_year)]
        years = list(dict.fromkeys(years))[:3]
        return [(y, q) for y in years for q in ("3", "1", "12", "16")]

    @staticmethod
    def _from_json(r: Dict[str, Any], idx: int) -> Course:
        def g(*keys):
            for k in keys:
                v = r.get(k)
                if v not in (None, ""):
                    return str(v).strip()
            return ""
        c = Course(index=idx)
        c.kch = g("kch", "kch_id", "kchid")
        c.kcmc = g("kcmc", "jxbmc", "kcbmc")
        c.teacher = g("xm", "jsxm", "jzgxx", "teacher")
        c.time_text = g("sksj", "sjdd", "xqjmc")
        c.room = g("cdmc", "jsmc", "room")
        c.weeks = g("zcd", "zcmc")
        cap = g("jxbzrs", "yxzrs", "kcrs")
        sel = g("yxzrs", "selected")
        c.capacity = f"{sel}/{cap}" if (cap or sel) else g("bzrs")
        c.jxb_id = g("jxb_id", "jxbids")
        c.button_text = "选课"
        c.cells = [c.kch, c.kcmc, c.teacher, c.time_text, c.capacity]
        return c

    def _enter_frame(self) -> str:
        d = self.driver
        try:
            d.switch_to.default_content()
        except Exception:
            pass
        frames = []
        try:
            frames = d.find_elements("tag name", "iframe")
        except Exception:
            pass
        for fr in frames:
            try:
                fid = (fr.get_attribute("id") or "") + (fr.get_attribute("name") or "")
            except Exception:
                continue
            if re.search(r"content|main|ifr|frame|xsxk|body", fid, re.I):
                try:
                    d.switch_to.frame(fr)
                    return "iframe"
                except Exception:
                    pass
        if frames:
            try:
                d.switch_to.frame(frames[0])
                return "iframe"
            except Exception:
                pass
        return "top"

    def _read_dom_table(self):
        """返回 (行列表, 表头, 是否找到表格)。"""
        from zf_grab import _TABLE_SELECTORS  # 复用选择器
        d = self.driver
        self._enter_frame()

        table = None
        for sel in _TABLE_SELECTORS:
            try:
                els = d.find_elements("css selector", sel)
            except Exception:
                continue
            for el in els:
                try:
                    if len(el.find_elements("tag name", "tr")) >= 2:
                        table = el
                        break
                except Exception:
                    continue
            if table is not None:
                break
        if table is None:
            return [], [], False

        headers: List[str] = []
        rows: List[Tuple[int, List[str], str, bool]] = []
        try:
            trs = table.find_elements("tag name", "tr")
        except Exception:
            return [], [], False

        for i, tr in enumerate(trs):
            try:
                ths = tr.find_elements("tag name", "th")
                tds = tr.find_elements("tag name", "td")
                cells_el = ths or tds
                if not cells_el:
                    continue
                lines = []
                for c in cells_el:
                    t = (c.text or "").strip()
                    if t:
                        lines.append(re.sub(r"\s+", " ", t))
                if not lines:
                    continue
                if (ths or i == 0) and not headers:
                    headers = lines
                    continue

                btn_text, done = self._row_buttons(tr)
                rows.append((i, lines, btn_text, done))
            except Exception:
                continue

        if not headers and rows:
            headers = rows[0][1]
        return rows, headers, True

    def _row_buttons(self, tr) -> Tuple[str, bool]:
        txts = []
        try:
            for el in tr.find_elements("css selector", "a, button, input[type=button]"):
                t = ((el.text or "") + " " + (el.get_attribute("value") or "")).strip()
                t = re.sub(r"\s+", " ", t)
                if t:
                    txts.append(t)
        except Exception:
            return "", False
        for t in txts:
            if t.strip() in ("退选", "退课", "已选", "已选定", "取消", "已报名"):
                return t, True
        for t in txts:
            if any(h in t for h in SELECT_TEXT_HINTS):
                return t, False
        return (txts[0] if txts else ""), False

    def _rows_to_courses(self, rows, headers) -> List[Course]:
        from zf_grab import BrowserGrab
        cols = BrowserGrab._locate_columns(headers) if headers else {}
        out: List[Course] = []
        for idx, lines, btn, done in rows:
            c = Course(index=idx, cells=lines, button_text=btn, already=done)

            def pick(key, *fallback_idx):
                i = cols.get(key)
                if i is not None and 0 <= i < len(lines):
                    return lines[i]
                for j in fallback_idx:
                    if 0 <= j < len(lines):
                        return lines[j]
                return ""
            c.kch = pick("code", 0)
            c.kcmc = pick("name", 1)
            c.teacher = pick("teacher", 2)
            c.time_text = pick("class", 3, 4)
            c.capacity = pick("_cap", len(lines) - 2, len(lines) - 1)
            out.append(c)
        return out

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------
    def fetch(self, prefer: str = "auto", dump_path: Optional[str] = None
              ) -> Tuple[List[Course], str]:
        """获取课程列表，返回 (课程, 来源说明)。

        会设置 self.last_ok：
          True  = 成功读到了页面/接口（哪怕 0 门课，说明还没开课）
          False = 所有通道都读不到（服务器问题），调用方应该退避
        """
        self.last_ok = False

        if prefer in ("auto", "json"):
            cs, reok = self.fetch_json()
            if cs:
                self.last_ok = True
                return cs, "JSON 接口"
            if reok:
                self.last_ok = True

        if prefer in ("auto", "dom"):
            cs, dok = self.fetch_dom(dump_path=dump_path)
            if cs:
                self.last_ok = True
                return cs, "浏览器页面"
            if dok:
                self.last_ok = True

        # JSON 通道已证明页面可读，但 DOM 也没数据
        if self.last_ok:
            return [], "页面正常但暂无课程"
        return [], "读取失败"

    # ------------------------------------------------------------------
    # 通道 2：浏览器 DOM（可靠）
    # ------------------------------------------------------------------
    def fetch_dom(self, dump_path: Optional[str] = None
                  ) -> Tuple[List[Course], bool]:
        """返回 (课程, 页面是否读到)。"""
        if self.driver is None:
            return [], False
        try:
            rows, headers, found = self._read_dom_table()
        except Exception as exc:
            self.log(f"读取页面表格失败：{exc}")
            return [], False
        if not found:
            # 没找到表格：可能是需要先选学年学期 / 点一次查询。
            # 只在第一次做，避免每轮都瞎点。
            if not self._primed:
                self._primed = True
                acts = prime_select_page(self.driver)
                if acts:
                    for a in acts:
                        self.log(f"页面预处理：{a}")
                    time.sleep(1.2)
                    try:
                        rows, headers, found = self._read_dom_table()
                    except Exception:
                        found = False
                    if found:
                        return self._rows_to_courses(rows, headers), True

            # 页面是否真的打开了？判据宽松些：body 存在且加载完成即算可读
            alive = False
            try:
                alive = bool(self.driver.execute_script(
                    "try {"
                    "  if (!document || !document.body) return false;"
                    "  var rs = document.readyState;"
                    "  if (rs !== 'complete' && rs !== 'interactive') return false;"
                    "  return document.body.innerHTML.length > 60;"
                    "} catch(e) { return false; }"))
            except Exception:
                alive = False
            return [], alive
        return self._rows_to_courses(rows, headers), True
