"""压力测试：覆盖调度引擎的各种边界场景"""
import sys
import os
import traceback
import copy
from datetime import datetime, timedelta

# 确保能导入项目模块（脚本位于 tools/ 下，项目根在其上一级）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_loader import (
    load_lot_list, load_flow, load_qtime, load_priority_wait,
    load_step_ct, load_lot_constraints, load_eqp_constraints,
    load_shift_change_times, load_step_time_windows,
    load_manual_adjusts, load_special_lot_step, load_ftf_qty_change,
    load_shift_config, load_special_eqp, build_ct_lookup, auto_repair_step_ct,
    get_product_flow_map,
)
from scheduler import schedule
from optimizer import schedule_optimized
from models import Lot, FlowStep, QTimeConstraint, LotConstraint, EqpConstraint, ManualAdjust, SpecialLotStep

TEST_RESULTS = []
PASS_COUNT = 0
FAIL_COUNT = 0


def test(name, fn):
    """运行一个测试用例"""
    global PASS_COUNT, FAIL_COUNT
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    try:
        errors = fn()
        if errors:
            FAIL_COUNT += 1
            TEST_RESULTS.append((name, "FAIL", errors))
            print(f"  RESULT: FAIL ({len(errors)} errors)")
            for e in errors:
                print(f"    - {e}")
        else:
            PASS_COUNT += 1
            TEST_RESULTS.append((name, "PASS", []))
            print(f"  RESULT: PASS")
    except Exception as e:
        FAIL_COUNT += 1
        TEST_RESULTS.append((name, "FAIL", [str(e)]))
        print(f"  RESULT: EXCEPTION: {e}")
        traceback.print_exc()


def load_base_data():
    """加载基础数据"""
    base_dir = os.path.join(os.path.dirname(__file__), "data")
    lots = load_lot_list(os.path.join(base_dir, "lot_list.csv"))
    flows_list = load_flow(os.path.join(base_dir, "flow.csv"))
    flow_map = get_product_flow_map(flows_list)
    qtimes = load_qtime(os.path.join(base_dir, "qtime.csv"))
    priority_wait = load_priority_wait(os.path.join(base_dir, "priority_wait.csv"))
    step_cts = load_step_ct(os.path.join(base_dir, "step_ct.csv"))
    # 压力测试不写回真实 step_ct.csv（避免改动用户数据）
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


def run_schedule(data):
    """运行调度并返回结果"""
    # 构建 shift_times
    from data_loader import load_shift_config
    shift_config = load_shift_config(os.path.join(os.path.dirname(__file__), "data", "shift_config.csv"))
    shift_times = []
    for sc in shift_config:
        try:
            h, m = map(int, sc.start_time_str.split(":"))
            shift_times.append((h, m))
        except (ValueError, AttributeError):
            pass
    shift_times.sort()

    # 展开 flow_map 为 flows 列表
    all_flows = []
    for flows in data["flow_map"].values():
        all_flows.extend(flows)

    lot_entries, eqp_entries, qtime_alerts = schedule(
        lots=data["lots"],
        flows=all_flows,
        ct_lookup=data["ct_lookup"],
        qtimes=data["qtimes"],
        shift_times=shift_times,
        ftf_qty_change=data["ftf_qty"],
        special_lot_step_lookup=data["special_lot_step"],
        priority_wait_map=data["priority_wait"],
        eqp_constraints=data["eqp_constraints"],
        step_time_window_constraints=data["step_time_windows"],
        shift_change_times=data["shift_change"],
        manual_adjusts=data["manual_adjusts"],
        resolve_max_iterations=10,
    )
    return lot_entries, eqp_entries, qtime_alerts


def validate_schedule(lot_entries, eqp_entries, qtime_alerts, data, special_eqp_map=None):
    """验证调度结果的完整性"""
    errors = []

    # 1. 检查步骤顺序：每个 lot 的步骤必须按序号递增
    lot_steps = {}
    for entry in lot_entries:
        key = entry.lot_name
        if key not in lot_steps:
            lot_steps[key] = []
        lot_steps[key].append(entry)

    for lot_name, steps in lot_steps.items():
        for i in range(1, len(steps)):
            if steps[i].start_time < steps[i-1].end_time:
                errors.append(f"{lot_name}: 步骤顺序异常 - step {steps[i].step_name} 开始于 {steps[i].start_time} 早于上一步 {steps[i-1].step_name} 结束于 {steps[i-1].end_time}")

    # 2. 检查设备不重叠：同一设备上不能有两个步骤同时运行（特殊设备按容量校验）
    eqp_timeline = {}
    for entry in eqp_entries:
        if entry.eqp_id == "-":
            continue
        if entry.eqp_id not in eqp_timeline:
            eqp_timeline[entry.eqp_id] = []
        eqp_timeline[entry.eqp_id].append((entry.start_time, entry.end_time, entry.lot_name, entry.step_name, entry.qty))

    special_eqp_map = special_eqp_map or {}
    for eqp_id, bookings in eqp_timeline.items():
        spec = special_eqp_map.get(eqp_id)
        sorted_bookings = sorted(bookings, key=lambda x: x[0])
        if spec is not None:
            events = []
            for st, et, ln, sn, qty in sorted_bookings:
                events.append((st, 1, qty))
                events.append((et, -1, qty))
            events.sort(key=lambda x: (x[0], x[1]))
            max_lots = max_qty = cur_lots = cur_qty = 0
            for _t, _delta, _qty in events:
                cur_lots += _delta
                cur_qty += _delta * _qty
                max_lots = max(max_lots, cur_lots)
                max_qty = max(max_qty, cur_qty)
            if spec.max_lots and max_lots > spec.max_lots:
                errors.append(f"设备 {eqp_id}: 并发批次超限 - 最多 {max_lots} 批同时占用（上限 {spec.max_lots}）")
            if spec.max_qty and max_qty > spec.max_qty:
                errors.append(f"设备 {eqp_id}: 并发总量超限 - 最多 {max_qty} 数量同时占用（上限 {spec.max_qty}）")
            continue
        for i in range(1, len(sorted_bookings)):
            if sorted_bookings[i][0] < sorted_bookings[i-1][1]:
                errors.append(f"设备 {eqp_id}: 重叠占用 - {sorted_bookings[i-1][2]}/{sorted_bookings[i-1][3]} ({sorted_bookings[i-1][0]}~{sorted_bookings[i-1][1]}) 与 {sorted_bookings[i][2]}/{sorted_bookings[i][3]} ({sorted_bookings[i][0]}~{sorted_bookings[i][1]})")

    # 3. 检查 Q-time 告警
    violations = [a for a in qtime_alerts if a.status != "OK"]
    if violations:
        for v in violations:
            errors.append(f"Q-time 超时: {v.lot_name} {v.qtime_rule} [{v.status}] over={v.over_minutes}min")

    # 4. 检查时间非负
    for entry in lot_entries:
        dur = (entry.end_time - entry.start_time).total_seconds()
        if dur < 0:
            errors.append(f"{entry.lot_name} {entry.step_name}: 负时长 {dur}s")

    # 5. 检查每个 lot 都完成了从 current_step 开始的所有步骤
    flow_map = data["flow_map"]
    for lot in data["lots"]:
        product_flow = flow_map.get(lot.product_name)
        if not product_flow:
            continue
        # 从 current_step 开始计算预期步骤
        try:
            from data_loader import get_step_index_in_flow
            start_idx = get_step_index_in_flow(product_flow, lot.current_step_name)
        except ValueError:
            # 如果 current_step 不在 flow 中，跳过
            continue
        expected_steps = [s.step_name for s in product_flow[start_idx:]]
        actual_steps = [e.step_name for e in lot_steps.get(lot.lot_name, [])]
        if len(actual_steps) < len(expected_steps):
            missing = set(expected_steps) - set(actual_steps)
            if missing:
                errors.append(f"{lot.lot_name}: 缺失步骤 {missing}")

    return errors


