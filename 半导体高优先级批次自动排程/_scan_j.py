#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["SCHED_TRACE"] = "0"
from datetime import datetime, timedelta
import random, zlib
from tools import lead_stress as ls
from validation import validate_schedule

seed0 = 20260901
name = "J_long_ct"
ns = zlib.crc32(name.encode("utf-8")) % 100000
for k in range(8):
    seed = seed0 * 1000 + k
    r = random.Random(seed + ns)
    lots, flows, qtimes, ma = ls.gen_scenario_J(r)
    cons = [c for l in lots for c in (l.references or [])]
    le, ee, qa = ls.run_sched(lots, flows, qtimes, ma)
    errs = list(validate_schedule(le, ee, qa, lots, flows, qtimes,
                                  lot_constraints=cons, shift_times=ls.ST, special_eqp_map={}))
    qerr = [e for e in errs if "Q-time" in e]
    if qerr:
        print(f"case k={k}: {len(errs)} errors, {len(qerr)} Q:")
        for e in qerr[:4]:
            print(f"    {e}")
        print("    gaps:", ls.compute_gaps(le, lots))