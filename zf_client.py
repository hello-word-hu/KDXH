#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
正方教务系统 (ZFSoft jwglxt) 客户端 —— 登录 + 选课

针对: http://jwxt.cumtxh.cn/jwglxt/  (中国矿业大学徐海学院教务系统, 正方 V-9.0)

登录机制 (已对真实服务器验证):
    1. GET  /xtgl/login_slogin.html            -> 取得会话 Cookie 与 csrftoken
    2. GET  /xtgl/login_getPublicKey.html      -> 取得 RSA modulus / exponent (base64)
    3. RSA 加密密码: base64(pow(int(password), e, n))
    4. POST /xtgl/login_slogin.html            -> 表单 yhm / mm(密文) / csrftoken ...
    5. 成功后会 302 跳转; 失败则回显"用户名或密码不正确"

仅使用 Python 标准库, 无需 pip 安装任何依赖。
"""

from __future__ import annotations

import base64
import gzip
import http.cookiejar
import json
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
__all__ = ["Config", "OpenZfClient", "LoginError", "StopError", "ZfError", "rsa_encrypt_b64"]

READ_TIMEOUT = 30

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 选课成功后页面上常见的字样
SUCCESS_MARKS = ("选课成功", "选定成功", "success")
# 明确表示"这次没抢到, 稍后再试"的字样
RETRY_MARKS = (
    "已满", "人数已满", "容量已满", "余量不足", "超出容量",
    "教学班人数已满", "该教学班已满", "不能选择", "已选人数已满",
    "课容量已满", "已选满", "剩余容量不足",
)
# 明确表示"无法再抢, 应当停止"的字样
FATAL_MARKS = (
    "已选过该课程", "已经选过", "重复选课", "已选该课程",
    "不在选课时间内", "选课时间已过", "未到选课时间", "未开放",
    "无选课权限", "不允许选课", "登录超时", "会话已过期", "未登录",
)


class ZfError(Exception):
    """业务/网络层可重试错误。"""


class LoginError(ZfError):
    """登录失败, 或会话掉线需要重新登录。"""


class StopError(ZfError):
    """服务端明确拒绝, 重试也没有意义 —— 应停止对该课程的抢课。

    (例如: 不在选课时间内 / 没有选课权限 / 课程与已有课程冲突且系统不允许)
    """


# --------------------------------------------------------------------------
# RSA (PKCS#1 v1.5 无需填充处理: 正方前端使用 rsa.js 的 RSAKey.encrypt)
# --------------------------------------------------------------------------
def rsa_encrypt_b64(modulus_b64: str, exponent_b64: str, text: str) -> str:
    """复刻正方前端: rsaKey.setPublic(b64tohex(modulus), b64tohex(exponent))
    -> hex2b64(rsaKey.encrypt(plaintext))

    注意: 正方使用的 rsa.js 在 PKCS#1 填充后直接做大数幂运算, 服务端按
    "无填充裸 RSA" 解密, 因此这里必须用裸 pow(), 不能加 PKCS#1 填充。
    """
    n = int.from_bytes(base64.b64decode(modulus_b64), "big")
    e = int.from_bytes(base64.b64decode(exponent_b64), "big")
    raw = text.encode("utf-8")
    m = int.from_bytes(raw, "big")
    if m >= n:
        raise ZfError("密码过长, 超出 RSA 密钥长度, 无法加密")
    c = pow(m, e, n)
    klen = (n.bit_length() + 7) // 8
    return base64.b64encode(c.to_bytes(klen, "big")).decode("ascii")


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass
class Config:
    base_url: str = "http://jwxt.cumtxh.cn/jwglxt"
    username: str = ""
    password: str = ""

    # 选课模块
    #   xsxk  = 学生选课中心 (第一轮/第二轮选课)
    #   xszx  = 学生在线选课 (部分学校)
    #   retake= 重修/补修报名
    modules: Tuple[str, ...] = ("xsxk", "xszx")
    gnmkdm: str = "N253508"
    retake_gnmkdm: str = "N253512"

    # 轮询
    interval: float = 0.35
    jitter: float = 0.15
    max_attempts: int = 0          # 0 = 无限
    max_minutes: float = 0.0       # 0 = 不限制

    # 网络
    timeout: float = READ_TIMEOUT
    insecure: bool = True
    verbose: bool = True

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class OpenZfClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._ctx = ssl.create_default_context()
        if cfg.insecure:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=self._ctx),
        )
        self.logged_in = False
        self._public_key: Optional[Tuple[str, str]] = None
        # 探测成功后固定下来的选课端点与传参方式
        self._select_endpoint: Optional[str] = None
        self._select_style: str = "post"

    # ---------------- 基础 HTTP ----------------
    def log(self, msg: str, level: str = "info") -> None:
        if not self.cfg.verbose and level == "debug":
            return
        stamp = time.strftime("%H:%M:%S")
        prefix = {"info": "*", "ok": "+", "warn": "!", "err": "x", "debug": "."}.get(level, "*")
        try:
            print(f"[{stamp}] {prefix} {msg}", flush=True)
        except UnicodeEncodeError:
            print(f"[{stamp}] {prefix} {msg.encode('utf-8', 'replace')}", flush=True)

    @staticmethod
    def _decode(raw: bytes, headers) -> str:
        """正方部分页面返回 GBK, 部分返回 UTF-8, 这里做兼容解码。"""
        charset = ""
        try:
            ctype = headers.get("Content-Type", "") or ""
            m = re.search(r"charset=([\w-]+)", ctype, re.I)
            if m:
                charset = m.group(1)
        except Exception:
            pass
        for enc in ([charset] if charset else []) + ["utf-8", "gbk", "gb18030", "latin-1"]:
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", "replace")

    def _request(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        referer: Optional[str] = None,
        method: Optional[str] = None,
        ajax: bool = False,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str, int]:
        """返回 (最终URL, 正文, 状态码)。"""
        if not url.startswith("http"):
            url = self.cfg.base_url + ("" if url.startswith("/") else "/") + url
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")

        headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/javascript, */*; q=0.01" if ajax
                      else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Referer": referer or (self.cfg.base_url + "/xtgl/index_initMenu.html"),
        }
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        req = urllib.request.Request(url, data=body, headers=headers,
                                     method=method or ("POST" if body is not None else "GET"))
        try:
            resp = self.opener.open(req, timeout=timeout or self.cfg.timeout)
        except urllib.error.HTTPError as exc:
            # 正方失败时也常返回 200/500 + 文本, 统一按正文处理
            raw = exc.read()
            enc = exc.headers.get("Content-Encoding", "") if exc.headers else ""
            raw = self._unzip(raw, enc)
            return exc.geturl(), self._decode(raw, exc.headers or {}), exc.code
        except urllib.error.URLError as exc:
            raise ZfError(f"网络错误: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise ZfError(f"连接异常: {exc}") from exc

        raw = resp.read()
        raw = self._unzip(raw, resp.headers.get("Content-Encoding", ""))
        return resp.geturl(), self._decode(raw, resp.headers), resp.status

    @staticmethod
    def _unzip(raw: bytes, encoding: str) -> bytes:
        enc = (encoding or "").lower()
        try:
            if "gzip" in enc:
                return gzip.decompress(raw)
            if "deflate" in enc:
                try:
                    return zlib.decompress(raw)
                except zlib.error:
                    return zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            return raw
        return raw

    # ---------------- 登录 ----------------
    def _get_csrf(self, page: str) -> str:
        m = (re.search(r'name=["\']csrftoken["\'][^>]*?value=["\']([^"\']+)', page, re.I)
             or re.search(r'value=["\']([^"\']+)["\'][^>]*?name=["\']csrftoken["\']', page, re.I)
             or re.search(r'id=["\']csrftoken["\'][^>]*?value=["\']([^"\']+)', page, re.I))
        if not m:
            raise ZfError("登录页未找到 csrftoken, 页面结构可能已变化")
        # 页面里是 "uuid,32位无横线" 两段式, 表单直接提交整串
        return m.group(1)

    def load_public_key(self) -> Tuple[str, str]:
        if self._public_key:
            return self._public_key
        url = f"{self.cfg.base_url}/xtgl/login_getPublicKey.html?time={int(time.time() * 1000)}"
        _, text, _ = self._request(url, referer=self.cfg.base_url + "/xtgl/login_slogin.html",
                                   ajax=True)
        try:
            data = json.loads(text)
            key = (data["modulus"], data["exponent"])
        except Exception as exc:
            raise ZfError(f"公钥接口返回异常: {text[:200]!r}") from exc
        self._public_key = key
        return key

    def login(self, max_retries: int = 4) -> str:
        if self.logged_in:
            return ""
        cfg = self.cfg
        last_err: Optional[Exception] = None

        for attempt in range(1, max_retries + 1):
            try:
                return self._login_once()
            except LoginError:
                raise
            except ZfError as exc:
                last_err = exc
                if attempt >= max_retries:
                    break
                wait = min(8.0, 1.5 * attempt)
                self.log(f"登录尝试 {attempt} 失败 ({exc}), {wait:.1f}s 后重试", "warn")
                time.sleep(wait)
        raise LoginError(f"登录失败: {last_err}")

    def _login_once(self) -> str:
        cfg = self.cfg
        login_url = cfg.base_url + "/xtgl/login_slogin.html"

        # 1) 建立会话 + csrftoken
        _, page, _ = self._request(login_url, referer=cfg.base_url + "/")
        csrf = self._get_csrf(page)

        # 2) 公钥 + 加密密码
        modulus, exponent = self.load_public_key()
        enc_pwd = rsa_encrypt_b64(modulus, exponent, cfg.password)

        # 3) 提交
        form = {
            "csrftoken": csrf,
            "language": "zh_CN",
            "yhm": cfg.username,
            "mm": enc_pwd,
            "mmsfjm": "1",
        }
        url = f"{login_url}?time={int(time.time() * 1000)}"
        final, body, _ = self._request(url, data=form, referer=login_url)

        if self._login_failed(body, final):
            reason = self._extract_error(body) or "用户名或密码不正确"
            raise LoginError(f"登录被拒绝: {reason}")

        self.logged_in = True
        name = self._extract_student_name(body)
        self.log(f"登录成功{'：' + name if name else ''}", "ok")
        return name

    _LOGIN_FORM_RE = re.compile(r"<form[^>]*login_slogin\.html[^>]*>", re.I)
    # 登录失败时页面会回显这些提示
    _LOGIN_ERRORS = ("用户名或密码不正确", "密码不正确", "用户名不存在", "用户不存在",
                     "验证码错误", "登录失败", "密码错误")

    @classmethod
    def _login_failed(cls, body: str, final_url: str) -> bool:
        if any(bad in body for bad in cls._LOGIN_ERRORS):
            return True
        if cls._LOGIN_FORM_RE.search(body):
            return True
        return "login_slogin" in final_url

    @staticmethod
    def _extract_error(body: str) -> str:
        m = re.search(r'id=["\']tips["\'][^>]*>(.*?)</p>', body, re.S)
        if m:
            txt = re.sub(r"<[^>]+>", "", m.group(1))
            txt = re.sub(r"\s+", " ", txt).strip()
            if txt:
                return txt
        return ""

    def _extract_student_name(self, body: str) -> str:
        for pat in (r"欢迎[^,，<]{0,4}[,，]?\s*([\u4e00-\u9fa5]{2,4})",
                    r"当前用户[：:]\s*([\u4e00-\u9fa5]{2,4})",
                    r"<span[^>]*class=\"[^\"]*user[^\"]*\"[^>]*>([\u4e00-\u9fa5]{2,4})<"):
            m = re.search(pat, body)
            if m:
                return m.group(1)
        return ""

    def ensure_login(self) -> None:
        if self.logged_in:
            return
        self.login()

    # ---------------- 复用外部会话 (浏览器登录) ----------------
    def adopt_cookies(self, cookies) -> bool:
        """把浏览器里拿到的 cookie 装进来, 并验证会话是否有效。

        浏览器模式下的入口: 你自己在浏览器里登录, 脚本只接管会话,
        完全不接触密码。
        """
        self.jar.clear()
        for c in cookies:
            name = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
            if not name:
                continue
            value = c.get("value") if isinstance(c, dict) else getattr(c, "value", "")
            if isinstance(c, dict):
                domain = c.get("domain") or ""
                path = c.get("path") or "/"
                secure = bool(c.get("secure", False))
                expiry = c.get("expiry")
            else:
                domain = getattr(c, "domain", "") or ""
                path = getattr(c, "path", "/") or "/"
                secure = bool(getattr(c, "secure", False))
                expiry = getattr(c, "expiry", None)

            try:
                cookie = http.cookiejar.Cookie(
                    version=0, name=name, value=value or "",
                    port=None, port_specified=False,
                    domain=domain, domain_specified=bool(domain),
                    domain_initial_dot=str(domain).startswith("."),
                    path=path, path_specified=bool(path),
                    secure=secure,
                    expires=int(expiry) if expiry else None,
                    discard=False, comment=None, comment_url=None,
                    rest={}, rfc2109=False,
                )
                self.jar.set_cookie(cookie)
            except Exception:
                continue

        if not any(c.name == "JSESSIONID" for c in self.jar):
            self.log(f"警告: 没有拿到 JSESSIONID, 会话可能无效", "warn")

        # 验证会话：拉一个需要登录的页面看看
        try:
            body = self.fetch("/xtgl/index_initMenu.html")
        except ZfError as exc:
            self.log(f"验证会话失败: {exc}", "warn")
            return False

        if self._LOGIN_FORM_RE.search(body):
            self.log("浏览器会话无效(被服务端要求重新登录)", "err")
            return False

        self.logged_in = True
        name = self._extract_student_name(body)
        self.log(f"已接管浏览器会话{'：' + name if name else ''}", "ok")
        return True

    # ---------------- 选课页面 ----------------
    def fetch(self, path: str, referer: Optional[str] = None, **kw) -> str:
        _, body, _ = self._request(path, referer=referer, **kw)
        return body

    # 本校真实入口（从主菜单 clickMenu 里挖出来的）优先
    # 注意：base_url 已包含 /jwglxt，这里不能再带该前缀
    SELECT_PAGE_PATHS = (
        "/xsxk/zzxkyzb_cxZzxkYzbIndex.html?gnmkdm=N253512",
        "/xsxk/xsxk_index.html?gnmkdm=N253508",
        "/xsxk/xsxk_list.html?gnmkdm=N253508",
        "/xszx/xsxk_index.html?gnmkdm=N253508",
    )

    # 选课页上表示「不在选课期」的提示
    NOT_OPEN_MARKS = ("不属于选课阶段", "不在选课时间", "选课已结束",
                      "选课未开始", "未开放", "没有选课", "请与管理员联系")

    @classmethod
    def detect_not_open(cls, body: str) -> str:
        """若页面提示不在选课期，返回该提示；否则返回空串。"""
        text = re.sub(r"<script.*?</script>", " ", body or "", flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        for mark in cls.NOT_OPEN_MARKS:
            if mark in text:
                i = text.find(mark)
                return text[max(0, i - 25):i + 60].strip()
        return ""

    def find_select_page(self) -> Optional[Tuple[str, str]]:
        """探测选课页面, 返回 (路径, 页面HTML)。

        注意: 即使当前不在选课期, 页面也会正常返回(只是没有课程数据),
        这种情况同样算"找到选课页", 由调用方通过 detect_not_open 判断。
        """
        candidates: List[str] = list(self.SELECT_PAGE_PATHS)
        for mod in self.cfg.modules:
            for leaf in ("xsxk_index", "xsxk_list"):
                p = f"/{mod}/{leaf}.html?gnmkdm={self.cfg.gnmkdm}"
                if p not in candidates:
                    candidates.append(p)

        for path in candidates:
            try:
                body = self.fetch(path)
            except ZfError as exc:
                self.log(f"探测 {path} 失败: {exc}", "debug")
                continue
            if self._LOGIN_FORM_RE.search(body):
                raise LoginError("会话已失效, 请重新登录")
            if "Page Not Found" in body or "抱歉" in body[:200]:
                self.log(f"探测 {path} —— 该地址不存在", "debug")
                continue
            if self.detect_not_open(body) or self.looks_like_select_page(body):
                return path, body
            self.log(f"探测 {path} —— 非选课页", "debug")
        return None

    @staticmethod
    def looks_like_select_page(body: str) -> bool:
        hints = ("xsxk", "选课", "教学班", "kch_id", "jxb_id", "选课轮次",
                 "课程名称", "zzxkyzb")
        return sum(1 for h in hints if h in body) >= 2

    def list_terms(self, page: str) -> List[Tuple[str, str]]:
        """从选课页提取可选学期 (xnm/xqm)。"""
        terms: List[Tuple[str, str]] = []
        seen = set()
        for m in re.finditer(r'<option[^>]*value=["\']([^"\']*)["\'][^>]*>([^<]*)</option>', page):
            val, label = m.group(1), m.group(2).strip()
            if not label:
                continue
            mm = re.match(r"^(\d{4})-(\d{4})-(\d)$", label.strip())
            if mm:
                xnm, _, xqm = mm.group(1), mm.group(2), mm.group(3)
                if (xnm, xqm) not in seen:
                    seen.add((xnm, xqm))
                    terms.append((xnm, xqm))
        return terms

    def list_courses(self, path: str, data: Optional[Dict[str, Any]] = None) -> str:
        """拉取可选课程列表页。"""
        return self.fetch(path, data=data or {})

    @staticmethod
    def parse_courses(page: str) -> List[Dict[str, str]]:
        """从页面里提取可选的 教学班/课程, 用于 --list 展示。

        正方页面上常见的几种写法都要兼容：
            <a onclick="xsxk('jxb_id','3F0A1B2C','kch_id','B12345')">选课</a>
            <a href="javascript:xsxk('jxb_id','3F0A1B2C')">
            <input type="hidden" name="jxb_id" value="3F0A1B2C">
            <tr><td>高等数学</td> ... <td>选课</td></tr>
        """
        out: List[Dict[str, str]] = []

        def rows_with_ids() -> List[Tuple[str, str]]:
            """产出 (jxb_id, 该 id 所在的完整 <tr> HTML)。"""
            pairs: List[Tuple[str, str]] = []

            # 逐 <tr> 扫描
            for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", page, re.S):
                html = row.group(1)
                jxb = ""
                kch_js = ""

                # (a) xsxk('jxb_id','<id>','kch_id','<id>') 形式
                #     这是正方最真实、最常见的写法，两个 id 都要抠出来
                m = re.search(r"xsxk\s*\((.*?)\)", html, re.S)
                if m:
                    args = re.findall(r"['\"]([^'\"]*)['\"]", m.group(1))
                    for i, a in enumerate(args):
                        if i + 1 >= len(args):
                            break
                        if a == "jxb_id" and not jxb:
                            jxb = args[i + 1]
                        elif a == "kch_id" and not kch_js:
                            kch_js = args[i + 1]
                    if not jxb and args:
                        # 有些版本直接是 xsxk('<id>')
                        cand = [a for a in args if re.fullmatch(r"[A-Za-z0-9]{6,}", a or "")]
                        if cand:
                            jxb = cand[0]

                # (b) 隐藏域 / data 属性 / href
                if not jxb:
                    m = (re.search(r"name=['\"]jxb_id['\"][^>]*value=['\"]([^'\"]+)", html, re.I)
                         or re.search(r"value=['\"]([^'\"]+)['\"][^>]*name=['\"]jxb_id['\"]", html, re.I))
                    if m:
                        jxb = m.group(1)
                if not jxb:
                    m = (re.search(r"jxb_id['\"]?\s*[:=]\s*['\"]([A-Za-z0-9]+)['\"]", html)
                         or re.search(r"jxb_id=([A-Za-z0-9]{4,})", html))
                    if m:
                        jxb = m.group(1)

                if jxb:
                    pairs.append((jxb, html))
            return pairs

        for jxb, html in rows_with_ids():
            kch = ""
            m = (re.search(r"kch_id['\"]?\s*[:=]\s*['\"]([A-Za-z0-9]+)['\"]", html)
                 or re.search(r"kch_id=([A-Za-z0-9]{4,})", html))
            if m:
                kch = m.group(1)
            if not kch:
                # 回退：从 xsxk('jxb_id','X','kch_id','Y') 的参数里拿
                m2 = re.search(r"xsxk\s*\((.*?)\)", html, re.S)
                if m2:
                    args = re.findall(r"['\"]([^'\"]*)['\"]", m2.group(1))
                    for i, a in enumerate(args):
                        if a == "kch_id" and i + 1 < len(args):
                            kch = args[i + 1]
                            break
            cells = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", html, re.S)]
            out.append({
                "jxb_id": jxb,
                "kch_id": kch,
                "text": " | ".join(c for c in cells if c)[:200],
            })
        return out

    # ---------------- 选课提交 ----------------
    # 正方不同版本的选课端点/传参方式有差异, 这里全部试一遍。
    _SELECT_LEAVES = ("xsxk_operate", "xsxk_xk", "xsxk_xkBc", "xsxk_saveXk",
                      "xsxk_doXk", "xsxk_xkOperate")
    _RETRY_KEYWORDS = RETRY_MARKS
    _STOP_KEYWORDS = FATAL_MARKS

    def _select_referer(self, mod: str) -> str:
        # base_url 已包含 /jwglxt，这里不能再带该前缀，否则会变成
        # .../jwglxt/jwglxt/... 而落到 404 上
        return f"{self.cfg.base_url}/{mod}/xsxk_list.html?gnmkdm={self.cfg.gnmkdm}"

    @staticmethod
    def _looks_like_html(body: str) -> bool:
        head = body.lstrip()[:200].lower()
        if not head.startswith("<"):
            return False
        # 404/500 错误页也算"端点不对"
        return True

    def _classify_select(self, final: str, body: str) -> Tuple[str, str]:
        """把一次选课响应归类为 ok / retry / stop / unknown。"""
        # 会话掉线: 服务端把登录页又发回来了
        if self._LOGIN_FORM_RE.search(body) or (
                "login_slogin" in final and "csrftoken" in body):
            raise LoginError("会话已失效")

        text = body.strip()
        if not text:
            # 空响应无法判断端点是否正确(探测阶段), 但轮询时值得重试
            return ("retry" if self._select_endpoint else "unknown"), "空响应"

        if self._looks_like_html(text):
            if "选课成功" in text:
                return "ok", "选课成功"
            if "404" in text[:120] or "Not Found" in text[:120] or "500" in text[:120]:
                return "unknown", "HTTP 错误页"
            return "unknown", "返回 HTML 而非 JSON"

        js = None
        try:
            js = json.loads(text)
        except Exception:
            js = None

        msg = ""
        positive = False
        if isinstance(js, dict):
            msg = str(js.get("msg") or js.get("message") or js.get("info") or "")
            flag = str(js.get("flag") or js.get("status") or js.get("code") or "")
            positive = flag in ("1", "success", "true", "200") and "失败" not in msg
        else:
            msg = text.strip().strip('"')
            positive = any(k in msg for k in SUCCESS_MARKS)

        if any(k in msg for k in self._STOP_KEYWORDS):
            return "stop", msg
        if any(k in msg for k in self._RETRY_KEYWORDS):
            return "retry", msg
        if positive:
            return "ok", msg or "选课成功"
        if msg:
            return "retry", msg
        return "unknown", msg or "空响应"

    def submit_select(self, jxb_id: str, extra: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """提交一次选课。返回 (是否成功, 服务器消息)。

        会自动探测可用的端点与传参方式, 并把第一个"看起来被服务端接受"的组合记住,
        后续轮询只走这一条路径, 保证抢课时的速度。
        """
        cfg = self.cfg
        base_payload: Dict[str, Any] = {"jxb_ids": jxb_id, "jxb_id": jxb_id}
        if extra:
            base_payload.update(extra)

        # 已确定的路径
        if self._select_endpoint:
            return self._do_select(self._select_endpoint, jxb_id, base_payload)

        unknowns: List[str] = []
        for mod in cfg.modules:
            for leaf in self._SELECT_LEAVES:
                # 注意：base_url 已含 /jwglxt，这里只拼 /{mod}/{leaf}.html
                path = f"/{mod}/{leaf}.html?gnmkdm={cfg.gnmkdm}"
                # 方式 A: 表单 POST (大多数版本)
                styles = [
                    ("post", {**base_payload}),
                    # 方式 B: GET 查询串 (部分版本, 参数名复数)
                    ("get", {"jxb_ids": jxb_id,
                             "gnmkdm": cfg.gnmkdm,
                             **({k: v for k, v in base_payload.items()
                                 if k not in ("jxb_ids", "jxb_id")})}),
                ]
                for style, payload in styles:
                    try:
                        if style == "post":
                            final, body, status = self._request(
                                path, data=payload, ajax=True,
                                referer=self._select_referer(mod))
                        else:
                            url = path + "&" + urllib.parse.urlencode(payload, doseq=True)
                            final, body, status = self._request(
                                url, referer=self._select_referer(mod), ajax=True)
                    except LoginError:
                        raise
                    except ZfError as exc:
                        unknowns.append(f"{mod}/{leaf}/{style}: {exc}")
                        continue

                    if status in (404, 500):
                        unknowns.append(f"{mod}/{leaf}/{style}: HTTP {status}")
                        continue

                    kind, msg = self._classify_select(final, body)
                    if kind == "ok":
                        self._select_endpoint, self._select_style = path, style
                        self.log(f"选课端点已确定: {path} ({style})", "ok")
                        return True, msg
                    if kind == "stop":
                        if self._is_already(msg):
                            self._select_endpoint, self._select_style = path, style
                            return True, msg
                        raise StopError(msg)
                    if kind == "retry":
                        self._select_endpoint, self._select_style = path, style
                        self.log(f"选课端点已确定: {path} ({style})", "ok")
                        return False, msg
                    unknowns.append(f"{mod}/{leaf}/{style}: {msg}")

        detail = "; ".join(dict.fromkeys(unknowns))[:400]
        raise ZfError(f"没能找到可用的选课端点。请先运行 --discover 并把 URL 发来适配。{detail}")

    def _do_select(self, path: str, jxb_id: str,
                   payload: Dict[str, Any]) -> Tuple[bool, str]:
        style = self._select_style
        if style == "get":
            url = path + "&" + urllib.parse.urlencode(
                {"jxb_ids": jxb_id, "gnmkdm": self.cfg.gnmkdm}, doseq=True)
            final, body, _ = self._request(url, referer=self._select_referer(self.cfg.modules[0]),
                                           ajax=True)
        else:
            final, body, _ = self._request(path, data=payload, ajax=True,
                                           referer=self._select_referer(self.cfg.modules[0]))
        kind, msg = self._classify_select(final, body)
        if kind == "stop":
            # "已选过"其实是成功(B 计划: 课程已在课表里), 其余 stop 为终止错误
            if self._is_already(msg):
                return True, msg
            raise StopError(msg)
        return kind == "ok", msg

    # ---------------- 端点诊断 ----------------
    DUMMY_JXB = "ZZ_FAKE_JXB_ID_ZZ"

    def diagnose_select_endpoints(self) -> List[Dict[str, Any]]:
        """用假 jxb_id 逐个探测候选选课端点, 记录服务端真实响应。

        目的: 在不真的选上课的前提下, 判断哪个端点/传参方式是"活的"。
        "活的"表现为: 返回业务错误(如"教学班不存在"/"已满")而不是 404 / 空响应 / 登录页。
        """
        cfg = self.cfg
        results: List[Dict[str, Any]] = []
        referer = self._select_referer(cfg.modules[0])

        for mod in cfg.modules:
            for leaf in self._SELECT_LEAVES:
                # 同 submit_select：base_url 已含 /jwglxt，不要再带
                path = f"/{mod}/{leaf}.html?gnmkdm={cfg.gnmkdm}"
                for style in ("post", "get"):
                    entry: Dict[str, Any] = {"endpoint": f"{mod}/{leaf}", "style": style}
                    try:
                        if style == "post":
                            final, body, status = self._request(
                                path, data={"jxb_ids": self.DUMMY_JXB, "jxb_id": self.DUMMY_JXB},
                                ajax=True, referer=referer, timeout=8)
                        else:
                            url = path + "&" + urllib.parse.urlencode(
                                {"jxb_ids": self.DUMMY_JXB, "gnmkdm": cfg.gnmkdm})
                            final, body, status = self._request(
                                url, ajax=True, referer=referer, timeout=8)
                        snippet = re.sub(r"\s+", " ", body.strip())[:160]
                        entry.update({"status": status, "body": snippet})
                        if status in (404, 500):
                            entry["verdict"] = f"HTTP {status} (端点不存在)"
                        elif self._LOGIN_FORM_RE.search(body):
                            entry["verdict"] = "要求登录 (会话失效)"
                        elif not body.strip():
                            entry["verdict"] = "空响应 (可能是端点不对)"
                        elif body.lstrip().startswith("<"):
                            entry["verdict"] = "返回 HTML (端点不对)"
                        else:
                            entry["verdict"] = "★ 有业务响应 (端点疑似有效)"
                    except ZfError as exc:
                        entry.update({"status": "-", "body": str(exc)[:120],
                                      "verdict": "请求异常"})
                    results.append(entry)
        return results

    # ---------------- 轮询抢课 ----------------
    # 这些提示说明"这门课已经拿下了", 不必继续抢
    _ALREADY_MARKS = ("已选过", "已经选过", "重复选课", "已选该课程", "已选定")

    @classmethod
    def _is_already(cls, msg: str) -> bool:
        return any(k in msg for k in cls._ALREADY_MARKS)

    def grab(
        self,
        targets: List[str],
        extra: Optional[Dict[str, Any]] = None,
        on_success=None,
        on_failure=None,
    ) -> bool:
        cfg = self.cfg
        pending = list(dict.fromkeys(targets))
        attempts = 0
        started = time.time()
        self.ensure_login()

        if not pending:
            raise ZfError("没有指定任何课程 (--course)")

        self.log(f"开始抢课: {len(pending)} 个目标 -> {', '.join(pending)}")

        login_failures = 0
        while pending:
            attempts += 1
            if cfg.max_attempts and attempts > cfg.max_attempts:
                self.log(f"已达最大尝试次数 {cfg.max_attempts}, 停止", "warn")
                return False
            if cfg.max_minutes and (time.time() - started) / 60.0 >= cfg.max_minutes:
                self.log(f"已达最大运行时间 {cfg.max_minutes} 分钟, 停止", "warn")
                return False

            for jxb_id in list(pending):
                try:
                    ok, msg = self.submit_select(jxb_id, extra=extra)
                except StopError as exc:
                    # 服务端明确拒绝, 重试无意义: 把它从目标里摘掉
                    self.log(f"{jxb_id}: {exc} —— 放弃该课程", "err")
                    pending.remove(jxb_id)
                    if on_failure:
                        on_failure(jxb_id, str(exc))
                    continue
                except LoginError as exc:
                    self.log(f"会话可能需要重新登录: {exc}", "warn")
                    self.logged_in = False
                    self._select_endpoint = None
                    time.sleep(1.0)
                    try:
                        self.ensure_login()
                        login_failures = 0
                    except LoginError as login_exc:
                        login_failures += 1
                        self.log(f"重新登录失败 ({login_exc}), 第 {login_failures} 次", "err")
                        if login_failures >= 5:
                            raise LoginError(f"连续 {login_failures} 次重新登录失败, 已停止") from login_exc
                        time.sleep(min(30.0, 3.0 * login_failures))
                    continue
                except ZfError as exc:
                    self.log(f"{jxb_id}: 请求异常 {exc}", "warn")
                    continue

                if ok:
                    self.log(f"{jxb_id}: 抢到！服务器返回: {msg}", "ok")
                    pending.remove(jxb_id)
                    if on_success:
                        on_success(jxb_id, msg)
                else:
                    self.log(f"{jxb_id}: {msg}", "debug")

            if not pending:
                break
            time.sleep(max(0.0, cfg.interval + random.uniform(0, cfg.jitter)))

        self.log(f"全部完成, 共 {attempts} 轮 / {time.time() - started:.1f}s", "ok")
        return True
