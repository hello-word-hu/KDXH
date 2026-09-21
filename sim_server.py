"""正方教务（矿大徐海）故障注入模拟站 —— 用来复现"抢课高峰期"的恶劣环境。

    python sim_server.py peak          # 手动起一个高峰期模拟站

可以注入的故障（对应真实高峰期会遇到的情况）：
    latency      每次请求都慢（服务器负载高）
    fail_rate    随机返回 500/502/503/504（网关扛不住）
    reset_rate   直接掐断 TCP 连接（没有响应就断开）
    hang_rate    请求卡住不返回（超过客户端超时）
    session_ttl  会话到期，之后所有请求都被打回登录页
    open_at      选课开放时刻；之前提交只回"不在选课时间内"
    competitors  开放后每 N 秒被别人抢走一个名额（手速竞争）
    lose_success 服务器其实选上了，但响应在回程被丢掉（最危险的情况）

与"抄一遍算法"不同，模拟站自己生成一对 RSA 密钥，用私钥**真的解密**
客户端传来的密文再比对明文 —— 所以客户端的裸 RSA（无 PKCS#1 填充、
定长大端输出）只要有一点不对就会被识破。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# RSA：自己生成密钥，好真的解密验证
# --------------------------------------------------------------------------
_SMALL_PRIMES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47,
                 53, 59, 61, 67, 71, 73, 79, 83, 89, 97, 101, 103, 107,
                 109, 113, 127, 131, 137, 139, 149, 151, 157, 163, 167, 173]


def _is_prime(n: int, rounds: int = 20) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d = n - 1
    r = 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = random.randrange(2, n - 1)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        n = random.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_prime(n):
            return n


def gen_keypair(bits: int = 1024):
    """返回 (n, e, d, klen)。生成一次要一两秒，只在启动时做。"""
    half = bits // 2
    while True:
        p = _gen_prime(half)
        q = _gen_prime(half)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        e = 65537
        if phi % e == 0:
            continue
        d = pow(e, -1, phi)
        return n, e, d, (bits + 7) // 8


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


# --------------------------------------------------------------------------
# 档位
# --------------------------------------------------------------------------
PROFILES = {
    # 人少的时候 —— 和实测到的情况差不多
    "normal": dict(latency=0.05, jitter=0.05, fail_rate=0.0, reset_rate=0.0,
                   hang_rate=0.0, session_ttl=0, open_delay=0.0,
                   competitor_interval=0.0, page_delay=0.15,
                   competitor_grace=0.0),
    # 高峰期：慢、偶发 5xx、会话会掉、别人也在抢
    "peak": dict(latency=0.15, jitter=0.35, fail_rate=0.18, reset_rate=0.04,
                 hang_rate=0.03, hang_seconds=8.0, session_ttl=60,
                 open_delay=20.0, competitor_interval=2.5, page_delay=0.8,
                 competitor_grace=2.0),
    # 极限：网关半死，连接乱断，开放瞬间就是肉搏
    "brutal": dict(latency=0.4, jitter=1.2, fail_rate=0.35, reset_rate=0.10,
                   hang_rate=0.06, hang_seconds=8.0, session_ttl=45,
                   open_delay=45.0, competitor_interval=1.5, page_delay=1.6,
                   competitor_grace=4.0),
}

# 假设的账号
SIM_USER = "20260001"
SIM_PWD = "Xh@2026test"


class SimState:
    def __init__(self, profile: str = "normal", **over):
        cfg = dict(PROFILES.get(profile, PROFILES["normal"]))
        cfg.update(over)
        self.profile = profile
        for k, v in cfg.items():
            setattr(self, k, v)
        self.hang_seconds = cfg.get("hang_seconds", 8.0)

        self.n, self.e, self.d, self.klen = gen_keypair(1024)
        self.modulus_b64 = b64(self.n.to_bytes(self.klen, "big"))
        self.exponent_b64 = b64(self.e.to_bytes(3, "big"))

        self.open_at = time.time() + cfg.get("open_delay", 0.0)
        self.capacity = 1
        self.competitor_armed_at = self.open_at + cfg.get("competitor_grace", 2.0)

        # session_id -> {"born", "user", "csrf"}
        self.sessions: dict = {}
        self.created_sessions = 0
        self.csrf_tokens: dict = {}
        self.lose_next_success = False
        self.lose_injected = 0
        self.no_open_time = bool(cfg.get("no_open_time", False))
        self.selected: list = []          # 已选课程（按学生记）

        self.lock = threading.Lock()
        self.requests = 0
        self.injected_fail = 0
        self.injected_reset = 0
        self.injected_hang = 0
        self.submit_calls = 0
        self.login_ok = 0
        self.login_bad = 0
        self.bad_crypto = 0
        self.not_open_hits = 0
        self.grabbed_by_us = 0
        self.grabbed_by_others = 0
        self.log: list = []
        self._stop = threading.Event()
        self.t0 = time.time()

        # 目标课程：只有这一门有 1 个名额
        self.COURSES = [
            {"jxb_id": "JXB2026AI001", "kch": "09010010", "kcmc": "人工智能导论",
             "teacher": "刘明", "time": "周一第1-2节", "room": "博1-A101",
             "credit": "3.0", "capacity": 1, "taken": 0},
            {"jxb_id": "JXB2026MA002", "kch": "09010020", "kcmc": "高等数学A",
             "teacher": "陈红", "time": "周二第3-4节", "room": "博2-B203",
             "credit": "5.0", "capacity": 60, "taken": 0},
            {"jxb_id": "JXB2026EN003", "kch": "09010030", "kcmc": "大学英语",
             "teacher": "王芳", "time": "周三第5-6节", "room": "文3-C305",
             "credit": "4.0", "capacity": 45, "taken": 0},
        ]

    def is_open(self) -> bool:
        return time.time() >= self.open_at

    def note(self, msg: str) -> None:
        self.log.append("%.2f %s" % (time.time() - self.t0, msg))

    def start_competitors(self) -> None:
        if not self.competitor_interval:
            return

        def loop():
            while not self._stop.is_set():
                if time.time() >= self.competitor_armed_at:
                    with self.lock:
                        target = self.COURSES[0]
                        if target["taken"] < target["capacity"]:
                            target["taken"] += 1
                            self.grabbed_by_others += 1
                            self.note("别人抢走一个名额")
                self._stop.wait(self.competitor_interval)
        threading.Thread(target=loop, daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------
# 页面模板
# --------------------------------------------------------------------------
LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>统一身份认证</title></head><body>
<form id="loginForm" action="/jwglxt/xtgl/login_slogin.html" method="post">
<input type="hidden" name="csrftoken" value="__CSRF__"/>
<input name="yhm" type="text"/>
<input name="mm" type="password"/>
<input type="hidden" name="mmsfjm" value="1"/>
</form>
<div id="error">__ERR__</div>
</body></html>"""

MAIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>教务系统</title></head><body>
<div>欢迎 同学 20260001</div>
<a href="/jwglxt/xsxk/zzxkyzb_cxZzxkYzbIndex.html?gnmkdm=N253512">选课</a>
</body></html>"""

SELECT_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>学生选课中心</title></head><body>
<div id="xsxk">选课轮次：第 1 轮</div>
<div>本轮选课时间：__OPEN__ 至 __CLOSE__</div>
<input id="xnm" value="__XNM__"/><input id="xqm" value="__XQM__"/>
<table id="kbtable">
<tr><th>课程名称</th><th>教师</th><th>上课时间</th><th>学分</th><th>容量</th><th>已选</th><th>操作</th></tr>
__ROWS__
</table>
</body></html>"""

PAGE_NOT_FOUND = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Page Not Found</title></head><body>抱歉，您访问的页面不存在。</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZfSim/1.0"

    @property
    def st(self) -> SimState:
        return self.server.state

    def log_message(self, fmt, *args):
        pass

    # ---------------- 基础设施 ----------------
    def _cookies(self) -> dict:
        raw = self.headers.get("Cookie") or ""
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8",
              extra=None, set_cookies=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for c in (set_cookies or []):
                self.send_header("Set-Cookie", c)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)
        except Exception:
            pass

    def _redirect(self, url, set_cookies=None):
        self._send(302, b"", extra={"Location": url}, set_cookies=set_cookies)

    def _kill_connection(self):
        try:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.connection.close()
        except Exception:
            pass

    def _chaos(self) -> bool:
        st = self.st
        with st.lock:
            st.requests += 1
        r = random.random()
        if r < st.reset_rate:
            with st.lock:
                st.injected_reset += 1
            self._kill_connection()
            return True
        r -= st.reset_rate
        if r < st.fail_rate:
            with st.lock:
                st.injected_fail += 1
            code = random.choice([500, 502, 503, 504])
            self._send(code, b"<html><body>Gateway Error</body></html>")
            return True
        r -= st.fail_rate
        if r < st.hang_rate:
            with st.lock:
                st.injected_hang += 1
            time.sleep(st.hang_seconds)
            self._kill_connection()
            return True
        delay = st.latency + random.random() * st.jitter
        if delay > 0:
            time.sleep(delay)
        return False

    # ---------------- 会话 ----------------
    def _session(self, create=False):
        st = self.st
        sid = self._cookies().get("JSESSIONID")
        with st.lock:
            rec = st.sessions.get(sid) if sid else None
            if rec and st.session_ttl and \
                    (time.time() - rec["born"]) > st.session_ttl:
                st.sessions.pop(sid, None)
                rec = None
            if rec is None and create:
                sid = uuid.uuid4().hex.upper()
                rec = {"born": time.time(), "user": None}
                st.sessions[sid] = rec
                st.created_sessions += 1
        return sid, rec

    def _need_login(self) -> bool:
        sid, rec = self._session()
        if not rec or not rec.get("user"):
            self._redirect("/jwglxt/xtgl/login_slogin.html")
            return True
        return False

    # ---------------- RSA ----------------
    def _decrypt_password(self, token: str) -> str:
        """私钥解密 —— 裸 RSA（无 PKCS#1 填充），和正方前端一致。"""
        raw = base64.b64decode(token)
        c = int.from_bytes(raw, "big")
        if c >= self.st.n:
            raise ValueError("ciphertext out of range")
        m = pow(c, self.st.d, self.st.n)
        blen = (m.bit_length() + 7) // 8
        return m.to_bytes(blen, "big").decode("utf-8")

    # ---------------- 路由 ----------------
    def do_GET(self):
        if self._chaos():
            return
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path in ("/", "/jwglxt", "/jwglxt/"):
                return self._redirect("/jwglxt/xtgl/login_slogin.html")
            if path == "/jwglxt/xtgl/login_slogin.html":
                return self.h_login_page()
            if path == "/jwglxt/xtgl/login_getPublicKey.html":
                return self.h_pubkey()
            if path == "/jwglxt/framework/main.jsp":
                if self._need_login():
                    return
                return self._send(200, MAIN_HTML.encode("utf-8"))
            if path == "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbIndex.html":
                return self.h_select_page(q)
            if path.startswith("/jwglxt/xsxk/") or path.startswith("/jwglxt/xszx/"):
                # 其它候选选课页：正方对不存在的地址返回"页面不存在"
                return self._send(200, PAGE_NOT_FOUND.encode("utf-8"))
            return self._send(200, PAGE_NOT_FOUND.encode("utf-8"))
        except Exception as exc:
            print("sim error:", type(exc).__name__, exc, file=sys.stderr)
            try:
                self._send(500, b"sim internal error")
            except Exception:
                pass

    def do_POST(self):
        if self._chaos():
            return
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            if path == "/jwglxt/xtgl/login_slogin.html":
                return self.h_login_post(body)
            if path.startswith("/jwglxt/xsxk/") or path.startswith("/jwglxt/xszx/"):
                return self.h_select_submit(path, body)
            return self._send(200, PAGE_NOT_FOUND.encode("utf-8"))
        except Exception as exc:
            print("sim error:", type(exc).__name__, exc, file=sys.stderr)
            try:
                self._send(500, b"sim internal error")
            except Exception:
                pass

    # ---------------- 登录 ----------------
    def h_login_page(self):
        sid, rec = self._session(create=True)
        csrf = uuid.uuid4().hex + str(uuid.uuid4().hex)[:16]
        with self.st.lock:
            self.st.csrf_tokens[csrf] = sid
        html = LOGIN_HTML.replace("__CSRF__", csrf).replace("__ERR__", "")
        self._send(200, html.encode("utf-8"),
                   set_cookies=["JSESSIONID=%s; Path=/" % sid])

    def h_pubkey(self):
        data = {"modulus": self.st.modulus_b64,
                "exponent": self.st.exponent_b64}
        self._send(200, json.dumps(data).encode("utf-8"),
                   ctype="application/json;charset=utf-8")

    def h_login_post(self, body):
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))

        def g(k):
            return (form.get(k) or [""])[0]

        csrf, yhm, mm = g("csrftoken"), g("yhm"), g("mm")
        sid, rec = self._session(create=True)
        with self.st.lock:
            good_csrf = csrf in self.st.csrf_tokens
            self.st.csrf_tokens.pop(csrf, None)

        def fail(msg):
            with self.st.lock:
                self.st.login_bad += 1
            csrf2 = uuid.uuid4().hex + str(uuid.uuid4().hex)[:16]
            with self.st.lock:
                self.st.csrf_tokens[csrf2] = sid
            html = LOGIN_HTML.replace("__CSRF__", csrf2).replace("__ERR__",
                                                                 msg)
            self._send(200, html.encode("utf-8"))

        if not good_csrf:
            return fail("会话已过期，请重新登录")
        try:
            pwd = self._decrypt_password(mm)
        except Exception:
            with self.st.lock:
                self.st.bad_crypto += 1
            return fail("用户名或密码不正确")
        if yhm != SIM_USER or pwd != SIM_PWD:
            return fail("用户名或密码不正确")

        with self.st.lock:
            rec["user"] = yhm
            rec["born"] = time.time()
            self.st.login_ok += 1
        self._redirect("/jwglxt/framework/main.jsp",
                       set_cookies=["JSESSIONID=%s; Path=/" % sid])

    # ---------------- 选课 ----------------
    def h_select_page(self, q):
        if self._need_login():
            return
        time.sleep(self.st.page_delay)
        rows = []
        with self.st.lock:
            picked = list(self.st.selected)
        for c in self.st.COURSES:
            done = c["kcmc"] in picked
            action = ("<span class=\"yx\">已选</span>" if done else
                      "<a href=\"javascript:void(0)\" "
                      "onclick=\"xsxk('jxb_id','%s','kch_id','%s')\">选课</a>"
                      % (c["jxb_id"], c["kch"]))
            rows.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                "<td>%d</td><td>%d</td><td>%s</td></tr>"
                % (c["kcmc"], c["teacher"], c["time"], c["credit"],
                   c["capacity"], c["taken"], action))
        html = (SELECT_HTML.replace("__ROWS__", "\n".join(rows))
                .replace("__XNM__", "2026").replace("__XQM__", "3")
                .replace("__OPEN__", "" if self.st.no_open_time else
                         time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(self.st.open_at)))
                .replace("__CLOSE__", "" if self.st.no_open_time else
                         time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(self.st.open_at + 3600))))
        self._send(200, html.encode("utf-8"))

    def h_select_submit(self, path, body):
        """只有 xsxk_operate 是"活的"，其余叶子返回页面不存在。"""
        leaf = os.path.basename(path).replace(".html", "")
        if leaf != "xsxk_operate":
            return self._send(200, PAGE_NOT_FOUND.encode("utf-8"))
        if self._need_login():
            return
        with self.st.lock:
            self.st.submit_calls += 1

        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        jxb = (form.get("jxb_ids") or form.get("jxb_id") or [""])[0]
        target = None
        for c in self.st.COURSES:
            if c["jxb_id"] == jxb:
                target = c
                break

        def reply(msg, flag="0"):
            s = json.dumps({"flag": flag, "msg": msg}, ensure_ascii=False)
            self._send(200, s.encode("utf-8"),
                       ctype="application/json;charset=utf-8")

        if target is None:
            return reply("教学班不存在")

        if not self.st.is_open():
            with self.st.lock:
                self.st.not_open_hits += 1
            return reply("不在选课时间内")

        if target["kcmc"] in self.st.selected:
            return reply("已经选过该课程")

        with self.st.lock:
            if target["taken"] < target["capacity"]:
                target["taken"] += 1
                self.st.grabbed_by_us += 1
                self.st.selected.append(target["kcmc"])
                self.st.note("★ 被脚本抢到了！")
                if self.st.lose_next_success:
                    self.st.lose_next_success = False
                    self.st.lose_injected += 1
                    self.st.note("响应被丢弃（客户端会看到超时）")
                    self._kill_connection()
                    return
                return reply("选课成功", "1")
        return reply("该教学班人数已满")


