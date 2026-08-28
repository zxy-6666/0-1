"""诊断 issue1: real1 改优先级后 PC1/real1 的 UF 段完整时间线 + PKPOV 占用"""
import os, sys, copy
from datetime import datetime

ROOT = "/workspace/半导体高优先级批次自动排程"
sys.path.insert(0, ROOT)

from data_loader import (
    load_lot_list, load_flow, load_step_ct, load_qtime,
    build_ct_lookup, auto_repair_step_ct,
    load_ftf_qty_change, load_special_lot_step, load_priority_wait,
    load_eqp_constraints, load_step_time_windows, load_shift_config, load_shift_change_times,
    load_special_eqp, load_lot_constraints,
)
from scheduler import schedule

D = f"{ROOT}/data"
def load():
    lots = load_lot_list(f"{D}/lot_list.csv", constraints_filepath=f"{D}/lot_constraints.csv")
    flows = load_flow(f"{D}/flow.csv")
    step_cts = auto_repair_step_ct(flows, load_step_ct(f"{D}/step_ct.csv"))
    qtimes = load_qtime(f"{D}/qtime.csv")
    ct_lookup = build_ct_lookup(step_cts)
    return lots, flows, ct_lookup, qtimes

lots, flows, ct_lookup, qtimes = load()
ftf_qty_change = load_ftf_qty_change(f"{D}/ftf_qty_change.csv")
special_lot_step_lookup = load_special_lot_step(f"{D}/special_lot_step.csv")
priority_wait_map = load_priority_wait(f"{D}/priority_wait.csv")
eqp_constraints = load_eqp_constraints(f"{D}/eqp_constraint.csv")
step_time_window_constraints = load_step_time_windows(f"{D}/step_time_window.csv")
shift_configs = load_shift_config(f"{D}/shift_config.csv")
shift_change_times = load_shift_change_times(f"{D}/shift_change_time.csv")
special_eqp_map = load_special_eqp(f"{D}/special_eqp.csv")
shift_times = sorted((int(s.start_time_str.split(":")[0]), int(s.start_time_str.split(":")[1])) for s in shift_configs)

def run(these_lots):
    le, ee, qa = schedule(
        lots=[copy.copy(l) for l in these_lots], flows=flows, ct_lookup=ct_lookup, qtimes=qtimes,
        shift_times=shift_times, ftf_qty_change=ftf_qty_change,
        special_lot_step_lookup=special_lot_step_lookup, priority_wait_map=priority_wait_map,
        eqp_constraints=eqp_constraints, step_time_window_constraints=step_time_window_constraints,
        shift_change_times=shift_change_times, special_eqp_map=special_eqp_map)
    return le, ee, qa

# modified: real1 -> 5-1
lots2 = [copy.copy(l) for l in lots]
for l in lots2:
    if l.lot_name == "real1":
        l.priority = (5, 1)

le, ee, qa = run(lots2)

by_lot = {}
for e in le:
    by_lot.setdefault(e.lot_name, []).append(e)

# PKPOV 设备占用时间线
print("=" * 80)
print("PKPOV 设备占用时间线 (UF-CURE=PKPOV001, DAF-CURE=PKPOV002):")
print("=" * 80)
pkpov = sorted([e for e in ee if e.eqp_id in ("PKPOV001","PKPOV002")], key=lambda e:e.start_time)
for e in pkpov:
    print(f"  {e.eqp_id:<10} {e.lot_name:<7} {e.step_name:<24} {e.start_time.strftime('%m/%d %H:%M')} -> {e.end_time.strftime('%m/%d %H:%M')}")

print("\n" + "=" * 80)
for ln in ["PC1","real1"]:
    ents = sorted(by_lot[ln], key=lambda e:e.start_time)
    print(f"\n[{ln}] priority={next(l.priority for l in lots2 if l.lot_name==ln)} 完整UF段+前后:")
    # 只打印 UF 相关 step（含 BAKE/AUTO-SPLIT/PLASMA/DISPENSE/CURE/INSP）
    for e in ents:
        if "UF-" in e.step_name or "FC-REFLOW" in e.step_name or "FC-DEFLUX" in e.step_name:
            flag = " <<超Q" if e.qtime_risk.startswith("RISK") else ""
            print(f"   {e.step_name:<36} {e.eqp_id:<12} {e.start_time.strftime('%m/%d %H:%M')} -> {e.end_time.strftime('%m/%d %H:%M')} {e.qtime_risk}{flag}")

over = [a for a in qa if a.status=="超时"]
print(f"\n超时告警: {[(a.lot_name, a.qtime_rule, a.over_minutes) for a in over]}")