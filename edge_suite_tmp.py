"""边界/多场景排程测试套件：正常数据、循环引用×Q-time×交接班/禁机、真·环、多环混合。
每个场景：schedule() 单跑 + schedule_optimized(seed 0) 多轮，validate 0 错误、无雪崩、告警合理。
"""
import sys, os
sys.path.insert(0, "/workspace/半导体高优先级批次自动排程")
from datetime import datetime, timedelta
import data_loader
from models import Lot, LotConstraint, ManualAdjust
from scheduler import schedule, FAR_FUTURE
from optimizer import schedule_optimized
from validation import validate_schedule

REPRO = "/tmp/repro_iou"
TOOLS = "/workspace/半导体高优先级批次自动排程/tools/data"
BASE = datetime(2026, 9, 1, 9, 30)

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}  {detail}")

# ---------------- 数据加载（repro 与 tools 两套） ----------------
def load_ctx(d):
    flows = data_loader.load_flow(os.path.join(d, "flow.csv"))
    fm = data_loader.get_product_flow_map(flows)
    allf = [s for fl in fm.values() for s in fl]
    qtimes = data_loader.load_qtime(os.path.join(d, "qtime.csv"))
    cts = data_loader.auto_repair_step_ct(flows, data_loader.load_step_ct(os.path.join(d, "step_ct.csv")), None)
    ct = data_loader.build_ct_lookup(cts)
    scfg = data_loader.load_shift_config(os.path.join(d, "shift_config.csv"))
    st = sorted(tuple(map(int, x.start_time_str.split(":"))) for x in scfg if getattr(x, "start_time_str", None))
    ctx = dict(
        flows=allf, fm=fm, qtimes=qtimes, ct=ct, st=st,
        pw=data_loader.load_priority_wait(os.path.join(d, "priority_wait.csv")),
        eqp=data_loader.load_eqp_constraints(os.path.join(d, "eqp_constraint.csv")),
        tw=data_loader.load_step_time_windows(os.path.join(d, "step_time_window.csv")),
        sc=data_loader.load_shift_change_times(os.path.join(d, "shift_change_time.csv")),
        sls=data_loader.load_special_lot_step(os.path.join(d, "special_lot_step.csv")),
        fq=data_loader.load_ftf_qty_change(os.path.join(d, "ftf_qty_change.csv")),
        se=data_loader.load_special_eqp(os.path.join(d, "special_eqp.csv")),
        lc=data_loader.load_lot_constraints(os.path.join(d, "lot_constraints.csv")),
    )
    return ctx

R = load_ctx(REPRO)
T = load_ctx(TOOLS)

def mk(name, product, qty, priority, cur_step, refs=(), start=BASE, state="wait", rt=0):
    return Lot(lot_name=name, carrier_id=f"C{name}", product_name=product, qty=qty,
               priority=priority, current_step_name=cur_step, target_step=None,
               lot_state=state, running_time=rt, start_time=start, references=list(refs))

def ref(lot, ref_lot, ref_step, start_step, mod="0"):
    return LotConstraint(lot_name=lot, reference_lot=ref_lot, reference_step=ref_step,
                         start_mod=mod, start_step=start_step)

def run_case(name, ctx, lots, manual_adjusts=None, seeds=(0,), max_iterations=8):
    print(f"\n===== {name} =====")
    le, ee, qa, meta = schedule_optimized(
        lots=[__import__('copy').copy(l) for l in lots], flows=ctx["flows"], ct_lookup=ctx["ct"],
        qtimes=ctx["qtimes"], shift_times=ctx["st"], ftf_qty_change=ctx["fq"],
        special_lot_step_lookup=ctx["sls"], priority_wait_map=ctx["pw"],
        eqp_constraints=ctx["eqp"], step_time_window_constraints=ctx["tw"],
        shift_change_times=ctx["sc"], manual_adjusts=manual_adjusts,
        special_eqp_map=ctx["se"], lot_constraints=ctx["lc"],
        resolve_max_iterations=10, max_iterations=max_iterations, seed=seeds[0])
    errs = validate_schedule(le, ee, qa, lots, ctx["flows"], ctx["qtimes"],
                             lot_constraints=ctx["lc"], shift_times=ctx["st"], special_eqp_map=ctx["se"])
    print(f"  valid={meta.get('valid_iterations')}/{meta.get('total_iterations')}  errs={len(errs)}")
    for e in errs[:4]:
        print("   -", getattr(e, "message", str(e))[:130])
    ws = meta.get("schedule_warnings") or []
    print(f"  warnings({len(ws)}):")
    for w in ws:
        print("    *", w[:120])
    # 无雪崩：所有排程步骤不得逼近 FAR_FUTURE
    max_t = max((e.end_time for e in le), default=BASE)
    check(f"{name}: 无雪崩(全部步骤 < 14天)", max_t < BASE + timedelta(days=14),
          f"max_end={max_t}")
    check(f"{name}: 全量校验 0 错误", len(errs) == 0, str(errs[:2]))
    # 报告每 lot 首个与最后一个步骤
    from collections import defaultdict
    by = defaultdict(list)
    for e in le:
        by[e.lot_name].append(e)
    for ln, es in sorted(by.items()):
        es.sort(key=lambda x: x.start_time)
        print(f"    {ln}: 首站 {es[0].step_name} {es[0].start_time:%m/%d %H:%M}  末站 {es[-1].step_name} {es[-1].end_time:%m/%d %H:%M}")
    return le, errs, ws

