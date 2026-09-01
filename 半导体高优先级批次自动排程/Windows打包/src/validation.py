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
    if not shift_times:
        return dt
    for h, m in shift_times:
        candidate = dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > dt:
            return candidate
    return dt.replace(hour=shift_times[0][0], minute=shift_times[0][1], second=0, microsecond=0) + timedelta(days=1)


def _next_morning_shift(dt: datetime, shift_times: list[tuple[int, int]]) -> datetime:
    if not shift_times:
        return dt
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


def _qtime_margin_benefit(rel: float) -> float:
    """Q-time 余量收益（非线性，越大越好，返回 [0,1]）。

    用户规则（2026-09-02 修订）：收益以"用户设置的 Q-time 安全余量"为参考点，
      safe = max(预算 D × 安全余量%, 安全余量下限)，rel = 实际余量 / safe：
      - rel ≥ 1（余量达到安全余量）: 收益 0.2 → 1.0 线性回升，2 倍安全余量处饱和
        （"余量足够多 ≈ 同等收益"）；
      - rel < 1（余量低于安全余量）: 收益 = 0.2 × rel² 平方衰减，缺口越大收益损失
        越剧烈（rel=0.5 → 0.05，rel=0.25 → 0.0125，接近耗尽 → 0）；
      - 恰好在安全余量点（rel=1）收益 = 0.2。
    效果：低于安全余量收益断崖式下降；达到安全余量才有 0.2；再往上渐近饱和。
    """
    if rel <= 0:
        return 0.0
    if rel < 1.0:
        return 0.2 * rel * rel
    return min(1.0, 0.2 + 0.8 * (rel - 1.0))


# ── 统一余量计量（分钟，直接并入得分，与加权完工时间同一单位） ──
# 以用户安全余量 safe = max(预算×安全余量%, 安全余量下限) 为分界：
#   - margin ≥ safe（达标区）：微小奖励（减分，渐近饱和）——"得分很少"；
#   - margin < safe（缺口区）：重罚，越接近 0 罚分越大，逼近一次超Q违规的量级。
_MARGIN_REWARD_CAP = 20.0    # 达标区单链奖励上限（分钟）
_ZERO_MARGIN_PENALTY = 1e6   # 余量=0 时单链罚分基数（分钟）：≈ 超Q违规（_PENALTY_PER_ERR）的量级


