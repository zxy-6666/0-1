"""Trace _compute_batch_slot for PKPOV001 to find CURE delay root cause"""
import os, sys, copy
from datetime import datetime
ROOT = "/workspace/半导体高优先级批次自动排程"
sys.path.insert(0, ROOT)

import scheduler
from scheduler import schedule, _compute_batch_slot, get_step_ct, get_step_wait_time
from datetime import timedelta

_orig = _compute_batch_slot

import scheduler as _sched

_orig_precompute = getattr(_sched, "_precompute_whole_chain_block", None)

def _patched(eqp_id, lot, step, ct, ready_time, spec, lot_state,
             special_lot_step_lookup, ct_lookup, eqp_batch_state,
             priority_wait_map, wait_window=240, cur_time=None):
    if eqp_id == "PKPOV001":
        bs = eqp_batch_state.get("PKPOV001")
        pend = bs.get("pending") if bs else None
        bu = bs.get("busy_until") if bs else None
        print(f"[BATCH-ENTRY] lot={lot.lot_name} ready={ready_time} cur_time={cur_time} "
              f"busy_until={bu} pending_start={pend.get('start') if pend else None} "
              f"pending_members={list(pend.get('members',{}).keys()) if pend else None}")
    res = _orig(eqp_id, lot, step, ct, ready_time, spec, lot_state,
                special_lot_step_lookup, ct_lookup, eqp_batch_state,
                priority_wait_map, wait_window, cur_time)
    if eqp_id == "PKPOV001":
        can_use, slot = res
        bs = eqp_batch_state.get("PKPOV001")
        pend = bs.get("pending") if bs else None
        print(f"[BATCH] eqp=PKPOV001 lot={lot.lot_name} -> can={can_use} slot={slot} "
              f"new_pending_start={pend.get('start') if pend else None} "
              f"new_pending_members={pend.get('members') if pend else None}")
    return res

scheduler._compute_batch_slot = _patched

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