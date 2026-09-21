import os
import sys

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
sys.path.insert(0, D + r"\libs")
import zf_course as Z

ok = True


def chk(label, got, want):
    global ok
    st = "PASS" if got == want else "FAIL"
    if got != want:
        ok = False
    print("[%s] %s: got=%r want=%r" % (st, label, got, want))


print("=" * 60)
print("1) 反馈分类")
print("=" * 60)
cases = [
    ("选课成功", "ok"),
    ("{\"flag\":\"1\",\"msg\":\"选课成功\"}", "ok"),
    ("该教学班人数已满", "retry"),
    ("容量已满，请选择其他教学班", "retry"),
    ("余量不足", "retry"),
    ("已选过该课程", "already"),
    ("已经选过", "already"),
    ("不在选课时间内", "stop"),
    ("无选课权限", "stop"),
    ("与已选课程冲突", "stop"),
    ("", "unknown"),
    ("一些没见过的提示", "unknown"),
]
for text, want in cases:
    chk("分类 %r" % text[:22], Z.classify_feedback(text), want)

print()
print("=" * 60)
print("2) 课程匹配")
print("=" * 60)
CS = [
    Z.Course(index=0, kch="202610001001", kcmc="大学生职业生涯发展与规划",
             teacher="刘澜", cells=["202610001001", "大学生职业生涯发展与规划", "刘澜", "周一3-4"]),
    Z.Course(index=1, kch="B7654321", kcmc="Python程序设计",
             teacher="冯庆华", cells=["B7654321", "Python程序设计", "冯庆华", "周三11-13"]),
    Z.Course(index=2, kch="C1111111", kcmc="高等数学A(1)",
             teacher="余俊", cells=["C1111111", "高等数学A(1)", "余俊", "周二3-4"]),
]
chk("无条件 -> 全部", len(Z.match_courses(CS)), 3)
chk("按代码", len(Z.match_courses(CS, code="B7654321")), 1)
chk("按代码(部分)", len(Z.match_courses(CS, code="7654")), 1)
chk("按名称", len(Z.match_courses(CS, name="Python")), 1)
chk("按名称(中文部分)", len(Z.match_courses(CS, name="数学")), 1)
chk("按教师", len(Z.match_courses(CS, teacher="余俊")), 1)
chk("按教师(单字)", len(Z.match_courses(CS, teacher="张")), 0)
chk("多条件AND命中", len(Z.match_courses(CS, name="高等", teacher="余")), 1)
chk("多条件AND不命中", len(Z.match_courses(CS, name="高等", teacher="刘")), 0)
chk("大小写不敏感", len(Z.match_courses(CS, code="b7654321")), 1)
chk("无匹配", len(Z.match_courses(CS, teacher="不存在")), 0)
chk("类型'不限'不筛选", len(Z.match_courses(CS, course_type="不限")), 3)

print()
print("=" * 60)
print("3) CourseSource（无 http/driver 时不能崩）")
print("=" * 60)
src = Z.CourseSource(http_client=None, driver=None, base_url="http://x",
                     log=lambda m: None)
courses, how = src.fetch()
chk("无依赖时返回空", courses, [])
chk("来源说明", how, "读取失败")
chk("无依赖时 last_ok=False", src.last_ok, False)

print()
print("=" * 60)
print("4) GrabEngine（无 driver 时优雅报错）")
print("=" * 60)
eng = Z.GrabEngine(src, None, log=lambda m: None)
r = eng.submit(CS[0])
chk("无浏览器 -> ok=False", r.ok, False)
chk("有错误说明", bool(r.message), True)

print()
print("=" * 60)
print("5) Course 对象")
print("=" * 60)
c = CS[1]
chk("text() 拼接", c.text(), "B7654321 | Python程序设计 | 冯庆华 | 周三11-13")
chk("already 默认 False", c.already, False)
chk("SelectResult 默认", Z.SelectResult().ok, False)

print()
print("=== 全部通过 ===" if ok else "=== 有失败项 ===")
sys.exit(0 if ok else 1)

