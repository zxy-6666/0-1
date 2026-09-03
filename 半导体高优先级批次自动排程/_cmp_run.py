# -*- coding: utf-8 -*-
"""统一对比驱动：对指定构建目录(project_dir)以同一数据跑 schedule_optimized，
输出中性指标 + 该版原生 validate/compute_objective 结果。
用法: python cmp_run.py <project_dir> <seed> <max_iters> <refine_iters>
"""
import sys, os, json, time
from datetime import datetime, timedelta

proj = sys.argv[1]
seed = int(sys.argv[2])
max_iters = int(sys.argv[3])
refine_iters = int(sys.argv[4])
sys.path.insert(0, proj)

from paths import DATA_DIR  # noqa
from data_loader import (load_lot_list, load_flow, load_step_ct, load_qtime,
                         build_ct_lookup, auto_repair_step_ct, load_ftf_qty_change,
                         load_special_lot_step, load_priority_wait, load_lot_constraints,
                         load_eqp_constraints, load_shift_change_times, load_shift_config,
                         load_step_time_windows, load_manual_adjusts, load_special_eqp)
from optimizer import schedule_optimized
from validation import validate_schedule, compute_objective

os.environ.setdefault("SCHED_TRACE", "0")

t0 = time.time()
sc = load_shift_config(f"{DATA_DIR}/shift_config.csv")
shift_times = []
for s in sc:
    h, m = map(int, s.start_time_str.split(":"))
    shift_times.append((h, m))

lot_constraints = load_lot_constraints(f"{DATA_DIR}/lot_constraints.csv")
lots = load_lot_list(f"{DATA_DIR}/lot_list.csv",
                     constraints_filepath=f"{DATA_DIR}/lot_constraints.csv")
flows = load_flow(f"{DATA_DIR}/flow.csv")
step_cts = auto_repair_step_ct(flows, load_step_ct(f"{DATA_DIR}/step_ct.csv"),
                               step_ct_filepath=f"{DATA_DIR}/step_ct.csv")
qtimes = load_qtime(f"{DATA_DIR}/qtime.csv")
ct_lookup = build_ct_lookup(step_cts)
ma_path = f"{DATA_DIR}/manual_adjust.csv"
manual_adjusts = load_manual_adjusts(ma_path) if os.path.exists(ma_path) else []

le, ee, qa, meta = schedule_optimized(
    lots, flows, ct_lookup, qtimes, shift_times,
    ftf_qty_change=load_ftf_qty_change(f"{DATA_DIR}/ftf_qty_change.csv"),
    special_lot_step_lookup=load_special_lot_step(f"{DATA_DIR}/special_lot_step.csv"),
    priority_wait_map=load_priority_wait(f"{DATA_DIR}/priority_wait.csv"),
    eqp_constraints=load_eqp_constraints(f"{DATA_DIR}/eqp_constraint.csv"),
    step_time_window_constraints=load_step_time_windows(f"{DATA_DIR}/step_time_window.csv"),
    shift_change_times=load_shift_change_times(f"{DATA_DIR}/shift_change_time.csv"),
    manual_adjusts=manual_adjusts,
    special_eqp_map=load_special_eqp(f"{DATA_DIR}/special_eqp.csv"),
    lot_constraints=lot_constraints,
    max_iterations=max_iters,
    refine_max_iterations=refine_iters,
    seed=seed,
)
elapsed = time.time() - t0

if isinstance(flows, dict):
    flow_list = [s for fl in flows.values() for s in fl]
elif flows and hasattr(flows[0], "step_name"):
    flow_list = flows          # 已是扁平 FlowStep 列表
else:
    flow_list = [s for fl in flows for s in fl]
errs = list(validate_schedule(le, ee, qa, lots, flow_list, qtimes,
                              lot_constraints=lot_constraints, shift_times=shift_times,
                              special_eqp_map=load_special_eqp(f"{DATA_DIR}/special_eqp.csv")))
obj = compute_objective(le, lots, schedule_start=lots[0].start_time, qtimes=qtimes)

by_lot = {}
for e in le:
    by_lot.setdefault(e.lot_name, []).append(e)

print(json.dumps({
    "build": os.path.basename(proj),
    "seed": seed,
    "elapsed_s": round(elapsed, 1),
    "meta_score": meta.get("best_score"),
    "meta_warning": meta.get("warning"),
    "valid_iterations": meta.get("valid_iterations"),
    "native_valid_errors": errs[:6],
    "native_valid_error_count": len(errs),
    "obj_score": obj["score"],
    "obj_weighted_total": obj["weighted_total"],
    "obj_min_qmargin": obj.get("min_qtime_margin"),
    "obj_qmargin_viol": obj.get("qtime_margin_violations", [])[:6],
}, ensure_ascii=False, indent=1, default=str))

print("== LOT COMPLETIONS ==")
comp = {}
for ln, es in by_lot.items():
    comp[ln] = max(e.end_time for e in es)
for ln in sorted(comp, key=lambda x: comp[x]):
    print(f"  {ln}: {comp[ln]:%m-%d %H:%M}  (steps={len(by_lot[ln])})")

print("== UF 链时间线 (real*/f*) ==")
for ln in sorted(by_lot):
    if not (ln.startswith("real") or ln.startswith("f")):
        continue
    uf = [e for e in sorted(by_lot[ln], key=lambda x: x.start_time)
          if "UF-" in e.step_name]
    if uf:
        tl = " | ".join(f"{e.step_name.split('-')[-1]} {e.start_time:%m-%d %H:%M}->{e.end_time:%m-%d %H:%M}"
                        for e in uf)
        print(f"  {ln}: {tl}")

print("== LEAD GAPS (min) ==")
by_lot_step = {(e.lot_name, e.step_name): e for e in le}
n_gt90 = 0
for lot in lots:
    for lp in lot.lead_pairs or []:
        e1 = by_lot_step.get((lp.lot1, lp.step1))
        e2 = by_lot_step.get((lp.lot2, lp.step2))
        if e1 is None or e2 is None:
            print(f"  {lp.lot1}.{lp.step1}->{lp.lot2}.{lp.step2}: MISSING")
            continue
        g = (e2.start_time - e1.end_time).total_seconds() / 60.0
        tag = ""
        if g < -1e-6:
            tag = " [倒序!]"
        if g > 90:
            n_gt90 += 1
            tag += " [>90min]"
        print(f"  {lp.lot1}.{lp.step1} -> {lp.lot2}.{lp.step2}: gap={g:.0f}min{tag}")
print("  lead pairs >90min:", n_gt90)
