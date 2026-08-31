import sys, os
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import Lot, FlowStep, QTimeConstraint, LotConstraint, LeadPair
from scheduler import schedule
from validation import validate_schedule

# 每步 2 台设备 → 两批各占一台，无跨批设备争用；CT 小；Q 预算宽松
flow = [
    FlowStep(product_name="PROD", step_number="10", step_name="BAKE", eqp_ids=["EQP-BAKE1", "EQP-BAKE2"]),
    FlowStep(product_name="PROD", step_number="20", step_name="PLASMA", eqp_ids=["EQP-PLASMA1", "EQP-PLASMA2"]),
    FlowStep(product_name="PROD", step_number="30", step_name="DISPENSE", eqp_ids=["EQP-DISP1", "EQP-DISP2"]),
    FlowStep(product_name="PROD", step_number="40", step_name="CURE", eqp_ids=["EQP-CURE1", "EQP-CURE2"]),
]
ct = {("PROD","10",1):30.0, ("PROD","20",1):30.0, ("PROD","30",1):30.0, ("PROD","40",1):60.0}
qtimes = [
    QTimeConstraint("PROD","BAKE","PLASMA","track in","track out",480),
    QTimeConstraint("PROD","PLASMA","DISPENSE","track in","track out",480),
    QTimeConstraint("PROD","DISPENSE","CURE","track in","track out",480),
]
BASE = datetime(2026,9,1,9,0)

def make_lot(name, extra):
    st = BASE + extra
    return Lot(lot_name=name, carrier_id=f"C{name}", product_name="PROD", qty=1,
        priority=(1,1), current_step_name="BAKE", target_step=None,
        lot_state="wait", running_time=0, start_time=st, references=[])

lead = make_lot("LEAD", timedelta(0))
follow = make_lot("FOLLOW", timedelta(hours=2))   # FOLLOW 延后 2h 开始 → 制造可回拉的空带
lead.lead_pairs = [LeadPair("LEAD","DISPENSE","FOLLOW","DISPENSE","lead0")]
follow.references = [LotConstraint(lot_name="FOLLOW", reference_lot="LEAD",
    reference_step="DISPENSE", start_step="DISPENSE", start_mod=None, lead_id="lead0")]

st = [(9,0),(17,0)]
le, ee, qa = schedule(lots=[lead,follow], flows=flow, ct_lookup=ct, qtimes=qtimes,
    shift_times=st, ftf_qty_change=None, special_lot_step_lookup=None,
    priority_wait_map={}, eqp_constraints=[], step_time_window_constraints=[],
    shift_change_times=[], manual_adjusts=[], special_eqp_map={}, resolve_max_iterations=10)
# 对照组：关掉 back-shift（清空 lead_pairs），仅保留闸A 引用
import copy
lead_nb = copy.deepcopy(lead); lead_nb.lead_pairs = []
follow_nb = copy.deepcopy(follow)
le2, ee2, qa2 = schedule(lots=[lead_nb,follow_nb], flows=flow, ct_lookup=ct, qtimes=qtimes,
    shift_times=st, ftf_qty_change=None, special_lot_step_lookup=None,
    priority_wait_map={}, eqp_constraints=[], step_time_window_constraints=[],
    shift_change_times=[], manual_adjusts=[], special_eqp_map={}, resolve_max_iterations=10)
def find2(ls, lot, step):
    for e in ls:
        if e.lot_name==lot and e.step_name==step: return e
    return None
print("WITHOUT back-shift:")
print(f"  LEAD.DISPENSE.end={find2(le2,'LEAD','DISPENSE').end_time}  FOLLOW.DISPENSE.start={find2(le2,'FOLLOW','DISPENSE').start_time}  gap={(find2(le2,'FOLLOW','DISPENSE').start_time-find2(le2,'LEAD','DISPENSE').end_time).total_seconds()/60}min")

def find(lot, step):
    for e in le:
        if e.lot_name==lot and e.step_name==step: return e
    return None

print("=== clean back-shift probe ===")
for lot in ["LEAD","FOLLOW"]:
    for s in ["BAKE","PLASMA","DISPENSE","CURE"]:
        e = find(lot,s)
        if e: print(f"{lot:6} {s:8} {e.start_time} -> {e.end_time}")
        else: print(f"{lot:6} {s:8} MISSING")

l_disp = find("LEAD","DISPENSE"); f_disp = find("FOLLOW","DISPENSE")
if l_disp and f_disp:
    print(f"gateA gap minute: {(f_disp.start_time - l_disp.end_time).total_seconds()/60}")
errs = validate_schedule(le,ee,qa,[lead,follow],flow,qtimes)
print("\nvalidation:", errs[:5] if errs else "NONE")