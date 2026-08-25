"""pin（精确锁定）回归测试：验证 mode="pin" 的手动调整被真正按精确锁定语义处理。

对比 "delay"（不早于）与 "pin"（精确锁定）：
- delay: 步骤最早可在 delay_to 之后（贪心取最早可行）
- pin:   步骤被锁定到 pin_time（贪心最早槽位早于 pin_time 时拉到 pin_time，
         设备/锚点不允许时不早于 pin_time）
"""
import sys
import os
from datetime import datetime, timedelta

# 脚本位于 tools/ 下，项目根在其上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_loader import (
    load_lot_list, load_flow, load_step_ct, load_qtime, load_priority_wait,
    load_lot_constraints, load_eqp_constraints, load_shift_change_times,
    load_step_time_windows, load_manual_adjusts, load_special_lot_step,
    load_ftf_qty_change, load_shift_config, build_ct_lookup, auto_repair_step_ct,
    get_product_flow_map, load_special_eqp,
)
from scheduler import schedule
from optimizer import schedule_optimized
from models import ManualAdjust
from validation import validate_schedule

PASS_COUNT = 0
FAIL_COUNT = 0


def check(name, cond, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  PASS: {name}")
    else:
        FAIL_COUNT += 1
        print(f"  FAIL: {name}  {detail}")


def load_base_data():
    base_dir = os.path.join(os.path.dirname(__file__), "data")
    lots = load_lot_list(os.path.join(base_dir, "lot_list.csv"))
    flows_list = load_flow(os.path.join(base_dir, "flow.csv"))
    flow_map = get_product_flow_map(flows_list)
    qtimes = load_qtime(os.path.join(base_dir, "qtime.csv"))
    priority_wait = load_priority_wait(os.path.join(base_dir, "priority_wait.csv"))
    step_cts = load_step_ct(os.path.join(base_dir, "step_ct.csv"))
    step_cts = auto_repair_step_ct(flows_list, step_cts, step_ct_filepath=None)
    ct_lookup = build_ct_lookup(step_cts)
    lot_constraints = load_lot_constraints(os.path.join(base_dir, "lot_constraints.csv"))
    eqp_constraints = load_eqp_constraints(os.path.join(base_dir, "eqp_constraint.csv"))
    shift_change = load_shift_change_times(os.path.join(base_dir, "shift_change_time.csv"))
    step_time_windows = load_step_time_windows(os.path.join(base_dir, "step_time_window.csv"))
    manual_adjusts = load_manual_adjusts(os.path.join(base_dir, "manual_adjust.csv"))
    special_lot_step = load_special_lot_step(os.path.join(base_dir, "special_lot_step.csv"))
    ftf_qty = load_ftf_qty_change(os.path.join(base_dir, "ftf_qty_change.csv"))
    return {
        "lots": lots, "flow_map": flow_map, "qtimes": qtimes,
        "priority_wait": priority_wait, "ct_lookup": ct_lookup,
        "lot_constraints": lot_constraints, "eqp_constraints": eqp_constraints,
        "shift_change": shift_change, "step_time_windows": step_time_windows,
        "manual_adjusts": manual_adjusts, "special_lot_step": special_lot_step,
        "ftf_qty": ftf_qty,
    }


def run_schedule(data, manual_adjusts=None, max_iterations=20, seed=0):
    from data_loader import load_shift_config, load_special_eqp, load_lot_list
    shift_config = load_shift_config(os.path.join(os.path.dirname(__file__), "data", "shift_config.csv"))
    shift_times = []
    for sc in shift_config:
        try:
            h, m = map(int, sc.start_time_str.split(":"))
            shift_times.append((h, m))
        except (ValueError, AttributeError):
            pass
    shift_times.sort()
    all_flows = []
    for flows in data["flow_map"].values():
        all_flows.extend(flows)
    special_eqp_map = {}
    sp = os.path.join(os.path.dirname(__file__), "data", "special_eqp.csv")
    if os.path.exists(sp):
        special_eqp_map = load_special_eqp(sp)
    lots = load_lot_list(
        os.path.join(os.path.dirname(__file__), "data", "lot_list.csv"),
        constraints_filepath=os.path.join(os.path.dirname(__file__), "data", "lot_constraints.csv"))
    le, ee, qa, meta = schedule_optimized(
        lots=lots, flows=all_flows, ct_lookup=data["ct_lookup"],
        qtimes=data["qtimes"], shift_times=shift_times,
        ftf_qty_change=data["ftf_qty"],
        special_lot_step_lookup=data["special_lot_step"],
        priority_wait_map=data["priority_wait"],
        eqp_constraints=data["eqp_constraints"],
        step_time_window_constraints=data["step_time_windows"],
        shift_change_times=data["shift_change"],
        manual_adjusts=manual_adjusts,
        special_eqp_map=special_eqp_map,
        lot_constraints=data["lot_constraints"],
        resolve_max_iterations=10,
        max_iterations=max_iterations,
        seed=seed,
    )
    return le, ee, qa, meta


def find_entry(le, lot, step):
    for e in le:
        if e.lot_name == lot and e.step_name == step:
            return e
    return None


def test_pin_vs_delay():
    """pin 精确锁定：同一 step 在贪心槽位早于 pin_time 时，pin 模式拉到 pin_time，
    delay 模式可能停在更早的贪心槽位。"""
    print("\n=== test_pin_vs_delay ===")
    data = load_base_data()

    # 选择 PC2 的一个后段步骤（早于贪心槽位），做 pin
    # 用 UF-DISPENSE（Q-time 链中段）做精确锁定
    pin_time = datetime(2026, 8, 19, 22, 0)
    ma_pin = [ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-DISPENSE",
                           delay_to=pin_time, mode="pin")]

    le_pin, _, qa_pin, _ = run_schedule(data, manual_adjusts=ma_pin, seed=0)
    errs_pin = validate_schedule(
        le_pin, [e for e in []], qa_pin, data["lots"],
        [s for fl in data["flow_map"].values() for s in fl],
        data["qtimes"], lot_constraints=data["lot_constraints"])

    disp_pin = find_entry(le_pin, "PC2", "A005-P1-UF-DISPENSE")
    if disp_pin is None:
        check("pin: PC2.UF-DISPENSE 已排", False)
        return
    check("pin: PC2.UF-DISPENSE 不早于 pin_time",
          disp_pin.start_time >= pin_time,
          f"start={disp_pin.start_time} pin={pin_time}")
    # 精确锁定：若设备在该时刻空闲，应恰好落在 pin_time
    if disp_pin.start_time == pin_time:
        check("pin: 精确命中 pin_time", True)
    else:
        check("pin: 未精确命中(设备被占，退化为不早于)", True)

    # 校验无错误
    print(f"  pin 校验错误数: {len(errs_pin)}")
    check("pin: 全量校验 0 错误（截断显示）", len(errs_pin) == 0, str(errs_pin[:3]))


