#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抢课核心

两条读取路径：
  BrowserGrab —— 用 Selenium 读真实页面（可靠，稍慢）
  HttpGrab    —— 用 HTTP 读选课页（快），真正点「选课」仍交给浏览器

思路：不猜 HTTP 选课端点，直接用浏览器点学校自己的「选课」按钮 ——
      那是学校前端代码，一定能工作。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# 重新导出，方便 GUI 只 import 一个模块
from zf_browser import (  # noqa: F401
    SeleniumUnavailable,
    browser_login,
    cookies_to_jar,
    find_browser_binary,
)

__all__ = [
    "SeleniumUnavailable", "browser_login", "cookies_to_jar", "build_driver",
    "find_browser_binary", "CourseRow", "TableInfo", "BrowserGrab", "MatchRule",
    "HttpGrab",
]


def build_driver(browser: str = "edge", headless: bool = False,
                 binary: str = None, profile_dir: str = None):
    """启动浏览器（懒加载 selenium，缺依赖时报清晰错误）。

    会自动在常见安装位置查找浏览器，避免 Selenium Manager 查找失败。
    profile_dir 传入时使用持久配置目录，可保留 Cookie（"记住我"生效）。
    """
    from zf_browser import _build_driver
    return _build_driver(browser, headless, binary, profile_dir=profile_dir)


# 「选课」按钮上可能出现的文字
SELECT_TEXTS = ("选课", "选择", "选定", "选 课", "报名", "我要选")
# 表示这门课已经选过了
DONE_TEXTS = ("已选", "退选", "已选定", "取消", "已报名")


@dataclass
class MatchRule:
    """匹配规则：填了的条件之间是「且」的关系。"""
    course_code: str = ""      # 课程代码 / 课程号
    course_name: str = ""      # 课程名称（部分匹配）
    teacher: str = ""          # 教师姓名（部分匹配）
    teacher_contains: bool = True

    def is_empty(self) -> bool:
        return not (self.course_code.strip() or self.course_name.strip()
                    or self.teacher.strip())

    def describe(self) -> str:
        parts = []
        if self.course_code.strip():
            parts.append(f"课程代码={self.course_code.strip()}")
        if self.course_name.strip():
            parts.append(f"课程名称含「{self.course_name.strip()}」")
        if self.teacher.strip():
            parts.append(f"教师含「{self.teacher.strip()}」")
        return " 且 ".join(parts) if parts else "（未设置条件）"


@dataclass
class CourseRow:
    """表格里的一行课程。"""
    index: int
    lines: List[str]
    button_text: str = ""
    already_done: bool = False
    raw_html: str = ""

    def __str__(self) -> str:
        return " | ".join(self.lines)[:160]


@dataclass
class TableInfo:
    """表格结构信息，用于调试。"""
    url: str = ""
    found: bool = False
    headers: List[str] = field(default_factory=list)
    row_count: int = 0
    sample_rows: List[List[str]] = field(default_factory=list)
    frame: str = "top"
    note: str = ""


# 页面上所有可能装课程列表的表格/容器的候选选择器
_TABLE_SELECTORS = [
    "#kbtable", "#xkKbTable", "table#tableObj", "#dataTable",
    "table.dataTable", "table.table", "table.table-bordered",
    "#contentBox table", ".xsxk-table table", "div#tabGrid table",
    "table",
]

# 本校「自主选课」入口（从真实主菜单 clickMenu 里挖出来的）
#   菜单: 选课 -> 自主选课  gnmkdm=N253512
SELECT_PAGE_PATHS = [
    "/jwglxt/xsxk/zzxkyzb_cxZzxkYzbIndex.html?gnmkdm=N253512",
    "/jwglxt/xsxk/xsxk_index.html?gnmkdm=N253508",
    "/jwglxt/xsxk/xsxk_list.html?gnmkdm=N253508",
    "/jwglxt/xszx/xsxk_index.html?gnmkdm=N253508",
]

# 选课页上表示「不在选课期」的提示
NOT_OPEN_MARKS = (
    "不属于选课阶段", "不在选课时间", "选课已结束", "选课未开始",
    "未开放", "没有选课",
)


