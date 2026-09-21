#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抢课脚本 —— 中国矿业大学徐海学院教务系统 (正方 jwglxt V-9.0)

用法示例
--------
# 0) 先看看能不能登录 / 选课页面长什么样（强烈建议第一次先跑这个）
python grab.py --user 学号 --password 密码 --discover

# 1) 列出当前可选课程（拿到 jxb_id）
python grab.py --list

# 2) 抢指定教学班（可多个，逗号分隔）
python grab.py --course 3F0A1B2C,4D5E6F70 --interval 0.3

# 3) 试运行：只登录 + 校验，不真正提交
python grab.py --course 3F0A1B2C --dry-run

# 4) 凭据写进 config.json 后就不用每次输（见 config.example.json）
python grab.py --config config.json --course 3F0A1B2C

# 5) 到点自动开抢（本机时间 08:00:00 开始轮询）
python grab.py --course 3F0A1B2C --at "08:00:00"

# 6) 浏览器模式：不接触密码，自己扫码/手输登录，脚本接管会话
#    （需要 pip install selenium；登录密码有问题时用这个）
python grab.py --browser --login-only
python grab.py --browser --list
python grab.py --browser --course 3F0A1B2C

# 7) 抓取浏览器真实发出的登录请求，用于排查加密差异
python grab.py --browser --login-only --dump-login login_request.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from zf_client import Config, LoginError, OpenZfClient, StopError, ZfError  # noqa: E402

DEFAULT_BASE = "http://jwxt.cumtxh.cn/jwglxt"

BANNER = r"""
  _____             _        ____            _         _     _
 |__  / __ _  __ _ | | __   / ___|_ __ __ _ | |__     / \   | |__  _   _
   / / / _` |/ _` || |/ /  | |  _| '__/ _` || '_ \   / _ \  | '_ \| | | |
  / /_| (_| | (_| ||   <   | |_| | | | (_| || |_) | / ___ \ | |_) | |_| |
 /____|\__,_|\__,_||_|\_\   \____|_|  \__,_||_.__/ /_/   \_\|_.__/ \__, |
                                                                   |___/
      正方教务系统 抢课脚本   (仅用于本人账号的课程选择)
"""


def die(msg: str, code: int = 1) -> None:
    print(f"\n[错误] {msg}\n", file=sys.stderr)
    sys.exit(code)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="grab.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="正方教务系统抢课脚本 (中国矿业大学徐海学院)",
        epilog=__doc__.split("用法示例")[-1] if "用法示例" in __doc__ else "",
    )
    g = p.add_argument_group("账号")
    g.add_argument("--user", "-u", help="学号")
    g.add_argument("--password", "-p", help="密码（建议改用 --config，避免出现在命令历史里）")
    g.add_argument("--config", "-c", help="JSON 配置文件路径（含 user/password/course 等）")
    g.add_argument("--base-url", default=None,
                   help=f"教务系统根地址（默认 {DEFAULT_BASE}，可写在配置文件里）")

    g = p.add_argument_group("操作")
    g.add_argument("--discover", action="store_true",
                   help="诊断模式：登录 + 探测选课页面，把页面保存到本地供检查")
    g.add_argument("--list", action="store_true", help="列出可选课程及其 jxb_id")
    g.add_argument("--course", help="要抢的教学班 jxb_id，多个用逗号分隔")
    g.add_argument("--course-file", help="从文件读取 jxb_id（每行一个，# 开头为注释）")
    g.add_argument("--dry-run", action="store_true", help="只登录并验证，不真正提交选课")
    g.add_argument("--selfcheck", action="store_true",
                   help="自检：一次跑完 登录+菜单+选课页探测+端点诊断，产出排查报告")
    g.add_argument("--at", help="等到指定时间再开始，格式 HH:MM:SS")

    g = p.add_argument_group("轮询参数")
    # 注意：这些都刻意不设 argparse 默认值（None），否则 pick() 永远拿到
    # 命令行默认值，配置文件里写的 interval/jitter/... 就全成了摆设。
    # 真正的默认值放在下面组装 Config 时作为 pick() 的兜底参数。
    g.add_argument("--interval", type=float, default=None,
                   help="每轮间隔秒数（默认 0.35，可写在配置文件里）")
    g.add_argument("--jitter", type=float, default=None,
                   help="间隔随机抖动上限秒数（默认 0.15，可写在配置文件里）")
    g.add_argument("--attempts", type=int, default=None,
                   help="最大轮数，0=不限（默认 0，可写在配置文件里）")
    g.add_argument("--minutes", type=float, default=None,
                   help="最长运行分钟数，0=不限（默认 0，可写在配置文件里）")
    g.add_argument("--timeout", type=float, default=None,
                   help="单次请求超时秒数（默认 20，可写在配置文件里）")

    g = p.add_argument_group("模块")
    g.add_argument("--modules", default=None,
                   help="选课模块，按顺序尝试（默认 xsxk,xszx；配置文件里可写成数组）")
    g.add_argument("--gnmkdm", default=None,
                   help="功能模块代码（默认 N253508，可写在配置文件里）")

    g = p.add_argument_group("浏览器模式（不接触密码，推荐）")
    g.add_argument("--browser", nargs="?", const="edge", default=None,
                   choices=["edge", "chrome", "auto"],
                   help="用真实浏览器手动登录，再把会话交给脚本抢课。"
                        "不指定浏览器时默认 edge")
    g.add_argument("--browser-binary", help="浏览器 exe 完整路径（自动找不到时用）")
    g.add_argument("--browser-headless", action="store_true", help="无头模式（你没法手动登录，一般别用）")
    g.add_argument("--login-wait", type=int, default=300, help="等待手动登录的秒数（默认 300）")
    g.add_argument("--login-only", action="store_true",
                   help="只做浏览器登录并验证会话，不抢课")
    g.add_argument("--dump-login", metavar="FILE",
                   help="把浏览器真实发出的登录请求保存到文件（排查加密差异用）")

    p.add_argument("--quiet", action="store_true", help="安静模式")
    p.add_argument("--verbose", action="store_true", help="打印调试信息")
    return p.parse_args(argv)


