"""核心场景测试：选课在抢课过程中突然开放。

这是整个程序的价值所在 —— 必须验证：
  1. 未开放时保持正常节奏轮询（不退避放慢）
  2. 一旦开放，立刻发现并提交
  3. 提交「已满」继续等，出现空位再抢到
  4. 服务器报错时才退避
"""
import os
import sys
import threading
import time

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)
sys.path.insert(0, os.path.join(D, "libs"))

import zf_course as Z

ok = True


def chk(label, got, want):
    global ok
    st = "PASS" if got == want else "FAIL"
    if got != want:
        ok = False
    print("[%s] %s: got=%r want=%r" % (st, label, got, want))


class FakeSource:
    """可控的课程数据源。"""

    def __init__(self, script):
        # script: 每轮返回的元素，('empty',) / ('fail',) / ('courses', [..])
        self.script = list(script)
        self.i = 0
        self.calls = []
        self.last_ok = False

    def fetch(self, prefer="auto", dump_path=None):
        self.i += 1
        item = self.script[min(self.i - 1, len(self.script) - 1)]
        self.calls.append(item[0])
        if item[0] == "empty":
            self.last_ok = True
            return [], "页面正常但暂无课程"
        if item[0] == "fail":
            self.last_ok = False
            return [], "读取失败"
        self.last_ok = True
        return list(item[1]), "JSON 接口"


class FakeEngine:
    """可控的提交引擎：按预设序列返回结果。"""

    def __init__(self, results):
        self.results = list(results)
        self.submitted = []

    def submit(self, course):
        self.submitted.append(course.kcmc)
        r = self.results.pop(0) if self.results else ("retry", "已满")
        return Z.SelectResult(ok=(r[0] == "ok"), message=r[1], course=course)


def C(name, teacher="张伟", idx=1, already=False):
    return Z.Course(index=idx, kch="B111", kcmc=name, teacher=teacher,
                    cells=["B111", name, teacher, "周一1-2", "10/50"],
                    already=already)


def run(script, results, **kw):
    events = []
    src = FakeSource(script)
    eng = FakeEngine(results)
    runner = Z.GrabRunner(src, eng, on_event=lambda t, m: events.append((t, m)),
                          **kw)
    res = runner.run()
    return res, src, eng, events


print("=" * 62)
print("场景 1：跑 5 轮都没课，第 6 轮突然开放 -> 应立刻抢到")
print("=" * 62)
script = [("empty",)] * 5 + [("courses", [C("目标课")])] * 3
res, src, eng, ev = run(script, [("ok", "选课成功")],
                        name="目标课", interval=0.05)
print("   轮数:", res["rounds"], " 成功:", res["success"])
chk("在第 6 轮抢到", res["rounds"], 6)
chk("成功列表非空", len(res["success"]), 1)
chk("reason=grabbed", res["reason"], "grabbed")
chk("提交了目标课", eng.submitted, ["目标课"])
chk("有『检测到课程数据』提示",
    any("检测到课程数据" in m for _, m in ev), True)

print()
print("=" * 62)
print("场景 2：未开放期间不能退避（轮数要跟得上）")
print("=" * 62)
print("   注：interval 有 0.3 秒安全下限（防止把服务器打垮）")
script = [("empty",)] * 12 + [("courses", [C("目标课")])] * 2
t0 = time.time()
res2, src2, eng2, ev2 = run(script, [("ok", "选课成功")],
                            name="目标课", interval=0.05)
dt = time.time() - t0
print("   轮数:", res2["rounds"], " 耗时 %.2fs" % dt)
chk("第 13 轮抢到（说明前 12 轮都保持节奏）", res2["rounds"], 13)
chk("12 轮在 5 秒内完成（未退避到 8 秒档）", dt < 5.0, True)

print()
print("=" * 62)
print("场景 2b：对比『退避』场景，确认未开放时明显更快")
print("=" * 62)
# 同样 12 轮，但都是读取失败 -> 应该慢得多
script_b = [("fail",)] * 12 + [("courses", [C("目标课")])] * 2
t0 = time.time()
res2b, _, _, _ = run(script_b, [("ok", "选课成功")], name="目标课",
                     interval=0.3, max_cool=1.0)
dt_b = time.time() - t0
print("   读取失败 12 轮耗时: %.2fs" % dt_b)
print("   页面正常 12 轮耗时: %.2fs" % dt)
chk("失败场景确实更慢（说明退避生效）", dt_b > dt, True)

