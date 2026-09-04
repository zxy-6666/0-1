#!/usr/bin/env python3
import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SCHED_TRACE", "1")
import logging
logging.getLogger("scheduler").setLevel(logging.CRITICAL)
logging.disable(logging.CRITICAL)

from models import (Lot, FlowStep, QTimeConstraint, LeadPair, LotConstraint, EqpConstraint)
from tools.lead_stress import (FLOW_MOLD, Q_MOLD, CT, ST)

BASE = __import__("datetime").datetime(2026, 9, 1, 9, 0)
from datetime import datetime, timedelta

def mk_lot(name, start_delta, priority=(1,1), qty=1, cur="DAF-BAKE"):
    return Lot(lot_name=name, carrier_id=f"C{name}", product_name="PROD", qty=qty,
               priority=priority, current_step_name=cur, target_step=None,
               lot_state="wait", running_time=0, start_time=BASE+start_delta,
               references=[], lead_pairs=[])

def attach_lead(follow, lead_lot):
    follow.lead_pairs = []
    lead_lot.lead_pairs = [LeadPair(lead_lot.lot_name, "MD-MOLDING", follow.lot_name, "MD-MOLDING", "p0")]
    follow.references = [LotConstraint(follow.lot_name, lead_lot.lot_name, "MD-MOLDING", "MD-MOLDING", None, lead_id="p0")]

# 复刻 P：3 对，全部落在窗口，h0 不同
eqp = [EqpConstraint("EQP-MOLD", "22:00", "08:30", "-1")]
lots=[]
h0s=[7.5,9.0,10.5]
ds=[0.5,1.0,1.5]
for i,(h0,d) in enumerate(zip(h0s,ds)):
    ld=mk_lot(f"PLEAD{i}", timedelta(hours=h0), priority=(1,1), cur="DAF-BAKE")
    fl=mk_lot(f"PFOLLOW{i}", timedelta(hours=h0+d), priority=(3,1), cur="DAF-BAKE")
    attach_lead(fl,ld)
    lots += [ld,fl]

from scheduler import schedule
le,ee,qa = schedule(lots=lots, flows=FLOW_MOLD, ct_lookup=CT, qtimes=Q_MOLD, shift_times=ST,
                    ftf_qty_change=None, special_lot_step_lookup=None, priority_wait_map={},
                    eqp_constraints=eqp, step_time_window_constraints=[], shift_change_times=[],
                    manual_adjusts=[], special_eqp_map={}, resolve_max_iterations=10)

by={(e.lot_name,e.step_name):e for e in le}
print("=== MOLDING timeline ===")
for name in sorted(set(e.lot_name for e in le)):
    for st in ["DAF-BAKE","WARP-MEAS","MD-PLASMA","MD-MOLDING"]:
        e=by.get((name,st))
        if e: print(f"  {name} {st:12s} {e.start_time:%m/%d %H:%M}->{e.end_time:%m/%d %H:%M}")
print("=== gaps ===")
for lot in lots:
    for lp in lot.lead_pairs or []:
        e1=by.get((lp.lot1,lp.step1)); e2=by.get((lp.lot2,lp.step2))
        if e1 and e2:
            g=(e2.start_time-e1.end_time).total_seconds()/60
            print(f"  {lp.lot2} <- {lp.lot1}: {g:.1f}min")