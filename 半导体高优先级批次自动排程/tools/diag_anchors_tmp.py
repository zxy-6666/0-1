"""诊断：dump coarse anchors for UF chain steps, baseline vs real1=5-1"""
import sys, copy
ROOT = "/workspace/半导体高优先级批次自动排程"
sys.path.insert(0, ROOT)

from data_loader import (
    load_lot_list, load_flow, load_step_ct, load_qtime,
    build_ct_lookup, auto_repair_step_ct,
    load_ftf_qty_change, load_special_lot_step, load_priority_wait,
    load_eqp_constraints, load_step_time_windows, load_shift_config, load_shift_change_times,
    load_special_eqp, load_lot_constraints,
    get_step_index_in_flow, get_product_flow_map,
)
from scheduler import _coarse_earliest_anchors

D = f"{ROOT}/data"
lots = load_lot_list(f"{D}/lot_list.csv", constraints_filepath=f"{D}/lot_constraints.csv")
flows = load_flow(f"{D}/flow.csv")
step_cts = auto_repair_step_ct(flows, load_step_ct(f"{D}/step_ct.csv"))
qtimes = load_qtime(f"{D}/qtime.csv")
ct_lookup = build_ct_lookup(step_cts)
priority_wait_map = load_priority_wait(f"{D}/priority_wait.csv")
special_lot_step_lookup = load_special_lot_step(f"{D}/special_lot_step.csv")

flow_map = get_product_flow_map(flows)
schedule_start = None
for l in lots:
    t = l.start_time if l.start_time else None
    if t:
        schedule_start = t if schedule_start is None else min(schedule_start, t)

def dump(label, these_lots):
    anchors = _coarse_earliest_anchors(
        these_lots, flow_map, ct_lookup, special_lot_step_lookup,
        priority_wait_map, schedule_start, qtimes=qtimes)
    print(f"\n===== {label} =====")
    lot_by_name = {l.lot_name: l for l in these_lots}
    for ln in ["PC1", "real1", "PC2", "real2"]:
        lot = lot_by_name[ln]
        flow = flow_map[lot.product_name]
        cur = get_step_index_in_flow(flow, lot.current_step_name)
        remaining = flow[cur:]
        anc = anchors.get(ln, [])
        print(f"\n[{ln}] priority={lot.priority}")
        for i, s in enumerate(remaining):
            if "UF-" not in s.step_name:
                continue
            a = anc[i].strftime('%m/%d %H:%M') if i < len(anc) else "?"
            print(f"   idx={i:3d} {s.step_name:<30} anchor={a}")

dump("BASELINE", lots)
lots2 = [copy.copy(l) for l in lots]
for l in lots2:
    if l.lot_name == "real1":
        l.priority = (5, 1)
dump("real1=5-1", lots2)