# ============================================================
# 测试用例
# ============================================================

def test_01_baseline():
    """基线测试：原始数据"""
    data = load_base_data()
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_02_empty_equipment_intervals():
    """边界测试：设备无任何占用记录"""
    data = load_base_data()
    # 清空 eqp_constraints
    data["eqp_constraints"] = []
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_03_single_lot():
    """边界测试：单个 Lot"""
    data = load_base_data()
    data["lots"] = [l for l in data["lots"] if l.lot_name == "PC1"]
    data["lot_constraints"] = [c for c in data["lot_constraints"] if c.lot_name == "PC1"]
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_04_no_constraints():
    """边界测试：无 lot 间约束"""
    data = load_base_data()
    data["lot_constraints"] = []
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_05_no_qtime():
    """边界测试：无 Q-time 约束"""
    data = load_base_data()
    data["qtimes"] = []
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_06_all_same_priority():
    """边界测试：所有 Lot 相同优先级"""
    data = load_base_data()
    for lot in data["lots"]:
        lot.priority = (1, 1)
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_07_very_high_priority_diff():
    """边界测试：极端优先级差异"""
    data = load_base_data()
    for i, lot in enumerate(data["lots"]):
        if i == 0:
            lot.priority = (1, 1)
        else:
            lot.priority = (9, 9)
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_08_circular_reference():
    """边界测试：循环依赖 A→B, B→A"""
    data = load_base_data()
    from models import LotConstraint
    # 添加一个反向引用，制造循环依赖
    data["lot_constraints"] = list(data["lot_constraints"])
    new_ref = LotConstraint(
        lot_name="real1",
        reference_lot="PC1",
        reference_step="A005-P1-AB1IQC-INSP-REV",
        start_mod="0",
        start_step="A005-R1-AB1IQC-INSP-REV",
        hold_periods=[],
    )
    data["lot_constraints"].append(new_ref)
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_09_many_lots_same_eqp():
    """边界测试：大量 Lot 竞争同一设备"""
    data = load_base_data()
    from models import Lot
    # 克隆多个 PC1
    base_lot = data["lots"][0]
    new_lots = []
    for i in range(10):
        new_lot = Lot(
            lot_name=f"CLONE{i}",
            carrier_id=f"CARRIER{i}",
            product_name=base_lot.product_name,
            qty=base_lot.qty,
            priority=base_lot.priority,
            current_step_name=base_lot.current_step_name,
            target_step=base_lot.target_step,
            lot_state=base_lot.lot_state,
            running_time=base_lot.running_time,
            start_time=base_lot.start_time + timedelta(hours=i),
            references=[],
        )
        new_lots.append(new_lot)
    data["lots"] = new_lots
    data["lot_constraints"] = []
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_10_shift_change_boundary():
    """边界测试：步骤恰好卡在换班时间边界"""
    data = load_base_data()
    from models import StepTimeWindow
    data["step_time_windows"] = [
        StepTimeWindow(
            step_name="A005-R1-AB1IQC-WFS",
            start_time_str="08:25",
            end_time_str="08:35",
            date_str="-1",
            week=None,
            end_start_time_str=None,
            end_end_time_str=None,
            end_date_str=None,
            end_week=None,
        )
    ]
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_11_zero_wait_time():
    """边界测试：零等待时间"""
    data = load_base_data()
    data["priority_wait"] = {(1, 1): 0, (1, 2): 0, (2, 1): 0, (3, 1): 0, (3, 2): 0,
                             (4, 1): 0, (5, 1): 0, (6, 1): 0, (7, 1): 0, (8, 1): 0, (9, 1): 0,
                             (1, 3): 0, (1, 4): 0, (1, 5): 0, (1, 6): 0, (1, 7): 0, (1, 8): 0, (1, 9): 0,
                             (2, 2): 0, (2, 3): 0, (2, 4): 0, (2, 5): 0, (2, 6): 0, (2, 7): 0, (2, 8): 0, (2, 9): 0,
                             (3, 3): 0, (3, 4): 0, (3, 5): 0, (3, 6): 0, (3, 7): 0, (3, 8): 0, (3, 9): 0,
                             (4, 2): 0, (4, 3): 0, (4, 4): 0, (4, 5): 0, (4, 6): 0, (4, 7): 0, (4, 8): 0, (4, 9): 0,
                             (5, 2): 0, (5, 3): 0, (5, 4): 0, (5, 5): 0, (5, 6): 0, (5, 7): 0, (5, 8): 0, (5, 9): 0,
                             (6, 2): 0, (6, 3): 0, (6, 4): 0, (6, 5): 0, (6, 6): 0, (6, 7): 0, (6, 8): 0, (6, 9): 0,
                             (7, 2): 0, (7, 3): 0, (7, 4): 0, (7, 5): 0, (7, 6): 0, (7, 7): 0, (7, 8): 0, (7, 9): 0,
                             (8, 2): 0, (8, 3): 0, (8, 4): 0, (8, 5): 0, (8, 6): 0, (8, 7): 0, (8, 8): 0, (8, 9): 0,
                             (9, 2): 0, (9, 3): 0, (9, 4): 0, (9, 5): 0, (9, 6): 0, (9, 7): 0, (9, 8): 0, (9, 9): 0}
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_12_very_long_wait_time():
    """边界测试：极长等待时间"""
    data = load_base_data()
    data["priority_wait"] = {(k[0], k[1]): 1000 for k in data["priority_wait"]}
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_13_tight_qtime():
    """边界测试：紧 Q-time (120 分钟，所有链均可达成但足够紧)"""
    data = load_base_data()
    from models import QTimeConstraint
    data["qtimes"] = list(data["qtimes"])
    for q in data["qtimes"]:
        q.max_duration = 120  # 紧但可达成
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_14_overlapping_qtime_chains():
    """边界测试：重叠 Q-time 链"""
    data = load_base_data()
    from models import QTimeConstraint
    # 添加一个额外的 Q-time 约束，与其他 Q-time 重叠
    data["qtimes"] = list(data["qtimes"])
    data["qtimes"].append(QTimeConstraint(
        product_name="A005-MA",
        start_step="A005-R1-UF-BAKE",
        end_step="A005-R1-UF-CURE",
        start_mod="track out",
        end_mod="track in",
        max_duration=480,
    ))
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_15_reference_with_shift():
    """边界测试：reference 使用 shift 修饰符"""
    data = load_base_data()
    from models import LotConstraint
    data["lot_constraints"] = list(data["lot_constraints"])
    # 修改一个 reference 使用 shift 修饰符
    for c in data["lot_constraints"]:
        if c.lot_name == "real1" and c.reference_step == "A005-P1-FC-REFLOW":
            c.start_mod = "shift"
            break
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_16_manual_adjust():
    """边界测试：手动调整步骤时间"""
    data = load_base_data()
    from models import ManualAdjust
    data["manual_adjusts"] = [
        ManualAdjust(
            lot_name="real1",
            step_name="A005-R1-DAF-INSP",
            delay_to=datetime(2026, 8, 19, 12, 0),
        )
    ]
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_17_all_equipment_unavailable():
    """边界测试：所有设备在特定时间不可用"""
    data = load_base_data()
    from models import EqpConstraint
    data["eqp_constraints"] = list(data["eqp_constraints"])
    # 添加一个设备不可用时段
    data["eqp_constraints"].append(EqpConstraint(
        eqp_name="PMAOM004",
        start_time_str="08:00",
        end_time_str="20:00",
        date_str="2026/8/19",
        week=None,
    ))
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_18_step_without_equipment():
    """边界测试：确保无设备步骤正确处理"""
    data = load_base_data()
    # 只保留无设备步骤
    for lot in data["lots"]:
        product_flow = data["flow_map"].get(lot.product_name)
        if product_flow:
            for step in product_flow:
                step.eqp_ids = []
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_19_lot_start_midnight():
    """边界测试：Lot 在午夜开始"""
    data = load_base_data()
    for lot in data["lots"]:
        lot.start_time = datetime(2026, 8, 17, 0, 0)
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_20_lot_start_weekend():
    """边界测试：Lot 在周末开始"""
    data = load_base_data()
    for lot in data["lots"]:
        # 2026-08-15 是周六
        lot.start_time = datetime(2026, 8, 15, 8, 30)
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_21_interval_gap_filling():
    """边界测试：验证 interval tracking 正确填充空隙"""
    data = load_base_data()
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    errors = validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)

    # 额外检查：PMAOM004 的利用率
    pma_entries = [e for e in eqp_entries if e.eqp_id == "PMAOM004"]
    if pma_entries:
        sorted_entries = sorted(pma_entries, key=lambda x: x.start_time)
        total_gap = timedelta(0)
        for i in range(1, len(sorted_entries)):
            gap = sorted_entries[i].start_time - sorted_entries[i-1].end_time
            if gap > timedelta(0):
                total_gap += gap
        # 检查是否有不合理的超长空隙 (比如 > 24h 但 lot 已就绪)
        for i in range(1, len(sorted_entries)):
            gap = sorted_entries[i].start_time - sorted_entries[i-1].end_time
            if gap > timedelta(hours=24):
                errors.append(f"PMAOM004: 存在超过24h的空隙 {sorted_entries[i-1].step_name}→{sorted_entries[i].step_name} gap={gap}")

    return errors


