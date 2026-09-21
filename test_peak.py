"""离线测试：不需要联网、不需要模拟站，直接跑。

    python test_peak.py

覆盖 zf_peak 里最容易出错的两块：
  1. 提交反馈归类 —— 高峰期全靠它区分"要重投"和"已经成功了"
  2. 开放时间解析 —— 决定"待命"还是"马上投"
外加一条**回归测试**：提交地址绝不能出现 /jwglxt/jwglxt。
"""
from __future__ import annotations

import os
import sys
import time

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)

import zf_peak as PK                                    # noqa: E402
from zf_client import Config, OpenZfClient              # noqa: E402

FAILS: list = []


def check(cond, label, extra=""):
    print("  [%s] %s %s" % ("OK" if cond else "!!", label, extra))
    if not cond:
        FAILS.append(label)


# --------------------------------------------------------------------------
# 1. 反馈归类
# --------------------------------------------------------------------------
print("=== 1. 提交反馈归类 ===")
CASES = [
    # 服务端原话                                  期望
    ('{"flag":"1","msg":"选课成功"}', 200, "ok"),
    ("选课成功", 200, "ok"),
    ('{"flag":"0","msg":"已经选过该课程"}', 200, "already"),
    ("该教学班人数已满", 200, "full"),
    ('{"flag":"0","msg":"不在选课时间内"}', 200, "not_open"),
    ('{"flag":"0","msg":"未到选课时间"}', 200, "not_open"),
    ('{"flag":"0","msg":"登录超时，请重新登录"}', 200, "session"),
    ('{"flag":"0","msg":"无选课权限"}', 200, "stop"),
    ("", 502, "gateway"),
    ("<html><body>Gateway Error</body></html>", 503, "gateway"),
    ("", 0, "unknown"),
    ("随便什么别的东西", 200, "error"),
    # 陷阱：这句里同时有"已选"和"重复"，必须先判成 already 而不是"成功"
    ('{"flag":"0","msg":"已经选过该课程，不能重复选课"}', 200, "already"),
    # 陷阱：登录页被当成响应发回来
    ('<html><form action="login_slogin.html">'
     '<input name="csrftoken" value="x"></form></html>', 200, "session"),
]
for text, status, want in CASES:
    got = PK.classify_submit(text, status)
    check(got == want, "%-38s -> %s" % (repr(text[:34]), want),
          "(得到 %s)" % got)

# 顺序陷阱单独强调
check(PK.classify_submit('{"msg":"已经选过该课程"}') == "already",
      "「已经选过」不会被误判成别的")
check(PK.classify_submit('{"msg":"该教学班人数已满"}') == "full",
      "「已满」不会被误判成成功")

# --------------------------------------------------------------------------
# 2. 开放时间解析
# --------------------------------------------------------------------------
print("\n=== 2. 开放时间解析 ===")
today = time.strftime("%Y-%m-%d", time.localtime())
page = ("<div>本轮选课时间：%s 12:30:00 至 %s 18:00:00</div>"
        % (today, today))
ts = PK.parse_open_time(page)
check(ts is not None, "能解析出开放时间", str(ts))
if ts:
    lt = time.localtime(ts)
    check(lt.tm_hour == 12 and lt.tm_min == 30, "时分正确",
          "%02d:%02d" % (lt.tm_hour, lt.tm_min))
page2 = "<div>选课时间：2026/3/5 8:00</div>"
ts2 = PK.parse_open_time(page2)
check(ts2 is not None and time.localtime(ts2).tm_mon == 3,
      "兼容 2026/3/5 这种写法")
check(PK.parse_open_time("<div>没有任何时间</div>") is None,
      "读不到时返回 None（那就持续投）")
# 关键：不能把无关日期误当成开放时间
check(PK.parse_open_time("<div>公告：2026-01-01 放假</div>") is None,
      "不相关的日期不会被误抓")

# --------------------------------------------------------------------------
# 3. 课程解析与匹配
# --------------------------------------------------------------------------
print("\n=== 3. 课程解析与匹配 ===")
SAMPLE = """<table>
<tr><td>人工智能导论</td><td>刘明</td><td>周一第1-2节</td><td>3.0</td>
    <td>60</td><td>0</td>
    <td><a onclick="xsxk('jxb_id','JXB001','kch_id','K001')">选课</a></td></tr>
<tr><td>高等数学A</td><td>陈红</td><td>周二第3-4节</td><td>5.0</td>
    <td>60</td><td>0</td>
    <td><a onclick="xsxk('jxb_id','JXB002','kch_id','K002')">选课</a></td></tr>
</table>"""
cs = PK.parse_courses(SAMPLE)
check(len(cs) == 2, "解析出 2 门课", "%d" % len(cs))
if cs:
    check(cs[0].jxb_id == "JXB001", "jxb_id 正确", cs[0].jxb_id)
    check(cs[0].kch_id == "K001", "kch_id 正确", cs[0].kch_id)
    check("人工智能导论" in cs[0].name, "课程名正确", cs[0].name)
    check("刘明" in cs[0].teacher, "教师正确", cs[0].teacher)
check(len(PK.match(cs, "人工智能")) == 1, "按课程名匹配")
check(len(PK.match(cs, "陈红")) == 1, "按教师匹配")
check(len(PK.match(cs, "JXB002")) == 1, "按 jxb_id 匹配")
check(len(PK.match(cs, "人工智能, 刘明")) == 1, "逗号分隔多条件")
check(PK.match(cs, "不存在xyz") == [], "无匹配返回空")
check(PK.match(cs, "") == cs, "空关键词返回全部")

# --------------------------------------------------------------------------
# 4. 地址拼接（回归测试）
# --------------------------------------------------------------------------
print("\n=== 4. 提交地址拼接（回归）===")
cli = OpenZfClient(Config(base_url="http://example.com/jwglxt",
                          username="u", password="p"))
check(cli._select_referer("xsxk").count("/jwglxt") == 1,
      "referer 里 /jwglxt 只出现一次", cli._select_referer("xsxk"))
check("/jwglxt/jwglxt" not in cli._select_referer("xsxk"),
      "referer 没有重复前缀")
# _request 的拼接规则：base_url + path
built = cli.cfg.base_url + "/xsxk/xsxk_operate.html"
check(built.count("/jwglxt") == 1, "提交地址没有重复前缀", built)
check(OpenZfClient._SELECT_LEAVES[0] == "xsxk_operate",
      "候选叶子列表没变")

# --------------------------------------------------------------------------
print()
print("=" * 60)
print("失败项：%d" % len(FAILS))
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)
