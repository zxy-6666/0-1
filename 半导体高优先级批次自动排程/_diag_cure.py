#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
from data_loader import (
    load_lot_list, load_flow, load_step_ct, load_qtime, build_ct_lookup,
    auto_repair_step_ct, load_ftf_qty_change, load_special_lot_step,
    load_priority_wait, load_lot_constraints, load_eqp_constraints,
    load_shift_change_times, load_step_time_windows, load_shift_config,
    load_manual_adjusts, load_special_eqp)
from scheduler import schedule
from validation import validate_schedule

lots = load_lot_list(f"{DATA_DIR}/lot_list.csv", constraints_filepath=f"{DATA_DIR}/lot_constraints.csv")
flows = load_flow(f"{DATA_DIR}/flow.csv")
step_cts = auto_repair_step_ct(flows, load_step_ct(f"{DATA_DIR}/step_ct.csv"))
qtimes = load_qtime(f"{DATA_DIR}/qtime.csv")
ct_lookup = build_ct_lookup(step_cts)
ftf = load_ftf_qty_change(f"{DATA_DIR}/ftf_qty_change.csv")
spec = load_special_lot_step(f"{DATA_DIR}/special_lot_step.csv")
pw = load_priority_wait(f"{DATA_DIR}/priority_wait.csv")
eqp = load_eqp_constraints(f"{DATA_DIR}/eqp_constraint.csv")
tw = load_step_time_windows(f"{DATA_DIR}/step_time_window.csv")
sc = load_shift_config(f"{DATA_DIR}/shift_config.csv")
sct = load_shift_change_times(f"{DATA_DIR}/shift_change_time.csv")
ma = load_manual_adjusts(f"{DATA_DIR}/manual_adjust.csv")
seqp = load_special_eqp(f"{DATA_DIR}/special_eqp.csv")
cons = load_lot_constraints(f"{DATA_DIR}/lot_constraints.csv")
st = sorted((int(x.start_time_str.split(':')[0]), int(x.start_time_str.split(':')[1])) for x in sc if x.start_time_str)

le, ee, qa = schedule(lots=lots, flows=flows, ct_lookup=ct_lookup, qtimes=qtimes, shift_times=st,
    ftf_qty_change=ftf, special_lot_step_lookup=spec, priority_wait_map=pw,
    eqp_constraints=eqp, step_time_window_constraints=tw, shift_change_times=sct,
    manual_adjusts=ma, special_eqp_map=seqp, resolve_max_iterations=16)

by = {(e.lot_name, e.step_name): e for e in le}
print("=== UF chain for real7/real8/real6 ===")
for name in ["real6","real7","real8","real9"]:
    for st in ["A005-R1-UF-BAKE","A005-R1-UF-PLASMA","A005-R1-UF-DISPENSE","A005-R1-UF-CURE","A005-R1-UF-INSP"]:
        e=by.get((name,st))
        if e:
            gap = ""
            disp = by.get((name,"A005-R1-UF-DISPENSE"))
            if st=="A005-R1-UF-CURE" and disp:
                g=(e.start_time-disp.end_time).total_seconds()/60
                gap=f"  disp->cure={g:.1f}min"
            print(f"  {name:6s} {st:24s} {e.start_time:%m/%d %H:%M}->{e.end_time:%m/%d %H:%M} eqp={e.eqp_id}{gap}")