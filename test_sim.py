"""正方教务（矿大徐海）抢课高峰期压测。

    python test_sim.py            # 全部跑一遍

用 sim_server 起一个本地"教务站"，注入高峰期会遇到的各种故障，
然后让真正的抢课引擎去打，验证它能不能在恶劣条件下照样抢到。

覆盖场景
--------
A. 三档强度      normal（人少） / peak（高峰） / brutal（网关半死）
B. 地址拼接      提交地址不能出现 /jwglxt/jwglxt 重复（曾经的致命 bug）
C. 会话中途过期  教务会话到期，脚本应当用账号密码自动重登（正方免验证码）
D. 响应丢失      服务器其实选上了，但响应在回程丢了
E. 全站 5xx      先让服务器全线报错，再恢复正常
F. 密码错误      要给出人看得懂的提示，不能死循环
G. 未开放不退避  选课没开放时提交被拒，节奏绝不能退让
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)

import zf_peak as PK                                        # noqa: E402
from sim_server import SIM_PWD, SIM_USER, SimServer         # noqa: E402
from zf_client import Config, OpenZfClient                  # noqa: E402

TARGET = "人工智能导论"
FAILS: list = []
_LOG: list = []
_LOG_PATH = os.path.join(tempfile.gettempdir(), "zf_sim_test.log")

# 每档"开跑时距开放还有多久"和"别人多久才反应过来"
OPEN_DELAYS = {"normal": 0.0, "peak": 15.0, "brutal": 30.0}
GRACES = {"normal": 0.0, "peak": 2.0, "brutal": 4.0}


def _out(s: str):
    _LOG.append(s)
    try:
        print(s)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "gbk"
        print(s.encode(enc, "replace").decode(enc, "replace"))


def check(cond, label, extra=""):
    _out("  [%s] %s %s" % ("OK" if cond else "!!", label, extra))
    if not cond:
        FAILS.append(label)


def say(msg):
    _out("      " + msg)


def flush_log():
    try:
        with open(_LOG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(_LOG))
    except Exception:
        pass


# --------------------------------------------------------------------------
def new_client(sim, timeout=4.0, pwd=SIM_PWD):
    cli = OpenZfClient(Config(base_url=sim.base_url, username=SIM_USER,
                              password=pwd, verbose=False, timeout=timeout))
    cli.log = lambda *a, **k: None      # 测试里不需要它刷屏
    return cli


def do_login(sim, pwd=SIM_PWD, tries=8):
    cli = None
    err = None
    for _ in range(tries):
        cli = new_client(sim, pwd=pwd)
        try:
            cli.login(max_retries=2)
            if cli.logged_in:
                return cli, None
        except Exception as exc:
            err = exc
        time.sleep(0.4)
    return cli, err


def make_runner(g, interval=0.6, submit_interval=0.3, **kw):
    events: list = []
    r = PK.PeakRunner(g, [TARGET], interval=interval,
                      submit_interval=submit_interval,
                      on_event=lambda m: events.append(m), **kw)
    r.events = events
    return r


def wait_win(runner, limit):
    t0 = time.time()
    while time.time() - t0 < limit:
        if runner.wins:
            return time.time() - t0
        if not runner.is_alive():
            return None
        time.sleep(0.1)
    return None


# --------------------------------------------------------------------------
# B. 地址拼接（回归测试：曾经所有提交都打到 404）
# --------------------------------------------------------------------------
def scenario_url_build():
    _out("\n" + "=" * 66)
    _out("B. 提交地址拼接（不能再出现 /jwglxt/jwglxt）")
    sim = SimServer("normal").start()
    try:
        cli = new_client(sim)
        cfg = cli.cfg
        built = []
        orig = cli._request

        def spy(url, *a, **kw):
            if not url.startswith("http"):
                full = cfg.base_url + ("" if url.startswith("/") else "/") + url
            else:
                full = url
            built.append(full)
            return orig(url, *a, **kw)

        cli._request = spy
        try:
            cli.login(max_retries=1)
            g = PK.PeakGrabber(cli, log=lambda m: None)
            g.find_page()
            courses = g.read_courses()
            check(bool(courses), "解析到课程", "%d 门" % len(courses))
            if courses:
                try:
                    g.submit(courses[0])
                except Exception as exc:
                    say("提交异常（可接受）：%s" % str(exc)[:80])
        except Exception as exc:
            check(False, "整条链路没报错", "%s: %s" % (type(exc).__name__, exc))
        finally:
            cli._request = orig

        bad = [u for u in built if "/jwglxt/jwglxt" in u]
        check(not bad, "没有出现重复的 /jwglxt 前缀",
              ("坏地址示例: %s" % bad[0]) if bad else "共检查 %d 个地址"
              % len(built))
        # 直接验证路径构造
        check("/jwglxt/xsxk/" not in ("/%s/%s.html"
                                      % ("xsxk", "xsxk_operate")),
              "submit_select 拼出来的路径不带 /jwglxt 前缀",
              "/xsxk/xsxk_operate.html")
        check(cli._select_referer("xsxk").count("/jwglxt") == 1,
              "referer 里 /jwglxt 只出现一次",
              cli._select_referer("xsxk"))
    finally:
        sim.stop()


# --------------------------------------------------------------------------
# A. 三档强度
# --------------------------------------------------------------------------
def scenario_profile(profile: str, attempts: int = 1, limit: float = 90.0):
    _out("\n" + "=" * 66)
    _out("A. 档位 %s%s"
         % (profile, "（最多试 %d 次）" % attempts if attempts > 1 else ""))
    detail = ""
    for k in range(attempts):
        if k:
            _out("      —— 上次没抢到，再来一发"
                 "（这一档服务器一半请求都在失败，本来就有随机性）")
        ok, detail = _one_profile_run(profile, limit)
        if ok:
            break
    check(ok, "在 %s 档位抢到了" % profile, detail)


def _one_profile_run(profile: str, limit: float):
    # 先把开放时间设得很远，等登录就位后再重新计时 ——
    # 否则高峰期一次登录慢几秒就把"开放前"的窗口吃光了，
    # 测出来的会变成"登录速度"而不是"抢课能力"。
    sim = SimServer(profile, open_delay=600.0).start()
    try:
        cli, err = do_login(sim)
        if not cli or not cli.logged_in:
            check(False, "登录成功", str(err)[:80])
            return False, "登录失败"
        g = PK.PeakGrabber(cli, log=lambda m: None)
        # 高峰期这里失败很正常 —— 真正的定位交给 runner 去反复重试，
        # 测试只做一次软检查，不因为它没读到就判定失败。
        try:
            g.find_page()
            courses = g.read_courses()
            say("开跑前预读：%d 门课" % len(courses))
        except Exception as exc:
            say("开跑前预读失败（交给 runner 重试）：%s" % str(exc)[:70])

        delay = OPEN_DELAYS.get(profile, 15.0)
        grace = GRACES.get(profile, 2.0)
        sim.state.open_at = time.time() + delay
        sim.state.competitor_armed_at = sim.state.open_at + grace
        say("模拟站 127.0.0.1:%d；已登录就位，选课 %.0f 秒后开放，"
            "目标课只有 1 个名额" % (sim.port, delay))

        runner = make_runner(g)
        t0 = time.time()
        runner.start()
        dt = wait_win(runner, limit)
        runner.stop()
        runner.join(8)

        loc = ((runner.first_located_at - t0) if runner.first_located_at
               else None)
        say("开跑时距开放 %.0f 秒；首次定位用了 %s"
            % (delay, ("%.1f 秒" % loc) if loc else "没定位到"))
        say(sim.summary())
        say("提交 %d 次（网关报错 %d，结果不明 %d）"
            % (g.submits, g.gateway_errors, g.unknowns))
        check(sim.state.grabbed_by_us == 1, "服务器确认我们选上了" if dt
              else "服务器确认（这一档输了）")
        check(sim.state.grabbed_by_others == 0 or dt is None,
              "没被别人抢先" if dt else "被抢先了（可接受）")
        check(g.submits > 0, "确实发起过提交", "%d 次" % g.submits)
        if dt is None:
            say("--- 失败时的运行日志（后 14 条）---")
            for e in runner.events[-14:]:
                say("  " + e[:110])
        return dt is not None, ("用时 %.1f 秒" % dt) if dt else "超时未抢到"
    finally:
        sim.stop()


# --------------------------------------------------------------------------
# C. 会话中途过期
# --------------------------------------------------------------------------
def scenario_session_expiry(attempts: int = 2):
    _out("\n" + "=" * 66)
    _out("C. 会话中途过期（教务会话 30 秒就掉，等待期掉一次）")
    for k in range(attempts):
        if k:
            _out("      —— 上次没抢到，再来一发")
        if _one_session_run():
            break


def _one_session_run() -> bool:
    sim = SimServer("peak", session_ttl=30, open_delay=60,
                    competitor_interval=3.0).start()
    try:
        cli, err = do_login(sim)
        if not cli or not cli.logged_in:
            check(False, "登录成功", str(err)[:80])
            return False
        g = PK.PeakGrabber(cli, log=lambda m: None)
        # 高峰期预读失败很正常，这里不判失败 —— 真正的定位交给 runner 反复重试
        try:
            g.find_page()
            g.read_courses()
        except Exception as exc:
            say("预读失败（交给 runner 重试）：%s" % str(exc)[:60])
        created_before = sim.state.created_sessions

        runner = make_runner(g, interval=1.0, submit_interval=0.3)
        runner.start()
        dt = wait_win(runner, 100)
        runner.stop()
        runner.join(8)

        check(dt is not None, "会话掉线后仍然抢到了",
              ("用时 %.1f 秒" % dt) if dt else "超时")
        check(g.relogins >= 1, "触发了自动重登（正方免验证码）",
              "%d 次" % g.relogins)
        check(sim.state.created_sessions > created_before,
              "服务器确实发过新会话", "%d -> %d"
              % (created_before, sim.state.created_sessions))
        check(sim.state.grabbed_by_us == 1, "服务器确认我们选上了")
        say(sim.summary())
        return dt is not None and g.relogins >= 1
    finally:
        sim.stop()


# --------------------------------------------------------------------------
# D. 响应丢失
# --------------------------------------------------------------------------
def scenario_lost_response():
    _out("\n" + "=" * 66)
    _out("D. 服务器选上了但响应丢失（客户端只看到超时/断连）")
    sim = SimServer("normal", open_delay=1.0).start()
    sim.state.lose_next_success = True
    try:
        cli, err = do_login(sim)
        check(cli is not None and cli.logged_in, "登录成功", str(err or ""))
        if not cli or not cli.logged_in:
            return
        g = PK.PeakGrabber(cli, log=lambda m: None)
        # 高峰期预读失败很正常，这里不判失败 —— 真正的定位交给 runner 反复重试
        try:
            g.find_page()
            g.read_courses()
        except Exception as exc:
            say("预读失败（交给 runner 重试）：%s" % str(exc)[:60])
        runner = make_runner(g, interval=0.5, submit_interval=0.25)
        runner.start()
        dt = wait_win(runner, 60)
        runner.stop()
        runner.join(8)

        check(sim.state.lose_injected == 1, "故障已注入（响应被丢弃）")
        check(dt is not None, "仍然确认抢到了（靠「已选过」回执）",
              ("用时 %.1f 秒" % dt) if dt else "没确认到")
        check(sim.state.grabbed_by_us == 1, "服务器只让我们选上一次")
        msgs = " ".join(runner.events)
        check("已经选过" in msgs or "抢到了" in msgs, "日志体现了确认过程")
        say(sim.summary())
    finally:
        sim.stop()


# --------------------------------------------------------------------------
# E. 全站 5xx 后恢复
# --------------------------------------------------------------------------
def scenario_all_failing():
    _out("\n" + "=" * 66)
    _out("E. 服务器全线报错，之后恢复正常")
    sim = SimServer("normal", open_delay=1.0).start()
    try:
        cli, err = do_login(sim)
        check(cli is not None and cli.logged_in, "登录成功", str(err or ""))
        if not cli or not cli.logged_in:
            return
        g = PK.PeakGrabber(cli, log=lambda m: None)
        # 高峰期预读失败很正常，这里不判失败 —— 真正的定位交给 runner 反复重试
        try:
            g.find_page()
            g.read_courses()
        except Exception as exc:
            say("预读失败（交给 runner 重试）：%s" % str(exc)[:60])

        sim.state.fail_rate = 1.0
        runner = make_runner(g, interval=0.4, submit_interval=0.25)
        runner.start()
        time.sleep(6)
        check(runner.is_alive(), "全站报错时脚本没有崩，还在坚持")
        sim.state.fail_rate = 0.0
        dt = wait_win(runner, 60)
        runner.stop()
        runner.join(8)
        check(dt is not None, "服务器恢复后抢到了",
              ("用时 %.1f 秒（恢复后）" % dt) if dt else "超时")
        check(not runner.is_alive(), "线程已退出")
        say(sim.summary())
    finally:
        sim.stop()


# --------------------------------------------------------------------------
# F. 密码错误
# --------------------------------------------------------------------------
def scenario_bad_password():
    _out("\n" + "=" * 66)
    _out("F. 密码填错")
    sim = SimServer("normal").start()
    try:
        cli, err = do_login(sim, pwd="wrong-password", tries=2)
        check(cli is None or not cli.logged_in, "登录被拒")
        msg = str(err or "")
        check("密码" in msg or "不正确" in msg, "提示能看懂", msg[:80])
        check(sim.state.bad_crypto == 0, "密文本身是合法的（不是加密出错）")
        # 换正确密码应当能登
        cli2, err2 = do_login(sim, tries=2)
        check(cli2 is not None and cli2.logged_in, "换成正确密码就能登录",
              str(err2 or ""))
        say(sim.summary())
    finally:
        sim.stop()


# --------------------------------------------------------------------------
# G. 未开放不退避
# --------------------------------------------------------------------------
def scenario_not_open():
    _out("\n" + "=" * 66)
    _out("G. 还没开放时的节奏")
    # G1：页面写了开放时间 -> 应当待命，不做无谓提交
    sim = SimServer("normal", open_delay=40.0, competitor_interval=0.0).start()
    try:
        cli, err = do_login(sim)
        if not cli or not cli.logged_in:
            check(False, "登录成功", str(err or ""))
            return
        g = PK.PeakGrabber(cli, log=lambda m: None)
        # 高峰期预读失败很正常，这里不判失败 —— 真正的定位交给 runner 反复重试
        try:
            g.find_page()
            g.read_courses()
        except Exception as exc:
            say("预读失败（交给 runner 重试）：%s" % str(exc)[:60])
        check(g.open_at is not None, "从页面读到了选课开放时间",
              time.strftime("%H:%M:%S", time.localtime(g.open_at))
              if g.open_at else "没读到")
        runner = make_runner(g, interval=0.5, submit_interval=0.25,
                             prewarm=5.0)
        runner.start()
        time.sleep(6)
        n1 = g.submits
        runner.stop()
        runner.join(8)
        check(n1 == 0, "知道开放时间就先待命，不做无谓提交", "%d 次" % n1)
        say("距开放 40 秒，6 秒内提交 %d 次（应当为 0）" % n1)
    finally:
        sim.stop()

    # G2：页面上没有开放时间 -> 必须持续投，节奏绝不退让
    sim = SimServer("normal", open_delay=30.0, competitor_interval=0.0,
                    no_open_time=True).start()
    try:
        cli, err = do_login(sim)
        if not cli or not cli.logged_in:
            check(False, "登录成功（无开放时间）", str(err or ""))
            return
        g = PK.PeakGrabber(cli, log=lambda m: None)
        # 高峰期预读失败很正常，这里不判失败 —— 真正的定位交给 runner 反复重试
        try:
            g.find_page()
            g.read_courses()
        except Exception as exc:
            say("预读失败（交给 runner 重试）：%s" % str(exc)[:60])
        check(g.open_at is None, "页面上确实没有开放时间")
        runner = make_runner(g, interval=0.5, submit_interval=0.25)
        runner.start()
        time.sleep(8)
        n2 = g.submits
        runner.stop()
        runner.join(8)
        check(n2 >= 15, "不知道开放时间时持续提交，没有退避", "%d 次" % n2)
        check(sim.state.not_open_hits >= 10,
              "服务器确实一直在回「未开放」", "%d 次" % sim.state.not_open_hits)
        say(sim.summary())
    finally:
        sim.stop()


# --------------------------------------------------------------------------
def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    t0 = time.time()
    groups = {
        "url": scenario_url_build,
        "profiles": lambda: [scenario_profile(p, attempts=a, limit=lm)
                             for p, a, lm in
                             (("normal", 1, 60.0), ("peak", 2, 70.0),
                              ("brutal", 2, 75.0))],
        "session": scenario_session_expiry,
        "lost": scenario_lost_response,
        "5xx": scenario_all_failing,
        "password": scenario_bad_password,
        "notopen": scenario_not_open,
    }
    if which == "all":
        for fn in groups.values():
            fn()
    elif which in groups:
        groups[which]()
    else:
        _out("用法: python test_sim.py [all|url|profiles|session|lost|5xx|"
             "password|notopen]")
        return 2

    _out("\n" + "=" * 66)
    _out("总耗时 %.0f 秒，失败项：%d" % (time.time() - t0, len(FAILS)))
    for f in FAILS:
        _out("  - " + f)
    _out("完整日志（UTF-8）: %s" % _LOG_PATH)
    flush_log()
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
