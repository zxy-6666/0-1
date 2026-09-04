#!/usr/bin/env python3
import sys, os, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SCHED_TRACE", "1")
import logging
logging.getLogger("scheduler").setLevel(logging.CRITICAL)
logging.disable(logging.CRITICAL)
from tools.lead_stress import gen_scenario_H, run_sched, compute_gaps, FLOW_WIDE, Q_TIGHT, ST
from validation import validate_schedule
from data_loader import get_product_flow_map

rng = random.Random(20260901222+431)
lots, flows, qtimes, ma = gen_scenario_H(rng)
print("lots:", [(l.lot_name, l.priority, (l.start_time.strftime('%m%d %H:%M') if l.start_time else None)) for l in lots])
le, ee, qa = run_sched(lots, flows, qtimes, ma)
by={(e.lot_name,e.step_name):e for e in le}
for name in ["CHAINA","CHAINB","CHAINC"]:
    for st in ["BAKE","PLASMA","DISPENSE","CURE"]:
        e=by.get((name,st))
        if e: print(f"  {name} {st:9s} {e.start_time:%m-%d %H:%M}->{e.end_time:%m-%d %H:%M}")
print("gaps:", compute_gaps(le, lots))
cons=[c for l in lots for c in (l.references or [])]
errs=list(validate_schedule(le,ee,qa,lots,flows,qtimes,lot_constraints=cons,shift_times=ST))
print("errors:", len(errs))
for e in errs[:10]:print("  ",e)