"""诊断：紧链调度路径与余量分布。
输出每条紧 Q 链的起点站/终点站排程时刻、跨度、余量，以及链路是否由整链块排定。
用法: python3 tools/diag_margin.py [safety_pct] [min_margin]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

from data_loader import (
    load_lot_list, load_flow, load_step_ct,
    load_qtime, build_ct_lookup, auto_repair_step_ct,
    load_ftf_qty_change,
    load_special_lot_step, load_priority_wait, load_lot_constraints,
    load_eqp_constraints, load_shift_change_times,
    load_step_time_windows, load_shift_config, load_manual_adjusts,
    load_special_eqp,
)
from scheduler import schedule, TIGHT_CHAIN_THRESHOLD
from validation import _qtime_margins_from_entries


def main():
    safety_pct = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    min_margin = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

    lots = load_lot_list(f"{DATA_DIR}/lot_list.csv",
                         constraints_filepath=f"{DATA_DIR}/lot_constraints.csv")
    flows = load_flow(f"{DATA_DIR}/flow.csv")
    step_cts = load_step_ct(f"{DATA_DIR}/step_ct.csv")
    step_cts = auto_repair_step_ct(flows, step_cts, step_ct_filepath=f"{DATA_DIR}/step_ct.csv")
    qtimes = load_qtime(f"{DATA_DIR}/qtime.csv")
    ct_lookup = build_ct_lookup(step_cts)
    ftf_qty_change = load_ftf_qty_change(f"{DATA_DIR}/ftf_qty_change.csv")
    special_lot_step_lookup = load_special_lot_step(f"{DATA_DIR}/special_lot_step.csv")
    priority_wait_map = load_priority_wait(f"{DATA_DIR}/priority_wait.csv")
    eqp_constraints = load_eqp_constraints(f"{DATA_DIR}/eqp_constraint.csv")
    step_time_window_constraints = load_step_time_windows(f"{DATA_DIR}/step_time_window.csv")
    shift_configs = load_shift_config(f"{DATA_DIR}/shift_config.csv")
    shift_change_times = load_shift_change_times(f"{DATA_DIR}/shift_change_time.csv")
    manual_adjusts = load_manual_adjusts(f"{DATA_DIR}/manual_adjust.csv")
    special_eqp_map = load_special_eqp(f"{DATA_DIR}/special_eqp.csv")
    lot_constraints = load_lot_constraints(f"{DATA_DIR}/lot_constraints.csv")

    shift_times = []
    for sc in shift_configs:
        try:
            h, m = map(int, sc.start_time_str.split(":"))
            shift_times.append((h, m))
        except (ValueError, AttributeError):
            pass
    shift_times.sort()

    le, ee, qa = schedule(
        lots=lots, flows=flows, ct_lookup=ct_lookup, qtimes=qtimes,
        shift_times=shift_times,
        ftf_qty_change=ftf_qty_change,
        special_lot_step_lookup=special_lot_step_lookup,
        priority_wait_map=priority_wait_map,
        eqp_constraints=eqp_constraints,
        step_time_window_constraints=step_time_window_constraints,
        shift_change_times=shift_change_times,
        manual_adjusts=manual_adjusts,
        special_eqp_map=special_eqp_map,
        resolve_max_iterations=16,
        qtight_safety_margin=safety_pct,
        qtight_min_margin=min_margin,
    )
    margins = _qtime_margins_from_entries(le, lots, qtimes)
    prod = {l.lot_name: l.product_name for l in lots}
    by_lot = {}
    for e in le:
        by_lot.setdefault(e.lot_name, []).append(e)
    rows = []
    for q in qtimes:
        if q.max_duration is None or q.max_duration > TIGHT_CHAIN_THRESHOLD:
            continue
        for ln, es in by_lot.items():
            se = next((x for x in es if x.step_name == q.start_step), None)
            ee2 = next((x for x in es if x.step_name == q.end_step), None)
            if se is None or ee2 is None:
                continue
            m = margins.get((ln, q.start_step, q.end_step))
            safe = max(q.max_duration * safety_pct / 100.0, min_margin)
            rows.append((m, ln, q.start_step, q.end_step, q.max_duration, se.start_time, se.end_time,
                         ee2.start_time, ee2.end_time, safe))
    rows.sort()
    print(f"== 紧链（D<={TIGHT_CHAIN_THRESHOLD}min）余量分布：共 {len(rows)} 条 ==")
    for m, ln, qs, qe, D, sst, sen, est, een, safe in rows:
        span = (een - sst).total_seconds() / 60.0
        flag = " <<< 低于安全" if m < safe else ""
        print(f"  {ln} {qs}->{qe}: margin={m:.1f} D={D} span={span:.0f} "
              f"S={sst:%m/%d %H:%M} E={een:%m/%d %H:%M} safe={safe:.0f}{flag}")


if __name__ == "__main__":
    main()