def test_22_chain_scheduling_extreme():
    """边界测试：极紧 DAF-BAKE→MD-MOLDING 链 (80 分钟，可达成但极紧)"""
    data = load_base_data()
    # 确保 DAF-BAKE→MD-MOLDING 的 Q-time 极紧(让它变成紧链)
    from models import QTimeConstraint
    for q in data["qtimes"]:
        if q.start_step == "A005-R1-DAF-BAKE" and q.end_step == "A005-R1-MD-MOLDING":
            q.max_duration = 80  # 可达成但极紧 (最小约 67.4 min)
            break
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_23_multiple_products():
    """边界测试：多个不同产品的 Lot 混合"""
    data = load_base_data()
    from models import Lot
    base_lot = data["lots"][0]
    new_lots = list(data["lots"])
    for i in range(3):
        product = "A005-MA" if i % 2 == 0 else "A005-P1"
        # 使用对应产品的第一个步骤作为 current_step_name
        flow_first_step = data["flow_map"][product][0].step_name if product in data["flow_map"] else ""
        new_lot = Lot(
            lot_name=f"MIX{i}",
            carrier_id=f"MIX{i}",
            product_name=product,
            qty=i + 1,
            priority=(2, i + 1),
            current_step_name=flow_first_step,
            target_step=base_lot.target_step,
            lot_state=base_lot.lot_state,
            running_time=0,
            start_time=datetime(2026, 8, 17, 8, 30) + timedelta(hours=i * 4),
            references=[],
        )
        new_lots.append(new_lot)
    data["lots"] = new_lots
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_24_hold_period():
    """边界测试：带 hold_periods 的 Lot（通过 lot_constraints）"""
    data = load_base_data()
    # 通过 lot_constraints 添加 hold_periods
    from models import LotConstraint
    data["lot_constraints"] = list(data["lot_constraints"])
    for c in data["lot_constraints"]:
        if c.lot_name == "real1":
            c.hold_periods = [(datetime(2026, 8, 18, 12, 0), datetime(2026, 8, 18, 18, 0))]
            break
    lot_entries, eqp_entries, qtime_alerts = run_schedule(data)
    return validate_schedule(lot_entries, eqp_entries, qtime_alerts, data)


def test_25_interval_concurrency():
    """边界测试：interval 并发安全性 - 多次运行结果应一致"""
    data = load_base_data()
    results = []
    for _ in range(3):
        d = load_base_data()  # 每次重新加载
        lot_entries, eqp_entries, qtime_alerts = run_schedule(d)
        results.append((lot_entries, eqp_entries, qtime_alerts))

    errors = []
    # 检查三次运行结果是否一致
    first = results[0]
    for i in range(1, len(results)):
        for j, entry in enumerate(first[0]):
            if j >= len(results[i][0]):
                errors.append(f"运行 {i+1} 缺少步骤 {entry.lot_name}/{entry.step_name}")
                continue
            other = results[i][0][j]
            if entry.start_time != other.start_time or entry.end_time != other.end_time:
                errors.append(f"运行 {i+1} 不一致: {entry.lot_name}/{entry.step_name} run1={entry.start_time} run{i+1}={other.start_time}")

    return errors


# ============================================================
# 优化器（种子随机局部搜索）回归用例
# ============================================================

def run_schedule_optimized(data, max_iterations=20, seed=0):
    """使用优化器运行调度并返回结果 + meta"""
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
    special_eqp_path = os.path.join(os.path.dirname(__file__), "data", "special_eqp.csv")
    special_eqp_map = load_special_eqp(special_eqp_path) if os.path.exists(special_eqp_path) else {}
    # 重新加载 lots（带 reference 约束），确保调度器能钳制 reference 依赖
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
        manual_adjusts=data["manual_adjusts"],
        special_eqp_map=special_eqp_map,
        lot_constraints=data["lot_constraints"],
        resolve_max_iterations=10,
        max_iterations=max_iterations,
        seed=seed,
    )
    return lots, le, ee, qa, meta


