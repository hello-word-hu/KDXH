"""测试自适应能力：从页面读出真实接口 + 自动触发查询。"""
import os
import sys
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


# 仿真页面：带 jQuery 存根 + select + 查询按钮 + 可被"发现"的 grid
PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>选课页仿真</title></head><body>
<select id="xnm" name="xnm">
  <option value="">---请选择---</option>
  <option value="2026">2026-2027</option>
  <option value="2025">2025-2026</option>
</select>
<select id="xqm" name="xqm">
  <option value="">---请选择---</option>
  <option value="3">1</option>
  <option value="1">2</option>
</select>
<button id="cxBtn" type="button">查询</button>
<table id="gridTable" class="ui-jqgrid-btable"><tr><td>占位</td></tr></table>
<pre id="log"></pre>
<script>
// ---- 极简 jQuery 存根（只实现本测试用到的部分）----
window.__clicked = [];
window.__gridReloaded = false;
function J(selOrEl){
  var els;
  if (typeof selOrEl === 'string') {
    els = Array.prototype.slice.call(document.querySelectorAll(selOrEl));
  } else if (selOrEl && selOrEl.nodeType) {
    els = [selOrEl];
  } else { els = []; }
  var o = {
    length: els.length,
    each: function(fn){ els.forEach(function(e,i){ fn.call(e,i,e); }); return o; },
    text: function(){ return els.length ? (els[0].innerText||'') : ''; },
    val: function(){ return els.length ? els[0].value : ''; },
    is: function(){ return els.length > 0; },
    trigger: function(ev){
      els.forEach(function(e){
        if (ev === 'click') { window.__clicked.push(e.id || e.innerText); }
        if (ev === 'reloadGrid') { window.__gridReloaded = true; }
        // 模拟查询：把 grid 的 url 暴露出来
        if (ev === 'change') { /* nothing */ }
      });
      return o;
    },
    jqGrid: function(cmd){
      if (cmd === 'getGridParam') {
        return '/xsxk/zzxkyzb_cxZzxkYzb.html';   // ← 被"发现"的地址
      }
      return o;
    }
  };
  o[0] = els[0];
  return o;
}
J.fn = {};
window.jQuery = J;
window.$ = J;
</script></body></html>"""

d, kind = zf_browser._build_driver("edge", True, None)
try:
    path = os.path.join(D, "_gridpage.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(PAGE)
    d.get("file:///" + path.replace("\\", "/"))
    time.sleep(0.6)

    print("=" * 60)
    print("1) discover_grid_url：从页面读出真实接口")
    print("=" * 60)
    u = Z.discover_grid_url(d)
    print("   发现:", u)
    chk("成功读出 grid 地址", u, "/xsxk/zzxkyzb_cxZzxkYzb.html")
    chk("无 driver 时返回 None", Z.discover_grid_url(None), None)

    print()
    print("=" * 60)
    print("2) prime_select_page：自动选学年学期 + 点查询")
    print("=" * 60)
    acts = Z.prime_select_page(d)
    print("   动作:", acts)
    chk("选中了学年", any("xnm=" in a for a in acts), True)
    chk("选中了学期", any("xqm=" in a for a in acts), True)
    chk("点击了查询按钮", any("查询" in a for a in acts), True)

    # 验证页面状态真的变了
    xn = d.execute_script("return document.getElementById('xnm').value;")
    xq = d.execute_script("return document.getElementById('xqm').value;")
    clicked = d.execute_script("return window.__clicked;") or []
    chk("页面 xnm 已选", xn, "2026")
    chk("页面 xqm 已选", xq, "3")
    chk("按钮确实被点", any("cxBtn" in str(c) for c in clicked), True)

    print()
    print("=" * 60)
    print("3) 已有值时不应重复改动")
    print("=" * 60)
    acts2 = Z.prime_select_page(d)
    print("   第二次动作:", acts2)
    chk("不再改学年",
        any("xnm=" in a for a in acts2), False)

    print()
    print("=" * 60)
    print("4) 没有 jQuery 的页面不能崩")
    print("=" * 60)
    p2 = os.path.join(D, "_plainpage.html")
    with open(p2, "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><html><body><p>"
                 + "普通页面" * 40 + "</p></body></html>")
    d.get("file:///" + p2.replace("\\", "/"))
    time.sleep(0.4)
    chk("discover 返回 None", Z.discover_grid_url(d), None)
    chk("prime 返回空列表", Z.prime_select_page(d), [])
finally:
    zf_browser.quit_driver(d)
    for f in ("_gridpage.html", "_plainpage.html"):
        try:
            os.remove(os.path.join(D, f))
        except OSError:
            pass

print()
print("=== 全部通过 ===" if ok else "=== 有失败项 ===")
sys.exit(0 if ok else 1)
