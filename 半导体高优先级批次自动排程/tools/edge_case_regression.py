"""边界/复杂场景回归测试套件（正式回归用）。

覆盖：正常数据、循环引用×Q-time×交接班/禁机、真·环（同 step 互等）、多环混合、
环外引用环内、双环共享单机、环内 pin、环+FTF qty 变化、四 lot 长环跨产品、
循环引用×紧Q×恒组批单机（CURE）瓶颈。

每个场景：schedule_optimized(seed=0, max_iterations) 多轮择优，validate 0 错误、
无雪崩（全部步骤 < 14 天）、告警合理（引用环/雪崩回退/异常间隙）。

T3 说明：PC↔real 紧 Q(UF240)+手动延迟下，PKPOV001 恒组批 CURE 单机 + 4 lot 竞争，
单轮/8 轮贪心会拆两炉导致第二炉超 Q；数学上可满足（max_lots=4/max_qty=25），
多轮探索（30 轮）能通过 eqp_preferences 让 DISPENSE 并行、4 lot 同炉 → 0 错误。
此场景验证"多轮择优"的价值：存在一轮通过且结果合法即可。

运行：python tools/edge_case_regression.py
"""
import sys, os, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from datetime import datetime, timedelta
from collections import defaultdict
import data_loader
from models import Lot, LotConstraint, ManualAdjust
from optimizer import schedule_optimized
from validation import validate_schedule

REPRO = "/tmp/repro_iou"
TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
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
    cts = data_loader.auto_repair_step_ct(flows, data_loader.load_step_ct(os.path.join(d, "step_ct.csv")), None)
    ct = data_loader.build_ct_lookup(cts)
    scfg = data_loader.load_shift_config(os.path.join(d, "shift_config.csv"))
    st = sorted(tuple(map(int, x.start_time_str.split(":"))) for x in scfg if getattr(x, "start_time_str", None))
    return dict(
        flows=allf, fm=fm, ct=ct, st=st,
        qtimes=data_loader.load_qtime(os.path.join(d, "qtime.csv")),
        pw=data_loader.load_priority_wait(os.path.join(d, "priority_wait.csv")),
        eqp=data_loader.load_eqp_constraints(os.path.join(d, "eqp_constraint.csv")),
        tw=data_loader.load_step_time_windows(os.path.join(d, "step_time_window.csv")),
        sc=data_loader.load_shift_change_times(os.path.join(d, "shift_change_time.csv")),
        sls=data_loader.load_special_lot_step(os.path.join(d, "special_lot_step.csv")),
        fq=data_loader.load_ftf_qty_change(os.path.join(d, "ftf_qty_change.csv")),
        se=data_loader.load_special_eqp(os.path.join(d, "special_eqp.csv")),
        lc=data_loader.load_lot_constraints(os.path.join(d, "lot_constraints.csv")),
    )


R = load_ctx(REPRO)
T = load_ctx(TOOLS)


def mk(name, product, qty, priority, cur_step, refs=(), start=BASE, state="wait", rt=0):
    return Lot(lot_name=name, carrier_id=f"C{name}", product_name=product, qty=qty,
               priority=priority, current_step_name=cur_step, target_step=None,
               lot_state=state, running_time=rt, start_time=start, references=list(refs))


def ref(lot, ref_lot, ref_step, start_step, mod="0"):
    return LotConstraint(lot_name=lot, reference_lot=ref_lot, reference_step=ref_step,
                         start_mod=mod, start_step=start_step)


def run_case(name, ctx, lots, manual_adjusts=None, max_iterations=8, want_errs=0, want_valid=1):
    """跑 schedule_optimized 并断言：无雪崩、errs==want_errs、valid>=want_valid。"""
    print(f"\n===== {name} =====")
    le, ee, qa, meta = schedule_optimized(
        lots=[copy.copy(l) for l in lots], flows=ctx["flows"], ct_lookup=ctx["ct"],
        qtimes=ctx["qtimes"], shift_times=ctx["st"], ftf_qty_change=ctx["fq"],
        special_lot_step_lookup=ctx["sls"], priority_wait_map=ctx["pw"],
        eqp_constraints=ctx["eqp"], step_time_window_constraints=ctx["tw"],
        shift_change_times=ctx["sc"], manual_adjusts=manual_adjusts,
        special_eqp_map=ctx["se"], lot_constraints=ctx["lc"],
        resolve_max_iterations=10, max_iterations=max_iterations, seed=0)
    errs = validate_schedule(le, ee, qa, lots, ctx["flows"], ctx["qtimes"],
                             lot_constraints=ctx["lc"], shift_times=ctx["st"], special_eqp_map=ctx["se"])
    print(f"  valid={meta.get('valid_iterations')}/{meta.get('total_iterations')}  errs={len(errs)}")
    for e in errs[:4]:
        print("   -", getattr(e, "message", str(e))[:130])
    ws = meta.get("schedule_warnings") or []
    print(f"  warnings({len(ws)}):")
    for w in ws:
        print("    *", w[:120])
    max_t = max((e.end_time for e in le), default=BASE)
    check(f"{name}: 无雪崩(全部步骤 < 14天)", max_t < BASE + timedelta(days=14), f"max_end={max_t}")
    check(f"{name}: 全量校验 {want_errs} 错误", len(errs) == want_errs, str(errs[:2]))
    check(f"{name}: 有效轮次 >= {want_valid}", (meta.get("valid_iterations") or 0) >= want_valid,
          f"valid={meta.get('valid_iterations')}/{meta.get('total_iterations')}")
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
if mnt:
    check("T2: IOU1.MOUNT 当天完成(非雪崩)", mnt[0].end_time.date() == BASE.date(), str(mnt[0]))
