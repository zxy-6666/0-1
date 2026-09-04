#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("NO_INJECT", os.environ.get("NI", "0"))
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
errors = list(validate_schedule(le, ee, qa, lots, flows, qtimes, lot_constraints=cons, shift_times=st, special_eqp_map=seqp))
lead_gateB = sum(1 for e in errors if "闸B" in e)
lead_gateA = sum(1 for e in errors if "闸A" in e)
qtime_ov = sum(1 for e in errors if "Q-time" in e or "超时" in e)
order = sum(1 for e in errors if "顺序" in e)
missing = sum(1 for e in errors if "缺" in e)
print(f"errors: {len(errors)}  gateB={lead_gateB} gateA={lead_gateA} qtime={qtime_ov} order={order} missing={missing} (NI={os.environ.get('NI','0')})")
for e in errors[:16]: print("  -", e)

by = {(e.lot_name, e.step_name): e for e in le}
print("=== MOLDING / UF-DISPENSE / FC-REFLOW timeline ===")
for name in sorted(set(e.lot_name for e in le)):
    for st in ["A005-R1-MD-MOLDING","A005-P1-MD-MOLDING","A005-R1-UF-DISPENSE","A005-P1-UF-DISPENSE","A005-R1-FC-REFLOW","A005-P1-FC-REFLOW"]:
        e=by.get((name,st))
        if e: print(f"  {name:7s} {st:24s} {e.start_time:%m/%d %H:%M}->{e.end_time:%m/%d %H:%M}")
print("=== lead gaps ===")
for lot in lots:
    for lp in (lot.lead_pairs or []):
        e1=by.get((lp.lot1,lp.step1)); e2=by.get((lp.lot2,lp.step2))
        if e1 and e2:
            g=(e2.start_time-e1.end_time).total_seconds()/60
            print(f"  {lp.lot2} <- {lp.lot1}: {g:.1f}min")