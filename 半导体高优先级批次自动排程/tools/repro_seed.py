#!/usr/bin/env python3
"""复现 fuzz 某个 seed 的算例，dump 调度结果细节（哪些步骤缺失/卡在哪）。
用法: python repro_seed.py <seed> [--pass1|--pass2]
"""
import sys, os, random, copy
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SCHED_TRACE", "0")

import logging
logging.getLogger("scheduler").setLevel(logging.CRITICAL)
logging.disable(logging.CRITICAL)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fuzz_scheduler import load_base, mutate
from scheduler import schedule, _run_schedule_pass, _collect_ref_release_forecast
from data_loader import get_product_flow_map

SEED0 = 20260828


def main():
    args = sys.argv[1:]
    seed = int(args[0]) if args else 20260828000
    which = "both"
    for a in args:
        if a == "--pass1":
            which = "pass1"
        elif a == "--pass2":
            which = "pass2"

    b = load_base()
    rng = random.Random(seed)
    lots, flows, qtimes, se, ma, cons = mutate(b, rng, benign=False)
    fm = get_product_flow_map(flows)

    print(f"===== seed={seed} 算例概况 =====")
    for l in lots:
        print(f"  lot={l.lot_name} product={l.product_name} prio={l.priority} qty={l.qty} "
              f"start={l.start_time} cur={l.current_step_name} target={l.target_step} "
              f"refs={[(r.reference_lot, r.reference_step, r.start_mod, r.start_step) for r in (l.references or [])]}")
    print(f"  special_eqp={ {k: (v.max_lots, v.max_qty, v.together) for k, v in se.items()} }")
    print(f"  manual_adjusts={[(m.lot_name, m.step_name, m.delay_to) for m in ma]}")

    # ---- 跑单遍 ----
    def run_pass(rff):
        le, ee, qa = _run_schedule_pass(
            lots=copy.deepcopy(lots), flows=flows, ct_lookup=b["ct"], qtimes=qtimes,
            shift_times=b["st"], ftf_qty_change=b["ftf"],
            special_lot_step_lookup=b["sls"], priority_wait_map=b["pw"],
            eqp_constraints=b["ec"], step_time_window_constraints=b["sw"],
            shift_change_times=b["sc"], manual_adjusts=ma,
            special_eqp_map=se, resolve_max_iterations=10,
            ref_release_forecast=rff)
        return le, ee, qa

    _le1, _ee1, _qa1 = run_pass(None)
    _forecast = _collect_ref_release_forecast(_le1, lots, b["st"])
    _le2, _ee2, _qa2 = run_pass(_forecast)

    for label, le, ee, qa in (("PASS1", _le1, _ee1, _qa1), ("PASS2", _le2, _ee2, _qa2)):
        if which not in ("both", label.lower()):
            continue
        print(f"\n===== {label} 结果 =====")
        by_lot = defaultdict(list)
        for e in le:
            by_lot[e.lot_name].append(e)
        for l in lots:
            es = sorted(by_lot.get(l.lot_name, []), key=lambda e: e.start_time)
            fl = fm.get(l.product_name)
            idx = {s.step_name: i for i, s in enumerate(fl)} if fl else {}
            # 缺失步骤：与调度器一致，从 current_step 起到 target（无则末尾）为止
            got = set(e.step_name for e in es)
            exp = set(s.step_name for s in fl)
            if fl:
                _sidx = idx.get(l.current_step_name, 0)
                _eidx = len(fl)
                if l.target_step and l.target_step in idx:
                    _eidx = idx[l.target_step] + 1
                exp = set(s.step_name for s in fl[_sidx:_eidx])
            missing = sorted(exp - got, key=lambda s: idx.get(s, 9999))
            print(f"\n  -- lot={l.lot_name} 已排={len(es)} 缺失={len(missing)}")
            if missing:
                print(f"     缺失: {missing[:30]}{' ...' if len(missing) > 30 else ''}")
            # 打印已排步骤（时间线）
            for e in es:
                print(f"     {e.step_name}  {e.start_time.strftime('%m/%d %H:%M')}~{e.end_time.strftime('%m/%d %H:%M')} eqp={e.eqp_id}")
        # Q-time 告警
        qbad = [a for a in qa if a.status != "OK"]
        print(f"\n  Q-time 告警: {len(qbad)}")
        for a in qbad[:10]:
            print(f"     {a.lot_name} {a.qtime_rule} over={a.over_minutes}min")
        # 引用违背
        from validation import _check_references
        rv = _check_references(le, cons, b["st"])
        print(f"  reference 违背: {len(rv)}")
        for r in rv[:10]:
            print(f"     {r}")


if __name__ == "__main__":
    main()