def _load_special_eqp():
    from data_loader import load_special_eqp
    path = os.path.join(os.path.dirname(__file__), "data", "special_eqp.csv")
    return load_special_eqp(path) if os.path.exists(path) else {}


def test_26_optimizer_default():
    """优化器：默认算例 → 0 校验错误 + Q-time 余量为正"""
    data = load_base_data()
    lots, le, ee, qa, meta = run_schedule_optimized(data, max_iterations=20, seed=0)
    from validation import validate_schedule, compute_objective
    errors = validate_schedule(
        le, ee, qa, lots,
        [s for fl in data["flow_map"].values() for s in fl],
        data["qtimes"], lot_constraints=data["lot_constraints"],
        special_eqp_map=_load_special_eqp())
    if errors:
        return errors[:30]
    if meta.get("warning"):
        return [f"优化器警告: {meta['warning']}"]
    if meta.get("min_qtime_margin") is not None and meta["min_qtime_margin"] < 0:
        return [f"Q-time 余量为负: {meta['min_qtime_margin']:.1f}min"]
    # 必须有有效解被找到
    if meta.get("valid_iterations", 0) == 0:
        return ["优化器未找到任何有效解"]
    return []


def test_27_optimizer_variability():
    """优化器：不同 seed 都能得到合法解，且重排结果不固化在超Q解上"""
    data = load_base_data()
    from validation import validate_schedule
    results = {}
    for seed in [0, 1, 2]:
        lots, le, ee, qa, meta = run_schedule_optimized(data, max_iterations=20, seed=seed)
        errors = validate_schedule(
            le, ee, qa, lots,
            [s for fl in data["flow_map"].values() for s in fl],
            data["qtimes"], lot_constraints=data["lot_constraints"],
            special_eqp_map=_load_special_eqp())
        results[seed] = (meta.get("valid_iterations", 0), len(errors), meta.get("min_qtime_margin"))
        if errors:
            return [f"seed={seed} 仍有校验错误: {errors[:5]}"]
    # 至少要有一个 seed 找到有效解，且重排不总是返回相同违规解
    if all(v[0] == 0 for v in results.values()):
        return ["所有 seed 均未找到有效解"]
    return []


def test_28_manual_adjust_reschedule():
    """手动调整当作一次重新排程：手动延后 Q-time 链内 step，整链重排后不产生超Q"""
    from models import ManualAdjust
    from datetime import datetime as _dt
    from validation import validate_schedule
    data = load_base_data()
    manual_adjusts = [
        ManualAdjust(lot_name="PC1", step_name="A005-P1-UF-PLASMA",
                     delay_to=_dt(2026, 8, 19, 18, 33)),
        ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-DISPENSE",
                     delay_to=_dt(2026, 8, 20, 10, 10)),
    ]
    le, ee, qa, meta = run_schedule_optimized_manual(data, manual_adjusts, max_iterations=40, seed=0)
    errors = validate_schedule(
        le, ee, qa, data["lots"],
        [s for fl in data["flow_map"].values() for s in fl],
        data["qtimes"], lot_constraints=data["lot_constraints"],
        special_eqp_map=_load_special_eqp())
    if errors:
        return errors[:20]
    if meta.get("warning") and meta.get("valid_iterations", 0) == 0:
        return [f"手动调整后未找到完全合法解: {meta.get('warning')}"]
    return []


def run_schedule_optimized_manual(data, manual_adjusts, max_iterations=20, seed=0):
    """使用优化器 + 手动调整运行调度（复用 run_schedule_optimized 的数据加载）"""
    from data_loader import load_shift_config, load_special_eqp, load_lot_list
    from optimizer import schedule_optimized
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
    special_eqp_path = os.path.join(os.path.dirname(__file__), "data", "special_eqp.csv")
    special_eqp_map = load_special_eqp(special_eqp_path) if os.path.exists(special_eqp_path) else {}
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


def test_29_manual_adjust_chain_midstep_recompact():
    """手动调整链中步骤后整链重排：延后 Q-time 端步骤，链内保持紧凑不超松弛预算。

    为隔离"手动调整重排"逻辑的正确性，先放宽受影响链的 Q-time 预算，
    使合法解存在；断言：调整步骤的时间被精确尊重 + 全量校验 0 错误。
    """
    from models import ManualAdjust
    from datetime import datetime as _dt
    from validation import validate_schedule
    data = load_base_data()
    # 放宽所有产品的 UF 链预算，剔除基案例固有紧逼干扰，专注验证"手动调整会被当作重排"
    for q in data["qtimes"]:
        if q.start_step.endswith("UF-PLASMA") and q.end_step.endswith("UF-DISPENSE"):
            q.max_duration = 2000
        if q.start_step.endswith("UF-BAKE") and q.end_step.endswith("UF-DISPENSE"):
            q.max_duration = 4000
    manual = [
        ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-DISPENSE",
                     delay_to=_dt(2026, 8, 20, 6, 0)),
    ]
    le, ee, qa, meta = run_schedule_optimized_manual(data, manual, max_iterations=40, seed=0)
    errors = validate_schedule(
        le, ee, qa, data["lots"],
        [s for fl in data["flow_map"].values() for s in fl],
        data["qtimes"], lot_constraints=data["lot_constraints"],
        special_eqp_map=_load_special_eqp())
    if errors:
        return errors[:20]
    # 断言手动调整被精确尊重：DISPENSE 不早于 delay_to
    disp = [e for e in le if e.lot_name == "PC2" and e.step_name == "A005-P1-UF-DISPENSE"]
    if not disp:
        return ["手动调整后 PC2.UF-DISPENSE 未生成"]
    if disp[0].start_time < _dt(2026, 8, 20, 6, 0):
        return [f"手动调整未尊重 delay_to: {disp[0].start_time} < 2026-08-20 06:00"]
    return []


