"""统一基准评测：覆盖 8-lot 恒组批 / 6P+6M 混合 / 4 个手动调整场景。
报告各场景 Q-time 超时总数与关键 over 明细，便于改动前后对比。
"""
import sys, os, copy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta
from data_loader import (load_lot_list, load_flow, load_step_ct, load_qtime, build_ct_lookup,
    auto_repair_step_ct, load_ftf_qty_change, load_special_lot_step, load_priority_wait,
    load_eqp_constraints, load_step_time_windows, load_shift_config, load_shift_change_times,
    load_special_eqp, load_lot_constraints, get_product_flow_map)
from models import Lot, SpecialEqp, ManualAdjust
from scheduler import schedule

BASE = os.path.join(os.path.dirname(__file__), "data")
flows = load_flow(os.path.join(BASE, "flow.csv")); fm = get_product_flow_map(flows)
qtimes = load_qtime(os.path.join(BASE, "qtime.csv"))
pw = load_priority_wait(os.path.join(BASE, "priority_wait.csv"))
cts = auto_repair_step_ct(flows, load_step_ct(os.path.join(BASE, "step_ct.csv")), None); ct = build_ct_lookup(cts)
ec = load_eqp_constraints(os.path.join(BASE, "eqp_constraint.csv"))
sc = load_shift_change_times(os.path.join(BASE, "shift_change_time.csv"))
sw = load_step_time_windows(os.path.join(BASE, "step_time_window.csv"))
sls = load_special_lot_step(os.path.join(BASE, "special_lot_step.csv"))
ftf = load_ftf_qty_change(os.path.join(BASE, "ftf_qty_change.csv"))
scfg = load_shift_config(os.path.join(BASE, "shift_config.csv"))
st = sorted(tuple(map(int, x.start_time_str.split(":"))) for x in scfg if getattr(x, "start_time_str", None))
se = load_special_eqp(os.path.join(BASE, "special_eqp.csv"))
allf = [s for fl in fm.values() for s in fl]
no_ma = None  # 无 manual_adjust.csv 则不加载

def count_over(qa):
    return len([a for a in qa if a.status != "OK"])

def run_lots(lots, ma=None, special=None):
    le, ee, qa = schedule(lots=[copy.copy(l) for l in lots], flows=allf, ct_lookup=ct, qtimes=qtimes,
        shift_times=st, ftf_qty_change=ftf, special_lot_step_lookup=sls, priority_wait_map=pw,
        eqp_constraints=ec, step_time_window_constraints=sw, shift_change_times=sc,
        manual_adjusts=ma, special_eqp_map=special or {})
    return le, ee, qa, count_over(qa)

def scenario_realdata():
    """4 lot (PC1/real1/PC2/real2) 真实数据 + 手动调整"""
    real_lots = load_lot_list(os.path.join(BASE, "lot_list.csv"),
                              constraints_filepath=os.path.join(BASE, "lot_constraints.csv"))
    out = []
    # baseline
    le, ee, qa, n = run_lots(real_lots, special=se)
    out.append(("T1 基线(4lot)", n, [(a.lot_name, a.over_minutes) for a in qa if a.status != "OK"]))
    ma = [ManualAdjust("real1", "A005-R1-UF-CURE", datetime(2026, 8, 18, 20, 0), "delay")]
    le, ee, qa, n = run_lots(real_lots, ma)
    out.append(("T2 delay real1 CURE>=20:00", n, [(a.lot_name, a.over_minutes) for a in qa if a.status != "OK"]))
    ma = [ManualAdjust("real1", "A005-R1-FC-REFLOW", datetime(2026, 8, 17, 20, 0), "delay")]
    le, ee, qa, n = run_lots(real_lots, ma)
    out.append(("T3 delay real1 FC-REFLOW>=20:00", n, [(a.lot_name, a.over_minutes) for a in qa if a.status != "OK"]))
    return out

def scenario_mixed(together=True):
    start = datetime(2026, 8, 17, 8, 30)
    lots = []
    for i in range(6):
        lots.append(Lot(lot_name=f"P{i}", priority=(1, 1), qty=1, carrier_id=f"C{i:03d}",
                        current_step_name="A005-P1-FC-DUMMY", product_name="A005-P1",
                        lot_state="wait", running_time=0, references=[],
                        start_time=start + timedelta(hours=i % 6)))
    for i in range(6):
        lots.append(Lot(lot_name=f"M{i}", priority=(2, 1), qty=1, carrier_id=f"MC{i:03d}",
                        current_step_name="A005-R1-AB1IQC-WFS", product_name="A005-MA",
                        lot_state="wait", running_time=0, references=[],
                        start_time=start + timedelta(hours=(i % 6) + 3)))
    special = {"PKPOV001": SpecialEqp("PKPOV001", 4, 25, together)} if together is not None else {}
    le, ee, qa, n = run_lots(lots, special=special)
    missing = [l.lot_name for l in lots if not any(e.lot_name == l.lot_name and "UF-CURE" in e.step_name for e in le)]
    return n, missing, [(a.lot_name, a.over_minutes) for a in qa if a.status != "OK"]

def scenario_batch8():
    start = datetime(2026, 8, 17, 8, 30)
    lots = [Lot(lot_name=f"loop{i}", priority=(1, 1), qty=1, carrier_id=f"b{i:03d}",
                current_step_name="A005-P1-FC-DUMMY", product_name="A005-P1",
                lot_state="wait", running_time=0, references=[],
                start_time=start + timedelta(minutes=60 * i)) for i in range(8)]
    ke = {"PKPOV001": SpecialEqp("PKPOV001", 4, 25, True)}
    le, ee, qa, n = run_lots(lots, special=ke)
    furn = {}
    for e in sorted([e for e in ee if e.eqp_id == "PKPOV001" and "UF-CURE" in e.step_name], key=lambda e: e.start_time):
        furn.setdefault((e.start_time, e.end_time), []).append(e.lot_name)
    return n, len(furn), [(a.lot_name, a.over_minutes) for a in qa if a.status != "OK"]

if __name__ == "__main__":
    print("==== 基准评测 ====\n")
    tot = 0
    for name, n, det in scenario_realdata():
        print(f"[{name}] 超时={n}  {det}")
        tot += n
    n, missing, det = scenario_mixed(True)
    print(f"[T4 6P+6M together=True] 超时={n} 缺CURE={missing}  {det}")
    tot += n
    n, nf, det = scenario_batch8()
    print(f"[T5 8P 恒组批] 超时={n} 炉次数={nf}  {det}")
    tot += n
    print(f"\n======== 总计超时 = {tot} ========")