class SimServer:
    def __init__(self, profile: str = "normal", **over):
        self.state = SimState(profile, **over)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.state = self.state
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = None

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d/jwglxt" % self.port

    def start(self):
        self.state.start_competitors()
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.state.shutdown()
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    def summary(self) -> str:
        s = self.state
        return ("请求 %d 次 | 注入 5xx=%d 断连=%d 卡死=%d | 登录 成功%d/失败%d "
                "| 密文非法 %d | 提交 %d 次（未开放被拒 %d）| 丢响应 %d | "
                "我们抢到 %d 别人抢到 %d"
                % (s.requests, s.injected_fail, s.injected_reset,
                   s.injected_hang, s.login_ok, s.login_bad, s.bad_crypto,
                   s.submit_calls, s.not_open_hits, s.lose_injected,
                   s.grabbed_by_us, s.grabbed_by_others))


def run_forever(profile: str, port: int = 8766):
    st_state = SimState(profile)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.state = st_state
    httpd.daemon_threads = True
    st_state.start_competitors()
    print("正方模拟站已启动: http://127.0.0.1:%d/jwglxt  档位=%s"
          % (port, profile))
    print("账号 %s  密码 %s" % (SIM_USER, SIM_PWD))
    print("选课在 %.0f 秒后开放，目标课只有 1 个名额"
          % (st_state.open_at - time.time()))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭")


if __name__ == "__main__":
    prof = sys.argv[1] if len(sys.argv) > 1 else "peak"
    p = int(sys.argv[2]) if len(sys.argv) > 2 else 8766
    run_forever(prof, p)