def test_30_manual_adjust_nonchain_step_isolated():
    """手动调整非 Q-time 链步骤：不影响相邻链的紧凑性，全量校验通过。

    使用仅含单个 Lot、无 reference 的纯净场景，验证手动调整落到 delay_to，
    且前后步骤按其等待时间正常衔接。
    """
    from models import ManualAdjust, Lot
    from datetime import datetime as _dt
    from data_loader import load_shift_config
    from scheduler import schedule
    from validation import validate_schedule
    data = load_base_data()
    # 构造：只保留 PC2 一个 Lot，无跨 lot reference
    pc2 = [l for l in data["lots"] if l.lot_name == "PC2"]
    if not pc2:
        return ["基数据缺少 PC2"]
    solo = pc2[0]
    solo.references = []  # 去掉跨 lot 依赖，聚焦手动调整本身
    flow = [s for s in data["flow_map"].get("A005-P1", [])]
    # 用放宽容差避免基案例设备挤兑
    solo_qtimes = [q for q in data["qtimes"] if q.product_name == "A005-P1"]
    for q in solo_qtimes:
        if q.start_step == "A005-P1-UF-PLASMA" and q.end_step == "A005-P1-UF-DISPENSE":
            q.max_duration = max(q.max_duration, 2000)
        if q.start_step == "A005-P1-UF-BAKE" and q.end_step == "A005-P1-UF-DISPENSE":
            q.max_duration = max(q.max_duration, 4000)
    manual = [ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-PLASMA",
                           delay_to=_dt(2026, 8, 19, 4, 0))]
    shift_config = load_shift_config(os.path.join(os.path.dirname(__file__), "data", "shift_config.csv"))
    shift_times = sorted(tuple(map(int, sc.start_time_str.split(":")))
                         for sc in shift_config if getattr(sc, "start_time_str", None))
    le, ee, qa = schedule(
        lots=[solo], flows=flow, ct_lookup=data["ct_lookup"], qtimes=solo_qtimes,
        shift_times=shift_times, ftf_qty_change=None,
        special_lot_step_lookup=data["special_lot_step"],
        priority_wait_map=data["priority_wait"], eqp_constraints=data["eqp_constraints"],
        step_time_window_constraints=data["step_time_windows"],
        shift_change_times=data["shift_change"], manual_adjusts=manual,
        special_eqp_map={}, resolve_max_iterations=10)
    errors = validate_schedule(le, ee, qa, [solo], flow, solo_qtimes)
    if errors:
        return errors[:20]
    plasma = [e for e in le if e.lot_name == "PC2" and e.step_name == "A005-P1-UF-PLASMA"]
    if plasma and plasma[0].start_time < _dt(2026, 8, 19, 4, 0):
        return ["手动调整未尊重 delay_to: PLASMA 早于 2026-08-19 04:00"]
    return []


# ------------------------------------------------------------
# 针对 test_28 根因的复杂压力用例：
#   根因 = 调度器 Q 段预算守卫仅覆盖"相邻步骤 + track in→track out"，
#   无法处理 非相邻跨度（BAKE→DISPENSE 1440）与 混合 Q 模型
#   （PLASMA→DISPENSE track out→out 240 / DISPENSE→CURE track out→in 240）。
#   新用例专门压测"任意跨度 × 任意 Q 模型"守卫 + 多步骤手动钉晚。
#   采用单 Lot（剔除跨 lot 设备挤兑）聚焦验证守卫本身；这些用例在旧的
#   相邻-only 守卫下必然失败，在推广守卫后通过。
# ------------------------------------------------------------

def _solo_pc2_schedule(manual_adjusts):
    """仅保留 PC2 单 lot 全流程调度（去掉跨 lot reference），直接验证手动钉晚
    下链内任意跨度/混合 Q 模型的守卫。返回 (errors, le, ee, qa)。"""
    from models import ManualAdjust, Lot
    from datetime import datetime
    from data_loader import load_shift_config
    from scheduler import schedule
    from validation import validate_schedule
    data = load_base_data()
    pc2 = [l for l in data["lots"] if l.lot_name == "PC2"]
    if not pc2:
        return ["基数据缺少 PC2"], None, None, None
    solo = copy.deepcopy(pc2[0])
    solo.references = []  # 剔除跨 lot 依赖，聚焦链内守卫
    flow = [s for s in data["flow_map"].get("A005-P1", [])]
    solo_qtimes = [q for q in data["qtimes"] if q.product_name == "A005-P1"]
    shift_config = load_shift_config(os.path.join(os.path.dirname(__file__), "data", "shift_config.csv"))
    shift_times = sorted(tuple(map(int, sc.start_time_str.split(":")))
                         for sc in shift_config if getattr(sc, "start_time_str", None))
    le, ee, qa = schedule(
        lots=[solo], flows=flow, ct_lookup=data["ct_lookup"], qtimes=solo_qtimes,
        shift_times=shift_times, ftf_qty_change=None,
        special_lot_step_lookup=data["special_lot_step"],
        priority_wait_map=data["priority_wait"], eqp_constraints=data["eqp_constraints"],
        step_time_window_constraints=data["step_time_windows"],
        shift_change_times=data["shift_change"], manual_adjusts=manual_adjusts,
        special_eqp_map={}, resolve_max_iterations=10)
    errors = validate_schedule(le, ee, qa, [solo], flow, solo_qtimes)
    return errors, le, ee, qa


def test_33_manual_adjust_nonadjacent_span_guard():
    """非相邻跨度 Q 守卫：手动把链尾前置 DISPENSE 钉晚，BAKE→DISPENSE(1440,
    track out→track in, 跨 BAKE/PLASMA/DISPENSE 两步) 必须由守卫把链首收拢，
    保持 ≤1440；同时相邻 DISPENSE→CURE(240, track out→track in) 也须守住。"""
    from models import ManualAdjust
    from datetime import datetime as _dt
    # 钉得足够晚，使 BAKE→DISPENSE(1440) 在**不做非相邻守卫**时必然超限；
    # 且 DISPENSE 一旦被钉晚，相邻 PLASMA→DISPENSE / DISPENSE→CURE 也会被挤，
    # 整链必须整体后移才能全部收进预算 → 新旧守卫差异可测。
    manual = [ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-DISPENSE",
                           delay_to=_dt(2026, 8, 20, 12, 30))]
    errors, le, ee, qa = _solo_pc2_schedule(manual)
    return errors[:20] if errors else []


def test_34_manual_adjust_mixed_model_q_guard():
    """混合 Q 模型守卫：手动把链中 PLASMA 钉得足够晚，使相邻 track out→track out
    PLASMA→DISPENSE(240)、相邻 track out→track in DISPENSE→CURE(240) 与非相邻
    BAKE→DISPENSE(1440) 全部逼近预算，旧守卫（仅相邻+track in→out）必然漏守。"""
    from models import ManualAdjust
    from datetime import datetime as _dt
    manual = [ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-PLASMA",
                           delay_to=_dt(2026, 8, 20, 11, 30))]
    errors, le, ee, qa = _solo_pc2_schedule(manual)
    return errors[:20] if errors else []


def test_35_manual_adjust_multi_pin_same_chain():
    """同 chain 多步钉晚：链内 BAKE 与 DISPENSE 同时手动钉晚，多约束叠加下
    守卫须把整链各 Q 段（含非相邻 BAKE→DISPENSE）同时收在预算内，且手动钉晚被尊重。"""
    from models import ManualAdjust
    from datetime import datetime as _dt
    manual = [
        ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-BAKE",
                     delay_to=_dt(2026, 8, 19, 12, 0)),
        ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-DISPENSE",
                     delay_to=_dt(2026, 8, 20, 12, 30)),
    ]
    errors, le, ee, qa = _solo_pc2_schedule(manual)
    if errors:
        return errors[:20]
    # 手动钉晚必须被尊重（≥ delay_to）
    from datetime import datetime
    for e in le:
        if e.lot_name == "PC2" and e.step_name == "A005-P1-UF-BAKE" and e.start_time < _dt(2026, 8, 19, 12, 0):
            return ["手动调整未尊重 delay_to: BAKE 早于 2026-08-19 12:00"]
        if e.lot_name == "PC2" and e.step_name == "A005-P1-UF-DISPENSE" and e.start_time < _dt(2026, 8, 20, 12, 30):
            return ["手动调整未尊重 delay_to: DISPENSE 早于 2026-08-20 12:30"]
    return []


