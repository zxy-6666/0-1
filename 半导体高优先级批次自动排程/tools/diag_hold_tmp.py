"""Trace PC1's CURE ready/blocked state around 08/18 14:34 -> 08/19 07:58"""
import os, sys, copy
from datetime import datetime, timedelta
ROOT = "/workspace/半导体高优先级批次自动排程"
sys.path.insert(0, ROOT)

import scheduler
from scheduler import schedule

# 包一层主循环不好做，改为在 _update_blocked_ready_time 与 _tight_qtime_target_start 打点
_ubt = scheduler._update_blocked_ready_time
_orig_tight = scheduler._tight_qtime_target_start

def _ubt_patched(name, state, pending_refs, ref_release_times):
    before = state.get("ready_time")
    res = _ubt(name, state, pending_refs, ref_release_times)
    after = state.get("ready_time")
    if name in ("PC1", "real1") and after != before:
        step = state["remaining_steps"][state["step_index"]].step_name if state["step_index"] < len(state["remaining_steps"]) else "END"
        print(f"[BLOCK-READY] {name} step={step} ready {before} -> {after} pending={pending_refs.get(name)}")
    return res

scheduler._update_blocked_ready_time = _ubt_patched

def _tight_patched(lot, state, step, ct, product_qtimes, ready_time, s_start,
                   machine_available, machine_intervals, pending_refs, ref_release_times,
                   special_lot_step_lookup, ct_lookup, shift_change_intervals=None,
                   step_windows=None, end_windows=None, priority_wait_map=None,
                   ref_release_forecast=None):
    res = _orig_tight(lot, state, step, ct, product_qtimes, ready_time, s_start,
                      machine_available, machine_intervals, pending_refs, ref_release_times,
                      special_lot_step_lookup, ct_lookup, shift_change_intervals,
                      step_windows, end_windows, priority_wait_map, ref_release_forecast)
    if lot.lot_name in ("PC1", "real1"):
        if isinstance(res, tuple) and res and res[0] == "DEFER":
            print(f"[QHOLD] {lot.lot_name} step={step.step_name} DEFER keys={res[1]} ready={ready_time} s_start={s_start} pending={pending_refs.get(lot.lot_name)} rel={ref_release_times.get(lot.lot_name)}")
        elif isinstance(res, datetime):
            if res > s_start:
                print(f"[QTIGHT] {lot.lot_name} step={step.step_name} s_start {s_start} -> target {res}")
    return res

scheduler._tight_qtime_target_start = _tight_patched

from data_loader import (
    load_lot_list, load_flow, load_step_ct, load_qtime,
    build_ct_lookup, auto_repair_step_ct,
    load_ftf_qty_change, load_special_lot_step, load_priority_wait,
    load_eqp_constraints, load_step_time_windows, load_shift_config, load_shift_change_times,
    load_special_eqp,
)

D = f"{ROOT}/data"
lots = load_lot_list(f"{D}/lot_list.csv", constraints_filepath=f"{D}/lot_constraints.csv")
flows = load_flow(f"{D}/flow.csv")
step_cts = auto_repair_step_ct(flows, load_step_ct(f"{D}/step_ct.csv"))
qtimes = load_qtime(f"{D}/qtime.csv")
ct_lookup = build_ct_lookup(step_cts)
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
    shift_times=shift_times, priority_wait_map=priority_wait_map,
    eqp_constraints=eqp_constraints, step_time_window_constraints=step_time_window_constraints,
    shift_change_times=shift_change_times, special_eqp_map=special_eqp_map)