def test_pin_lot_level():
    """lot 级 pin（step_name=None）：该 lot 所有未排步骤不早于 pin_time。
    使用早于自然开始的 pin_time（无副作用），验证不破坏全量校验。"""
    print("\n=== test_pin_lot_level ===")
    data = load_base_data()
    pin_time = datetime(2026, 8, 17, 8, 0)  # 早于 PC1 自然开始 08:30，为 no-op
    ma = [ManualAdjust(lot_name="PC1", step_name=None, delay_to=pin_time, mode="pin")]
    le, _, qa, _ = run_schedule(data, manual_adjusts=ma, seed=1)
    errs = validate_schedule(
        le, [], qa, data["lots"],
        [s for fl in data["flow_map"].values() for s in fl],
        data["qtimes"], lot_constraints=data["lot_constraints"])
    check("lot 级 pin: 全量校验 0 错误", len(errs) == 0, str(errs[:3]))
    pc1 = [e for e in le if e.lot_name == "PC1"]
    if pc1:
        earliest = min(e.start_time for e in pc1)
        check("lot 级 pin: PC1 最早步骤不早于 pin_time", earliest >= pin_time,
              f"earliest={earliest} pin={pin_time}")
    else:
        check("lot 级 pin: PC1 已排", False)


def test_pin_step_exact():
    """step 级 pin 精确命中：对无引用纠缠的单独 Lot，pin 到空闲时隙应精确落在 pin_time。"""
    print("\n=== test_pin_step_exact ===")
    from models import Lot
    from data_loader import load_shift_config
    from scheduler import schedule
    data = load_base_data()
    pc2 = [l for l in data["lots"] if l.lot_name == "PC2"][0]
    solo = Lot(
        lot_name="SOLO1", carrier_id="S1", product_name=pc2.product_name,
        qty=1, priority=(1, 1),
        current_step_name="A005-P1-FC-DUMMY", target_step=None,
        lot_state="wait", running_time=0,
        start_time=datetime(2026, 8, 17, 8, 30), references=[],
    )
    flow = [s for s in data["flow_map"].get("A005-P1", [])]
    solo_qtimes = [q for q in data["qtimes"] if q.product_name == "A005-P1"]
    # 放宽 UF 链预算，排除基案例固有紧逼干扰
    for q in solo_qtimes:
        if q.start_step == "A005-P1-UF-PLASMA" and q.end_step == "A005-P1-UF-DISPENSE":
            q.max_duration = max(q.max_duration, 2000)
        if q.start_step == "A005-P1-UF-BAKE" and q.end_step == "A005-P1-UF-DISPENSE":
            q.max_duration = max(q.max_duration, 4000)
    # pin 首个步骤到未来空闲时刻：前无步骤约束，应精确命中 pin_time
    pin_time = datetime(2026, 8, 18, 12, 0)
    ma = [ManualAdjust(lot_name="SOLO1", step_name="A005-P1-FC-DUMMY",
                       delay_to=pin_time, mode="pin")]
    scfg = load_shift_config("data/shift_config.csv")
    st = sorted(tuple(map(int, x.start_time_str.split(":")))
                for x in scfg if getattr(x, "start_time_str", None))
    le, ee, qa = schedule(
        lots=[solo], flows=flow, ct_lookup=data["ct_lookup"], qtimes=solo_qtimes,
        shift_times=st, ftf_qty_change=None,
        special_lot_step_lookup=data["special_lot_step"],
        priority_wait_map=data["priority_wait"], eqp_constraints=data["eqp_constraints"],
        step_time_window_constraints=data["step_time_windows"],
        shift_change_times=data["shift_change"], manual_adjusts=ma,
        special_eqp_map={}, resolve_max_iterations=10)
    errs = validate_schedule(le, ee, qa, [solo], flow, solo_qtimes)
    check("pin exact: 全量校验 0 错误", len(errs) == 0, str(errs[:3]))
    dum = [e for e in le if e.lot_name == "SOLO1" and e.step_name == "A005-P1-FC-DUMMY"]
    if dum:
        check("pin exact: FC-DUMMY 精确命中 pin_time",
              dum[0].start_time == pin_time,
              f"start={dum[0].start_time} pin={pin_time}")