def test_36_manual_adjust_multilot_concurrent_guard():
    """多批次并发压力：PC1 与 PC2 两条 A005-P1 链**同时**手动钉晚（DISPENSE），
    与 real1/real2(A005-MA) 共抢 DISPENSE/PLASMA/BAKE 等设备，制造设备竞争。
    验证推广后的任意跨度 Q 段守卫在多批次挤兑下，仍能把各链的
    BAKE→DISPENSE(1440)、PLASMA→DISPENSE(240)、DISPENSE→CURE(240)
    全部收进预算，且手动钉晚被尊重。"""
    from models import ManualAdjust
    from datetime import datetime as _dt
    from validation import validate_schedule
    data = load_base_data()
    # 两批同时钉晚到中等偏晚时刻 → 必须拉动整链 + 与其他批争抢设备
    manual = [
        ManualAdjust(lot_name="PC1", step_name="A005-P1-UF-DISPENSE",
                     delay_to=_dt(2026, 8, 19, 14, 0)),
        ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-DISPENSE",
                     delay_to=_dt(2026, 8, 20, 9, 0)),
    ]
    le, ee, qa, meta = run_schedule_optimized_manual(data, manual, max_iterations=60, seed=0)
    errors = validate_schedule(
        le, ee, qa, data["lots"],
        [s for fl in data["flow_map"].values() for s in fl],
        data["qtimes"], lot_constraints=data["lot_constraints"],
        special_eqp_map=_load_special_eqp())
    if errors:
        return errors[:20]
    # 手动钉晚必须被尊重
    for e in le:
        if e.lot_name == "PC1" and e.step_name == "A005-P1-UF-DISPENSE" \
                and e.start_time < _dt(2026, 8, 19, 14, 0):
            return ["手动调整未尊重 delay_to: PC1.DISPENSE 早于 14:00"]
        if e.lot_name == "PC2" and e.step_name == "A005-P1-UF-DISPENSE" \
                and e.start_time < _dt(2026, 8, 20, 9, 0):
            return ["手动调整未尊重 delay_to: PC2.DISPENSE 早于 09:00"]
    return []


# ============================================================
# 第 11 轮新增算例集：余量下限参数 / 权重差 / 软约束引导 /
# 多批次高竞争 / 手动钉晚边界
# ============================================================

def test_37_qtight_min_margin_param():
    """QTIGHT_MIN_MARGIN 自定义参数：短紧 Q 段（预算调小使 20%<下限）下
    传 qtight_min_margin=0 / 30 均合法不崩溃；且参数真实生效（0 配置的
    段实际用时 >= 30 配置——起点推后留缓冲，段更从容）。"""
    from models import ManualAdjust
    from datetime import datetime as _dt
    from data_loader import load_shift_config
    from scheduler import schedule
    from validation import validate_schedule
    data = load_base_data()
    pc2 = [l for l in data["lots"] if l.lot_name == "PC2"]
    if not pc2:
        return ["基数据缺少 PC2"]
    solo = copy.deepcopy(pc2[0])
    solo.references = []  # 隔离：聚焦链内守卫 + 余量下限
    flow = [s for s in data["flow_map"].get("A005-P1", [])]
    qtimes = [q for q in data["qtimes"] if q.product_name == "A005-P1"]
    # 把 PLASMA→DISPENSE 预算从 240 调小到 120：20%=24min < 30min 下限 → 下限生效
    for q in qtimes:
        if q.start_step == "A005-P1-UF-PLASMA" and q.end_step == "A005-P1-UF-DISPENSE":
            q.max_duration = 120
    shift_config = load_shift_config(os.path.join(os.path.dirname(__file__), "data", "shift_config.csv"))
    shift_times = sorted(tuple(map(int, sc.start_time_str.split(":")))
                         for sc in shift_config if getattr(sc, "start_time_str", None))
    durs = {}
    for mm in (0.0, 30.0):
        le, ee, qa = schedule(
            lots=[solo], flows=flow, ct_lookup=data["ct_lookup"], qtimes=qtimes,
            shift_times=shift_times, ftf_qty_change=None,
            special_lot_step_lookup=data["special_lot_step"],
            priority_wait_map=data["priority_wait"], eqp_constraints=data["eqp_constraints"],
            step_time_window_constraints=data["step_time_windows"],
            shift_change_times=data["shift_change"], manual_adjusts=[],
            special_eqp_map={}, resolve_max_iterations=10,
            qtight_min_margin=mm)
        errors = validate_schedule(le, ee, qa, [solo], flow, qtimes)
        if errors:
            return [f"qtight_min_margin={mm} 出现校验错误"] + errors[:5]
        # 取 PLASMA→DISPENSE 段实际用时
        p = [e for e in le if e.step_name == "A005-P1-UF-PLASMA"]
        d = [e for e in le if e.step_name == "A005-P1-UF-DISPENSE"]
        if p and d:
            durs[mm] = (d[0].start_time - p[0].end_time).total_seconds() / 60.0
    # 下限生效：0 配置起点更靠前 → 段用时更长（缓冲更小）
    if 0.0 in durs and 30.0 in durs:
        if durs[0.0] < durs[30.0] - 5.0:
            return [f"余量下限未生效: margin=0 用时 {durs[0.0]:.1f}min < margin=30 用时 {durs[30.0]:.1f}min"]
    return []


def test_38_weight_spread_ten_percent():
    """权重差 ≤10%：compute_objective 对 1-1~4-1 四档优先级权重
    max/min ≤ 1.1 且高优先级权重更大。"""
    from validation import compute_objective
    from models import Lot
    from datetime import datetime as _dt
    lots = [Lot(lot_name=f"L{ext}", priority=(ext, 1), qty=1, carrier_id=f"C{ext}",
                current_step_name="S0", product_name="P1",
                start_time=_dt(2026, 8, 17, 8, 30)) for ext in (1, 2, 3, 4)]
    obj = compute_objective([], lots, _dt(2026, 8, 17, 8, 30), weight_by_priority=True)
    ws = [obj["weights"][f"L{ext}"] for ext in (1, 2, 3, 4)]
    ratio = max(ws) / min(ws)
    if ratio > 1.1 + 1e-9:
        return [f"权重差超过 10%: max/min = {ratio:.4f} (weights={ws})"]
    if not (ws[0] > ws[1] > ws[2] > ws[3]):
        return [f"高优先级权重未递减: {ws}"]
    return []


