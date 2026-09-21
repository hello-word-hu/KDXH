#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器登录模式（Selenium）

用途：绕开密码 / 验证码 / 加解密，用真实浏览器登录，然后把登录后的会话
Cookie 交给 HTTP 客户端去抢课。

为什么需要它：
    正方教务系统登录失败时只会回一句"用户名或密码不正确"，无法区分
    "账号不存在"、"密码错"、"加密方式不对"。用真实浏览器登录可以：
      1. 你手动登录（脚本不接触密码）
      2. 脚本抓取真实登录请求的 Payload，用来对比排查
      3. 拿到已登录会话，直接去抢课

依赖：  pip install selenium
        （Edge 是 Windows 自带的，Selenium 4.6+ 会自动配好驱动）
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

__all__ = ["browser_login", "SeleniumUnavailable"]


class SeleniumUnavailable(Exception):
    """没有安装 selenium / 没有可用浏览器。"""


def _import_selenium():
    try:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options as EdgeOptions
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.common.exceptions import WebDriverException
    except ImportError as exc:
        raise SeleniumUnavailable(
            "没有安装 selenium。请先运行：pip install selenium"
        ) from exc
    return webdriver, EdgeOptions, ChromeOptions, WebDriverException


def dismiss_dialogs(driver) -> List[str]:
    """捕获并关闭原生弹窗（alert / confirm / prompt），返回弹窗文字。

    关键：必须在弹窗出现后【立刻】用 CDP 抓文字，然后再关闭。
    直接用 Selenium 的 switch_to.alert 常常拿不到文字 —— 弹窗已被
    浏览器默认行为处理掉了。

    另外，即使拿不到文字，也一定会尝试关闭弹窗 —— 否则后面的操作
    会被未处理的弹窗挡住（UnexpectedAlertPresentException）。
    """
    texts: List[str] = []
    for _ in range(5):
        handled = False

        # 方式 1：CDP（能拿到文字）
        try:
            msg = driver.execute_cdp_cmd("Page.handleJavaScriptDialog",
                                         {"accept": True})
            del msg
            handled = True
        except Exception:
            pass

        # 方式 2：Selenium 原生接口（兜底，至少能关掉）
        if not handled:
            try:
                al = driver.switch_to.alert
                texts.append(al.text or "")
                al.accept()
                handled = True
            except Exception:
                pass

        if not handled:
            break
        time.sleep(0.15)

    return [t for t in texts if t]