def compute_objective(
    lot_entries: list[ScheduleEntry],
    lots: list[Lot],
    schedule_start: datetime,
    weight_by_priority: bool = True,
    qtimes: Optional[list[QTimeConstraint]] = None,
    qtime_safety_margin_pct: float = 20.0,
    qtime_min_margin_min: float = 30.0,
    qtime_shortfall_gradient: float = 3.0,
) -> dict:
    """计算目标函数。

    返回:
      - completion_times: {lot_name: 末步 end_time}
      - weighted_total: 加权总完工时间（分钟），= Σ w(lot) * (末步end - schedule_start)
          w 按优先级：高优先级（ext_priority 小）权重更大
          w = 1 + (max_ext_priority - ext_priority)
      - qtime_margins: 每条 Q-time 规则的剩余余量（分钟）；无 Q-time 时为空 dict
      - min_qtime_margin: 全部 Q-time 规则中的最小余量（分钟），无 Q-time 时为 None
      - min_qtime_margin_ratio: 最小余量 / 该规则预算（窗口占比，用于展示）
      - min_qtime_margin_benefit: 非线性收益（同分次目标，越大越好）
      - qtime_margin_term: 统一余量项（分钟，正=罚分、负=奖励），直接并入 score。
          达标区（margin ≥ safe）微小奖励（≤ _MARGIN_REWARD_CAP，渐近饱和）；
          缺口区（margin < safe）重罚 = gradient × _ZERO_MARGIN_PENALTY × (1-rel)³，
          margin→0 时逼近超Q违规量级，引导搜索远离"余量濒危"的解。
      - qtime_margin_violations: 余量低于安全余量的链明细（供前端告警展示）
      - score: 加权总完工时间（分钟，不含余量项）
    """
    completion: dict[str, datetime] = {}
    for e in lot_entries:
        cur = completion.get(e.lot_name)
        if cur is None or e.end_time > cur:
            completion[e.lot_name] = e.end_time

    max_ext = 1
    for lot in lots:
        max_ext = max(max_ext, lot.priority[0])
    # 优先级跨度（防 0 除；span 即 ext 从 1 到 max_ext 的档位数）
    span = max(1, max_ext - 1)
    weights: dict[str, float] = {}
    for lot in lots:
        ext = lot.priority[0]
        if weight_by_priority:
            # 权重差 ≤10%：ext=1（最高优先）权重 1.10，ext=max_ext 权重 1.00，
            # 在 [1, max_ext] 间线性递减，任意两个优先级权重差 ≤10%。
            weights[lot.lot_name] = 1.0 + 0.1 * (max_ext - ext) / span
        else:
            weights[lot.lot_name] = 1.0

    weighted_total = 0.0
    for lot_name, end in completion.items():
        dur_min = (end - schedule_start).total_seconds() / 60.0
        weighted_total += weights.get(lot_name, 1.0) * dur_min

    # 统一余量项：Q-time 剩余余量并入得分（达标区微奖励 / 缺口区重罚）
    qtime_margins = {}
    min_qtime_margin = None
    min_qtime_margin_ratio = None
    min_qtime_margin_benefit = None
    qtime_margin_term = 0.0
    _violations: list[str] = []
    if qtimes:
        qtime_margins = _qtime_margins_from_entries(lot_entries, lots, qtimes)
        if qtime_margins:
            min_qtime_margin = min(qtime_margins.values())
            # 归一化余量：margin / budget（比率）。不同长度 Q 段可比，
            # 紧段（240min）与宽松段（1440min）统一在同一尺度上。
            _prod_of = {l.lot_name: l.product_name for l in lots}
            _ratios = []
            # 收益参考点 = 用户安全余量：safe = max(预算 × %, 下限)。
            # rel = margin / safe：rel=1 为分界（达标 / 缺口）。
            _rels = []
            for (ln, qs, qe), m in qtime_margins.items():
                for q in qtimes:
                    if (q.product_name == _prod_of.get(ln)
                            and q.start_step == qs and q.end_step == qe
                            and q.max_duration and q.max_duration > 0):
                        _ratios.append(m / float(q.max_duration))
                        safe = max(q.max_duration * qtime_safety_margin_pct / 100.0,
                                   qtime_min_margin_min)
                        rel = m / safe if safe > 0 else 1.0
                        _rels.append(rel)
                        if rel >= 1:
                            # 达标区：微小奖励（减分），渐近饱和于 _MARGIN_REWARD_CAP
                            qtime_margin_term -= _MARGIN_REWARD_CAP * (1.0 - 1.0 / rel)
                        else:
                            # 缺口区：重罚（立方放大），margin→0 逼近超Q违规量级
                            qtime_margin_term += qtime_shortfall_gradient * _ZERO_MARGIN_PENALTY * (
                                (1.0 - rel) ** 3)
                            _violations.append(
                                f"{ln} {qs}→{qe} 余量{m:.0f}min<安全{safe:.0f}min")
                        break
            if _rels:
                min_qtime_margin_ratio = min(_ratios)
                # 非线性收益：低于安全余量断崖下降，达到安全余量 0.2，往上渐近饱和
                min_qtime_margin_benefit = min(_qtime_margin_benefit(r) for r in _rels)

    score = weighted_total

    return {
        "completion_times": completion,
        "weighted_total": weighted_total,
        "score": score,
        "weights": weights,
        "schedule_start": schedule_start,
        "qtime_margins": qtime_margins,
        "min_qtime_margin": min_qtime_margin,
        "min_qtime_margin_ratio": min_qtime_margin_ratio,
        "min_qtime_margin_benefit": min_qtime_margin_benefit,
        "qtime_margin_term": qtime_margin_term,
        "qtime_margin_violations": _violations,
    }
