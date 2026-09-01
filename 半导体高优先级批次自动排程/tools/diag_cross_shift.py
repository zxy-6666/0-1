"""诊断：紧链跨班次行为。构造一条紧 Q 链（PLASMA→DISPENSE D=240），
让链首自然落在班次切换时刻附近，观察当前排程是否跨班次/起点是否在交接班。
用法: python3 tools/diag_cross_shift.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from collections import defaultdict

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
from models import Lot
from scheduler import schedule, TIGHT_CHAIN_THRESHOLD, _detect_qtime_cross_shift


def main():
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

    # 只看 PC1 lot：把 DISPENSE 手动钉到 20:00（紧邻 20:30 交接班），使 UF-PLASMA 落在
    # 白班、DISPENSE 跨入夜班，观察紧链是否整体后移到交接班之后。
    from copy import deepcopy
    from models import ManualAdjust
    lots2 = deepcopy(lots)
    ma = []
    for lot in lots2:
        lot.start_time = datetime(2026, 8, 18, 8, 10)
        if lot.lot_name == "PC1":
            ma.append(ManualAdjust(lot_name="PC1", step_name="A005-P1-UF-DISPENSE",
                                   delay_to=datetime(2026, 8, 18, 20, 0), mode="delay"))
    # 只保留 PC1/PC2 两个 lot，减少设备竞争噪声
    lots2 = [l for l in lots2 if l.lot_name in ("PC1", "PC2")]

    le, ee, qa = schedule(
        lots=lots2, flows=flows, ct_lookup=ct_lookup, qtimes=qtimes,
        shift_times=shift_times,
        ftf_qty_change=ftf_qty_change,
        special_lot_step_lookup=special_lot_step_lookup,
        priority_wait_map=priority_wait_map,
        eqp_constraints=eqp_constraints,
        step_time_window_constraints=step_time_window_constraints,
        shift_change_times=shift_change_times,
        manual_adjusts=ma,
        special_eqp_map=special_eqp_map,
        resolve_max_iterations=16,
        qtight_safety_margin=20.0,
        qtight_min_margin=30.0,
    )
    warns = _detect_qtime_cross_shift(le, qtimes, shift_times)
    print("== 紧链跨班次告警 ==")
    for w in warns:
        print("  *", w)
    print("== Q-time alerts ==")
    for a in qa:
        if a.status != "OK":
            print(f"  {a.lot_name} {a.qtime_rule}: {a.status} over={a.over_minutes}")
    by_lot = defaultdict(list)
    for e in le:
        by_lot[e.lot_name].append(e)
    for _e in le:
        if _e.lot_name == "PC1" and "UF-PLASMA" in _e.step_name:
            print(f"== PC1 UF-PLASMA entry: start={_e.start_time} end={_e.end_time} eqp={_e.eqp_id} ct={_e.ct}")
    print("\n== 紧链相关步骤排程 ==")
    for ln, es in sorted(by_lot.items()):
        for e in es:
            if "PLASMA" in e.step_name or "DISPENSE" in e.step_name or "BAKE" in e.step_name \
                    or "MOLDING" in e.step_name or "CURE" in e.step_name:
                print(f"  {ln} {e.step_name}: {e.start_time:%m/%d %H:%M} -> {e.end_time:%m/%d %H:%M}")


if __name__ == "__main__":
    main()