def load_config_file(path: str) -> dict:
    try:
        # utf-8-sig: 兼容 Windows 记事本保存时带的 BOM
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        die(f"读取配置文件失败: {exc}")
    except UnicodeDecodeError as exc:
        die(f"配置文件编码不是 UTF-8: {exc}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"配置文件不是合法 JSON: {exc}")
    if not isinstance(data, dict):
        die("配置文件根节点必须是对象（形如 {\"user\": \"...\"}）")
    return data


def read_course_file(path: str) -> list:
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        die(f"读取课程文件失败: {exc}")
    except UnicodeDecodeError as exc:
        die(f"课程文件编码不是 UTF-8: {exc}")
    out = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            out.extend(x.strip() for x in line.replace("，", ",").split(",") if x.strip())
    return out


def split_ids(value: str) -> list:
    return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]


def wait_until(hhmmss: str) -> None:
    m = None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            m = time.strptime(hhmmss, fmt)
            break
        except ValueError:
            continue
    if m is None:
        die(f"--at 时间格式错误: {hhmmss}（应为 HH:MM:SS）")

    now = time.localtime()
    target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                          m.tm_hour, m.tm_min, m.tm_sec, 0, 0, -1))
    if target <= time.time():
        target += 86400  # 已过则等明天
    delta = target - time.time()
    print(f"[*] 等待 {delta/3600:.2f} 小时后于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(target))} 开始")
    while True:
        remain = target - time.time()
        if remain <= 0:
            break
        if remain > 30:
            time.sleep(min(30, remain - 20))
        else:
            time.sleep(max(0.0, remain - 0.05))
    print("[*] 时间到，开始抢课")


