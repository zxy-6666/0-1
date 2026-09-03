import os
from paths import DATA_DIR
from data_loader import (load_lot_list, load_flow, load_step_ct, load_qtime,
    build_ct_lookup, auto_repair_step_ct, load_ftf_qty_change, load_special_lot_step,
    load_priority_wait, load_lot_constraints, load_eqp_constraints, load_shift_change_times,
    load_step_time_windows, load_shift_config, load_manual_adjusts, load_special_eqp)
from optimizer import schedule_optimized

USER_CONSTRAINTS = """lot1\tstep1\tlot2\tstep2\tmod
real4\tA005-R1-FC-REFLOW\tf45\tA005-P1-FC-REFLOW\tlead
real4\tA005-R1-UF-DISPENSE\tf45\tA005-P1-UF-DISPENSE\tlead
real4\tA005-R1-MD-MOLDING\tf45\tA005-P1-MD-MOLDING\tlead
real5\tA005-R1-FC-REFLOW\treal4\tA005-R1-FC-REFLOW\tlead
real5\tA005-R1-UF-DISPENSE\treal4\tA005-R1-UF-DISPENSE\tlead
real5\tA005-R1-MD-MOLDING\treal4\tA005-R1-MD-MOLDING\tlead
real6\tA005-R1-FC-REFLOW\tf678\tA005-P1-FC-REFLOW\tlead
real6\tA005-R1-UF-DISPENSE\tf678\tA005-P1-UF-DISPENSE\tlead
real6\tA005-R1-MD-MOLDING\tf678\tA005-P1-MD-MOLDING\tlead
real7\tA005-R1-FC-REFLOW\treal6\tA005-R1-FC-REFLOW\tlead
real7\tA005-R1-UF-DISPENSE\treal6\tA005-R1-UF-DISPENSE\tlead
real7\tA005-R1-MD-MOLDING\treal6\tA005-R1-MD-MOLDING\tlead
real8\tA005-R1-FC-REFLOW\treal7\tA005-R1-FC-REFLOW\tlead
real8\tA005-R1-UF-DISPENSE\treal7\tA005-R1-UF-DISPENSE\tlead
real8\tA005-R1-MD-MOLDING\treal7\tA005-R1-MD-MOLDING\tlead
real9\tA005-R1-FC-REFLOW\tf9\tA005-P1-FC-REFLOW\tlead
real9\tA005-R1-UF-DISPENSE\tf9\tA005-P1-UF-DISPENSE\tlead
real9\tA005-R1-MD-MOLDING\tf9\tA005-P1-MD-MOLDING\tlead
"""

LOT_LIST = "lot_name\tproduct_name\tqty\tpriority\tstep_name\ttarget_step\tlot_state\trunning_time\tstart_time\n" + "\n".join(
    [f"f45\tA005-P1\t2\t1-1\tA005-P1-FC-REFLOW\t\twait\t0\t2026/9/4 9:30",
     f"real4\tA005-MA\t4\t3-1\tA005-R1-FC-REFLOW\t\twait\t0\t2026/9/4 9:30",
     f"real5\tA005-MA\t4\t3-1\tA005-R1-FC-REFLOW\t\twait\t0\t2026/9/4 9:30",
     f"f678\tA005-P1\t1\t1-1\tA005-P1-FC-REFLOW\t\twait\t0\t2026/9/6 9:30",
     f"real6\tA005-MA\t4\t3-1\tA005-R1-FC-REFLOW\t\twait\t0\t2026/9/6 9:30",
     f"real7\tA005-MA\t1\t3-1\tA005-R1-FC-REFLOW\t\twait\t0\t2026/9/6 9:30",
     f"real8\tA005-MA\t1\t3-1\tA005-R1-FC-REFLOW\t\twait\t0\t2026/9/6 9:30",
     f"f9\tA005-P1\t2\t1-1\tA005-P1-FC-REFLOW\t\twait\t0\t2026/9/8 9:30",
     f"real9\tA005-MA\t9\t3-1\tA005-R1-FC-REFLOW\t\twait\t0\t2026/9/8 9:30"])

def run(label, constr):
    open(f"{DATA_DIR}/lot_list.csv","w").write(LOT_LIST)
    open(f"{DATA_DIR}/lot_constraints.csv","w").write(constr)
    lots = load_lot_list(f"{DATA_DIR}/lot_list.csv", constraints_filepath=f"{DATA_DIR}/lot_constraints.csv")
    flows = load_flow(f"{DATA_DIR}/flow.csv")
    step_cts = auto_repair_step_ct(flows, load_step_ct(f"{DATA_DIR}/step_ct.csv"), step_ct_filepath=f"{DATA_DIR}/step_ct.csv")
    qtimes = load_qtime(f"{DATA_DIR}/qtime.csv")
    ct_lookup = build_ct_lookup(step_cts)
    lc = load_lot_constraints(f"{DATA_DIR}/lot_constraints.csv")
    sc = load_shift_config(f"{DATA_DIR}/shift_config.csv")
    shift_times=[]
    for s in sc:
        h,m=map(int,s.start_time_str.split(":")); shift_times.append((h,m))
    le, ee, _, _ = schedule_optimized(lots, flows, ct_lookup, qtimes, shift_times,
        ftf_qty_change=load_ftf_qty_change(f"{DATA_DIR}/ftf_qty_change.csv"),
        special_lot_step_lookup=load_special_lot_step(f"{DATA_DIR}/special_lot_step.csv"),
        priority_wait_map=load_priority_wait(f"{DATA_DIR}/priority_wait.csv"),
        eqp_constraints=load_eqp_constraints(f"{DATA_DIR}/eqp_constraint.csv"),
        step_time_window_constraints=load_step_time_windows(f"{DATA_DIR}/step_time_window.csv"),
        shift_change_times=load_shift_change_times(f"{DATA_DIR}/shift_change_time.csv"),
        manual_adjusts=load_manual_adjusts(f"{DATA_DIR}/manual_adjust.csv") if os.path.exists(f"{DATA_DIR}/manual_adjust.csv") else [],
        special_eqp_map=load_special_eqp(f"{DATA_DIR}/special_eqp.csv"),
        lot_constraints=lc, resolve_max_iterations=10, max_iterations=40,
        seed=42, weight_by_priority=True, early_stop_patience=0)
    print(f"\n########## {label} ##########")
    by={}
    for e in le: by.setdefault(e.lot_name,{})[e.step_name]=e.start_time
    for lot in ["f45","real4","real5","f678","real6","real7","real8","f9","real9"]:
        d=by.get(lot,{})
        def f(sub):
            return next((t for k,t in d.items() if sub in k),None)
        print(f"  {lot:6s} FC={f('FC-REFLOW'):%m/%d %H:%M}  UF-DISP={f('UF-DISPENSE'):%m/%d %H:%M}  DAF-BAKE={f('DAF-BAKE'):%m/%d %H:%M}  MD-MOLD={f('MD-MOLDING'):%m/%d %H:%M}")

run("A: EMPTY constraints", "")
run("B: USER lead constraints", USER_CONSTRAINTS)