# ---------------- T1 正常数据（无环、多 lot、同起点） ----------------
lots1 = [
    mk("L1", "A006-MS", 5, (2, 1), "A006-R1-FTF-INPUT-TO-OUTPUT"),
    mk("L2", "A006-D1", 3, (3, 1), "A006-D1-FTF-INPUT-TO-OUTPUT"),
    mk("L3", "A006-RS", 8, (4, 2), "A006-R1-FTF-INPUT-TO-OUTPUT"),
]
run_case("T1 正常: 3独立lot 同起点(FTF链,交接班/禁机约束)", R, lots1)

# ---------------- T2 循环引用 + 松Q-time(7天FTF链) + 交接班 ----------------
iou1 = mk("IOU1", "A006-MS", 13, (3, 1), "A006-R1-FTF-INPUT-TO-OUTPUT",
          refs=[ref("IOU1", "IOU1-f", "A006-D1-FTF-OPUT-FTF", "A006-R1-FTF-OPUT-FTF")])
iou1f = mk("IOU1-f", "A006-D1", 3, (2, 1), "A006-D1-FTF-INPUT-TO-OUTPUT",
           refs=[ref("IOU1-f", "IOU1", "A006-R1-FTF-OPUT-MOUNT", "A006-D1-FTF-OPUT-FTF")])
le2, e2, w2 = run_case("T2 循环引用+松Q+交接班: IOU1(13)↔IOU1-f(3)", R, [iou1, iou1f])
mnt = [e for e in le2 if e.lot_name == "IOU1" and "MOUNT" in e.step_name]
ftf_f = [e for e in le2 if e.lot_name == "IOU1-f" and e.step_name.endswith("OPUT-FTF")]
if mnt: check("T2: IOU1.MOUNT 当天完成(非雪崩)", mnt[0].end_time.date() == BASE.date(), str(mnt[0]))
if ftf_f: check("T2: IOU1-f.FTF 当天完成", ftf_f[0].start_time.date() == BASE.date(), str(ftf_f[0]))

# ---------------- T3 循环引用 + 紧Q-time(UF 240min) + 手动延迟(工具数据) ----------------
t3_lots = data_loader.load_lot_list(os.path.join(TOOLS, "lot_list.csv"),
                                    constraints_filepath=os.path.join(TOOLS, "lot_constraints.csv"))
ma3 = [ManualAdjust(lot_name="PC1", step_name="A005-P1-UF-BAKE",
                    delay_to=datetime(2026, 8, 18, 10, 0), mode="delay")]
run_case("T3 循环引用+紧Q(UF240)+手动延迟: PC↔real", T, t3_lots, manual_adjusts=ma3)

# ---------------- T4 两对环 + 独立 lot 混合 ----------------
ion1 = mk("ION1", "A007-MS", 6, (2, 2), "A007-R1-FTF-INPUT-TO-OUTPUT",
          refs=[ref("ION1", "ION1-f", "A007-D1-FTF-OPUT-FTF", "A007-R1-FTF-OPUT-FTF")])
ion1f = mk("ION1-f", "A007-D1", 4, (2, 3), "A007-D1-FTF-INPUT-TO-OUTPUT",
           refs=[ref("ION1-f", "ION1", "A007-R1-FTF-OPUT-MOUNT", "A007-D1-FTF-OPUT-FTF")])
solox = mk("SOLO", "A006-MS", 2, (5, 1), "A006-R1-FTF-INPUT-TO-OUTPUT")
run_case("T4 两对环+独立lot: IOU1对 + ION1对 + SOLO", R, [iou1, iou1f, ion1, ion1f, solox])

