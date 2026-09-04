#!/usr/bin/env python3
"""聚焦诊断：单场景背靠背间隙。追踪 lead_back_shift 决策与步骤时间线。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["SCHED_TRACE"] = "1"
from datetime import datetime, timedelta
import random, zlib
from tools import lead_stress as ls

def main():
    seed0 = 20260901
    name = sys.argv[1] if len(sys.argv) > 1 else "H_chain_lead"
    k = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    name_seed = zlib.crc32(name.encode("utf-8")) % 100000
    seed = seed0 * 1000 + k
    r = random.Random(seed + name_seed)
    gen = ls.SCENARIOS[name]
    res = gen(r)
    lots, flows, qtimes, ma = res[0], res[1], res[2], res[3]
    eqp = res[4] if len(res) > 4 else None
    win = res[5] if len(res) > 5 else None
    print(f"=== {name} case k={k} ===")
    for lot in lots:
        print(f"  {lot.lot_name}: start={lot.start_time:%m/%d %H:%M} pri={lot.priority} lead_pairs={[(lp.lot1,lp.step1,'->',lp.lot2,lp.step2) for lp in (lot.lead_pairs or [])]} pioneer={getattr(lot,'pioneer',False)}")
    for c in (eqp or []):
        print(f"  eqp_constraint: {c.eqp_name} {c.start_time_str}-{c.end_time_str}")
    steps = sorted({s.step_name for fl in flows for s in [fl]})

    print("\n=== schedule ===")
    le, ee, qa = ls.run_sched(lots, flows, qtimes, ma, eqp, win)
    by = {(e.lot_name, e.step_name): e for e in le}
    print("\n=== 步骤时间线 ===")
    fmap = ls.get_product_flow_map(flows)
    from collections import defaultdict
    order = defaultdict(list)
    for p, fs in fmap.items():
        order[p] = [s.step_name for s in fs]
    for lot in lots:
        os_ = order.get(lot.product_name, [])
        for st in os_:
            e = by.get((lot.lot_name, st))
            if e:
                print(f"  {lot.lot_name:9s} {st:12s} {e.start_time:%m/%d %H:%M}->{e.end_time:%m/%d %H:%M} eqp={e.eqp_id}")
    print("\n=== lead gaps ===")
    for g in ls.compute_gaps(le, lots):
        print(f"  gap = {g:.1f}min")

if __name__ == "__main__":
    main()