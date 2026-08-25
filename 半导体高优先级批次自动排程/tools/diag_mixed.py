"""混合 P+M qty=1：确认是否有 lot 真的缺 UF-CURE 条目"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta
from data_loader import *
from models import Lot, SpecialEqp
from scheduler import schedule

base = os.path.join(os.path.dirname(__file__), "data")
flows = load_flow(os.path.join(base, "flow.csv")); fm = get_product_flow_map(flows)
qtimes = load_qtime(os.path.join(base, "qtime.csv")); pw = load_priority_wait(os.path.join(base, "priority_wait.csv"))
cts = auto_repair_step_ct(flows, load_step_ct(os.path.join(base, "step_ct.csv")), None); ct = build_ct_lookup(cts)
ec = load_eqp_constraints(os.path.join(base, "eqp_constraint.csv")); sc = load_shift_change_times(os.path.join(base, "shift_change_time.csv"))
sw = load_step_time_windows(os.path.join(base, "step_time_window.csv")); ma = load_manual_adjusts(os.path.join(base, "manual_adjust.csv"))
sls = load_special_lot_step(os.path.join(base, "special_lot_step.csv")); ftf = load_ftf_qty_change(os.path.join(base, "ftf_qty_change.csv"))
scfg = load_shift_config(os.path.join(base, "shift_config.csv")); st = sorted(tuple(map(int, x.start_time_str.split(":"))) for x in scfg if getattr(x, "start_time_str", None))
allf = [s for fl in fm.values() for s in fl]

def run(together):
    start = datetime(2026, 8, 17, 8, 30)
    lots = []
    for i in range(6):
        lots.append(Lot(lot_name=f"P{i}", priority=(1, 1), qty=1, carrier_id=f"C{i:03d}",
                        current_step_name="A005-P1-FC-DUMMY", product_name="A005-P1",
                        target_step=None, lot_state="wait", running_time=0,
                        start_time=start + timedelta(hours=i % 6), references=[]))
    for i in range(6):
        lots.append(Lot(lot_name=f"M{i}", priority=(2, 1), qty=1, carrier_id=f"MC{i:03d}",
                        current_step_name="A005-R1-AB1IQC-WFS", product_name="A005-MA",
                        target_step=None, lot_state="wait", running_time=0,
                        start_time=start + timedelta(hours=(i % 6) + 3), references=[]))
    se = {"PKPOV001": SpecialEqp("PKPOV001", 4, 25, together)} if together is not None else {}
    le, ee, qa = schedule(lots=lots, flows=allf, ct_lookup=ct, qtimes=qtimes, shift_times=st,
        ftf_qty_change=ftf, special_lot_step_lookup=sls, priority_wait_map=pw, eqp_constraints=ec,
        step_time_window_constraints=sw, shift_change_times=sc, manual_adjusts=ma,
        special_eqp_map=se, resolve_max_iterations=10)
    print(f"=== together={together} ===")
    for lot in lots:
        cura = [e for e in le if e.lot_name == lot.lot_name and "UF-CURE" in e.step_name]
        if not cura:
            # 打印该 lot 所有含 'CURE' 的步骤名 + 前一步
            names = [e.step_name for e in le if e.lot_name == lot.lot_name]
            print(f"  [缺UF-CURE] {lot.lot_name} 步骤数={len(names)} cures={[n for n in names if 'CURE' in n]} last={names[-1] if names else None}")
    print(f"  CURE总条目={sum(1 for e in le if 'UF-CURE' in e.step_name)} Q超时={len([a for a in qa if a.status!='OK'])}")

for t in [True, False, None]:
    run(t)
    print()