"""诊断：列出 08/18 22:00 至 08/19 08:00 之间所有已排步骤，定位 current_time 如何跳到 07:38"""
import os, sys, copy
ROOT = "/workspace/半导体高优先级批次自动排程"
sys.path.insert(0, ROOT)
from datetime import datetime
from data_loader import (
    load_lot_list, load_flow, load_step_ct, load_qtime,
    build_ct_lookup, auto_repair_step_ct,
    load_ftf_qty_change, load_special_lot_step, load_priority_wait,
    load_eqp_constraints, load_step_time_windows, load_shift_config, load_shift_change_times,
    load_special_eqp, load_lot_constraints,
)
from scheduler import schedule

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

lots2 = [copy.copy(l) for l in lots]
for l in lots2:
    if l.lot_name == "real1":
        l.priority = (5, 1)

le, ee, qa = schedule(
    lots=lots2, flows=flows, ct_lookup=ct_lookup, qtimes=qtimes,
    shift_times=shift_times, ftf_qty_change=ftf_qty_change,
    special_lot_step_lookup=special_lot_step_lookup, priority_wait_map=priority_wait_map,
    eqp_constraints=eqp_constraints, step_time_window_constraints=step_time_window_constraints,
    shift_change_times=shift_change_times, special_eqp_map=special_eqp_map)

win = [e for e in sorted(le, key=lambda x: (x.start_time, x.lot_name))
       if datetime(2026,8,18,21,30) <= e.start_time <= datetime(2026,8,19,8,30)]
for e in win:
    print(f"  {e.lot_name:<7} {e.step_name:<36} {e.eqp_id:<12} {e.start_time.strftime('%m/%d %H:%M')} -> {e.end_time.strftime('%m/%d %H:%M')} {e.qtime_risk}")