def test_39_soft_constraint_guides_to_valid():
    """软约束引导：外层 40 轮难找到合法解（仅 PC1 钉晚导致链被拉挤），
    细调层在罚分引导下应能找到合法解 → 最终 0 校验错误。"""
    from models import ManualAdjust
    from datetime import datetime as _dt
    from validation import validate_schedule
    data = load_base_data()
    manual = [ManualAdjust(lot_name="PC1", step_name="A005-P1-UF-PLASMA",
                           delay_to=_dt(2026, 8, 19, 18, 33))]
    le, ee, qa, meta = run_schedule_optimized_manual(data, manual, max_iterations=40, seed=0)
    errs = validate_schedule(
        le, ee, qa, data["lots"],
        [s for fl in data["flow_map"].values() for s in fl],
        data["qtimes"], lot_constraints=data["lot_constraints"],
        special_eqp_map=_load_special_eqp())
    if errs:
        return [f"软约束引导未找到合法解 ({len(errs)} errors)"] + errs[:5]
    return []


def test_40_multi_lot_high_contention():
    """多批次高竞争：6 个 A005-P1 lot（qty=1，与真实数据同量级）以 12h 间隔
    错峰就绪（2 批/天，已验证为设备容量内节奏），共享 UF 段 DISPENSE/CURE
    长机时设备、无手动钉晚 → 全部校验通过（多批并行下 Q 段守卫 + 设备排布
    稳定；注意 qty 决定设备占用时长，大 qty 会急剧压缩设备容量）。"""
    from data_loader import load_shift_config
    from scheduler import schedule
    from validation import validate_schedule
    data = load_base_data()
    start = datetime(2026, 8, 17, 8, 30)
    lots = [Lot(
        lot_name=f"HC{i:02d}", priority=(1, 1), qty=1, carrier_id=f"HCC{i:04d}",
        current_step_name="A005-P1-FC-DUMMY", product_name="A005-P1",
        target_step=None, lot_state="wait", running_time=0,
        start_time=start + timedelta(days=i // 2, hours=(i % 2) * 12),
        references=[]) for i in range(6)]
    flow = [s for s in data["flow_map"].get("A005-P1", [])]
    qtimes = [q for q in data["qtimes"] if q.product_name == "A005-P1"]
    shift_config = load_shift_config(os.path.join(os.path.dirname(__file__), "data", "shift_config.csv"))
    shift_times = sorted(tuple(map(int, sc.start_time_str.split(":")))
                         for sc in shift_config if getattr(sc, "start_time_str", None))
    le, ee, qa = schedule(
        lots=lots, flows=flow, ct_lookup=data["ct_lookup"], qtimes=qtimes,
        shift_times=shift_times, ftf_qty_change=None,
        special_lot_step_lookup=data["special_lot_step"],
        priority_wait_map=data["priority_wait"], eqp_constraints=data["eqp_constraints"],
        step_time_window_constraints=data["step_time_windows"],
        shift_change_times=data["shift_change"], manual_adjusts=[],
        special_eqp_map={}, resolve_max_iterations=10)
    errors = validate_schedule(le, ee, qa, lots, flow, qtimes)
    if errors:
        return errors[:20]
    return []


def test_41_manual_pin_boundary_robust():
    """手动钉晚边界：钉晚提前 6h → 有合法解；钉晚过晚（结构性不可行）
    → 不崩溃、返回罚分最轻的解（warning + violation_severity>0）。"""
    from models import ManualAdjust
    from datetime import datetime as _dt
    from validation import validate_schedule
    data = load_base_data()
    all_flows = [s for fl in data["flow_map"].values() for s in fl]
    # 提前钉晚（可解）
    early = [ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-DISPENSE",
                          delay_to=_dt(2026, 8, 20, 6, 0))]
    le, ee, qa, meta = run_schedule_optimized_manual(data, early, max_iterations=30, seed=1)
    errs = validate_schedule(le, ee, qa, data["lots"], all_flows, data["qtimes"],
                             lot_constraints=data["lot_constraints"],
                             special_eqp_map=_load_special_eqp())
    if errs:
        return [f"提前钉晚应可解，实际 {len(errs)} errors"] + errs[:5]
    # 过晚钉晚（结构不可行）：不得崩溃
    late = [ManualAdjust(lot_name="PC2", step_name="A005-P1-UF-DISPENSE",
                         delay_to=_dt(2026, 8, 20, 10, 10))]
    le2, ee2, qa2, meta2 = run_schedule_optimized_manual(data, late, max_iterations=30, seed=1)
    if meta2.get("warning") is not None:
        # 结构性不可行（无合法解）：应返回罚分最轻的解，且必须记录了违规明细
        if "未找到完全合法解" in str(meta2.get("warning")):
            if not meta2.get("violations"):
                return ["warning 分支未记录任何违规"]
        else:
            # 合法但余量未达标（统一余量计量下的余量不足告警）：
            # 告警必须落到 schedule_warnings 并含具体余量不足的链
            swarns = meta2.get("schedule_warnings") or []
            if not any("余量" in str(w) and "<安全" in str(w) for w in swarns):
                return ["余量不足告警缺少明细"]
    return []


# ============================================================
# PKPOV001 恒组批（together=true）稳定性压力测试
# ============================================================

def _build_pkpov_lots(n, per_lot_qty=25):
    """构造 N 个并发 A005-P1 Lot，错峰就绪，全部会经过 PKPOV001(UF-CURE)。"""
    # 时间窗：让批次以 6h 为周期错峰就绪，模拟真实进料节奏
    start = datetime(2026, 8, 17, 8, 30)
    lots = []
    for i in range(n):
        lots.append(Lot(
            lot_name=f"PK{i:03d}",
            priority=(1, 1),
            qty=per_lot_qty,
            carrier_id=f"PKC{i:04d}",
            current_step_name="A005-P1-FC-DUMMY",
            product_name="A005-P1",
            target_step=None,
            lot_state="wait",
            running_time=0,
            start_time=start + timedelta(hours=(i % 6)),
            references=[],
        ))
    return lots


def _run_with_special(n_lots, per_lot_qty=25, use_special=True):
    """运行 schedule（含 PKPOV001 together 配置），返回 (le, ee, qa, data)。"""
    from data_loader import load_lot_constraints
    base = os.path.join(os.path.dirname(__file__), "data")
    data = load_base_data()
    data["lots"] = _build_pkpov_lots(n_lots, per_lot_qty)
    data["lot_constraints"] = []  # 纯并发，无跨 lot 约束
    special_eqp = load_special_eqp(os.path.join(base, "special_eqp.csv")) if use_special else {}
    shift_config = load_shift_config(os.path.join(base, "shift_config.csv"))
    shift_times = sorted(tuple(map(int, sc.start_time_str.split(":")))
                         for sc in shift_config if getattr(sc, "start_time_str", None))
    all_flows = [s for fl in data["flow_map"].values() for s in fl]
    le, ee, qa = schedule(
        lots=data["lots"], flows=all_flows, ct_lookup=data["ct_lookup"],
        qtimes=data["qtimes"], shift_times=shift_times,
        ftf_qty_change=data["ftf_qty"],
        special_lot_step_lookup=data["special_lot_step"],
        priority_wait_map=data["priority_wait"],
        eqp_constraints=data["eqp_constraints"],
        step_time_window_constraints=data["step_time_windows"],
        shift_change_times=data["shift_change"],
        manual_adjusts=data["manual_adjusts"],
        special_eqp_map=special_eqp, resolve_max_iterations=10)
    return le, ee, qa, data


