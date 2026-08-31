"""排程结果校验与目标函数计算

提供两个核心函数：
- validate_schedule(): 全量校验一个排程是否合法（步骤顺序、设备重叠、Q-time、reference、缺失步骤）
- compute_objective(): 计算目标函数（加权总完工时间、Q-time 余量）

供 optimizer.py 每轮迭代择优、stress_test.py 回归、run_constrained.py 结果校验复用。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from models import Lot, QTimeConstraint, ScheduleEntry, EqpScheduleEntry, QTimeAlert


def _next_shift_after(dt: datetime, shift_times: list[tuple[int, int]]) -> datetime:
    for h, m in shift_times:
        candidate = dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > dt:
            return candidate
    return dt.replace(hour=shift_times[0][0], minute=shift_times[0][1], second=0, microsecond=0) + timedelta(days=1)


def _next_morning_shift(dt: datetime, shift_times: list[tuple[int, int]]) -> datetime:
    h, m = shift_times[0]
    candidate = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate > dt:
        return candidate
    return candidate + timedelta(days=1)


def _reference_release_time(
    ref_step_end: datetime,
    start_mod: Optional[str],
    shift_times: list[tuple[int, int]],
) -> datetime:
    """计算 reference 释放时间：reference 步骤结束后，按 start_mod 偏移。"""
    if not start_mod or start_mod in ("", "0"):
        return ref_step_end
    if start_mod in ("shift",):
        return _next_shift_after(ref_step_end, shift_times)
    if start_mod in ("shift_day",):
        return _next_morning_shift(ref_step_end, shift_times)
    try:
        return ref_step_end + timedelta(hours=float(start_mod))
    except (ValueError, TypeError):
        return ref_step_end


def validate_schedule(
    lot_entries: list[ScheduleEntry],
    eqp_entries: list[EqpScheduleEntry],
    qtime_alerts: list[QTimeAlert],
    lots: list[Lot],
    flows: list,
    qtimes: list[QTimeConstraint],
    lot_constraints: Optional[list] = None,
    shift_times: Optional[list[tuple[int, int]]] = None,
    special_eqp_map: Optional[dict] = None,
) -> list[str]:
    """返回违反约束的错误列表；空列表 = 合法。

    检查项：
    1. 每 Lot 步骤按序号递增、不重叠
    2. 设备无重叠占用（特殊设备：按 max_lots/max_qty 容量校验，允许批次/并行重叠）
    3. Q-time：由 entries 重算每条规则 × 每个 Lot 的实际用时（比 qtime_alerts 更可靠）
    4. reference：lot 的 start_step 开始时间 >= reference 步骤结束时间 + start_mod 偏移
    5. 缺失步骤（该 Lot 未完成应有步骤）
    """
    errors: list[str] = []

    # ---- 1. 步骤顺序 ----
    lot_steps: dict[str, list[ScheduleEntry]] = {}
    for e in lot_entries:
        lot_steps.setdefault(e.lot_name, []).append(e)
    for lot_name, steps in lot_steps.items():
        ordered = sorted(steps, key=lambda e: (e.start_time, e.step_number))
        # 按实际开始顺序检查不重叠
        for i in range(1, len(ordered)):
            if ordered[i].start_time < ordered[i - 1].end_time:
                errors.append(
                    f"{lot_name}: 步骤顺序异常 - step {ordered[i].step_name} 开始于 "
                    f"{ordered[i].start_time} 早于上一步 {ordered[i-1].step_name} 结束于 {ordered[i-1].end_time}")

    # ---- 2. 设备不重叠 ----
    eqp_timeline: dict[str, list] = {}
    for e in eqp_entries:
        if e.eqp_id == "-":
            continue
        eqp_timeline.setdefault(e.eqp_id, []).append((e.start_time, e.end_time, e.lot_name, e.step_name, e.qty))
    special_eqp_map = special_eqp_map or {}
    for eqp_id, bookings in eqp_timeline.items():
        spec = special_eqp_map.get(eqp_id)
        sb = sorted(bookings, key=lambda x: x[0])
        if spec is not None:
            # 特殊设备（恒组批 together / 并行 together=false）：同一设备上多个批次/槽位
            # 允许重叠，但任一时刻同时在做的 Lot 数不得超过 max_lots、总量不得超过 max_qty。
            # 用扫描线统计最大并发数/并发量。
            events = []
            for st, et, ln, sn, qty in sb:
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
                errors.append(
                    f"设备 {eqp_id}: 并发批次超限 - 最多 {max_lots} 批同时占用（上限 {spec.max_lots}）")
            if spec.max_qty and max_qty > spec.max_qty:
                errors.append(
                    f"设备 {eqp_id}: 并发总量超限 - 最多 {max_qty} 数量同时占用（上限 {spec.max_qty}）")
            continue
        for i in range(1, len(sb)):
            if sb[i][0] < sb[i - 1][1]:
                errors.append(
                    f"设备 {eqp_id}: 重叠占用 - {sb[i-1][2]}/{sb[i-1][3]} "
                    f"({sb[i-1][0]}~{sb[i-1][1]}) 与 {sb[i][2]}/{sb[i][3]} ({sb[i][0]}~{sb[i][1]})")

    # ---- 3. Q-time 重算 ----
    if qtimes:
        q_errors = _check_qtime_from_entries(lot_entries, lots, qtimes)
        errors.extend(q_errors)
    # 兼容：qtime_alerts 兜底（若传入）
    if qtime_alerts:
        for a in qtime_alerts:
            if a.status != "OK":
                errors.append(f"Q-time 超时: {a.lot_name} {a.qtime_rule} [{a.status}] over={a.over_minutes}min")

    # ---- 4. reference 约束 ----
    if lot_constraints:
        shift_times = shift_times or []
        ref_errors = _check_references(lot_entries, lot_constraints, shift_times)
        errors.extend(ref_errors)

    # ---- 4b. lead 终检（闸A：lot2.step2 不得早于 lot1.step1 完成） ----
    _lead_errors = _check_lead(lot_entries, lots)
    errors.extend(_lead_errors)

    # ---- 5. 缺失步骤 ----
    flow_map: dict[str, list] = {}
    for f in flows:
        flow_map.setdefault(f.product_name, []).append(f)
    try:
        from data_loader import get_step_index_in_flow
    except ImportError:
        get_step_index_in_flow = None
    for lot in lots:
        product_flow = flow_map.get(lot.product_name)
        if not product_flow or get_step_index_in_flow is None:
            continue
        try:
            start_idx = get_step_index_in_flow(product_flow, lot.current_step_name)
        except ValueError:
            continue
        # 有 target_step 时只要求排到该步为止（与调度器 remaining 截断一致），
        # 否则会把 target 之后的步骤误报为"缺失步骤"（历史 bug）。
        end_idx = len(product_flow)
        if lot.target_step:
            try:
                end_idx = get_step_index_in_flow(product_flow, lot.target_step) + 1
            except ValueError:
                pass
        expected = [s.step_name for s in product_flow[start_idx:end_idx]]
        actual = [e.step_name for e in lot_steps.get(lot.lot_name, [])]
        missing = set(expected) - set(actual)
        if missing:
            errors.append(f"{lot.lot_name}: 缺失步骤 {missing}")

    return errors


def _check_qtime_from_entries(
    lot_entries: list[ScheduleEntry],
    lots: list[Lot],
    qtimes: list[QTimeConstraint],
) -> list[str]:
    """从排程条目重算 Q-time 是否超限，避免只依赖告警漏报。"""
    errors: list[str] = []
    for lot in lots:
        steps = {e.step_name: e for e in lot_entries if e.lot_name == lot.lot_name}
        if not steps:
            continue
        for q in qtimes:
            if q.product_name != lot.product_name:
                continue
            s = steps.get(q.start_step)
            e = steps.get(q.end_step)
            if s is None or e is None:
                continue
            start_mod = (q.start_mod or "track in").strip()
            end_mod = (q.end_mod or "track out").strip()
            q_start = s.end_time if start_mod == "track out" else s.start_time
            q_end = e.start_time if end_mod == "track in" else e.end_time
            delta_min = (q_end - q_start).total_seconds() / 60.0
            if delta_min > q.max_duration + 1e-6:
                errors.append(
                    f"Q-time 超时: {lot.lot_name} {q.start_step}→{q.end_step} "
                    f"用时 {delta_min:.1f}min > 限制 {q.max_duration}min")
    return errors


def _check_references(
    lot_entries: list[ScheduleEntry],
    lot_constraints: list,
    shift_times: list[tuple[int, int]],
) -> list[str]:
    """校验 reference：lot 的 start_step 开始时间 >= reference 步骤结束时间 + start_mod 偏移。"""
    errors: list[str] = []
    by_lot_step: dict[tuple[str, str], ScheduleEntry] = {}
    for e in lot_entries:
        by_lot_step[(e.lot_name, e.step_name)] = e

    for c in lot_constraints:
        if not c.reference_lot or not c.reference_step:
            continue
        # lead 上游对齐引用（lead_id 带 "-u" 后缀）是相位对齐软目标（引擎评分不计入
        # 硬违背），校验时跳过——否则 leader 因设备窗整体后移到白班最早空位时，
        # 会被误判为硬错误（真实数据 PC2.MD-MOLDING 同样存在该软对齐差）。
        if getattr(c, "lead_id", "") and str(c.lead_id).endswith("-u"):
            continue
        # reference 步骤在 reference_lot 上的结束时间
        ref_entry = by_lot_step.get((c.reference_lot, c.reference_step))
        if ref_entry is None:
            continue
        release = _reference_release_time(ref_entry.end_time, c.start_mod, shift_times)
        # 被约束 lot 的 start_step
        start_step = c.start_step
        if not start_step:
            continue
        dep_entry = by_lot_step.get((c.lot_name, start_step))
        if dep_entry is None:
            continue
        if dep_entry.start_time < release:
            errors.append(
                f"reference 违背: {c.lot_name}.{start_step} 开始 {dep_entry.start_time} "
                f"早于 {c.reference_lot}.{c.reference_step} 释放 {release}")
    return errors


def _check_lead(
    lot_entries: list[ScheduleEntry],
    lots: list[Lot],
) -> list[str]:
    """lead 终检：闸A——lot2(配套).step2 不得早于 lot1(领导).step1 完成。

    逐 Q 不超已由 validate_schedule 第 3 步（_check_qtime_from_entries 全量重算）覆盖，
    此处只补闸A 的硬性违背检查。
    """
    errors: list[str] = []
    by_lot_step = {(e.lot_name, e.step_name): e for e in lot_entries}
    for lot in lots:
        for lp in lot.lead_pairs or []:
            e1 = by_lot_step.get((lp.lot1, lp.step1))
            e2 = by_lot_step.get((lp.lot2, lp.step2))
            if e1 is None or e2 is None:
                continue
            if e2.start_time < e1.end_time:
                errors.append(
                    f"lead 违背(闸A): {lp.lot2}.{lp.step2} 开始 {e2.start_time} "
                    f"早于领导批 {lp.lot1}.{lp.step1} 完成 {e1.end_time}")
    return errors


def _qtime_margins_from_entries(
    lot_entries: list[ScheduleEntry],
    lots: list[Lot],
    qtimes: list[QTimeConstraint],
) -> dict:
    """计算每条 Q-time 规则的实际剩余余量（分钟）= max_duration - 实际用时。

    返回 { (lot_name, start_step, end_step): margin_min }，越大的余量越好。
    """
    margins: dict = {}
    for lot in lots:
        steps = {e.step_name: e for e in lot_entries if e.lot_name == lot.lot_name}
        if not steps:
            continue
        for q in qtimes:
            if q.product_name != lot.product_name:
                continue
            s = steps.get(q.start_step)
            e = steps.get(q.end_step)
            if s is None or e is None:
                continue
            start_mod = (q.start_mod or "track in").strip()
            end_mod = (q.end_mod or "track out").strip()
            q_start = s.end_time if start_mod == "track out" else s.start_time
            q_end = e.start_time if end_mod == "track in" else e.end_time
            delta_min = (q_end - q_start).total_seconds() / 60.0
            margins[(lot.lot_name, q.start_step, q.end_step)] = q.max_duration - delta_min
    return margins


def compute_objective(
    lot_entries: list[ScheduleEntry],
    lots: list[Lot],
    schedule_start: datetime,
    weight_by_priority: bool = True,
    qtimes: Optional[list[QTimeConstraint]] = None,
) -> dict:
    """计算目标函数。

    返回:
      - completion_times: {lot_name: 末步 end_time}
      - weighted_total: 加权总完工时间（分钟），= Σ w(lot) * (末步end - schedule_start)
          w 按优先级：高优先级（ext_priority 小）权重更大
          w = 1 + (max_ext_priority - ext_priority)
      - qtime_margins: 每条 Q-time 规则的剩余余量（分钟）；无 Q-time 时为空 dict
      - min_qtime_margin: 全部 Q-time 规则中的最小余量（分钟），无 Q-time 时为 None
      - score: 主目标加权总完工时间（分钟）
    """
    completion: dict[str, datetime] = {}
    for e in lot_entries:
        cur = completion.get(e.lot_name)
        if cur is None or e.end_time > cur:
            completion[e.lot_name] = e.end_time

    max_ext = 1
    for lot in lots:
        max_ext = max(max_ext, lot.priority[0])
    weights: dict[str, float] = {}
    for lot in lots:
        ext = lot.priority[0]
        if weight_by_priority:
            weights[lot.lot_name] = 1.0 + (max_ext - ext)  # 高优先级权重更大
        else:
            weights[lot.lot_name] = 1.0

    weighted_total = 0.0
    for lot_name, end in completion.items():
        dur_min = (end - schedule_start).total_seconds() / 60.0
        weighted_total += weights.get(lot_name, 1.0) * dur_min

    # 次目标：Q-time 剩余余量（越大越好）
    qtime_margins = {}
    min_qtime_margin = None
    if qtimes:
        qtime_margins = _qtime_margins_from_entries(lot_entries, lots, qtimes)
        if qtime_margins:
            min_qtime_margin = min(qtime_margins.values())

    score = weighted_total

    return {
        "completion_times": completion,
        "weighted_total": weighted_total,
        "score": score,
        "weights": weights,
        "schedule_start": schedule_start,
        "qtime_margins": qtime_margins,
        "min_qtime_margin": min_qtime_margin,
    }
