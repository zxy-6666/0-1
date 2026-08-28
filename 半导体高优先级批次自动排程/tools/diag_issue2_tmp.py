"""诊断 issue2：手动调整对 FC-REFLOW 块的影响（临时脚本）"""
import sys, copy
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
from models import ManualAdjust

D = f"{ROOT}/data"
lots = load_lot_list(f"{D}/lot_list.csv", constraints_filepath=f"{D}/lot_constraints.csv")
flows = load_flow(f"{D}/flow.csv")
step_cts = auto_repair_step_ct(flows, load_step_ct(f"{D}/step_ct.csv"))
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
shift_times = sorted((int(s.start_time_str.split(":")[0]), int(s.start_time_str.split(":")[1])) for s in shift_configs)

def run(these_lots, manual_adjusts=None):
    le, ee, qa = schedule(
        lots=[copy.copy(l) for l in these_lots], flows=flows, ct_lookup=ct_lookup, qtimes=qtimes,
        shift_times=shift_times, ftf_qty_change=ftf_qty_change,
        special_lot_step_lookup=special_lot_step_lookup, priority_wait_map=priority_wait_map,
        eqp_constraints=eqp_constraints, step_time_window_constraints=step_time_window_constraints,
        shift_change_times=shift_change_times, manual_adjusts=manual_adjusts,
        special_eqp_map=special_eqp_map)
    return le, qa

def show(le, qa, label, lot="real1"):
    by_lot = {}
    for e in le:
        by_lot.setdefault(e.lot_name, []).append(e)
    print(f"\n===== {label} =====")
    for e in sorted(by_lot.get(lot, []), key=lambda x: x.start_time):
        if "FC-" in e.step_name or "UF-" in e.step_name or "AB1IQC" in e.step_name:
            flag = " <<<超Q" if e.qtime_risk.startswith("RISK") else ""
            print(f"   {e.step_name:<36} {e.eqp_id:<12} {e.start_time.strftime('%m/%d %H:%M')} -> {e.end_time.strftime('%m/%d %H:%M')} {e.qtime_risk}{flag}")
    over = [a for a in qa if a.status == "超时"]
    print(f"  超时告警: {[(a.lot_name, a.qtime_rule, a.over_minutes) for a in over]}")

# 1) baseline
le0, qa0 = run(lots)
show(le0, qa0, "BASELINE")

# 2) 手动推迟 real1 的 UF-CURE 到 08/18 20:00 (mode=delay 不早于)
ma1 = [ManualAdjust(lot_name="real1", step_name="A005-R1-UF-CURE", delay_to=datetime(2026,8,18,20,0), mode="delay")]
le1, qa1 = run(lots, ma1)
show(le1, qa1, "delay real1 UF-CURE >= 08/18 20:00")

# 3) 手动 pin real1 的 UF-CURE 到 08/18 20:00 (精确锁定)
ma2 = [ManualAdjust(lot_name="real1", step_name="A005-R1-UF-CURE", delay_to=datetime(2026,8,18,20,0), mode="pin")]
le2, qa2 = run(lots, ma2)
show(le2, qa2, "pin real1 UF-CURE @ 08/18 20:00")

# 4) 手动推迟 real1 的 FC-REFLOW 到 08/17 20:00
ma3 = [ManualAdjust(lot_name="real1", step_name="A005-R1-FC-REFLOW", delay_to=datetime(2026,8,17,20,0), mode="delay")]
le3, qa3 = run(lots, ma3)
show(le3, qa3, "delay real1 FC-REFLOW >= 08/17 20:00")