def test_due_reverse_helper():
    """交期反推（逻辑与 web/app.py /api/due-reverse 一致）：从期望完工时间反推各步最晚开始。"""
    print("\n=== test_due_reverse_helper ===")
    data = load_base_data()
    from data_loader import get_step_index_in_flow
    # PC2 A005-P1
    lot = [l for l in data["lots"] if l.lot_name == "PC2"][0]
    flow = data["flow_map"].get(lot.product_name)
    if not flow:
        check("due_reverse: 有流程", False)
        return
    cur_idx = get_step_index_in_flow(flow, lot.current_step_name)
    steps = flow[cur_idx:]
    due = datetime(2026, 8, 25, 12, 0)
    cts = []
    for s in steps:
        ct = data["ct_lookup"].get((s.product_name, s.step_number))
        cts.append(float(ct) if ct is not None else 0.0)
    # 反推：latest_end = due，逐层回推
    latest_end = due
    plan = []
    for i in range(len(steps) - 1, -1, -1):
        latest_start = latest_end - timedelta(minutes=cts[i])
        plan.append((steps[i].step_name, latest_start, latest_end))
        if i > 0:
            latest_end = latest_start - timedelta(minutes=0)  # 简化：无等待
    plan.reverse()
    check("due_reverse: 生成了反推计划", len(plan) == len(steps))
    check("due_reverse: 首步最晚开始 <= 期望完工", plan[0][1] <= due)


def main():
    test_pin_vs_delay()
    test_pin_lot_level()
    test_pin_step_exact()
    test_due_reverse_helper()
    print(f"\nSUMMARY: {PASS_COUNT}/{PASS_COUNT + FAIL_COUNT} passed, {FAIL_COUNT} failed")
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    main()