print()
print("=" * 62)
print("场景 3：服务器报错时才退避")
print("=" * 62)
script = [("fail",)] * 6 + [("courses", [C("目标课")])] * 2
t0 = time.time()
res3, src3, eng3, ev3 = run(script, [("ok", "选课成功")],
                            name="目标课", interval=0.05, max_cool=0.2)
dt3 = time.time() - t0
print("   轮数:", res3["rounds"], " 耗时 %.2fs" % dt3)
chk("报错后退避（耗时明显大于 6*0.05）", dt3 > 0.3, True)
chk("恢复后有课程仍能抢到", len(res3["success"]), 1)

print()
print("=" * 62)
print("场景 4：一直「已满」，出现空位后抢到")
print("=" * 62)
script = [("courses", [C("热门课")])] * 10
res4, src4, eng4, ev4 = run(
    script,
    [("retry", "该教学班人数已满")] * 3 + [("ok", "选课成功")],
    name="热门课", interval=0.05)
print("   轮数:", res4["rounds"], " 提交次数:", len(eng4.submitted))
chk("最终抢到", len(res4["success"]), 1)
chk("提交了 4 次（3 次已满 + 1 次成功）", len(eng4.submitted), 4)
chk("失败记录里有『已满』", any("已满" in f for f in res4["failed"]), True)

print()
print("=" * 62)
print("场景 5：课程已在课表中 -> 直接算成功，不重复提交")
print("=" * 62)
script = [("courses", [C("已有课", already=True)])] * 3
res5, src5, eng5, ev5 = run(script, [], name="已有课", interval=0.05)
chk("算作成功", len(res5["success"]), 1)
chk("reason=all_already", res5["reason"], "all_already")
chk("没有发起提交", len(eng5.submitted), 0)

print()
print("=" * 62)
print("场景 6：手动停止")
print("=" * 62)
stop = threading.Event()
script = [("empty",)] * 100
src6 = FakeSource(script)
eng6 = FakeEngine([])
runner6 = Z.GrabRunner(src6, eng6, name="X", interval=0.05,
                       stop_flag=stop, on_event=lambda t, m: None)


def stopper():
    time.sleep(0.4)
    stop.set()


threading.Thread(target=stopper, daemon=True).start()
t0 = time.time()
res6 = runner6.run()
dt6 = time.time() - t0
print("   轮数:", res6["rounds"], " 耗时 %.2fs" % dt6)
chk("停止后退出", res6["reason"], "stopped")
chk("停止及时(<3s)", dt6 < 3.0, True)

print()
print("=" * 62)
print("场景 7：达到轮数上限")
print("=" * 62)
res7, src7, eng7, ev7 = run([("empty",)] * 100, [], name="X",
                            interval=0.02, max_rounds=5)
chk("轮数不超过 5", res7["rounds"] <= 5, True)
chk("reason=reached_max_rounds", res7["reason"], "reached_max_rounds")

print()
print("=" * 62)
print("场景 8：页面在开放前打开（死页面）-> 应自动刷新")
print("=" * 62)
loads = {"n": 0}


def loader():
    loads["n"] += 1
    return True


# 前 30 轮都没课，第 31 轮才有 —— 期间应该触发过刷新
script8 = [("empty",)] * 30 + [("courses", [C("目标课")])] * 2
ev8 = []
src8 = FakeSource(script8)
eng8 = FakeEngine([("ok", "选课成功")])
runner8 = Z.GrabRunner(src8, eng8, name="目标课", interval=0.3,
                       on_event=lambda t, m: ev8.append((t, m)),
                       page_loader=loader, reload_every=10)
t0 = time.time()
res8 = runner8.run()
print("   轮数: %d  刷新次数: %d  耗时 %.1fs"
      % (res8["rounds"], loads["n"], time.time() - t0))
chk("触发了自动刷新", loads["n"] >= 2, True)
chk("刷新次数合理(每10轮一次)", loads["n"] == 3, True)
chk("最终仍抢到", len(res8["success"]), 1)
chk("日志里有刷新提示", any("重新加载选课页" in m for _, m in ev8), True)

print()
print("=" * 62)
print("场景 9：没有 page_loader 时不能崩")
print("=" * 62)
res9, _, _, _ = run([("empty",)] * 15, [], name="X", interval=0.3,
                    max_rounds=12)
chk("正常结束", res9["reason"], "reached_max_rounds")
chk("轮数=12", res9["rounds"], 12)

print()
print("=== 全部通过 ===" if ok else "=== 有失败项 ===")
sys.exit(0 if ok else 1)
