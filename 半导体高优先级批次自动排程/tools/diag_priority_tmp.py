"""诊断：优先级变化对 CURE 排程的影响 + manual_adjust 对 FC-REFLOW 的影响（临时脚本）"""
import os, sys, copy
from datetime import datetime

ROOT = "/workspace/半导体高优先级批次自动排程"
sys.path.insert(0, ROOT)

from data_loader import (
    load_lot_list, load_flow, load_step_ct, load_qtime,
    build_ct_lookup, auto_repair_step_ct,
    load_ftf_qty_change, load_special_lot_step, load_priority_wait,
    load_eqp_constraints, load_step_time_windows, load_shift_config, load_shift_change_times,
    load_special_eqp, load_lot_constraints, load_manual_adjusts,
)
from scheduler import schedule
from models import ManualAdjust

D = f"{ROOT}/data"
lots = load_lot_list(f"{D}/lot_list.csv", constraints_filepath=f"{D}/lot_constraints.csv")
flows = load_flow(f"{D}/flow.csv")
step_cts = load_step_ct(f"{D}/step_ct.csv")
step_cts = auto_repair_step_ct(flows, step_cts)
qtimes = load_qtime(f"{D}/qtime.csv")
ct_lookup = build_ct_lookup(step_cts)
ftf_qty_change = load_ftf_qty_change(f"{D}/ftf_qty_change.csv")
special_lot_step_lookup = load_special_lot_step(f"{D}/special_lot_step.csv")
priority_wait_map = load_priority_wait(f"{D}/priority_wait.csv")
eqp_constraints = load_eqp_constraints(f"{D}/eqp_constraint.csv")
step_time_window_constraints = load_step_time_windows(f"{D}/step_time_window.csv")
shift_configs = load_shift_config(f"{D}/shift_config.csv")
shift_change_times = load_shift_change_times(f"{D}/shift_change_time.csv")
special_eqp_map = load_special_eqp(f"{D}/special_eqp.csv")

shift_times = []
for sc in shift_configs:
    h, m = map(int, sc.start_time_str.split(":"))
    shift_times.append((h, m))
shift_times.sort()

def run(these_lots, manual_adjusts=None, label=""):
    le, ee, qa = schedule(
        lots=[copy.copy(l) for l in these_lots],
        flows=flows, ct_lookup=ct_lookup, qtimes=qtimes,
        shift_times=shift_times, ftf_qty_change=ftf_qty_change,
        special_lot_step_lookup=special_lot_step_lookup,
        priority_wait_map=priority_wait_map, eqp_constraints=eqp_constraints,
        step_time_window_constraints=step_time_window_constraints,
        shift_change_times=shift_change_times, manual_adjusts=manual_adjusts,
        special_eqp_map=special_eqp_map,
    )
    by_lot = {}
    for e in le:
        by_lot.setdefault(e.lot_name, []).append(e)
    return le, by_lot, qa

def show(le, by_lot, qa, label, focus_steps=("CURE", "REFLOW")):
    print(f"\n===== {label} =====")
    for ln in ["PC1","real1","PC2","real2"]:
        ents = by_lot.get(ln, [])
        if not ents:
            continue
        pri = next((l.priority for l in lots if l.lot_name==ln), '?')
        ents_sorted = sorted(ents, key=lambda e: e.start_time)
        print(f"\n[{ln}] priority={pri} {len(ents)}步")
        for e in ents_sorted:
            if focus_steps and not any(fs in e.step_name for fs in focus_steps):
                continue
            flag = " <<<超Q" if e.qtime_risk.startswith("RISK") else ""
            print(f"   {e.step_name:<36} {e.eqp_id:<12} {e.start_time.strftime('%m/%d %H:%M')} -> {e.end_time.strftime('%m/%d %H:%M')} ct={e.ct:>6.1f} {e.qtime_risk}{flag}")
    over = [a for a in qa if a.status == "超时"]
    print(f"\n  Q-time告警({len(over)}条超时):")
    for a in over:
        print(f"    WARN {a.lot_name} {a.qtime_rule} 超时{a.over_minutes}min")

# 1) baseline
le0, by_lot0, qa0 = run(lots, label="baseline")
show(le0, by_lot0, qa0, "BASELINE 当前优先级")

# 2) issue 1: 把 real1 优先级从 3-1 改成更低的 (如 5-1)
lots2 = [copy.copy(l) for l in lots]
for l in lots2:
    if l.lot_name == "real1":
        l.priority = (5, 1)
le2, by_lot2, qa2 = run(lots2, label="real1 priority 5-1")
show(le2, by_lot2, qa2, "real1 优先级 3-1 -> 5-1")

# 3) issue 2: 手动 pin real1 UF-CURE
pin_time = datetime(2026, 8, 17, 20, 0)
ma = [ManualAdjust(lot_name="real1", step_name="A005-R1-UF-CURE", delay_to=pin_time, mode="pin")]
le3, by_lot3, qa3 = run(lots, manual_adjusts=ma, label="pin real1 UF-CURE 20:00")
show(le3, by_lot3, qa3, "手动 pin real1 UF-CURE @ 8/17 20:00")