def detect_not_open(html: str) -> str:
    """如果页面提示不在选课期，返回该提示；否则返回空串。"""
    text = re.sub(r"<script.*?</script>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    for mark in NOT_OPEN_MARKS:
        if mark in text:
            i = text.find(mark)
            return text[max(0, i - 20):i + 60].strip()
    return ""


class BrowserGrab:
    """一个浏览器会话上的抢课操作。"""

    def __init__(self, driver, base_url: str, log: Callable[[str], None] = print):
        self.driver = driver
        self.base_url = base_url.rstrip("/")
        self.log = log
        self._frame: Optional[Any] = None

    # ---------------- 切换 iframe ----------------
    def _enter_content_frame(self) -> str:
        """正方把内容放在 iframe 里，这里自动切进去。返回框架描述。"""
        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass
        best = None
        try:
            frames = self.driver.find_elements("tag name", "iframe")
        except Exception:
            frames = []
        for fr in frames:
            try:
                fid = (fr.get_attribute("id") or "") + (fr.get_attribute("name") or "")
                if re.search(r"content|main|ifr|frame|xsxk|body", fid, re.I):
                    best = fr
                    break
            except Exception:
                continue
        if best is None and frames:
            best = frames[0]
        if best is not None:
            try:
                self.driver.switch_to.frame(best)
                return "iframe"
            except Exception:
                try:
                    self.driver.switch_to.default_content()
                except Exception:
                    pass
        return "top"

    # ---------------- 定位表格 ----------------
    def _find_table(self):
        for sel in _TABLE_SELECTORS:
            try:
                els = self.driver.find_elements("css selector", sel)
            except Exception:
                continue
            for el in els:
                try:
                    if len(el.find_elements("tag name", "tr")) >= 2:
                        return el
                except Exception:
                    continue
        return None

    # ---------------- 读取表格 ----------------
    def read_table(self, dump_path: Optional[str] = None) -> TableInfo:
        info = TableInfo()
        try:
            info.url = self.driver.current_url
        except Exception:
            pass
        info.frame = self._enter_content_frame()

        table = self._find_table()
        if table is None:
            info.note = "页面上没找到符合条件的表格"
            if dump_path:
                self._dump_debug(info, dump_path)
            return info

        info.found = True
        try:
            rows = table.find_elements("tag name", "tr")
        except Exception:
            info.note = "读取表格行失败"
            if dump_path:
                self._dump_debug(info, dump_path)
            return info

        for i, tr in enumerate(rows):
            try:
                ths = tr.find_elements("tag name", "th")
                tds = tr.find_elements("tag name", "td")
                if ths:
                    cells, is_header = ths, True
                elif tds:
                    cells, is_header = tds, False
                else:
                    continue

                lines = []
                for c in cells:
                    txt = (c.text or "").strip()
                    if txt:
                        lines.append(re.sub(r"\s+", " ", txt))
                if not lines:
                    continue

                if (is_header or i == 0) and not info.headers:
                    info.headers = lines
                    continue

                info.row_count += 1
                if len(info.sample_rows) < 8:
                    info.sample_rows.append(lines)
            except Exception:
                continue

        if not info.headers and info.sample_rows:
            info.headers = info.sample_rows[0]

        if dump_path:
            self._dump_debug(info, dump_path)
        return info

    def _dump_debug(self, info: TableInfo, path: str) -> None:
        payload = {
            "url": info.url, "frame": info.frame, "headers": info.headers,
            "row_count": info.row_count, "sample_rows": info.sample_rows,
            "note": info.note,
        }
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.log(f"[!] 保存调试信息失败: {exc}")

    # ---------------- 列定位 ----------------
    # 按顺序匹配，匹配到的列会被"占用"，避免"课程名称"里的"课程"
    # 被误判成课程代码列。
    _COL_RULES = (
        ("code",    ("课程代码", "课程号", "课程编号", "课程编码", "课号",
                     "课程标识")),
        ("teacher", ("任课教师", "授课教师", "主讲教师", "上课教师", "教师",
                     "老师", "主讲")),
        ("class",   ("教学班名称", "教学班", "班级名称", "开班")),
        ("name",    ("课程名称", "课程名", "科目名称")),
        ("name",    ("课程", "科目")),          # 兜底，放最后
    )

    @classmethod
    def _locate_columns(cls, headers: List[str]) -> Dict[str, int]:
        found: Dict[str, int] = {}
        used = set()
        norm = [(i, (h or "").strip()) for i, h in enumerate(headers)]
        for key, hints in cls._COL_RULES:
            if key in found:
                continue
            for i, h in norm:
                if i in used or not h:
                    continue
                if any(hint in h for hint in hints):
                    found[key] = i
                    used.add(i)
                    break
        return found

    def _row_text_of_columns(self, row, cols: Dict[str, int]) -> Dict[str, str]:
        out = {}
        for key, idx in cols.items():
            if 0 <= idx < len(row.lines):
                out[key] = row.lines[idx].strip()
        if not out:
            out = {"_all": " ".join(row.lines)}
        return out

    # ---------------- 匹配 ----------------
    @staticmethod
    def _matches(rule: MatchRule, cols: Dict[str, str], full_text: str) -> bool:
        code = " ".join(v for k, v in cols.items() if k in ("code", "class")) or full_text
        name = " ".join(v for k, v in cols.items() if k in ("name", "class")) or full_text
        teacher = cols.get("teacher", "") or full_text

        if rule.course_code.strip():
            if rule.course_code.strip().lower() not in code.lower():
                return False
        if rule.course_name.strip():
            if rule.course_name.strip() not in name:
                return False
        if rule.teacher.strip():
            if rule.teacher.strip() not in teacher:
                return False
        return True

    # ---------------- 按钮 ----------------
    def _all_action_buttons(self, row_el) -> List[Tuple[Any, str]]:
        out = []
        try:
            cands = row_el.find_elements("css selector", "a, button, input[type=button]")
        except Exception:
            return out
        for el in cands:
            try:
                txt = ((el.text or "") + " " +
                       (el.get_attribute("value") or "")).strip()
            except Exception:
                continue
            txt = re.sub(r"\s+", " ", txt)
            if txt:
                out.append((el, txt))
        return out

    def _find_buttons(self, row_el) -> List[Tuple[Any, str]]:
        return [(el, txt) for el, txt in self._all_action_buttons(row_el)
                if any(k in txt for k in SELECT_TEXTS)]

    def _row_state(self, row_el) -> Tuple[str, bool]:
        """返回 (按钮文字, 是否已经选过)。"""
        allb = self._all_action_buttons(row_el)
        if not allb:
            return "", False
        for el, txt in allb:
            stripped = txt.strip()
            if stripped in ("退选", "退课", "取消", "已选", "已选定", "已报名"):
                return txt, True
            if any(k in txt for k in DONE_TEXTS) and not any(
                    k == stripped for k in SELECT_TEXTS):
                return txt, True
        for el, txt in allb:
            if any(k in txt for k in SELECT_TEXTS):
                return txt, False
        return allb[0][1], False

    # ---------------- 扫描 ----------------
    def scan(self, rule: MatchRule, dump_path: Optional[str] = None
             ) -> Tuple[List[CourseRow], TableInfo]:
        info = self.read_table(dump_path=dump_path)
        if not info.found:
            return [], info

        cols = self._locate_columns(info.headers)
        table = self._find_table()
        if table is None:
            return [], info

        matched: List[CourseRow] = []
        try:
            trs = table.find_elements("tag name", "tr")
        except Exception:
            return [], info

        for i, tr in enumerate(trs):
            try:
                cells = tr.find_elements("tag name", "td")
                if not cells:
                    continue
                lines = []
                for td in cells:
                    txt = (td.text or "").strip()
                    if txt:
                        lines.append(re.sub(r"\s+", " ", txt))
                if not lines:
                    continue

                row = CourseRow(index=i, lines=lines)
                btn_text, done = self._row_state(tr)
                row.button_text = btn_text
                row.already_done = done

                colmap = self._row_text_of_columns(row, cols)
                if self._matches(rule, colmap, " ".join(lines)):
                    matched.append(row)
            except Exception:
                continue
        return matched, info

    # ---------------- 点「选课」 ----------------
    def click_select(self, row_index: int) -> Tuple[bool, str]:
        table = self._find_table()
        if table is None:
            return False, "找不到表格"
        try:
            trs = table.find_elements("tag name", "tr")
            tr = trs[row_index]
        except Exception:
            return False, "行号失效（页面可能已刷新）"

        btns = self._find_buttons(tr)
        if not btns:
            return False, "这一行没有可点的「选课」按钮"

        el, txt = btns[0]
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", el)
        except Exception:
            pass
        try:
            el.click()
            return True, f"已点击「{txt}」"
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", el)
                return True, f"已点击「{txt}」(JS 方式)"
            except Exception as exc:
                return False, f"点击失败: {exc}"

    # ---------------- 处理弹窗 ----------------
    def handle_dialogs(self) -> List[str]:
        """处理选课后的各种弹窗，返回捕获到的提示文字。

        顺序很重要：
          1. 先抓原生弹窗（alert/confirm）—— 用 CDP 才能拿到文字
          2. 再抓页面上的 DOM 弹窗（layui / bootbox 等），并点掉确认按钮
        """
        texts: List[str] = []

        # ---- 1) 原生弹窗 ----
        try:
            from zf_browser import dismiss_dialogs
            for t in dismiss_dialogs(self.driver):
                if t and t not in texts:
                    texts.append(t)
        except Exception:
            # 兜底：至少尝试关掉，避免挡住后续操作
            try:
                al = self.driver.switch_to.alert
                if al.text and al.text not in texts:
                    texts.append(al.text)
                al.accept()
            except Exception:
                pass

        # ---- 2) DOM 弹窗文字 ----
        for sel in (".layui-layer-content", ".bootbox-body", "#tips",
                    ".layui-layer-msg", ".alert", ".modal-body",
                    "#tipbox", ".weui_dialog", ".ui-alert"):
            try:
                for el in self.driver.find_elements("css selector", sel):
                    t = (el.text or "").strip()
                    if t and t not in texts:
                        texts.append(t)
            except Exception:
                continue

        # ---- 3) 点掉 DOM 弹窗的确认按钮 ----
        for sel in (".layui-layer-btn0", ".bootbox-accept", "button.btn-primary",
                    ".layui-layer-btn a", ".weui_dialog_confirm .weui_btn_primary"):
            try:
                for el in self.driver.find_elements("css selector", sel):
                    try:
                        if not el.is_displayed():
                            continue
                    except Exception:
                        continue
                    txt = (el.text or "").strip()
                    try:
                        el.click()
                    except Exception:
                        try:
                            self.driver.execute_script("arguments[0].click();", el)
                        except Exception:
                            continue
                    if txt and txt not in texts:
                        texts.append(txt)
            except Exception:
                continue

        return texts


class HttpGrab:
    """用 HTTP 读选课表格（比浏览器快很多）。

    scan() 接口与 BrowserGrab 一致，GUI 可无差别调用。
    只做「读」—— 真正点「选课」仍然交给浏览器，因为选课端点尚未在本校验证过。
    """

    def __init__(self, client, log: Callable[[str], None] = print):
        self.client = client
        self.log = log
        self.page_path: Optional[str] = None
        self._html = ""
        self._at = 0.0

    # ---- 表格解析（纯 HTML，无浏览器） ----
    @staticmethod
    def _table_rows(html: str) -> Tuple[List[str], List[List[str]]]:
        tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.S | re.I)
        best = None
        for t in tables:
            trs = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S | re.I)
            if len(trs) >= 2:
                best = trs
                break
        if best is None:
            return [], []

        headers: List[str] = []
        rows: List[List[str]] = []
        for i, tr in enumerate(best):
            ths = re.findall(r"<th[^>]*>(.*?)</th>", tr, re.S | re.I)
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
            raw = ths if ths else tds
            lines = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", c)).strip()
                     for c in raw]
            lines = [c for c in lines if c]
            if not lines:
                continue
            if (ths or i == 0) and not headers:
                headers = lines
                continue
            rows.append(lines)
        if not headers and rows:
            headers = rows[0]
        return headers, rows

    @classmethod
    def _parse(cls, html: str) -> Tuple[List[List[str]], TableInfo]:
        headers, rows = cls._table_rows(html or "")
        info = TableInfo()
        info.headers = headers
        info.row_count = len(rows)
        info.sample_rows = rows[:8]
        info.found = bool(rows) or bool(headers)
        if not info.found:
            info.note = "HTML 里没找到课程表格"
        return rows, info

    # ---- 取页面 ----
    def _get_html(self, force: bool = False) -> str:
        now = time.time()
        if (not force) and self._html and (now - self._at) < 1.0:
            return self._html
        if self.page_path is None:
            try:
                found = self.client.find_select_page()
            except Exception as exc:
                self.log(f"探测选课页失败：{exc}")
                return ""
            if not found:
                return ""
            self.page_path = found[0]
        try:
            self._html = self.client.fetch(self.page_path)
            self._at = now
        except Exception as exc:
            self.log(f"读取选课页失败：{exc}")
            return ""
        return self._html

    # ---- 与 BrowserGrab 对齐 ----
    def raw_html(self) -> str:
        """返回最近一次取到的选课页 HTML（用于判断是否在选课期）。"""
        return self._get_html()

    def read_table(self, dump_path: Optional[str] = None) -> TableInfo:
        _, info = self._parse(self._get_html())
        if not info.found:
            info.note = info.note or "没拿到选课页面"
        return info

    def scan(self, rule: MatchRule, dump_path: Optional[str] = None
             ) -> Tuple[List[CourseRow], TableInfo]:
        rows, info = self._parse(self._get_html())
        if not info.found:
            return [], info

        cols = BrowserGrab._locate_columns(info.headers)
        if not cols:
            # 没识别出表头，退化为"整行匹配"
            cols = {}

        matched: List[CourseRow] = []
        for idx, cells in enumerate(rows):
            row = CourseRow(index=idx, lines=cells)
            cm: Dict[str, str] = {}
            for key, i in cols.items():
                if 0 <= i < len(cells):
                    cm[key] = cells[i]
            cm.setdefault("_all", " ".join(cells))
            if BrowserGrab._matches(rule, cm, " ".join(cells)):
                matched.append(row)
        return matched, info