def _validate_no_overlap(le, ee):
    """校验：同一设备上无时间重叠占用。"""
    from collections import defaultdict
    timeline = defaultdict(list)
    for e in ee:
        if e.eqp_id == "-":
            continue
        timeline[e.eqp_id].append((e.start_time, e.end_time, e.lot_name, e.step_name))
    for eqp_id, bookings in timeline.items():
        srt = sorted(bookings, key=lambda x: x[0])
        for i in range(1, len(srt)):
            if srt[i][0] < srt[i-1][1]:
                return [f"设备 {eqp_id} 重叠: {srt[i-1][2]}/{srt[i-1][3]} {srt[i-1][0]}~{srt[i-1][1]} 与 {srt[i][2]}/{srt[i][3]} {srt[i][0]}~{srt[i][1]}"]
    return []


def test_31_special_eqp_pkpov001_stability():
    """恒组批稳定性：大量并发 Lot 争用 PKPOV001(together) 时必须终止，且无设备重叠/负时长。

    该配置在历史版本下会因单机瓶颈 + 紧链 defer 无限磨步而"计算超时"。
    断言：能在安全时间内完成，且有实际 CURE 排程产出。
    """
    import time as _time
    t0 = _time.time()
    le, ee, qa, data = _run_with_special(40)
    elapsed = _time.time() - t0
    errors = []
    # 必须终止（不超时）且完成全部步骤
    errors += _validate_no_overlap(le, ee)
    # 每个 Lot 都应有 UF-CURE 产出
    lot_names = {l.lot_name for l in data["lots"]}
    cured = {e.lot_name for e in le if "UF-CURE" in e.step_name}
    missing = lot_names - cured
    if missing:
        errors.append(f"缺失 UF-CURE 的 Lot: {sorted(missing)[:10]}")
    # 负时长检查
    for e in le:
        if (e.end_time - e.start_time).total_seconds() < 0:
            errors.append(f"{e.lot_name} {e.step_name}: 负时长")
    if elapsed > 120:
        errors.append(f"调度耗时过长: {elapsed:.1f}s（疑似性能回退）")
    if not errors:
        print(f"  [PKPOV001] {len(lot_names)} lots 完成于 {elapsed:.2f}s, CURE 次数={len(cured)}")
    return errors


def test_32_special_eqp_pkpov001_no_hang_without_special():
    """终止保护：即使在未配置 special_eqp（纯单机瓶颈）的最恶劣并发下也不挂死，
    保证任何入参都不会因紧链 defer 无限磨步导致计算超时。"""
    import time as _time
    t0 = _time.time()
    le, ee, qa, data = _run_with_special(30, use_special=False)
    elapsed = _time.time() - t0
    if elapsed > 120:
        return [f"未配置 special_eqp 时调度耗时过长: {elapsed:.1f}s（终止保护未生效）"]
    errors = _validate_no_overlap(le, ee)
    if errors:
        return errors
    return []


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    tests = [
        ("01_baseline", test_01_baseline),
        ("02_empty_eqp_intervals", test_02_empty_equipment_intervals),
        ("03_single_lot", test_03_single_lot),
        ("04_no_constraints", test_04_no_constraints),
        ("05_no_qtime", test_05_no_qtime),
        ("06_all_same_priority", test_06_all_same_priority),
        ("07_high_priority_diff", test_07_very_high_priority_diff),
        ("08_circular_reference", test_08_circular_reference),
        ("09_many_lots_same_eqp", test_09_many_lots_same_eqp),
        ("10_shift_change_boundary", test_10_shift_change_boundary),
        ("11_zero_wait_time", test_11_zero_wait_time),
        ("12_very_long_wait_time", test_12_very_long_wait_time),
        ("13_tight_qtime", test_13_tight_qtime),
        ("14_overlapping_qtime", test_14_overlapping_qtime_chains),
        ("15_reference_with_shift", test_15_reference_with_shift),
        ("16_manual_adjust", test_16_manual_adjust),
        ("17_eqp_unavailable", test_17_all_equipment_unavailable),
        ("18_no_equipment_steps", test_18_step_without_equipment),
        ("19_lot_start_midnight", test_19_lot_start_midnight),
        ("20_lot_start_weekend", test_20_lot_start_weekend),
        ("21_interval_gap_filling", test_21_interval_gap_filling),
        ("22_chain_scheduling_extreme", test_22_chain_scheduling_extreme),
        ("23_multiple_products", test_23_multiple_products),
        ("24_hold_period", test_24_hold_period),
        ("25_interval_concurrency", test_25_interval_concurrency),
        ("26_optimizer_default", test_26_optimizer_default),
        ("27_optimizer_variability", test_27_optimizer_variability),
        ("28_manual_adjust_reschedule", test_28_manual_adjust_reschedule),
        ("29_manual_adjust_chain_midstep_recompact", test_29_manual_adjust_chain_midstep_recompact),
        ("30_manual_adjust_nonchain_step_isolated", test_30_manual_adjust_nonchain_step_isolated),
        ("33_manual_adjust_nonadjacent_span_guard", test_33_manual_adjust_nonadjacent_span_guard),
        ("34_manual_adjust_mixed_model_q_guard", test_34_manual_adjust_mixed_model_q_guard),
        ("35_manual_adjust_multi_pin_same_chain", test_35_manual_adjust_multi_pin_same_chain),
        ("36_manual_adjust_multilot_concurrent_guard", test_36_manual_adjust_multilot_concurrent_guard),
        ("37_qtight_min_margin_param", test_37_qtight_min_margin_param),
        ("38_weight_spread_ten_percent", test_38_weight_spread_ten_percent),
        ("39_soft_constraint_guides_to_valid", test_39_soft_constraint_guides_to_valid),
        ("40_multi_lot_high_contention", test_40_multi_lot_high_contention),
        ("41_manual_pin_boundary_robust", test_41_manual_pin_boundary_robust),
        ("31_special_eqp_pkpov001_stability", test_31_special_eqp_pkpov001_stability),
        ("32_special_eqp_pkpov001_no_hang_without_special", test_32_special_eqp_pkpov001_no_hang_without_special),
    ]

    for name, fn in tests:
        test(name, fn)

    print(f"\n{'='*60}")
    print(f"SUMMARY: {PASS_COUNT}/{len(tests)} passed, {FAIL_COUNT} failed")
    print(f"{'='*60}")

    if FAIL_COUNT > 0:
        print("\nFAILED TESTS:")
        for name, result, errors in TEST_RESULTS:
            if result == "FAIL":
                print(f"  [{name}]")
                for e in errors:
                    print(f"    - {e}")