if ftf_f:
    check("T2: IOU1-f.FTF 当天完成", ftf_f[0].start_time.date() == BASE.date(), str(ftf_f[0]))

# ---------------- T3 循环引用 + 紧Q-time(UF 240) + 手动延迟 + 恒组批 CURE 单机瓶颈 ----------------
# PKPOV001(max_lots=4/max_qty=25) 单机，4 lot 竞争 UF-CURE；单轮贪心拆两炉会超 Q，
# 多轮（30）可让 4 lot 同炉（DISPENSE 并行 + 凑批）→ 存在 0 错误轮次。
t3_lots = data_loader.load_lot_list(os.path.join(TOOLS, "lot_list.csv"),
                                    constraints_filepath=os.path.join(TOOLS, "lot_constraints.csv"))
ma3 = [ManualAdjust(lot_name="PC1", step_name="A005-P1-UF-BAKE",
                    delay_to=datetime(2026, 8, 18, 10, 0), mode="delay")]
run_case("T3 循环引用+紧Q(UF240)+手动延迟+恒组批CURE单机: PC↔real", T, t3_lots,
         manual_adjusts=ma3, max_iterations=30, want_errs=0, want_valid=1)

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
t6_lots = data_loader.load_lot_list(os.path.join(TOOLS, "lot_list.csv"),
                                    constraints_filepath=os.path.join(TOOLS, "lot_constraints.csv"))
ma6 = [ManualAdjust(lot_name="real1", step_name="A005-R1-UF-DISPENSE",
                    delay_to=datetime(2026, 8, 17, 21, 0), mode="delay")]
run_case("T6 紧Q链(UF240)钉在交接班/禁机边界: real1.DISPENSE->21:00", T, t6_lots, manual_adjusts=ma6)

# ---------------- T7 三 lot 环 A→B→C→A ----------------
c7 = mk("C7", "A006-MS", 3, (1, 3), "A006-R1-FTF-INPUT-TO-OUTPUT",
        refs=[ref("C7", "D7", "A006-D1-FTF-OPUT-FTF", "A006-R1-FTF-OPUT-FTF")])
d7 = mk("D7", "A006-D1", 3, (1, 3), "A006-D1-FTF-INPUT-TO-OUTPUT",
        refs=[ref("D7", "E7", "A006-R1-FTF-OPUT-FTF", "A006-D1-FTF-OPUT-FTF")])
e7 = mk("E7", "A006-RS", 3, (1, 3), "A006-R1-FTF-INPUT-TO-OUTPUT",
        refs=[ref("E7", "C7", "A006-R1-FTF-OPUT-MOUNT", "A006-R1-FTF-OPUT-FTF")])
le7, e7, w7 = run_case("T7 三lot环: C7→D7→E7→C7", R, [c7, d7, e7])
_l7 = defaultdict(list)
for _e in le7:
    _l7[_e.lot_name].append(_e)
check("T7: 三个环内 lot 步骤完整(各15步)",
      all(len(_l7[n]) >= 15 for n in ("C7", "D7", "E7")), {n: len(_l7[n]) for n in ("C7", "D7", "E7")})

# ---------------- T8 环外 lot 引用环内 lot ----------------
out8 = mk("OUT8", "A006-RS", 5, (6, 1), "A006-R1-FTF-INPUT-TO-OUTPUT",
          refs=[ref("OUT8", "IOU1", "A006-R1-FTF-OPUT-FTF", "A006-R1-FTF-OPUT-FTF")])
