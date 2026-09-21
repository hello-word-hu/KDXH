"""验证 last_ok 的区分能力 —— 这决定抢课时会不会错过开放瞬间。"""
import os
import sys
import tempfile
import time

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
sys.path.insert(0, os.path.join(D, "libs"))

import zf_browser
import zf_course as Z

ok = True


def chk(label, got, want):
    global ok
    st = "PASS" if got == want else "FAIL"
    if got != want:
        ok = False
    print("[%s] %s: got=%r want=%r" % (st, label, got, want))


print("=" * 62)
print("场景 A：页面正常但没有课程表格")
print("=" * 62)
PAGE_EMPTY = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div>学年 学期 本学期选课要求 总学分最低 0 最高 100</div>
<div>......已到最后......</div>
</body></html>"""

_TMP = tempfile.gettempdir()
path = os.path.join(_TMP, "zf_test_empty.html")
with open(path, "w", encoding="utf-8") as fh:
    fh.write(PAGE_EMPTY)

d, kind = zf_browser._build_driver("edge", True, None)
try:
    d.get("file:///" + path.replace("\\", "/"))
    time.sleep(0.5)
    src = Z.CourseSource(driver=d, base_url="http://x", log=lambda m: None)
    courses, how = src.fetch(prefer="dom")
    chk("A 无课程", courses, [])
    chk("A last_ok=True（页面可读）", src.last_ok, True)
    print("   来源说明:", how)

    print()
    print("=" * 62)
    print("场景 B：页面有课程表格（模拟已开课）")
    print("=" * 62)
    PAGE_FULL = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<table id="kbtable" class="table">
<tr><th>课程代码</th><th>课程名称</th><th>教师</th><th>上课时间</th><th>选课人数</th><th>操作</th></tr>
<tr><td>B1111111</td><td>课程甲</td><td>张伟</td><td>周一1-2</td><td>10/50</td>
    <td><a href="javascript:void(0)">选课</a></td></tr>
</table></body></html>"""
    path2 = os.path.join(_TMP, "zf_test_full.html")
    with open(path2, "w", encoding="utf-8") as fh:
        fh.write(PAGE_FULL)
    d.get("file:///" + path2.replace("\\", "/"))
    time.sleep(0.5)
    src2 = Z.CourseSource(driver=d, base_url="http://x", log=lambda m: None)
    courses2, how2 = src2.fetch(prefer="dom")
    chk("B 读到 1 门课", len(courses2), 1)
    chk("B last_ok=True", src2.last_ok, True)
    print("   来源说明:", how2)

    print()
    print("=" * 62)
    print("场景 C：没有浏览器（读取失败）")
    print("=" * 62)
    src3 = Z.CourseSource(http_client=None, driver=None, base_url="http://x",
                          log=lambda m: None)
    courses3, how3 = src3.fetch()
    chk("C 无课程", courses3, [])
    chk("C last_ok=False（该退避）", src3.last_ok, False)
    print("   来源说明:", how3)

    print()
    print("=" * 62)
    print("场景 D：HTTP 接口可达但 0 条数据（模拟未开课）")
    print("=" * 62)

    class FakeHttp:
        """模拟真实服务器：返回 totalResult=0 的 jqGrid 响应。"""
        def _request(self, url, data=None, ajax=False, **kw):
            body = ('{"jgpxzd":"1","pageable":true,'
                    '"queryModel":{"totalResult":0},"totalResult":"0"}')
            return url, body, 910

        def fetch(self, path, **kw):
            return "<html><body>" + "x" * 500 + "</body></html>"

    src4 = Z.CourseSource(http_client=FakeHttp(), driver=None,
                          base_url="http://x", log=lambda m: None)
    courses4, how4 = src4.fetch(prefer="json")
    chk("D 无课程", courses4, [])
    chk("D last_ok=True（接口可读）", src4.last_ok, True)
    print("   来源说明:", how4)

    print()
    print("=" * 62)
    print("场景 E：HTTP 全部报错（服务器挂了）")
    print("=" * 62)

    class DeadHttp:
        def _request(self, url, data=None, ajax=False, **kw):
            raise TimeoutError("模拟超时")

        def fetch(self, path, **kw):
            raise TimeoutError("模拟超时")

    src5 = Z.CourseSource(http_client=DeadHttp(), driver=None,
                          base_url="http://x", log=lambda m: None)
    courses5, how5 = src5.fetch(prefer="json")
    chk("E 无课程", courses5, [])
    chk("E last_ok=False（该退避）", src5.last_ok, False)
    print("   来源说明:", how5)
finally:
    zf_browser.quit_driver(d)
    for f in ("zf_test_empty.html", "zf_test_full.html"):
        try:
            os.remove(os.path.join(_TMP, f))
        except OSError:
            pass

print()
print("=== 全部通过 ===" if ok else "=== 有失败项 ===")
sys.exit(0 if ok else 1)