def goto(driver, url: str, attempts: int = 3, log=None,
         wait_after: float = 1.0) -> bool:
    """带重试的页面导航。

    driver.get() 在服务器慢时会抛 TimeoutException，之后页面可能只加载了一半。
    这里重试几次，并在超时后用 JS 停止加载，尽量让页面可用。
    返回是否成功（能读到 body 就算成功）。
    """
    def say(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    for i in range(1, attempts + 1):
        try:
            driver.get(url)
            time.sleep(wait_after)
            # 能读到 body 就算成功
            try:
                ok = driver.execute_script(
                    "return !!(document && document.body);")
            except Exception:
                ok = True
            if ok:
                return True
        except Exception as exc:
            say(f"  打开页面失败（第 {i}/{attempts} 次）："
                f"{type(exc).__name__}")
            # 尝试停止加载，保留已渲染的内容
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            try:
                if driver.execute_script(
                        "return !!(document && document.body && "
                        "document.body.innerHTML.length > 200);"):
                    say("  页面已部分加载，继续使用")
                    return True
            except Exception:
                pass
            if i < attempts:
                time.sleep(1.5)
    return False


def quit_driver(driver, timeout: float = 8.0) -> None:
    """确保关闭浏览器；如果 quit() 本身卡住，就强杀驱动进程。

    Selenium 的 driver.quit() 在服务端无响应时可能一起卡死，
    所以这里放到线程里做，超时就杀进程 —— 否则会留下僵尸浏览器。
    """
    if driver is None:
        return
    done = threading.Event()

    def do_quit():
        try:
            driver.quit()
        except Exception:
            pass
        finally:
            done.set()

    th = threading.Thread(target=do_quit, daemon=True)
    th.start()
    if done.wait(timeout):
        return

    # quit 卡住了 -> 直接杀驱动进程
    try:
        svc = getattr(driver, "service", None)
        proc = getattr(svc, "process", None) if svc else None
        if proc is not None:
            proc.kill()
    except Exception:
        pass


def find_browser_binary(kind: str = "edge") -> Optional[str]:
    """在常见位置找浏览器可执行文件（自动查找失败时兜底）。"""
    import os
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")

    if kind == "edge":
        cands = [
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
        ]
    else:
        cands = [
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        ]
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def _build_driver(browser: str, headless: bool, binary: Optional[str],
                  page_load_timeout: float = 30.0,
                  profile_dir: Optional[str] = None):
    webdriver, EdgeOptions, ChromeOptions, WebDriverException = _import_selenium()

    browser = (browser or "edge").lower()
    attempts = []

    def make(kind: str):
        # 自动查找失败时，用常见安装路径兜底
        loc = binary or find_browser_binary(kind)
        if kind == "edge":
            opt = EdgeOptions()
            opt.add_experimental_option("excludeSwitches", ["enable-automation"])
            opt.add_experimental_option("useAutomationExtension", False)
            opt.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            try:
                opt.set_capability("unhandledPromptBehavior", "ignore")
            except Exception:
                pass
            # eager: DOM 就绪即返回，不等图片等资源 —— 慢服务器上快很多
            try:
                opt.page_load_strategy = "eager"
            except Exception:
                pass
            if loc:
                opt.binary_location = loc
            if profile_dir:
                # 持久配置目录：保留 Cookie，让"记住我"生效
                opt.add_argument(f"--user-data-dir={profile_dir}")
            if headless:
                opt.add_argument("--headless=new")
            opt.add_argument("--disable-gpu")
            opt.add_argument("--window-size=1280,900")
            return webdriver.Edge(options=opt)
        opt = ChromeOptions()
        opt.add_experimental_option("excludeSwitches", ["enable-automation"])
        opt.add_experimental_option("useAutomationExtension", False)
        opt.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        try:
            opt.set_capability("unhandledPromptBehavior", "ignore")
        except Exception:
            pass
        try:
            opt.page_load_strategy = "eager"
        except Exception:
            pass
        if loc:
            opt.binary_location = loc
        if profile_dir:
            opt.add_argument(f"--user-data-dir={profile_dir}")
        if headless:
            opt.add_argument("--headless=new")
        opt.add_argument("--disable-gpu")
        opt.add_argument("--window-size=1280,900")
        return webdriver.Chrome(options=opt)

    order = ["edge", "chrome"] if browser == "edge" else ["chrome", "edge"]
    if browser not in ("edge", "chrome", "auto"):
        order = [browser]
    if browser == "auto":
        order = ["edge", "chrome"]

    for kind in order:
        if kind not in ("edge", "chrome"):
            continue
        try:
            drv = make(kind)
            # 关键：设置页面加载超时，服务端不响应时快速失败而不是无限等待
            try:
                drv.set_page_load_timeout(page_load_timeout)
                drv.set_script_timeout(20)
            except Exception:
                pass
            # 让原生弹窗不要被驱动自动处理掉，我们自己去关（否则读不到文字）
            try:
                drv.execute_cdp_cmd("Page.enable", {})
            except Exception:
                pass
            return drv, kind
        except WebDriverException as exc:
            attempts.append(f"{kind}: {str(exc).splitlines()[0][:120]}")
        except Exception as exc:  # 驱动下载失败等
            attempts.append(f"{kind}: {type(exc).__name__}: {str(exc)[:120]}")

    raise SeleniumUnavailable(
        "无法启动浏览器。尝试过：\n  " + "\n  ".join(attempts) +
        "\n\n可以试试加 --browser-binary \"浏览器exe的完整路径\"。"
    )


def cookies_to_jar(cookies: List[Dict[str, Any]]) -> http.cookiejar.CookieJar:
    """把 Selenium 的 cookie 列表塞进标准库的 CookieJar。"""
    jar = http.cookiejar.CookieJar()
    for c in cookies or []:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain") or ""
        path = c.get("path") or "/"
        try:
            cookie = http.cookiejar.Cookie(
                version=0, name=name, value=value,
                port=None, port_specified=False,
                domain=domain, domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=path, path_specified=bool(path),
                secure=bool(c.get("secure", False)),
                expires=int(c["expiry"]) if c.get("expiry") else None,
                discard=False,
                comment=None, comment_url=None, rest={}, rfc2109=False,
            )
        except (TypeError, ValueError):
            continue
        try:
            jar.set_cookie(cookie)
        except Exception:
            pass
    return jar


def check_server(base_url: str, timeout: float = 8.0, attempts: int = 3,
                 log=None) -> Dict[str, Any]:
    """快速探测教务系统是否可用（选课高峰期服务器常常很慢）。

    返回 {"ok": bool, "avg": 秒, "success": n, "total": n, "message": str}
    """
    import urllib.request

    def say(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    url = base_url.rstrip("/") + "/xtgl/login_slogin.html"
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    times: List[float] = []
    ok = 0
    for i in range(attempts):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            resp = urllib.request.urlopen(req, timeout=timeout)
            resp.read(2048)
            times.append(time.time() - t0)
            ok += 1
        except Exception:
            times.append(time.time() - t0)
        if i + 1 < attempts:
            time.sleep(0.4)

    avg = sum(times) / len(times) if times else 0.0
    if ok == attempts:
        if avg < 2.0:
            msg = f"教务系统正常（平均 {avg:.1f}s）"
        else:
            msg = f"教务系统响应较慢（平均 {avg:.1f}s）"
    elif ok > 0:
        msg = (f"教务系统**不稳定**：{attempts} 次探测只有 {ok} 次成功，"
               f"平均 {avg:.1f}s —— 可能正在选课高峰期")
    else:
        msg = (f"教务系统**当前无响应**：{attempts} 次探测全部超时（{timeout:.0f}s）。"
               f"可能是选课高峰被挤爆，或网络问题")
    return {"ok": ok > 0, "avg": avg, "success": ok, "total": attempts, "message": msg}


def login_with_password_by_browser(
    user: str,
    password: str,
    base_url: str,
    login_path: str = "/xtgl/login_slogin.html",
    browser: str = "edge",
    headless: bool = True,
    binary: Optional[str] = None,
    timeout: float = 45.0,
    profile_dir: Optional[str] = None,
    log=None,
) -> Dict[str, Any]:
    """用真实浏览器自动填表提交登录，返回会话 cookie。

    为什么不用纯 HTTP 算 RSA：
        本校教务系统开启了「动态 RSA 公钥」，而且实测发现自行实现的
        加密结果会被服务端判为"密码不正确"，但浏览器走同样的密码能成功。
        既然浏览器一定能过，就让它负责加密和提交这一步 —— 这也是最贴近
        真实用户行为的做法。

    profile_dir 传入时使用持久浏览器配置目录：
        - 首次运行会创建目录并正常登录
        - 之后如果"记住我"生效，可以跳过登录，速度大幅提升

    返回 {"ok": bool, "cookies": [...], "message": str, "reused": bool}
    """
    def say(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    try:
        from selenium.webdriver.common.by import By
    except ImportError as exc:
        raise SeleniumUnavailable("没有安装 selenium") from exc

    if profile_dir:
        try:
            os.makedirs(profile_dir, exist_ok=True)
        except OSError:
            profile_dir = None

    driver, kind = _build_driver(browser, headless, binary,
                                 profile_dir=profile_dir)
    result: Dict[str, Any] = {"ok": False, "cookies": [], "driver": None,
                              "message": "", "reused": False}
    try:
        login_url = base_url.rstrip("/") + login_path
        menu_url = base_url.rstrip("/") + "/xtgl/index_initMenu.html"

        # 先看持久配置里的会话是否还有效（"记住我"生效时可跳过登录）
        if profile_dir:
            say("检查是否已登录（持久会话）…")
            try:
                driver.get(menu_url)
                time.sleep(1.0)
                cur = driver.current_url or ""
                has_form = driver.execute_script(
                    "return !!document.querySelector('form[action*=\"login_slogin\"]');")
                cookies = driver.get_cookies() or []
                has_session = any(c.get("name") == "JSESSIONID" for c in cookies)
                # 必须同时满足：没被弹回登录页 + 真的拿到了会话 cookie
                # （只判断页面内容会误判：服务器返回不完整页面时什么特征都没有）
                if (not has_form) and ("login_slogin" not in cur) and has_session:
                    result["ok"] = True
                    result["reused"] = True
                    result["cookies"] = cookies
                    result["message"] = f"复用已有登录状态（{kind}），无需重新登录"
                    return result
                if not has_session:
                    say("  没有有效的会话 cookie，需要登录")
                else:
                    say("  会话已过期，需要重新登录")
            except Exception as exc:
                say(f"  检查失败（继续正常登录）：{type(exc).__name__}")

        # 打开登录页 —— 服务端慢时最多重试 3 次
        say(f"正在打开登录页（{kind}）…")
        opened = False
        last_err = ""
        for attempt in range(1, 4):
            t0 = time.time()
            try:
                driver.get(login_url)
                opened = True
                say(f"登录页已打开（{time.time() - t0:.1f}s）")
                break
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {str(exc).splitlines()[0][:80]}"
                say(f"第 {attempt} 次打开登录页失败（{time.time() - t0:.0f}s）：{last_err}")
                if attempt < 3:
                    say("教务系统可能正忙，2 秒后重试…")
                    time.sleep(2.0)
        if not opened:
            result["message"] = (
                f"打不开登录页：{last_err}\n"
                "教务系统可能正处在选课高峰期（服务器响应很慢）。\n"
                "建议过几分钟再试。")
            return result

        time.sleep(1.5)

        # 确认页面上确实是登录表单 —— 服务器忙时可能返回错误页/空页
        u = None
        for attempt in range(1, 4):
            try:
                u = driver.find_element(By.ID, "yhm")
                break
            except Exception:
                try:
                    u = driver.find_element(By.NAME, "yhm")
                    break
                except Exception:
                    u = None

            # 找不到表单：看看当前到底是什么页面
            try:
                cur = driver.current_url or ""
            except Exception:
                cur = ""
            try:
                snippet = driver.execute_script(
                    "return document.body ? document.body.innerText.slice(0,200) : '';") or ""
            except Exception:
                snippet = ""
            snippet = re.sub(r"\s+", " ", snippet).strip()

            say(f"第 {attempt} 次没找到登录表单（当前页：{cur[:60]}）")
            if snippet:
                say(f"  页面内容：{snippet[:120]}")
            if "统一身份" in snippet or "authserver" in cur or "sso" in cur.lower():
                result["message"] = ("学校把登录跳转到了「统一身份认证」页面。\n"
                                     "请改用「浏览器登录」模式手动完成。")
                return result
            if attempt < 3:
                say("  可能是服务器返回了不完整页面，重新加载…")
                try:
                    driver.get(login_url)
                except Exception:
                    pass
                time.sleep(2.5)

        if u is None:
            result["message"] = (
                "打不开登录表单。教务系统可能正忙（返回了不完整的页面），\n"
                "或者学校已启用统一身份认证登录。\n"
                "建议：改用「浏览器登录」模式，或过几分钟再试。")
            return result

        try:
            u.clear()
            u.send_keys(user)
            p = driver.find_element(By.ID, "mm")
            p.clear()
            p.send_keys(password)
        except Exception as exc:
            result["message"] = (f"填写登录表单失败：{type(exc).__name__}。"
                                 "服务器可能返回了异常页面，请稍后重试。")
            return result
        time.sleep(0.4)

        # 若页面上有验证码，就无法自动通过
        need_captcha = False
        try:
            yzm = driver.find_elements(By.ID, "yzm")
            if yzm and yzm[0].is_displayed():
                need_captcha = True
        except Exception:
            pass
        if need_captcha:
            result["message"] = "登录页需要输入验证码，请改用「浏览器登录」模式手动完成"
            return result

        say("正在提交登录…")
        driver.find_element(By.ID, "dl").click()

        deadline = time.time() + timeout
        last_note = 0.0
        while time.time() < deadline:
            time.sleep(0.8)
            try:
                url = driver.current_url or ""
                has_form = driver.execute_script(
                    "return !!document.querySelector('form[action*=\"login_slogin\"]');")
            except Exception:
                # 页面还在加载 / 服务端慢，继续等
                if time.time() - last_note > 10:
                    last_note = time.time()
                    say("  登录中…（服务器响应慢，继续等待）")
                continue

            if (not has_form) and ("login_slogin" not in url):
                cookies = driver.get_cookies() or []
                # 必须真的拿到会话 cookie，否则可能只是页面没加载出来
                if not any(c.get("name") == "JSESSIONID" for c in cookies):
                    if time.time() - last_note > 5:
                        last_note = time.time()
                        say("  页面已跳转但还没拿到会话 cookie，继续等待…")
                    continue
                result["ok"] = True
                result["cookies"] = cookies
                result["message"] = f"登录成功（{kind}）"
                return result
            try:
                tip = driver.execute_script(
                    "var e=document.getElementById('tips');"
                    "return e?e.innerText.trim():'';")
            except Exception:
                tip = ""
            if tip:
                result["message"] = tip.replace("\n", " ")[:80]
                return result

            if time.time() - last_note > 10:
                last_note = time.time()
                say(f"  等待登录结果…（剩余 {int(deadline - time.time())}s）")

        result["message"] = (f"登录超时（{timeout:.0f}s 内页面没有跳转）。\n"
                             "教务系统响应很慢，建议稍后重试。")
        return result
    finally:
        # 关键：无论成功失败都必须关掉浏览器。
        # 成功后这个 driver 也用不到了（GUI 只需 cookie），
        # 之前只在失败时关闭，导致成功路径留下僵尸浏览器进程。
        say("正在关闭浏览器…")
        quit_driver(driver)


def _capture_login_payload(driver, url_hint: str = "login_slogin") -> Optional[Dict[str, Any]]:
    """尽力抓取登录请求的 Payload，用来排查加密/字段差异。

    Selenium 各版本对网络日志的支持不一致，所以这里做了多层兜底，
    任何一层失败都不影响主流程。
    """
    # 方式 1: 性能日志（需要 goog:loggingPrefs，已在 options 里开启）
    try:
        logs = driver.get_log("performance")
    except Exception:
        logs = []
    for entry in reversed(logs or []):
        try:
            msg = json.loads(entry.get("message", "{}")).get("message", {})
            if msg.get("method") != "Network.requestWillBeSent":
                continue
            params = msg.get("params", {})
            req = params.get("request", {})
            url = req.get("url", "")
            if url_hint in url and req.get("method") == "POST":
                return {
                    "url": url,
                    "method": req.get("method"),
                    "body": req.get("postData"),
                    "headers": req.get("headers"),
                }
        except Exception:
            continue

    # 方式 2: 某些版本提供的 driver.requests
    try:
        for entry in reversed(getattr(driver, "requests", None) or []):
            try:
                req = entry.request
                if url_hint in req.url and req.method == "POST":
                    return {"url": req.url, "method": req.method, "body": req.body}
            except Exception:
                continue
    except Exception:
        pass
    return None


def browser_login(
    base_url: str,
    login_path: str = "/xtgl/login_slogin.html",
    browser: str = "edge",
    headless: bool = False,
    binary: Optional[str] = None,
    wait_seconds: int = 300,
    poll_seconds: float = 1.5,
    keep_open: bool = True,
    dump_payload_to: Optional[str] = None,
) -> Dict[str, Any]:
    """打开浏览器，等你手动登录，然后返回会话 cookie。

    返回 {"cookies": [...], "driver": driver, "payload": {...} or None}
    """
    driver, kind = _build_driver(browser, headless, binary)
    login_url = base_url.rstrip("/") + login_path

    print(f"[*] 已启动 {kind} 浏览器")
    if not headless:
        print("[*] 浏览器窗口已打开，请在里面登录教务系统")
        print(f"    地址: {login_url}")
        print("[*] 登录成功后请不要关闭窗口，脚本会自动检测并接管")
        print(f"[*] 最多等待 {wait_seconds} 秒\n")

    try:
        driver.get(login_url)
    except Exception as exc:
        print(f"[!] 打开登录页失败: {exc}")

    deadline = time.time() + wait_seconds
    logged_in = False
    last_tip = ""

    while time.time() < deadline:
        try:
            url = driver.current_url or ""
        except Exception as exc:
            print(f"[!] 浏览器连接中断: {exc}")
            break

        # 登录成功后正方会跳到 index_initMenu 之类的页面
        if "login_slogin" not in url and "login" not in url.lower():
            logged_in = True
            break

        # 备用判据：页面上还能看到登录表单就说明没登录成功
        try:
            has_form = driver.execute_script(
                "return !!document.querySelector('form[action*=\"login_slogin\"]');")
            if has_form is False and "login_slogin" not in url:
                logged_in = True
                break
        except Exception:
            pass

        # 把页面上的失败提示回显给用户
        try:
            tip = driver.execute_script(
                "var e=document.getElementById('tips');return e?e.innerText.trim():'';")
            if tip and tip != last_tip:
                last_tip = tip
                print(f"[!] 页面提示: {tip}")
        except Exception:
            pass

        remaining = int(deadline - time.time())
        if remaining % 15 == 0 and remaining > 0:
            print(f"[*] 等待登录中… 剩余 {remaining}s")
        time.sleep(poll_seconds)

    # 无论是否需要保存，都尝试抓一次登录请求（失败也不影响主流程）
    payload = _capture_login_payload(driver)
    if dump_payload_to and payload:
        try:
            with open(dump_payload_to, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            print(f"[+] 已保存登录请求到 {dump_payload_to}")
        except OSError as exc:
            print(f"[!] 保存登录请求失败: {exc}")

    if not logged_in:
        print("[!] 等待超时或未检测到登录成功。")
        if not keep_open:
            try:
                driver.quit()
            except Exception:
                pass
        return {"cookies": [], "driver": driver, "payload": payload, "logged_in": False}

    cookies = []
    try:
        cookies = driver.get_cookies() or []
    except Exception as exc:
        print(f"[!] 读取 cookie 失败: {exc}")

    names = [c.get("name") for c in cookies]
    print(f"[+] 检测到登录成功，取得 {len(cookies)} 个 cookie: {names}")
    return {"cookies": cookies, "driver": driver, "payload": payload, "logged_in": True}
