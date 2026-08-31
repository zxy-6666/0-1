"""诊断：恢复 wait 后 lead 衔接 gap（临时脚本）。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_loader import (
    load_lot_list, load_flow, load_step_ct, load_qtime, build_ct_lookup,
    auto_repair_step_ct, load_ftf_qty_change, load_special_lot_step,
    load_priority_wait, load_eqp_constraints, load_step_time_windows,
    load_shift_config, load_shift_change_times, load_manual_adjusts,
    load_special_eqp, load_lot_constraints,
)
from scheduler import schedule
from validation import validate_schedule

DATA = "data"
lots = load_lot_list(f"{DATA}/lot_list.csv", constraints_filepath=f"{DATA}/lot_constraints.csv")
flows = load_flow(f"{DATA}/flow.csv")
step_cts = load_step_ct(f"{DATA}/step_ct.csv")
step_cts = auto_repair_step_ct(flows, step_cts, step_ct_filepath=None)
qtimes = load_qtime(f"{DATA}/qtime.csv")
ct_lookup = build_ct_lookup(step_cts)
ftf_qty_change = load_ftf_qty_change(f"{DATA}/ftf_qty_change.csv") if os.path.exists(f"{DATA}/ftf_qty_change.csv") else {}
special_lot_step_lookup = load_special_lot_step(f"{DATA}/special_lot_step.csv") if os.path.exists(f"{DATA}/special_lot_step.csv") else {}
priority_wait_map = load_priority_wait(f"{DATA}/priority_wait.csv") if os.path.exists(f"{DATA}/priority_wait.csv") else {}
eqp_constraints = load_eqp_constraints(f"{DATA}/eqp_constraint.csv") if os.path.exists(f"{DATA}/eqp_constraint.csv") else []
step_time_window_constraints = load_step_time_windows(f"{DATA}/step_time_window.csv") if os.path.exists(f"{DATA}/step_time_window.csv") else []
shift_configs = load_shift_config(f"{DATA}/shift_config.csv") if os.path.exists(f"{DATA}/shift_config.csv") else []
shift_change_times = load_shift_change_times(f"{DATA}/shift_change_time.csv") if os.path.exists(f"{DATA}/shift_change_time.csv") else []
manual_adjusts = load_manual_adjusts(f"{DATA}/manual_adjust.csv") if os.path.exists(f"{DATA}/manual_adjust.csv") else []
special_eqp_map = load_special_eqp(f"{DATA}/special_eqp.csv") if os.path.exists(f"{DATA}/special_eqp.csv") else {}
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
    shift_times=shift_times, ftf_qty_change=ftf_qty_change,
    special_lot_step_lookup=special_lot_step_lookup,
    priority_wait_map=priority_wait_map, eqp_constraints=eqp_constraints,
    step_time_window_constraints=step_time_window_constraints,
    shift_change_times=shift_change_times, manual_adjusts=manual_adjusts,
    special_eqp_map=special_eqp_map, resolve_max_iterations=10)
errs = validate_schedule(le, ee, qa, lots, flows, qtimes,
                         lot_constraints=load_lot_constraints(f"{DATA}/lot_constraints.csv"),
                         shift_times=shift_times, special_eqp_map=special_eqp_map)
print(f"排程 {len(le)} 步, 校验错误 {len(errs)}")
for e in errs[:6]:
    print("  ERR:", e)

def find(lot, step):
    for e in le:
        if e.lot_name == lot and e.step_name == step:
            return e
    return None

print("\n衔接 gap（用户视角：PC=领导在前，real=跟随）:")
pairs = [
    ("FC-REFLOW", "A005-P1-FC-REFLOW", "A005-R1-FC-REFLOW"),
    ("UF-DISPENSE", "A005-P1-UF-DISPENSE", "A005-R1-UF-DISPENSE"),
    ("BG2", "A005-P1-BG2-INSP2-REV", "A005-R1-BG2-PRE-INSP"),
]
for name, ps, rs in pairs:
    for lead, follow in [("PC1", "real1"), ("PC2", "real2")]:
        e1 = find(lead, ps); e2 = find(follow, rs)
        if e1 is None or e2 is None:
            print(f"  [{name}] {lead}.{ps} or {follow}.{rs} 未排!")
            continue
        gap = (e2.start_time - e1.end_time).total_seconds() / 60.0
        print(f"  [{name}] {lead}:{e1.start_time}~{e1.end_time}  {follow}:{e2.start_time}~{e2.end_time}  gap={gap:.0f}min")