def cmd_discover(client: OpenZfClient, cfg: Config, args) -> int:
    outdir = Path("discover")
    outdir.mkdir(exist_ok=True)

    print("[*] 步骤 1/3: 登录...")
    name = client.login()
    print(f"[+] 登录成功{'：' + name if name else ''}")

    print("[*] 步骤 2/3: 拉取主菜单...")
    try:
        menu = client.fetch("/xtgl/index_initMenu.html")
        (outdir / "menu.html").write_text(menu, encoding="utf-8")
        links = set()
        for m in re.finditer(r'href=["\']([^"\']*(?:xsxk|选课|retake|补修|重修)[^"\']*)["\']', menu, re.I):
            links.add(m.group(1))
        for m in re.finditer(r"['\"]([^'\"]*xsxk[^'\"]*)['\"]", menu, re.I):
            links.add(m.group(1))
        if links:
            print("[+] 菜单里找到这些选课相关入口：")
            for link in sorted(links):
                print(f"      {link}")
        else:
            print("[!] 菜单里没直接找到 xsxk 链接（正常，选课菜单可能是动态加载的）")
    except ZfError as exc:
        print(f"[!] 拉取主菜单失败: {exc}")

    print("[*] 步骤 3/3: 探测选课页面...")
    found = client.find_select_page()
    if not found:
        print("[!] 没能自动定位选课页面 —— 这通常说明：")
        print("      · 现在不在选课开放时间内，或")
        print("      · 学校的选课模块路径不是 xsxk/xszx")
        print("    请手动打开选课页面，按 F12 → Network，把选课按钮那次的请求")
        print("    URL 和表单参数发我，我来适配。")
        return 2

    path, html = found
    fname = outdir / ("select" + path.split("?")[0].replace("/", "_") + ".html")
    fname.write_text(html, encoding="utf-8")
    print(f"[+] 选课页面: {path}")
    print(f"[+] 已保存: {fname}")

    terms = client.list_terms(html)
    if terms:
        print("[+] 检测到学期选项：" + ", ".join(f"{a}-{b}" for a, b in terms))

    courses = client.parse_courses(html)
    if courses:
        print(f"[+] 页面中解析到 {len(courses)} 条课程记录：")
        for c in courses[:20]:
            print(f"      jxb_id={c['jxb_id']:<16} {c['text']}")
    else:
        print("[!] 页面上没有解析到课程（可能需要在页面里先选择学期/轮次）")
        print(f"    请打开 {fname} 检查真实结构。")

    print("\n[完成] 把上面的 jxb_id 用 --course 传给我就能开始抢课。")
    return 0


def cmd_list(client: OpenZfClient, args) -> int:
    client.login()
    found = client.find_select_page()
    if not found:
        die("未能定位选课页面，请先运行 --discover")
    path, html = found
    courses = client.parse_courses(html)
    if not courses:
        print("[!] 未解析到课程。页面已保存到 discover/ 供检查。")
        Path("discover").mkdir(exist_ok=True)
        Path("discover/select_list.html").write_text(html, encoding="utf-8")
        return 2
    print(f"{'jxb_id':<20} 详情")
    print("-" * 100)
    for c in courses:
        print(f"{c['jxb_id']:<20} {c['text']}")
    return 0


def cmd_selfcheck(client: OpenZfClient, cfg: Config, args) -> int:
    """一次性自检：把排查需要的全部信息打出来。"""
    import re as _re

    print("=" * 70)
    print("  自检报告")
    print("=" * 70)
    print(f"教务地址 : {cfg.base_url}")
    print(f"探测模块 : {', '.join(cfg.modules)}   gnmkdm={cfg.gnmkdm}")

    # 1) 会话
    print("\n[1] 会话状态")
    if not client.logged_in:
        try:
            client.login()
        except LoginError as exc:
            print(f"    ✗ 登录失败: {exc}")
            return 3
    print("    ✓ 会话有效" + (f"（{cfg.username}）" if cfg.username else ""))

    # 2) 主菜单里的选课入口
    print("\n[2] 主菜单里的选课入口")
    try:
        menu = client.fetch("/xtgl/index_initMenu.html")
        links = sorted({m.group(1) for m in
                        _re.finditer(r'["\'](/jwglxt/[^"\']*?\.html[^"\']*)["\']', menu)})
        hits = [l for l in links if _re.search(r'xsxk|选课|xk|retake|bx', l, _re.I)]
        if hits:
            for l in hits:
                print(f"    {l}")
        else:
            print("    (菜单里没有静态选课链接，选课入口可能是动态加载的)")
        print(f"    菜单里共 {len(links)} 个链接")
    except ZfError as exc:
        print(f"    ✗ 拉取菜单失败: {exc}")

    # 3) 选课页面探测
    print("\n[3] 选课页面探测")
    found = None
    try:
        found = client.find_select_page()
    except ZfError as exc:
        print(f"    ✗ 探测异常: {exc}")
    if found:
        path, html = found
        print(f"    ✓ 命中: {path}   (HTML {len(html)} 字符)")
        terms = client.list_terms(html)
        if terms:
            print(f"    学期选项: {', '.join(f'{a}-{b}' for a, b in terms)}")
        courses = client.parse_courses(html)
        print(f"    解析到课程: {len(courses)} 条")
        for c in courses[:20]:
            print(f"      jxb_id={c['jxb_id']:<20} {c['text'][:80]}")
        outdir = Path("discover")
        outdir.mkdir(exist_ok=True)
        safe = path.split("?")[0].replace("/", "_")
        (outdir / f"selfcheck{safe}.html").write_text(html, encoding="utf-8")
        print(f"    原始 HTML 已存到 discover/selfcheck{safe}.html")
    else:
        print("    ✗ 没能定位选课页")
        print("      -> 可能不在选课开放期，或学校模块路径不同")

    # 4) 选课端点诊断（用假 jxb_id，不会真的选上课）
    print("\n[4] 选课端点诊断（用假 jxb_id 探测，不会选中任何课）")
    try:
        results = client.diagnose_select_endpoints()
    except ZfError as exc:
        results = []
        print(f"    ✗ 诊断异常: {exc}")
    alive = [r for r in results if "★" in r.get("verdict", "")]
    others = [r for r in results if "★" not in r.get("verdict", "")]
    if alive:
        print(f"    ★ 发现 {len(alive)} 个疑似有效端点：")
        for r in alive:
            print(f"      {r['endpoint']:<24} [{r['style']}] HTTP {r['status']}")
            print(f"         响应: {r['body']}")
    else:
        print("    没有发现明确有效的端点。")
    if args.verbose and others:
        print(f"\n    （其余 {len(others)} 个组合的响应，仅 --verbose 显示）")
        for r in others[:12]:
            print(f"      {r['endpoint']:<24} [{r['style']}] {r['verdict']}")

    print("\n" + "=" * 70)
    print("  自检结束。把以上全部内容复制给我即可。")
    print("=" * 70)
    return 0


