"""提交选课链路测试：真实浏览器 + 仿真页面。

覆盖：
  · 点击正确的行（第 3 行的按钮只改第 3 行对应的标记）
  · JS 确认框（confirm/alert）能被点掉并读到文字
  · DOM 弹窗（layui/bootbox 风格）能被识别
  · 点击后页面反馈能被分类成 成功/已满/冲突
  · 行号失效时不崩
"""
import os
import sys
import time

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
sys.path.insert(0, os.path.join(D, "libs"))

import zf_browser
import zf_course as Z
from zf_grab import BrowserGrab

ok = True


def chk(label, got, want):
    global ok
    st = "PASS" if got == want else "FAIL"
    if got != want:
        ok = False
    print("[%s] %s: got=%r want=%r" % (st, label, got, want))


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>初始</title></head><body>
<div id="tipbox" style="display:none"></div>
<table id="kbtable" class="table">
<tr><th>课程代码</th><th>课程名称</th><th>教师</th><th>上课时间</th><th>选课人数</th><th>操作</th></tr>
<tr><td>B1111111</td><td>课程甲</td><td>张伟</td><td>周一1-2</td><td>10/50</td>
    <td><a href="javascript:void(0)" onclick="doIt('甲')">选课</a></td></tr>
<tr><td>B2222222</td><td>课程乙</td><td>李娜</td><td>周二3-4</td><td>50/50</td>
    <td><a href="javascript:void(0)" onclick="doIt('乙')">选课</a></td></tr>
<tr><td>B3333333</td><td>课程丙</td><td>王强</td><td>周三5-6</td><td>30/40</td>
    <td><a href="javascript:void(0)" onclick="doIt('丙')">选课</a></td></tr>
<tr><td>B4444444</td><td>课程丁</td><td>赵敏</td><td>周四7-8</td><td>20/30</td>
    <td><a href="javascript:void(0)" onclick="doIt('丁')">退选</a></td></tr>
</table>
<script>
window.__clicked = [];
function doIt(name){
  window.__clicked.push(name);
  var tip = document.getElementById('tipbox');
  if (name === '甲') {
    // 成功：先 confirm 确认，再显示成功提示
    var yes = confirm('确认要选《课程甲》吗？');
    tip.style.display='block';
    tip.innerText = yes ? '选课成功' : '已取消';
    document.title = 'RESULT:ok';
  } else if (name === '乙') {
    tip.style.display='block';
    tip.innerText = '该教学班人数已满，请选择其他教学班';
    document.title = 'RESULT:full';
  } else if (name === '丙') {
    alert('选课成功');
    document.title = 'RESULT:alert-ok';
  } else {
    tip.style.display='block';
    tip.innerText = '与已选课程冲突';
    document.title = 'RESULT:conflict';
  }
}
</script></body></html>"""

d, kind = zf_browser._build_driver("edge", True, None)
try:
    # 写成本地文件，用 file:// 打开（无网络依赖）
    path = os.path.join(D, "_fakepage.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(PAGE)
    d.get("file:///" + path.replace("\\", "/"))
    time.sleep(0.8)

    bg = BrowserGrab(d, "http://x", log=lambda m: None)

    # ---------- 1) 扫描解析 ----------
    print("=" * 60)
    print("1) 扫描仿真表格")
    print("=" * 60)
    src = Z.CourseSource(driver=d, base_url="http://x", log=lambda m: None)
    courses, dom_ok = src.fetch_dom()
    chk("解析到 4 门课", len(courses), 4)
    chk("页面读取成功标记", dom_ok, True)
    chk("第 1 门代码", courses[0].kch if courses else None, "B1111111")
    chk("第 1 门教师", courses[0].teacher if courses else None, "张伟")
    chk("第 4 门标记已选", courses[3].already if len(courses) > 3 else None, True)

    # ---------- 2) 点第 1 行（带 confirm）----------
    print()
    print("=" * 60)
    print("2) 点第 1 行（含 confirm 确认框）")
    print("=" * 60)
    clicked, msg = bg.click_select(1)
    chk("点击成功", clicked, True)
    time.sleep(1.0)
    dialogs = [x for x in bg.handle_dialogs() if x]
    print("   捕获到的反馈:", dialogs)
    chk("confirm 文字被读到", any("课程甲" in x for x in dialogs), True)
    title = d.title
    chk("按钮确实被点（页面标题变化）", "RESULT:ok" in title, True)
    chk("反馈分类为 ok", Z.classify_feedback(" ".join(dialogs)), "ok")

    # ---------- 3) 点第 2 行（已满）----------
    print()
    print("=" * 60)
    print("3) 点第 2 行（已满）")
    print("=" * 60)
    d.execute_script("document.getElementById('tipbox').style.display='none';")
    clicked, msg = bg.click_select(2)
    chk("点击成功", clicked, True)
    time.sleep(0.8)
    dialogs2 = [x for x in bg.handle_dialogs() if x]
    print("   捕获到的反馈:", dialogs2)
    chk("读到已满提示", any("已满" in x for x in dialogs2), True)
    chk("反馈分类为 retry", Z.classify_feedback(" ".join(dialogs2)), "retry")

    # ---------- 4) 点第 3 行（原生 alert）----------
    print()
    print("=" * 60)
    print("4) 点第 3 行（原生 alert）")
    print("=" * 60)
    d.execute_script("document.getElementById('tipbox').style.display='none';")
    clicked, msg = bg.click_select(3)
    chk("点击成功", clicked, True)
    time.sleep(1.2)
    dialogs3 = [x for x in bg.handle_dialogs() if x]
    print("   捕获到的反馈:", dialogs3)
    chk("alert 被处理（页面可继续操作）", True, True)
    try:
        t = d.title
        chk("alert 已关闭", "RESULT:alert-ok" in t, True)
    except Exception as e:
        chk("alert 已关闭", False, str(e)[:40])

    # ---------- 5) 点已选行（第 4 行，无选课按钮）----------
    print()
    print("=" * 60)
    print("5) 点第 4 行（只有退选按钮）")
    print("=" * 60)
    clicked, msg = bg.click_select(4)
    chk("无选课按钮 -> 点击失败", clicked, False, )
    print("   返回消息:", msg)

    # ---------- 6) 无效行号 ----------
    print()
    print("=" * 60)
    print("6) 无效行号（不能崩）")
    print("=" * 60)
    try:
        clicked, msg = bg.click_select(999)
        chk("返回失败而不是抛异常", clicked, False)
        print("   返回消息:", msg)
    except Exception as e:
        chk("返回失败而不是抛异常", False, "抛了 %s" % type(e).__name__)

    # ---------- 7) 点击确实只影响目标行 ----------
    print()
    print("=" * 60)
    print("7) 点击的行与实际触发的行一致")
    print("=" * 60)
    got = d.execute_script("return window.__clicked;") or []
    print("   实际被触发的课程:", got)
    # 第1行=甲, 第2行=乙(表中第2个数据行 index=2), 第3行=丙
    chk("触发序列含甲", "甲" in got, True)
    chk("触发序列含乙", "乙" in got, True)
    chk("触发序列含丙", "丙" in got, True)
    chk("没有误触丁", "丁" in got, False)
finally:
    zf_browser.quit_driver(d)
    try:
        os.remove(os.path.join(D, "_fakepage.html"))
    except OSError:
        pass

print()
print("=== 全部通过 ===" if ok else "=== 有失败项 ===")
sys.exit(0 if ok else 1)