le8, e8, w8 = run_case("T8 环外引用环内: OUT8 等 IOU1.FTF 完成", R, [iou1, iou1f, out8])
out_ftf = [e for e in le8 if e.lot_name == "OUT8" and e.step_name.endswith("OPUT-FTF")]
iou_ftf = [e for e in le8 if e.lot_name == "IOU1" and e.step_name.endswith("OPUT-FTF")]
if out_ftf and iou_ftf:
    check("T8: OUT8.FTF ≥ IOU1.FTF 完成", out_ftf[0].start_time >= iou_ftf[0].end_time,
          f"{out_ftf[0].start_time} vs {iou_ftf[0].end_time}")

# ---------------- T9 两对同产品环共享单机 FTF（PKFFS002） ----------------
iou2 = mk("IOU2", "A006-MS", 8, (3, 2), "A006-R1-FTF-INPUT-TO-OUTPUT",
          refs=[ref("IOU2", "IOU2-f", "A006-D1-FTF-OPUT-FTF", "A006-R1-FTF-OPUT-FTF")])
iou2f = mk("IOU2-f", "A006-D1", 5, (2, 2), "A006-D1-FTF-INPUT-TO-OUTPUT",
           refs=[ref("IOU2-f", "IOU2", "A006-R1-FTF-OPUT-MOUNT", "A006-D1-FTF-OPUT-FTF")])
le9, e9, w9 = run_case("T9 两对同产品环共享单机(PKFFS002): IOU1对+IOU2对", R, [iou1, iou1f, iou2, iou2f])
_l9 = defaultdict(list)
for _e in le9:
    _l9[_e.lot_name].append(_e)
check("T9: 四 lot 步骤完整(各15步)",
      all(len(_l9[n]) >= 15 for n in ("IOU1", "IOU1-f", "IOU2", "IOU2-f")),
      {n: len(_l9[n]) for n in ("IOU1", "IOU1-f", "IOU2", "IOU2-f")})

# ---------------- T10 环内 pin（精确锁定） ----------------
ma10 = [ManualAdjust(lot_name="IOU1-f", step_name="A006-D1-FTF-OPUT-FTF",
                     delay_to=datetime(2026, 9, 1, 14, 0), mode="pin")]
le10, e10, w10 = run_case("T10 环内pin: IOU1-f.FTF->09/01 14:00", R, [iou1, iou1f], manual_adjusts=ma10)
pin10 = [e for e in le10 if e.lot_name == "IOU1-f" and e.step_name == "A006-D1-FTF-OPUT-FTF"]
if pin10:
    check("T10: pin 精确命中 14:00", pin10[0].start_time == datetime(2026, 9, 1, 14, 0),
          str(pin10[0].start_time))

# ---------------- T11 环 + FTF qty 变化（A006-MS 392→351） ----------------
le11, e11, w11 = run_case("T11 环+FTF qty变化(392→351): IOU1↔IOU1-f", R, [iou1, iou1f])

# ---------------- T12 四 lot 长环跨 3 产品 ----------------
a12 = mk("A12", "A006-MS", 4, (1, 1), "A006-R1-FTF-INPUT-TO-OUTPUT",
         refs=[ref("A12", "B12", "A006-D1-FTF-OPUT-FTF", "A006-R1-FTF-OPUT-FTF")])
b12 = mk("B12", "A006-D1", 4, (1, 2), "A006-D1-FTF-INPUT-TO-OUTPUT",
         refs=[ref("B12", "C12", "A007-R1-FTF-OPUT-FTF", "A006-D1-FTF-OPUT-FTF")])
c12 = mk("C12", "A007-MS", 4, (1, 3), "A007-R1-FTF-INPUT-TO-OUTPUT",
         refs=[ref("C12", "D12", "A007-D1-FTF-OPUT-FTF", "A007-R1-FTF-OPUT-FTF")])
d12 = mk("D12", "A007-D1", 4, (1, 4), "A007-D1-FTF-INPUT-TO-OUTPUT",
         refs=[ref("D12", "A12", "A006-R1-FTF-OPUT-MOUNT", "A007-D1-FTF-OPUT-FTF")])
le12, e12, w12 = run_case("T12 四lot长环跨3产品: A12→B12→C12→D12→A12", R, [a12, b12, c12, d12])
_l12 = defaultdict(list)
for _e in le12:
    _l12[_e.lot_name].append(_e)
check("T12: 四 lot 步骤完整(各15步)",
      all(len(_l12[n]) >= 15 for n in ("A12", "B12", "C12", "D12")),
      {n: len(_l12[n]) for n in ("A12", "B12", "C12", "D12")})

print(f"\n========== SUMMARY: {PASS}/{PASS+FAIL} passed, {FAIL} failed ==========")
sys.exit(1 if FAIL else 0)
