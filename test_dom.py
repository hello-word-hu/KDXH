import os
"""在真实浏览器里注入仿真课程表格，验证 DOM 解析逻辑。"""
import sys, time

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
sys.path.insert(0, D + r"\libs")
import zf_browser, zf_client, zf_course as Z

BASE = "http://jwxt.cumtxh.cn/jwglxt"
ok = True


def chk(label, got, want):
    global ok
    st = "PASS" if got == want else "FAIL"
    if got != want:
        ok = False
    print("[%s] %s: got=%r want=%r" % (st, label, got, want))


r = zf_browser.login_with_password_by_browser(
    user="2226021196", password="-Hxy080701", base_url=BASE, headless=True)
c = zf_client.OpenZfClient(zf_client.Config(base_url=BASE, verbose=False))
c.adopt_cookies(r["cookies"])

d, kind = zf_browser._build_driver("edge", True, None)
try:
    zf_browser.goto(d, BASE + "/", log=lambda m: None)
    for ck in c.jar:
        try:
            d.add_cookie({"name": ck.name, "value": ck.value or "",
                          "path": ck.path or "/"})
        except Exception:
            pass
    zf_browser.goto(d, BASE + "/xsxk/zzxkyzb_cxZzxkYzbIndex.html?gnmkdm=N253512",
                    log=lambda m: None)
    time.sleep(1)

    # 注入仿真表格
    d.execute_script(r"""
    var old = document.getElementById('__faketbl');
    if (old) old.remove();
    var t = document.createElement('table');
    t.id = '__faketbl';
    t.className = 'table';
    t.innerHTML = ''
      + '<tr><th>课程代码</th><th>课程名称</th><th>教师</th>'
      + '<th>上课时间</th><th>选课人数</th><th>操作</th></tr>'
      + '<tr><td>B1234567</td><td>高等数学A(1)</td><td>张伟</td>'
      + '<td>周一3-4节</td><td>60/60</td>'
      + '<td><a href="javascript:void(0)" onclick="return false;">选课</a></td></tr>'
      + '<tr><td>B7654321</td><td>Python程序设计</td><td>李娜</td>'
      + '<td>周三11-13节</td><td>45/50</td>'
      + '<td><a href="javascript:void(0)" onclick="return false;">选课</a></td></tr>'
      + '<tr><td>B9999999</td><td>线性代数</td><td>张伟</td>'
      + '<td>周五5-6节</td><td>30/30</td>'
      + '<td><a href="javascript:void(0)" onclick="return false;">退选</a></td></tr>';
    document.body.appendChild(t);
    return t.querySelectorAll('tr').length;
    """)
    print("已注入仿真表格\n")

    src = Z.CourseSource(http_client=None, driver=d, base_url=BASE,
                         log=lambda m: print("   ", m))
    courses, dom_ok = src.fetch_dom()

    chk("解析到课程数", len(courses), 3)
    chk("页面读取成功标记", dom_ok, True)
    if courses:
        by_code = {c.kch: c for c in courses}
        chk("课程代码", "B1234567" in by_code, True)
        c1 = by_code.get("B1234567")
        if c1:
            chk("课程名称", c1.kcmc, "高等数学A(1)")
            chk("教师", c1.teacher, "张伟")
            chk("上课时间", c1.time_text, "周一3-4节")
            chk("容量", c1.capacity, "60/60")
            chk("按钮文字", c1.button_text, "选课")
            chk("未标记已选", c1.already, False)
        c3 = by_code.get("B9999999")
        if c3:
            chk("退选行标记已选", c3.already, True)

    # 匹配测试
    chk("按教师(张伟)命中", len(Z.match_courses(courses, teacher="张伟")), 2)
    chk("按代码命中", len(Z.match_courses(courses, code="B7654321")), 1)
    chk("按名称(数学)命中", len(Z.match_courses(courses, name="数学")), 1)
    chk("无匹配", len(Z.match_courses(courses, teacher="不存在")), 0)

    # 清理
    d.execute_script("var e=document.getElementById('__faketbl'); if(e) e.remove();")
finally:
    zf_browser.quit_driver(d)

print()
print("=== 全部通过 ===" if ok else "=== 有失败项 ===")
sys.exit(0 if ok else 1)