def run_browser_mode(client: "OpenZfClient", cfg: Config, args, courses: list) -> int:
    """浏览器登录模式：你手动登录，脚本接管会话。"""
    try:
        import zf_browser
    except ImportError as exc:
        die(f"加载浏览器模块失败: {exc}")
        return 1

    try:
        result = zf_browser.browser_login(
            base_url=cfg.base_url,
            browser=args.browser,
            headless=args.browser_headless,
            binary=args.browser_binary,
            wait_seconds=args.login_wait,
            dump_payload_to=args.dump_login,
        )
    except zf_browser.SeleniumUnavailable as exc:
        die(str(exc), 6)
        return 6

    driver = result.get("driver")

    try:
        if not result.get("logged_in"):
            die("浏览器登录未完成（超时或未检测到跳转）。", 7)

        if not client.adopt_cookies(result.get("cookies") or []):
            die("浏览器会话验证失败：拿到 cookie 但服务端仍要求登录。", 8)

        # 登录请求的对比信息
        payload = result.get("payload")
        if payload:
            print(f"\n[+] 浏览器真实登录请求: {payload.get('method')} {payload.get('url')}")
            body = payload.get("body") or ""
            print(f"    Payload: {body[:600]}")
            if args.dump_login:
                print(f"    完整内容已写入 {args.dump_login}")
            print()

        # 之后一律复用同一会话，不再走密码登录
        client.login = lambda *a, **k: ""  # type: ignore[assignment]

        if args.login_only:
            print("\n[完成] 会话有效。可用 --list 查看课程，或 --course 抢课。")
            return 0

        if args.selfcheck:
            return cmd_selfcheck(client, cfg, args)

        if args.discover:
            return cmd_discover(client, cfg, args)

        if args.list:
            return cmd_list(client, args)

        if not courses:
            print("[!] 没指定要抢的课程，先帮你列出可选课程：\n")
            cmd_list(client, args)
            print("\n[提示] 把上面的 jxb_id 用 --course 传进来就能抢课。")
            return 0

        if args.at:
            wait_until(args.at)

        def on_success(jxb_id, msg):
            print(f"\n{'=' * 60}\n  ✅ 抢课成功: {jxb_id}\n  {msg}\n{'=' * 60}\n")

        def on_failure(jxb_id, msg):
            print(f"\n  ❌ 放弃: {jxb_id} —— {msg}\n")

        finished = client.grab(courses, on_success=on_success, on_failure=on_failure)
        return 0 if finished else 1

    finally:
        if driver is not None:
            print("\n[*] 关闭浏览器")
            try:
                driver.quit()
            except Exception:
                pass


