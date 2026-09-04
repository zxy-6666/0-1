import os, sys, zlib, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['SCHED_TRACE'] = '0'
from tools import lead_stress as ls

seed0 = 20260901
name = 'P_molding_multi_pair'
ns = zlib.crc32(name.encode()) % 100000
best = None
for k in range(120):
    r = random.Random(seed0 * 1000 + k + ns)
    lots, flows, qtimes, ma, eqp = ls.SCENARIOS[name](r)
    cons = [c for l in lots for c in (l.references or [])]
    le, ee, qa = ls.run_sched(lots, flows, qtimes, ma, eqp_constraints=eqp)
    gaps = ls.compute_gaps(le, lots)
    mx = max(gaps) if gaps else 0
    if mx > 200:
        best = (k, lots, le, gaps)
        break
k, lots, le, gaps = best
print('case k=%d max_gap=%.0fmin pairs=%d' % (k, max(gaps), len(lots) // 2))
by = {(e.lot_name, e.step_name): e for e in le}
print('=== MD-MOLDING (EQP-MOLD) 占用序列 ===')
mold = [e for e in le if e.step_name == 'MD-MOLDING']
mold.sort(key=lambda x: x.start_time)
for e in mold:
    print('  %s: %s -> %s' % (e.lot_name, e.start_time, e.end_time))
print('=== 各链完整时间线 ===')
for lot in lots:
    line = []
    for s in ls.FLOW_MOLD:
        e = by.get((lot.lot_name, s.step_name))
        if e:
            line.append('%s[%s-%s]' % (e.step_name, e.start_time.strftime('%m/%d %H:%M'),
                                       e.end_time.strftime('%m/%d %H:%M')))
    print('  %s qty=%d start=%s: %s' % (lot.lot_name, lot.qty,
                                       lot.start_time.strftime('%m/%d %H:%M'), ' '.join(line)))