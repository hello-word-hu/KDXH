#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抢课助手 —— 图形界面版

两种登录方式：
  1. 账号密码登录  —— 直接输入学号密码，程序自动登录（快）
  2. 浏览器登录    —— 弹出浏览器让你扫码/手输（密码有问题时的备份方案）

运行：  python gui.py
依赖：  已随附在 libs\\ 目录，无需安装
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 本目录下自带依赖时优先使用（免去全局安装 / 免 pip）
_LIBS = HERE / "libs"
if _LIBS.is_dir():
    sys.path.insert(0, str(_LIBS))

DEFAULT_BASE = "http://jwxt.cumtxh.cn/jwglxt"
CONF_FILE = HERE / "gui_last.json"
TABLE_DUMP = HERE / "table_debug.json"
# 持久浏览器配置目录：保留 Cookie，让"记住我"生效，减少每次登录的等待
PROFILE_DIR = HERE / ".edge_profile"

SELECT_PATHS = [
    # 本校真实入口（从主菜单 clickMenu 挖出来的）：选课 -> 自主选课
    # 注意：DEFAULT_BASE 已包含 /jwglxt，这里不能重复
    "/xsxk/zzxkyzb_cxZzxkYzbIndex.html?gnmkdm=N253512",
    "/xsxk/xsxk_index.html?gnmkdm=N253508",
    "/xsxk/xsxk_list.html?gnmkdm=N253508",
    "/xszx/xsxk_index.html?gnmkdm=N253508",
]

# 选课页提示「不在选课期」的关键词
NOT_OPEN_MARKS = ("不属于选课阶段", "不在选课时间", "选课已结束",
                  "选课未开始", "未开放", "请与管理员联系")

MODULES = ["不限", "必修", "选修", "任选", "公选", "限选", "通识"]

MODE_PWD = "账号密码登录"
MODE_BROWSER = "浏览器登录（扫码/手输，无需密码）"


class GrabApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("抢课助手 · 中国矿业大学徐海学院教务系统")
        root.geometry("900x760")
        root.minsize(800, 620)

        self.msgq: "queue.Queue[tuple]" = queue.Queue()
        self.driver = None
        self.browser_kind = ""
        self.grabber = None
        self.http = None          # 账密登录用的 HTTP 客户端
        self.worker = None
        self.stop_flag = threading.Event()
        self.running = False
        self.tree_courses = []

        self._build_ui()
        self._load_conf()
        self.root.after(120, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ================= UI =================
    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}

        # ---------- 第 1 步：登录 ----------
        top = ttk.LabelFrame(self.root, text=" 第 1 步：登录 ")
        top.pack(fill="x", **pad)

        self.var_mode = tk.StringVar(value=MODE_PWD)
        rb1 = ttk.Radiobutton(top, text=MODE_PWD, value=MODE_PWD,
                              variable=self.var_mode, command=self._on_mode_change)
        rb2 = ttk.Radiobutton(top, text=MODE_BROWSER, value=MODE_BROWSER,
                              variable=self.var_mode, command=self._on_mode_change)
        rb1.grid(row=0, column=0, sticky="w", padx=(10, 14), pady=(8, 4))
        rb2.grid(row=0, column=1, sticky="w", pady=(8, 4))

        # 账密输入区
        self.frm_pwd = ttk.Frame(top)
        self.frm_pwd.grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 4))

        ttk.Label(self.frm_pwd, text="学号：").grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.var_user = tk.StringVar()
        ttk.Entry(self.frm_pwd, textvariable=self.var_user, width=20).grid(row=0, column=1, sticky="w")

        ttk.Label(self.frm_pwd, text="密码：").grid(row=0, column=2, sticky="e", padx=(16, 4))
        self.var_pwd = tk.StringVar()
        self.ent_pwd = ttk.Entry(self.frm_pwd, textvariable=self.var_pwd, width=20, show="●")
        self.ent_pwd.grid(row=0, column=3, sticky="w")

        self.var_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.frm_pwd, text="显示", variable=self.var_show,
                        command=self._toggle_pwd).grid(row=0, column=4, padx=(8, 0))

        self.lbl_pwdhint = ttk.Label(
            self.frm_pwd,
            text="密码只保存在内存里，不会写入磁盘。",
            foreground="#888")
        self.lbl_pwdhint.grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 2))

        # 浏览器模式提示
        self.lbl_browser = ttk.Label(
            top,
            text="会弹出浏览器，请在那里登录（账号密码或微信扫码都可以）。程序不接触你的密码。",
            foreground="#666", justify="left")
        self.lbl_browser.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(2, 8))

        # ---------- 第 2 步：抢什么课 ----------
        cond = ttk.LabelFrame(self.root, text=" 第 2 步：想抢什么课（至少填一个条件） ")
        cond.pack(fill="x", **pad)

        ttk.Label(cond, text="课程代码：").grid(row=0, column=0, sticky="e", padx=(10, 4), pady=6)
        self.var_code = tk.StringVar()
        ttk.Entry(cond, textvariable=self.var_code, width=24).grid(row=0, column=1, sticky="w")
        ttk.Label(cond, text="例：B1234567", foreground="#888").grid(row=0, column=2, sticky="w", padx=8)

        ttk.Label(cond, text="教师名称：").grid(row=1, column=0, sticky="e", padx=(10, 4), pady=6)
        self.var_teacher = tk.StringVar()
        ttk.Entry(cond, textvariable=self.var_teacher, width=24).grid(row=1, column=1, sticky="w")
        ttk.Label(cond, text="支持部分匹配，例：张", foreground="#888").grid(row=1, column=2, sticky="w", padx=8)

        ttk.Label(cond, text="课程名称：").grid(row=2, column=0, sticky="e", padx=(10, 4), pady=6)
        self.var_name = tk.StringVar()
        ttk.Entry(cond, textvariable=self.var_name, width=24).grid(row=2, column=1, sticky="w")
        ttk.Label(cond, text="支持部分匹配，例：高等数学", foreground="#888").grid(row=2, column=2, sticky="w", padx=8)

        ttk.Label(cond, text="课程类型：").grid(row=3, column=0, sticky="e", padx=(10, 4), pady=6)
        self.var_type = tk.StringVar(value="不限")
        ttk.Combobox(cond, textvariable=self.var_type, values=MODULES,
                     width=21, state="readonly").grid(row=3, column=1, sticky="w")

        ttk.Label(cond, text="轮询间隔（秒）：").grid(row=4, column=0, sticky="e", padx=(10, 4), pady=(6, 10))
        self.var_interval = tk.StringVar(value="1.5")
        ttk.Entry(cond, textvariable=self.var_interval, width=8).grid(row=4, column=1, sticky="w", pady=(6, 10))
        ttk.Label(cond, text="别设太小，1~2 秒足够", foreground="#888").grid(
            row=4, column=2, sticky="w", padx=8, pady=(6, 10))

        # ---------- 高峰期模式 ----------
        ttk.Label(cond, text="高峰期模式：").grid(row=5, column=0, sticky="e", padx=(10, 4), pady=(0, 10))
        self.var_fast = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            cond, text="高速直投提交接口（推荐，不用浏览器，快很多）",
            variable=self.var_fast).grid(row=5, column=1, columnspan=2,
                                         sticky="w", pady=(0, 10))
        self.var_submit_iv = tk.StringVar(value="0.5")
        ttk.Label(cond, text="提交间隔（秒）：").grid(row=6, column=0, sticky="e", padx=(10, 4))
        ttk.Entry(cond, textvariable=self.var_submit_iv, width=8).grid(row=6, column=1, sticky="w")
        ttk.Label(cond, text="越小投得越密，0.3~0.5 比较稳妥",
                  foreground="#888").grid(row=6, column=2, sticky="w", padx=8)

        # ---------- 按钮 ----------
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)

        self.btn_start = ttk.Button(btns, text="▶  开始抢课", command=self.on_start)
        self.btn_start.pack(side="left", padx=(2, 6))

        self.btn_stop = ttk.Button(btns, text="■  停止", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)

        ttk.Button(btns, text="查看我的课表", command=self.on_timetable).pack(side="left", padx=6)
        ttk.Button(btns, text="查看可选课程", command=self.on_scan).pack(side="left", padx=6)
        ttk.Button(btns, text="打开选课页", command=self.on_open_select).pack(side="left", padx=6)
        ttk.Button(btns, text="调试：导出表格", command=self.on_dump).pack(side="left", padx=6)

        self.pbar = ttk.Progressbar(self.root, mode="indeterminate")
        self.pbar.pack(fill="x", padx=6)

        # ---------- 课程结果表 ----------
        cf = ttk.LabelFrame(
            self.root, text=" 课程列表（双击一行可把它填进「课程名称」） ")
        cf.pack(fill="both", expand=False, **pad)

        cols = ("code", "name", "teacher", "time", "cap", "state")
        self.tree = ttk.Treeview(cf, columns=cols, show="headings", height=7)
        for cid, title, w in (("code", "课程代码", 110), ("name", "课程名称", 220),
                              ("teacher", "教师", 80), ("time", "上课时间", 130),
                              ("cap", "容量", 80), ("state", "状态", 70)):
            self.tree.heading(cid, text=title)
            self.tree.column(cid, width=w, anchor="w")
        tsb = ttk.Scrollbar(cf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.tag_configure("done", foreground="#0a7")
        self.tree.tag_configure("target", background="#fff6cc")
        self.tree.bind("<Double-1>", self._on_tree_dclick)

        # ---------- 日志 ----------
        logf = ttk.LabelFrame(self.root, text=" 运行日志 ")
        logf.pack(fill="both", expand=True, **pad)

        self.log = tk.Text(logf, height=18, wrap="word", state="disabled",
                           font=("Consolas", 9), background="#1e1e1e",
                           foreground="#d4d4d4", insertbackground="#d4d4d4")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        for tag, color in (("info", "#d4d4d4"), ("ok", "#4ec9b0"),
                           ("warn", "#dcdcaa"), ("err", "#f48771"),
                           ("hl", "#569cd6")):
            self.log.tag_configure(tag, foreground=color)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status, relief="sunken",
                  anchor="w").pack(fill="x", side="bottom")

        self._on_mode_change()

    def _on_mode_change(self) -> None:
        is_pwd = self.var_mode.get() == MODE_PWD
        state = "normal" if is_pwd else "disabled"
        for w in (self.ent_pwd,):
            w.configure(state=state)
        for child in self.frm_pwd.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass
        # 浏览器模式下把输入区变灰但仍可见（避免布局跳动）
        self.frm_pwd.configure()
        self.lbl_pwdhint.configure(
            foreground="#888" if is_pwd else "#bbb")
        self.lbl_browser.grid() if not is_pwd else self.lbl_browser.grid_remove()

    def _toggle_pwd(self) -> None:
        self.ent_pwd.configure(show="" if self.var_show.get() else "●")

    # ================= 课程表格 =================
    def _fill_course_table(self, courses) -> None:
        try:
            for iid in self.tree.get_children():
                self.tree.delete(iid)
        except Exception:
            return
        self.tree_courses = list(courses)
        for i, c in enumerate(courses):
            state = "已选" if c.already else "可选"
            tags = ("done",) if c.already else ()
            self.tree.insert("", "end", iid=str(i),
                             values=(c.kch, c.kcmc, c.teacher,
                                     c.time_text, c.capacity, state),
                             tags=tags)
        if courses:
            self.put(f"  已填充课程表格：{len(courses)} 行（双击可填入课程名称）")

    def _on_tree_dclick(self, event) -> None:
        try:
            sel = self.tree.selection()
            if not sel:
                return
            vals = self.tree.item(sel[0], "values")
            if vals and len(vals) > 1:
                self.var_name.set(vals[1])
                self.put(f"已填入课程名称：{vals[1]}", "ok")
        except Exception:
            pass

    def _mark_tree(self, course, state: str) -> None:
        """标记表格里的行（成功/失败）。"""
        try:
            idx = getattr(course, "index", -1)
            if str(idx) in self.tree.get_children():
                vals = list(self.tree.item(str(idx), "values"))
                if vals:
                    vals[-1] = state
                    self.tree.item(str(idx), values=vals,
                                   tags=("done",) if state == "已选" else ())
        except Exception:
            pass

    # ================= 日志/状态 =================
    def put(self, text: str, tag: str = "info") -> None:
        self.msgq.put(("log", text, tag))

    def set_status(self, text: str) -> None:
        self.msgq.put(("status", text))

    def _drain_queue(self) -> None:
        try:
            while True:
                item = self.msgq.get_nowait()
                if item[0] == "log":
                    _, text, tag = item
                    self.log.configure(state="normal")
                    self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n", tag)
                    self.log.configure(state="disabled")
                    self.log.see("end")
                elif item[0] == "status":
                    self.status.set(item[1])
                elif item[0] == "done":
                    self._finish(item[1], item[2])
        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)

    # ================= 配置（不保存密码） =================
    def _load_conf(self) -> None:
        try:
            d = json.loads(CONF_FILE.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        self.var_user.set(d.get("user", ""))
        self.var_code.set(d.get("course_code", ""))
        self.var_teacher.set(d.get("teacher", ""))
        self.var_name.set(d.get("course_name", ""))
        self.var_type.set(d.get("course_type", "不限"))
        self.var_interval.set(str(d.get("interval", "1.5")))
        mode = d.get("mode", MODE_PWD)
        if mode in (MODE_PWD, MODE_BROWSER):
            self.var_mode.set(mode)
            self._on_mode_change()
        if d.get("user"):
            self.put(f"已载入上次的学号：{d['user']}（密码不保存）")

    def _save_conf(self) -> None:
        d = {
            "user": self.var_user.get().strip(),
            "mode": self.var_mode.get(),
            "course_code": self.var_code.get().strip(),
            "teacher": self.var_teacher.get().strip(),
            "course_name": self.var_name.get().strip(),
            "course_type": self.var_type.get(),
            "interval": self.var_interval.get().strip(),
        }
        try:
            CONF_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except OSError:
            pass

    # ================= 校验 =================
    def _need_login(self) -> bool:
        if self.var_mode.get() == MODE_PWD:
            if not self.var_user.get().strip():
                messagebox.showwarning("缺学号", "请填写学号。")
                return False
            if not self.var_pwd.get():
                messagebox.showwarning("缺密码", "请填写密码。")
                return False
        return True

    def _need_rule(self) -> bool:
        if not (self.var_code.get().strip() or self.var_teacher.get().strip()
                or self.var_name.get().strip()):
            messagebox.showwarning(
                "还差一个条件",
                "请至少填写「课程代码」「教师名称」「课程名称」中的一个，\n"
                "否则程序不知道你要抢哪门课。")
            return False
        return True

    # ================= 服务器健康检查 =================
    def _check_server(self) -> bool:
        """启动前探测教务系统是否可用（选课高峰期常常很慢）。"""
        import zf_browser

        self.put("先检查一下教务系统是否可用…")
        try:
            r = zf_browser.check_server(
                DEFAULT_BASE, timeout=8.0, attempts=3,
                log=lambda m: self.put("  " + m))
        except Exception as exc:
            self.put(f"  检查失败（不影响继续）：{exc}", "warn")
            return True

        msg = r.get("message", "")
        if r.get("success") == r.get("total"):
            self.put(f"  ✓ {msg}", "ok")
            return True
        if r.get("success", 0) > 0:
            self.put(f"  ⚠ {msg}", "warn")
            self.put("  程序会继续尝试，但可能会比较慢。", "warn")
            return True
        self.put(f"  ✗ {msg}", "err")
        self.put("")
        self.put("  教务系统现在打不开。常见原因：", "warn")
        self.put("    · 选课高峰期，服务器被挤爆（等几分钟再试）", "warn")
        self.put("    · 你在校外，需要连 VPN / 校园网", "warn")
        self.put("    · 教务系统临时维护", "warn")
        self.put("")
        self.put("  要不要继续尝试？可以点「打开选课页」再试一次。", "warn")
        return False

    # ================= 登录：账密 =================
    def _login_by_password(self) -> bool:
        """账密登录。

        实现要点：网页的密码是「动态 RSA 公钥」加密后提交的，自行实现的
        加密结果会被服务端判为密码错误（实测），但浏览器用同样的密码能成功。
        所以这里让浏览器负责填表和提交（最贴近真实用户行为），
        拿到会话后再转成 HTTP 会话，后续请求就都是快速的 HTTP 了。
        """
        import zf_browser
        import zf_client

        user = self.var_user.get().strip()
        pwd = self.var_pwd.get()

        if not self._check_server():
            self.put("")
            self.put("如果确定是高峰期，也可以直接继续试一次。", "warn")

        self.put(f"正在登录（{user}）…")
        self.put("  提示：会短暂启动一个无头浏览器来完成加密提交，属正常现象")

        def try_adopt(cookies) -> bool:
            """用 HTTP 客户端严格验证这套 cookie 是否真的有效。"""
            if not cookies:
                return False
            try:
                cli = zf_client.OpenZfClient(
                    zf_client.Config(base_url=DEFAULT_BASE, verbose=False))
                if not cli.adopt_cookies(cookies):
                    return False
                self.http = cli
                return True
            except Exception:
                return False

        try:
            res = zf_browser.login_with_password_by_browser(
                user=user,
                password=pwd,
                base_url=DEFAULT_BASE,
                headless=True,
                profile_dir=str(PROFILE_DIR),
                log=lambda m: self.put("  " + m),
            )
        except zf_browser.SeleniumUnavailable as exc:
            self.put(f"✗ 浏览器不可用：{exc}", "err")
            return False
        except Exception as exc:
            self.put(f"✗ 登录出错：{exc}", "err")
            return False

        # 复用会话时，必须再做一次严格校验；不通过就老老实实重新登录
        if res.get("ok") and res.get("reused"):
            self.put("  正在验证复用到的会话…")
            if try_adopt(res.get("cookies")):
                self.put(f"✓ {res.get('message')}", "ok")
                return True
            self.put("  复用的会话已失效，改为正常登录…", "warn")
            try:
                res = zf_browser.login_with_password_by_browser(
                    user=user,
                    password=pwd,
                    base_url=DEFAULT_BASE,
                    headless=True,
                    profile_dir=str(PROFILE_DIR),
                    log=lambda m: self.put("  " + m),
                )
            except Exception as exc:
                self.put(f"✗ 登录出错：{exc}", "err")
                return False

        if not res.get("ok"):
            msg = res.get("message") or "未知原因"
            self.put(f"✗ 登录失败：{msg}", "err")
            self.put("")
            if "验证码" in msg:
                self.put("该账号现在需要验证码 —— 请改用「浏览器登录」模式。", "warn")
            else:
                self.put("可能原因：", "warn")
                self.put("  · 密码不对（勾选「显示」检查一下有没有打错）", "warn")
                self.put("  · 账号因多次失败被临时锁定（等 5 分钟）", "warn")
                self.put("  · 账号不是学号（可能是教务号/统一认证账号）", "warn")
                self.put("")
                self.put("如果确认密码没错，请改用「浏览器登录」模式。", "warn")
            return False

        self.put(f"✓ {res.get('message') or '登录成功'}", "ok")

        # 转成 HTTP 会话（严格校验）
        if try_adopt(res.get("cookies")):
            self.put("  HTTP 会话已建立", "ok")
            return True

        self.put("  ✗ 会话校验失败：拿到 cookie 但服务端仍要求登录", "err")
        self.put("    建议改用「浏览器登录」模式。", "warn")
        return False

    # ================= 登录：浏览器 =================
    def _login_by_browser(self) -> bool:
        try:
            import zf_grab
        except ImportError as exc:
            self.put(f"✗ 加载浏览器模块失败：{exc}", "err")
            return False

        self.put("正在启动浏览器…")
        try:
            self.driver, self.browser_kind = zf_grab.build_driver("edge", False, None)
        except Exception as exc:
            self.put(f"✗ 启动浏览器失败：{exc}", "err")
            messagebox.showerror("启动浏览器失败", f"{exc}")
            return False

        self.put(f"✓ 已启动 {self.browser_kind} 浏览器", "ok")
        try:
            self.driver.get(DEFAULT_BASE + "/xtgl/login_slogin.html")
        except Exception as exc:
            self.put(f"打开登录页失败：{exc}", "err")
            return False

        self.put("")
        self.put("═══════ 请在弹出的浏览器窗口里登录 ═══════", "hl")
        self.put("  · 可以输账号密码，也可以微信扫码")
        self.put("  · 登录完就不用管了，程序会自动检测")
        self.put("═══════════════════════════════════════", "hl")
        self.put("")
        self.set_status("等待你在浏览器里登录…")

        if not self._wait_login():
            return False

        # 接管会话
        try:
            import zf_client
            cfg = zf_client.Config(base_url=DEFAULT_BASE, verbose=False)
            client = zf_client.OpenZfClient(cfg)
            cookies = self.driver.get_cookies() or []
            if not client.adopt_cookies(cookies):
                self.put("✗ 浏览器会话验证失败", "err")
                return False
            self.http = client
        except Exception as exc:
            self.put(f"接管会话失败：{exc}", "warn")
            self.put("（不影响使用：页面操作仍然走浏览器）", "warn")

        return True

    def _wait_login(self, timeout: float = 300.0) -> bool:
        deadline = time.time() + timeout
        last_note = 0.0
        while time.time() < deadline:
            if self.stop_flag.is_set():
                return False
            try:
                url = self.driver.current_url or ""
                has_form = self.driver.execute_script(
                    "return !!document.querySelector('form[action*=\"login_slogin\"]');")
            except Exception as exc:
                self.put(f"浏览器连接异常：{exc}", "err")
                return False

            if (not has_form) and ("login_slogin" not in url):
                self.put(f"✓ 检测到登录成功（当前页：{url[:70]}）", "ok")
                return True

            now = time.time()
            if now - last_note > 20:
                last_note = now
                self.put(f"…仍在等待登录（剩余 {int(deadline - now)}s）")
                try:
                    tip = self.driver.execute_script(
                        "var e=document.getElementById('tips');return e?e.innerText.trim():'';")
                    if tip:
                        self.put(f"  页面提示：{tip}", "warn")
                except Exception:
                    pass
            time.sleep(1.0)

        self.put("✗ 等待登录超时", "err")
        return False

    # ================= 打开选课页 =================
    def _goto_select(self) -> bool:
        # 账密模式：没有浏览器，直接用 HTTP 探测
        if self.driver is None:
            return self._goto_select_http()

        try:
            cur = self.driver.current_url or ""
        except Exception:
            cur = ""
        if re.search(r"xsxk|选课", cur, re.I):
            self.put(f"已经在选课页面：{cur[:70]}")
            return True

        for path in SELECT_PATHS:
            self.put(f"尝试打开选课页：{path}")
            try:
                self.driver.get(DEFAULT_BASE + path)
            except Exception as exc:
                self.put(f"  打开失败：{exc}", "warn")
                continue
            time.sleep(2.0)
            try:
                now_url = self.driver.current_url or ""
                body = self.driver.execute_script(
                    "return document.body ? document.body.innerText.slice(0,3000) : '';") or ""
            except Exception:
                now_url, body = "", ""

            if "login_slogin" in now_url:
                self.put("  会话失效，需要重新登录", "warn")
                return False
            if any(k in body for k in ("课程代码", "教学班", "选课", "课程名称")):
                self.put(f"  ✓ 选课页已打开：{now_url[:70]}", "ok")
                return True
            self.put("  这个地址不是选课页，继续试下一个")

        self.put("")
        self.put("✗ 没能自动打开选课页。", "err")
        self.put("  请你在浏览器里手动点到「选课」页面，")
        self.put("  然后回来点「查看可选课程」或「开始抢课」，程序会在当前页面找课。")
        return False

    def _goto_select_http(self) -> bool:
        """账密模式：用 HTTP 找选课页面并加载到浏览器里。"""
        if self.http is None:
            self.put("✗ 没有有效会话，请先登录", "err")
            return False

        self.put("正在查找选课页面…")
        try:
            found = self.http.find_select_page()
        except Exception as exc:
            self.put(f"  探测出错：{exc}", "warn")
            found = None

        url = None
        if found:
            path = found[0]
            url = DEFAULT_BASE + path if not path.startswith("http") else path
            self.put(f"  ✓ 通过接口定位到选课页：{path}", "ok")
        else:
            url = DEFAULT_BASE + SELECT_PATHS[0]
            self.put(f"  没定位到，先试默认地址：{SELECT_PATHS[0]}", "warn")

        # 用浏览器打开它（后续读表格/点按钮都靠浏览器）
        try:
            import zf_grab
            self.put("启动浏览器以操作选课页面…")
            self.driver, self.browser_kind = zf_grab.build_driver("edge", False, None)
            self.grabber = zf_grab.BrowserGrab(
                self.driver, DEFAULT_BASE, log=lambda m: self.put(m))

            # 把 HTTP 会话的 cookie 同步给浏览器
            self._sync_cookies_to_browser()

            self.driver.get(url)
            time.sleep(2.0)
            now = self.driver.current_url or ""
            if "login_slogin" in now:
                self.put("  ✗ 浏览器跳回登录页：cookie 没能同步。", "err")
                self.put("    请改用「浏览器登录」模式。", "warn")
                return False
            self.put(f"  ✓ 选课页已在浏览器中打开：{now[:70]}", "ok")
            return True
        except Exception as exc:
            self.put(f"✗ 打开选课页失败：{exc}", "err")
            return False

    def _sync_cookies_to_browser(self) -> None:
        """把 HTTP 客户端的 cookie 灌进浏览器，实现免二次登录。"""
        if self.http is None or self.driver is None:
            return
        try:
            self.driver.get(DEFAULT_BASE + "/")
        except Exception:
            pass
        n = 0
        for ck in self.http.jar:
            try:
                self.driver.add_cookie({
                    "name": ck.name,
                    "value": ck.value or "",
                    "path": ck.path or "/",
                    "secure": bool(ck.secure),
                })
                n += 1
            except Exception:
                continue
        self.put(f"  已同步 {n} 个 cookie 到浏览器")

    # ================= 统一入口 =================
    def _ensure_ready(self, need_rule: bool = False) -> bool:
        """登录 + 打开选课页 + 建好 grabber。"""
        import zf_grab

        if self.grabber is not None:
            return True
        if self.var_mode.get() == MODE_PWD and self.http is None:
            if not self._login_by_password():
                return False

        if self.driver is not None:
            # 浏览器已就绪（浏览器登录模式，或之前为点击开过）
            if not self._goto_select():
                return False
            self.grabber = zf_grab.BrowserGrab(
                self.driver, DEFAULT_BASE, log=lambda m: self.put(m))
            return True

        # 账密模式：用 HTTP 快速读取
        if self.http is None:
            self.put("✗ 没有有效会话，请先登录", "err")
            return False
        self.grabber = zf_grab.HttpGrab(self.http, log=lambda m: self.put(m))
        self.put("已用 HTTP 会话接管，读取速度更快")
        return True

    def _ensure_browser(self) -> bool:
        """需要真正点「选课」时才启动浏览器。"""
        if self.driver is not None:
            return True
        import zf_grab

        self.put("启动浏览器以执行选课操作…")
        try:
            self.driver, self.browser_kind = zf_grab.build_driver("edge", False, None)
        except Exception as exc:
            self.put(f"✗ 启动浏览器失败：{exc}", "err")
            return False

        # 把 HTTP 会话的 cookie 同步进浏览器
        self._sync_cookies_to_browser()

        url = DEFAULT_BASE + SELECT_PATHS[0]
        self.put(f"打开选课页：{SELECT_PATHS[0]}")
        import zf_browser as _zb
        ok = _zb.goto(self.driver, url, attempts=3,
                      log=lambda m: self.put("  " + m))
        if not ok:
            # 退而求其次：直接试其它候选地址
            for p in SELECT_PATHS[1:]:
                self.put(f"  换一个地址试试：{p}")
                if _zb.goto(self.driver, DEFAULT_BASE + p, attempts=2,
                            log=lambda m: self.put("  " + m)):
                    ok = True
                    break
        if not ok:
            self.put("✗ 选课页打不开（服务器响应太慢）", "err")
            self.put("  可以稍后重试，或点「打开选课页」手动进入。", "warn")
            return False

        try:
            now = self.driver.current_url or ""
        except Exception:
            now = ""
        if "login_slogin" in now:
            self.put("✗ 浏览器跳回了登录页（cookie 未生效）", "err")
            self.put("  请改用「浏览器登录」模式。", "warn")
            return False
        self.put(f"  ✓ 选课页已打开：{now[:70]}", "ok")
        return True

    # ================= 动作 =================
    def on_open_select(self) -> None:
        def work():
            if not self._need_login():
                return
            self._ensure_ready()

        self._start_worker(work)

    def on_scan(self) -> None:
        def work():
            if not self._need_login():
                return
            if self._ensure_ready():
                self._scan_once(verbose=True)

        self._start_worker(work)

    # ================= 查看我的课表 =================
    def on_timetable(self) -> None:
        def work():
            if not self._need_login():
                return
            if self._ensure_ready():
                self._show_timetable()

        self._start_worker(work)

    WEEKDAYS = {1: "周一", 2: "周二", 3: "周三", 4: "周四",
                5: "周五", 6: "周六", 7: "周日"}

    def _show_timetable(self) -> None:
        """拉取并打印课表。"""
        if self.http is None:
            self.put("✗ 需要先登录才能查课表", "err")
            return

        self.put("正在获取课表…")
        data = self._fetch_timetable()
        if data is None:
            return

        stu = data.get("xsxx") or {}
        kb = data.get("kbList") or []
        self.put("")
        self.put("═" * 58, "hl")
        self.put(f"  {stu.get('XM', '?')}   学号 {stu.get('XH', '?')}", "hl")
        self.put(f"  班级 {stu.get('BJMC', '?')}   专业 {stu.get('ZYMC', '?')}", "hl")
        xn, xq = stu.get("XNMC", "?"), stu.get("XQM", "?")
        self.put(f"  学年 {xn}   第 {xq} 学期", "hl")
        self.put("═" * 58, "hl")
        self.put("")

        if not kb:
            self.put("本学期还没有课程记录。", "warn")
            return

        groups: dict = {}
        for it in kb:
            try:
                d = int(it.get("xqj") or 0)
            except Exception:
                d = 0
            groups.setdefault(d, []).append(it)

        for d in sorted(groups):
            label = self.WEEKDAYS.get(d, f"星期{d}")
            self.put(f"【{label}】", "ok")
            for it in groups[d]:
                nm = it.get("kcmc", "?")
                te = it.get("xm") or it.get("jsxm") or ""
                jcs = it.get("jcs", "")
                zcd = it.get("zcd", "")
                room = it.get("cdmc", "")
                line = f"   {jcs:<8} {nm}"
                if te:
                    line += f"   {te}"
                self.put(line)
                det = [x for x in (zcd, room) if x]
                if det:
                    self.put(f"            {'  '.join(det)}")
            self.put("")

        self.put(f"共 {len(kb)} 条课程记录", "ok")

    def _fetch_timetable(self):
        """获取课表 JSON。

        接口不带 xnm/xqm 会返回 null，必须带上学年学期。
        学年从课表页的下拉框取最新的，学期按顺序试，谁先返回数据就用谁。
        """
        if self.http is None:
            return None

        # 1) 拿可选学年（倒序，最新在前）
        years = []
        try:
            html = self.http.fetch("/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N2151")
            block = re.search(r'name="xnm"[\s\S]{0,3000}?</select>', html)
            if block:
                years = [v for v in re.findall(r'<option[^>]*value="(\d{4})"',
                                               block.group(0))]
        except Exception:
            pass
        if not years:
            years = [str(time.localtime().tm_year)]

        xqm_list = ["1", "3", "12", "16"]

        # 2) 依次尝试，谁先有数据用谁
        for xnm in years[:4]:
            for xqm in xqm_list:
                path = (f"/kbcx/xskbcx_cxXsKb.html?gnmkdm=N2151"
                        f"&xnm={xnm}&xqm={xqm}")
                try:
                    _, raw, _ = self.http._request(path, ajax=True)
                except Exception:
                    continue
                raw = (raw or "").strip()
                if not raw or raw == "null":
                    continue
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if isinstance(d, dict) and d.get("kbList"):
                    return d

        # 3) 兜底：任何返回了对象的结果
        try:
            _, raw, _ = self.http._request(
                f"/kbcx/xskbcx_cxXsKb.html?gnmkdm=N2151&xnm={years[0]}&xqm=1",
                ajax=True)
            if raw and raw.strip() not in ("", "null"):
                d = json.loads(raw)
                if isinstance(d, dict):
                    return d
        except Exception:
            pass

        self.put("⚠ 没取到课表数据。", "warn")
        self.put("  可能原因：当前学期没有课 / 接口需要额外参数。", "warn")
        return None

    def _not_open_notice(self) -> str:
        """检查当前页面是否提示「不在选课期」。"""
        html = ""
        try:
            if hasattr(self.grabber, "raw_html"):
                html = self.grabber.raw_html() or ""
            elif self.driver is not None:
                html = self.driver.execute_script(
                    "return document.body ? document.body.innerText : '';") or ""
        except Exception:
            html = ""
        text = re.sub(r"<[^>]+>", " ", html or "")
        for mark in NOT_OPEN_MARKS:
            if mark in text:
                i = text.find(mark)
                return re.sub(r"\s+", " ", text[max(0, i - 25):i + 60]).strip()
        return ""

    def _scan_once(self, verbose: bool = False):
        """读取可选课程并展示（JSON 接口 + 浏览器 DOM 双通道）。"""
        import zf_course

        self.put("正在读取可选课程…")

        # 造 CourseSource：优先用已有的 http/driver
        src = zf_course.CourseSource(
            http_client=self.http, driver=self.driver,
            base_url=DEFAULT_BASE, log=lambda m: self.put("  " + m))

        courses, how = src.fetch(
            dump_path=str(TABLE_DUMP) if verbose else None)

        if not courses:
            notice = self._not_open_notice()
            if src.last_ok:
                # 页面读到了，只是还没有课程数据
                if notice:
                    self.put(f"  ⚠ 选课页提示：{notice}", "warn")
                self.put("  页面可正常访问，但当前没有课程数据。", "warn")
                self.put("  说明选课还没开始 —— 等开放后点「开始抢课」即可。", "warn")
            elif notice:
                self.put(f"  ⚠ 选课页提示：{notice}", "warn")
                self.put("    现在不在选课开放时间内，所以没有课程数据。", "warn")
            else:
                self.put("  ✗ 读不到课程数据（服务器可能正忙）。", "warn")
                self.put("    点「调试：导出表格」可导出结构供分析。")
            return [], None

        self.put(f"  ✓ 来源：{how}，共 {len(courses)} 门课", "ok")
        self.put("")

        matched = zf_course.match_courses(
            courses,
            code=self.var_code.get().strip(),
            name=self.var_name.get().strip(),
            teacher=self.var_teacher.get().strip(),
            course_type=self.var_type.get(),
        )

        # 填充表格
        self._fill_course_table(matched if matched else courses)

        if matched:
            self.put(f"  按条件命中 {len(matched)} 门：", "ok")
        else:
            self.put(f"  （未命中筛选条件，下面列出全部 {len(courses)} 门）")
        for c in (matched or courses)[:60]:
            flag = "（已选）" if c.already else ""
            self.put(f"    · {c.text()[:110]}{flag}")
        if len(matched or courses) > 60:
            self.put(f"    …还有 {len(matched or courses) - 60} 门，见上方表格")
        return matched, courses

    def on_dump(self) -> None:
        def work():
            if not self._need_login():
                return
            if not self._ensure_ready():
                return
            self.put("导出当前页面的表格结构…")
            try:
                info = self.grabber.read_table(dump_path=str(TABLE_DUMP))
            except Exception as exc:
                self.put(f"✗ 导出失败：{exc}", "err")
                return
            self.put(f"✓ 已导出到：{TABLE_DUMP}", "ok")
            self.put(f"  表格找到：{info.found}   行数：{info.row_count}")
            self.put(f"  表头：{info.headers}")
            for r in info.sample_rows[:5]:
                self.put(f"  行样本：{r}")
            self.put("")
            self.put("把 table_debug.json 发给我，我就能精确适配你学校的表格。")

        self._start_worker(work)

    def on_start(self) -> None:
        if not self._need_login():
            return
        if not self._need_rule():
            return
        try:
            interval = max(0.6, float(self.var_interval.get().strip() or "1.5"))
        except ValueError:
            messagebox.showwarning("间隔不对", "轮询间隔要填数字，比如 1.5")
            return
        self._save_conf()

        def work():
            if self._ensure_ready():
                self._grab_loop(interval)

        self._start_worker(work)

    def _grab_loop_fast(self, interval: float) -> bool:
        """高速直投模式（只用 HTTP）。

        返回 True 表示"这一轮抢课已经由我处理完了"；返回 False 表示
        这条路走不通，让调用方退回浏览器方式。
        """
        import zf_peak

        if self.http is None or not getattr(self.http, "logged_in", False):
            self.put("高速模式需要用「账号密码」方式登录", "warn")
            return False

        kw = " ".join(x for x in (
            self.var_code.get().strip(),
            self.var_name.get().strip(),
            self.var_teacher.get().strip(),
            "" if self.var_type.get() in ("", "不限") else self.var_type.get(),
        ) if x)
        if not kw:
            self.put("高速模式需要填课程代码或课程名称", "warn")
            return False

        try:
            submit_iv = max(0.1, float(self.var_submit_iv.get().strip() or "0.5"))
        except ValueError:
            submit_iv = 0.5

        self.put("", "info")
        self.put("【高速直投模式】先把目标课定位好，再对着提交接口高频投。", "ok")
        self.put("高峰期用法：提前几分钟点开始；开放前显示「待命、提交 0 次」"
                 "是正常的，它在等正确时机。", "info")
        self.put(f"提交间隔 {submit_iv:.2f} 秒", "info")

        g = zf_peak.PeakGrabber(self.http, log=lambda m: self.put("  " + m))

        def on_status(info: dict) -> None:
            bits = ["阶段：%s" % info.get("phase", "-"),
                    "提交 %d 次" % info.get("submits", 0)]
            if info.get("gateway"):
                bits.append("网关重投 %d" % info["gateway"])
            if info.get("unknown"):
                bits.append("结果不明 %d" % info["unknown"])
            if info.get("relogins"):
                bits.append("自动重登 %d" % info["relogins"])
            if info.get("last"):
                bits.append(str(info["last"])[:46])
            self.set_status("　|　".join(bits))

        def on_courses(courses) -> None:
            try:
                self._fill_course_table(courses)
            except Exception:
                pass

        runner = zf_peak.PeakRunner(
            g, [kw], interval=interval, submit_interval=submit_iv,
            on_event=lambda m: self.put(m),
            on_courses=on_courses,
            on_hit=lambda r: self.put("★ 抢到了：%s —— %s"
                                      % (r.course, r.message), "ok"),
            on_status=on_status)
        runner.start()

        # 界面主循环：等它跑完，或者用户点了停止
        while runner.is_alive():
            if self.stop_flag.is_set():
                if runner.is_alive():
                    runner.stop()
            time.sleep(0.25)
        runner.join(5)

        self.put("")
        if runner.wins:
            self.put(f"共抢到 {len(runner.wins)} 门课：", "ok")
            for r in runner.wins:
                self.put(f"  ✅ {r.course}", "ok")
            self.put("请点「查看我的课表」确认结果，必要时手动退课。", "warn")
            self.msgq.put(("done", True, f"成功 {len(runner.wins)} 门"))
        elif self.stop_flag.is_set():
            self.put("已手动停止。", "warn")
            self.msgq.put(("done", False, "已停止"))
        else:
            self.put("结束了，但没有抢到。可以看看上面的日志了解原因。", "warn")
            self.msgq.put(("done", False, "未抢到"))
        return True

    def _grab_loop(self, interval: float) -> None:
        """抢课主循环。

        两种打法：
          · 高速直投（推荐）：只用 HTTP，先读一次页面拿到 jxb_id，
            之后直接对着提交接口高频投 —— 高峰期那几分钟快的就是这一点
          · 浏览器点击：走学校自己的前端逻辑，最保险但慢（要点按钮、等弹窗）
        """
        import zf_course

        if self.var_fast.get() and self.http is not None:
            if self._grab_loop_fast(interval):
                return
            self.put("高速直投不可用，改用浏览器方式…", "warn")

        # 抢课要点击学校页面上的按钮，所以必须有浏览器
        if not self._ensure_browser():
            self.put("✗ 无法启动浏览器，抢课中止", "err")
            return

        src = zf_course.CourseSource(
            http_client=self.http, driver=self.driver,
            base_url=DEFAULT_BASE, log=lambda m: self.put("  " + m))
        engine = zf_course.GrabEngine(
            src, self.driver, log=lambda m: self.put("  " + m),
            stop_flag=self.stop_flag)

        def on_event(tag: str, text: str) -> None:
            self.put(text, tag)
            m = re.search(r"第 (\d+) 轮", text)
            if m:
                self.set_status(f"第 {m.group(1)} 轮：抢课中…")

        def on_courses(courses) -> None:
            """抢课过程中同步刷新课程表格（界面要求：能准确显示课表）。"""
            try:
                self._fill_course_table(courses)
            except Exception:
                pass

        def reload_page() -> bool:
            """重新加载选课页（应对"开放前打开的页面"变成死页面）。"""
            if self.driver is None:
                return False
            import zf_browser as _zb
            for p in SELECT_PATHS:
                if _zb.goto(self.driver, DEFAULT_BASE + p, attempts=1,
                            wait_after=1.0, log=lambda m: None):
                    try:
                        cur = self.driver.current_url or ""
                    except Exception:
                        cur = ""
                    if "login_slogin" not in cur:
                        return True
            return False

        runner = zf_course.GrabRunner(
            src, engine,
            code=self.var_code.get().strip(),
            name=self.var_name.get().strip(),
            teacher=self.var_teacher.get().strip(),
            course_type=self.var_type.get(),
            interval=interval,
            on_event=on_event,
            stop_flag=self.stop_flag,
            page_loader=reload_page,
            on_courses=on_courses,
        )

        res = runner.run()

        # 收尾反馈
        self.put("")
        success = res.get("success") or []
        reason = res.get("reason") or ""
        if reason == "stopped":
            self.put("已手动停止。", "warn")
            self.msgq.put(("done", False, "已停止"))
        elif success:
            self.put(f"共抢到 {len(success)} 门课：", "ok")
            for s in dict.fromkeys(success):
                self.put(f"  ✅ {s[:100]}", "ok")
            self.put("请点「查看我的课表」确认结果，必要时手动退课。", "warn")
            self.msgq.put(("done", True, f"成功 {len(success)} 门"))
        elif reason == "reached_max_rounds":
            self.put("已达设定的轮数上限，停止。", "warn")
            self.msgq.put(("done", False, "已达轮数上限"))
        else:
            self.put("本次结束，没有抢到。", "err")
            notes = res.get("failed") or []
            if notes:
                self.put("最近的服务端反馈：", "warn")
                for f in list(dict.fromkeys(notes))[-5:]:
                    self.put(f"  · {f}", "warn")
            else:
                self.put("可能原因：课程已满 / 不满足条件 / 不在选课时间内。", "warn")
                self.put("（可以再点一次「开始抢课」继续）", "warn")
            self.msgq.put(("done", False, "未抢到"))

    # ================= 线程 =================
    def _start_worker(self, fn) -> None:
        if self.running:
            return
        self.running = True
        self.stop_flag.clear()
        self.btn_stop.configure(state="normal")
        self.btn_start.configure(state="disabled")
        self.pbar.start(12)
        self.set_status("运行中…")

        def wrapper():
            try:
                fn()
            except Exception as exc:
                self.put(f"✗ 发生未预期的错误：{exc}", "err")
                import traceback
                self.put(traceback.format_exc()[-800:], "err")
            finally:
                self.msgq.put(("done", True, "完成"))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def _finish(self, success: bool, summary: str) -> None:
        self.running = False
        self.pbar.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.set_status(summary)
        self.put(f"———— {summary} ————")

    def on_stop(self) -> None:
        self.stop_flag.set()
        self.set_status("正在停止…")
        self.put("收到停止指令，正在结束当前轮…", "warn")

    def _on_close(self) -> None:
        self.stop_flag.set()
        self._save_conf()
        if self.driver is not None:
            try:
                import zf_browser
                zf_browser.quit_driver(self.driver)
            except Exception:
                pass
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:
        pass
    app = GrabApp(root)

    app.put("欢迎使用抢课助手。")
    app.put("")
    app.put("怎么用：")
    app.put("  1. 选登录方式（默认「账号密码登录」），填学号密码")
    app.put("  2. 填「课程代码」或「教师名称」（至少一个）")
    app.put("  3. 点「▶ 开始抢课」")
    app.put("  4. 成功 / 失败都会在这里告诉你")
    app.put("")
    app.put("提示：第一次用建议先点「查看可选课程」，确认能读到课程列表。")
    app.put("      如果账密登录被拒，改用「浏览器登录」模式。")
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