def main(argv=None) -> int:
    args = parse_args(argv)

    # ---- 组装配置 ----
    file_cfg: dict = {}
    if args.config:
        file_cfg = load_config_file(args.config)

    def pick(cli_val, key, default=None):
        if cli_val is not None:
            return cli_val
        return file_cfg.get(key, default)

    user = pick(args.user, "user") or os.environ.get("JWXT_USER")
    password = pick(args.password, "password") or os.environ.get("JWXT_PASSWORD")

    browser_mode = bool(args.browser)

    # 浏览器模式下不需要密码: 你在浏览器里手动登录, 脚本只接管会话
    if not browser_mode and (not user or not password):
        die("缺少账号或密码。请用 --user/--password，或 --config 指定配置文件，"
            "或设置环境变量 JWXT_USER / JWXT_PASSWORD。\n"
            "    也可以用 --browser 走浏览器登录模式，完全不接触密码。")
    if not browser_mode and not user:
        die("缺少账号。")

    # modules 可能来自命令行(字符串) 或配置文件(数组), 两种都要支持
    raw_modules = args.modules
    if raw_modules is None:
        raw_modules = file_cfg.get("modules")
    if isinstance(raw_modules, (list, tuple)):
        modules = tuple(str(x).strip() for x in raw_modules if str(x).strip())
    elif isinstance(raw_modules, str):
        modules = tuple(x.strip() for x in raw_modules.split(",") if x.strip())
    else:
        modules = ()
    if not modules:
        modules = ("xsxk", "xszx")
    cfg = Config(
        base_url=pick(args.base_url, "base_url", DEFAULT_BASE),
        username=user or "",
        password=password or "",
        modules=modules,
        gnmkdm=pick(args.gnmkdm, "gnmkdm", "N253508"),
        interval=float(pick(args.interval, "interval", 0.35)),
        jitter=float(pick(args.jitter, "jitter", 0.15)),
        max_attempts=int(pick(args.attempts, "attempts", 0)),
        max_minutes=float(pick(args.minutes, "minutes", 0.0)),
        timeout=float(pick(args.timeout, "timeout", 20.0)),
        verbose=not args.quiet,
    )

    # 提前收集并校验目标课程, 避免"没给课程却先去登录"
    courses: list = []
    if args.course:
        courses.extend(split_ids(args.course))
    if args.course_file:
        courses.extend(read_course_file(args.course_file))
    needs_course = not (args.discover or args.list or args.login_only or args.selfcheck)
    if needs_course:
        cfg_course = file_cfg.get("course") or []
        if isinstance(cfg_course, str):
            cfg_course = split_ids(cfg_course)
        courses.extend(str(x).strip() for x in cfg_course if str(x).strip())
        courses = list(dict.fromkeys(courses))
        if not courses:
            # 浏览器模式下先让你登录看看有哪些课, 再决定抢什么
            if browser_mode:
                args.list = True
                needs_course = False
            else:
                die("没有指定要抢的课程。用 --course <jxb_id>，或先跑 --list 看看有哪些。")

    if not args.quiet:
        print(BANNER)
        print(f"[*] 目标: {cfg.base_url}")
        if browser_mode:
            print("[*] 模式: 浏览器登录（脚本不接触密码）")
        else:
            print(f"[*] 账号: {cfg.username}")
        if courses:
            print(f"[*] 课程: {', '.join(courses)}")

    client = OpenZfClient(cfg)

    try:
        # ---- 浏览器模式: 先手动登录, 再接管会话 ----
        if browser_mode:
            return run_browser_mode(client, cfg, args, courses)

        if args.discover:
            return cmd_discover(client, cfg, args)

        if args.list:
            return cmd_list(client, args)

        if args.dry_run:
            client.login()
            found = client.find_select_page()
            print(f"[+] 选课页面: {found[0] if found else '未定位到'}")
            print(f"[+] 目标课程: {', '.join(courses)}")
            print("[完成] dry-run 通过，未提交任何选课请求。")
            return 0

        if args.at:
            wait_until(args.at)

        def on_success(jxb_id, msg):
            print(f"\n{'=' * 60}\n  ✅ 抢课成功: {jxb_id}\n  {msg}\n{'=' * 60}\n")

        def on_failure(jxb_id, msg):
            print(f"\n  ❌ 放弃: {jxb_id} —— {msg}\n")

        finished = client.grab(courses, on_success=on_success, on_failure=on_failure)
        return 0 if finished else 1

    except StopError as exc:
        die(str(exc), 5)
    except LoginError as exc:
        die(str(exc), 3)
    except ZfError as exc:
        die(str(exc), 4)
    except KeyboardInterrupt:
        print("\n[*] 已手动中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
