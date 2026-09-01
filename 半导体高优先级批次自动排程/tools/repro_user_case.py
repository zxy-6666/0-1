"""复现用户当前算例：按 web/app.py _run_schedule 的数据加载与参数，跑 schedule_optimized。

用法: python3 tools/repro_user_case.py [max_iterations] [seed] [safety_pct] [min_margin] [gradient]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

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
from optimizer import schedule_optimized
from validation import validate_schedule, compute_objective


def main():
    max_iter = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 32258064
    safety_pct = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
    min_margin = float(sys.argv[4]) if len(sys.argv) > 4 else 30.0
    gradient = float(sys.argv[5]) if len(sys.argv) > 5 else 3.0

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

    print(f"== 复现运行: max_iter={max_iter} seed={seed} safety%={safety_pct} min_margin={min_margin}min gradient={gradient} ==",
          flush=True)
    le, ee, qa, meta = schedule_optimized(
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
        lot_constraints=lot_constraints,
        resolve_max_iterations=16,
        max_iterations=max_iter,
        seed=seed,
        early_stop_patience=5,
        qtight_safety_margin=safety_pct,
        qtight_min_margin=min_margin,
        qtime_shortfall_gradient=gradient,
    )
    errors = validate_schedule(
        le, ee, qa, lots, flows, qtimes, lot_constraints=lot_constraints,
        shift_times=shift_times, special_eqp_map=special_eqp_map)
    print(f"valid_iterations={meta.get('valid_iterations')} total={meta.get('total_iterations')} "
          f"warning={meta.get('warning')}")
    print(f"best_score={meta.get('best_score')} min_qtime_margin={meta.get('min_qtime_margin')}")
    print(f"validation_errors: {len(errors)}")
    for e in errors[:10]:
        print("  -", e)

    ss = min(e.start_time for e in le)
    obj = compute_objective(le, lots, ss, qtimes=qtimes,
                            qtime_safety_margin_pct=safety_pct,
                            qtime_min_margin_min=min_margin,
                            qtime_shortfall_gradient=gradient)
    margins = obj.get("qtime_margins") or {}
    prod = {l.lot_name: l.product_name for l in lots}
    rows = []
    for (ln, qs, qe), m in sorted(margins.items()):
        D = None
        for q in qtimes:
            if q.product_name == prod.get(ln) and q.start_step == qs and q.end_step == qe:
                D = q.max_duration
                break
        rows.append((m, ln, qs, qe, D))
    rows.sort()
    print("== 余量最低的 8 条 Q 链 ==")
    for m, ln, qs, qe, D in rows[:8]:
        safe = max(D * safety_pct / 100.0, min_margin) if D else 0
        print(f"  {ln} {qs}->{qe}: margin={m:.1f}min / D={D} ({100*m/D:.1f}%) / safe={safe:.0f}min")


if __name__ == "__main__":
    main()