# ---------------- T5 真·循环引用(同step互相等待) → 自然锚点回退 ----------------
a = mk("A5", "A006-MS", 4, (1, 1), "A006-R1-FTF-INPUT-TO-OUTPUT",
       refs=[ref("A5", "B5", "A006-D1-FTF-OPUT-FTF", "A006-R1-FTF-OPUT-FTF")])
b5 = mk("B5", "A006-D1", 4, (1, 2), "A006-D1-FTF-INPUT-TO-OUTPUT",
        refs=[ref("B5", "A5", "A006-R1-FTF-OPUT-FTF", "A006-D1-FTF-OPUT-FTF")])
le5, e5, w5 = run_case("T5 真·循环(同step互等): A5.FTF↔B5.FTF", R, [a, b5])
check("T5: 环雪崩回退被识别并告警",
      any("雪崩" in w or "引用环" in w for w in w5), str(w5[:2]))

# ---------------- T6 紧Q链跨交接班/禁机边界 ----------------
# 用工具数据(UF 240min链)但把起点压到接近禁机时段，观察链内 Q 是否守住
t6_lots = data_loader.load_lot_list(os.path.join(TOOLS, "lot_list.csv"),
                                    constraints_filepath=os.path.join(TOOLS, "lot_constraints.csv"))
ma6 = [ManualAdjust(lot_name="real1", step_name="A005-R1-UF-DISPENSE",
                    delay_to=datetime(2026, 8, 17, 21, 0), mode="delay")]
le6, e6, w6 = run_case("T6 紧Q链(UF240)钉在交接班/禁机边界: real1.DISPENSE->21:00", T, t6_lots, manual_adjusts=ma6)

# ---------------- T7 三 lot 环 A→B→C→A ----------------
c7 = mk("C7", "A006-MS", 3, (1, 3), "A006-R1-FTF-INPUT-TO-OUTPUT",
        refs=[ref("C7", "D7", "A006-D1-FTF-OPUT-FTF", "A006-R1-FTF-OPUT-FTF")])
d7 = mk("D7", "A006-D1", 3, (1, 3), "A006-D1-FTF-INPUT-TO-OUTPUT",
        refs=[ref("D7", "E7", "A006-R1-FTF-OPUT-FTF", "A006-D1-FTF-OPUT-FTF")])
e7 = mk("E7", "A006-RS", 3, (1, 3), "A006-R1-FTF-INPUT-TO-OUTPUT",
        refs=[ref("E7", "C7", "A006-R1-FTF-OPUT-MOUNT", "A006-R1-FTF-OPUT-FTF")])
le7, e7, w7 = run_case("T7 三lot环: C7→D7→E7→C7", R, [c7, d7, e7])
from collections import defaultdict as _dd
_l7 = _dd(list)
for _e in le7:
    _l7[_e.lot_name].append(_e)
check("T7: 三个环内 lot 步骤完整(各15步)", all(len(_l7[n]) >= 15 for n in ("C7", "D7", "E7")),
      {n: len(_l7[n]) for n in ("C7", "D7", "E7")})
check("T7: 全量校验 0 错误", len(e7) == 0, str(e7[:2]))

# ---------------- T8 环外 lot 引用环内 lot（环外释放传播） ----------------
out8 = mk("OUT8", "A006-RS", 5, (6, 1), "A006-R1-FTF-INPUT-TO-OUTPUT",
          refs=[ref("OUT8", "IOU1", "A006-R1-FTF-OPUT-FTF", "A006-R1-FTF-OPUT-FTF")])
le8, e8, w8 = run_case("T8 环外引用环内: OUT8 等 IOU1.FTF 完成", R, [iou1, iou1f, out8])
out_ftf = [e for e in le8 if e.lot_name == "OUT8" and e.step_name.endswith("OPUT-FTF")]
iou_ftf = [e for e in le8 if e.lot_name == "IOU1" and e.step_name.endswith("OPUT-FTF")]
if out_ftf and iou_ftf:
    check("T8: OUT8.FTF ≥ IOU1.FTF 完成", out_ftf[0].start_time >= iou_ftf[0].end_time,
          f"{out_ftf[0].start_time} vs {iou_ftf[0].end_time}")

print(f"\n========== SUMMARY: {PASS}/{PASS+FAIL} passed, {FAIL} failed ==========")
sys.exit(1 if FAIL else 0)
