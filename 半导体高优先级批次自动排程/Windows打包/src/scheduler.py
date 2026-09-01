"""核心排程引擎 —— 启发式调度器

特性：
- 链式排程：Q-time 链段步骤打包调度，紧链/松链判定
- 反向调度：链拆分后，后缀步骤从 Q-time deadline 往前倒排，避免两段相隔太远
- Q-time 感知：有活跃 Q-time 的 Lot 在就绪队列中优先级提升
- 特殊设备批处理：支持多 Lot 同时作业（max_lots/max_qty/together）
- 所有约束：设备不可用、时间窗、换班、reference、手动调整、FTF 数量变更
- 确定性 Greedy 调度，作为 GA 的评估函数和 baseline
"""
from __future__ import annotations

import logging
import math
import os
import bisect
import dataclasses
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# 业务告警（跨班次风险 / 排程合理性 / 无法满足约束跳过）默认只写入 out_warnings
# 供前端展示，不再直接打印终端——避免多 seed 优化（默认 40 轮）时每轮重复刷屏。
# 需要恢复终端输出时：设置环境变量 SCHED_WARN=1，或调用方传 verbose=True。
_WARN_ENV = os.environ.get("SCHED_WARN") == "1"

# 最近一次粗排程的"合理性审计"结果，供 schedule() 生成智能告警：
#   cycle_lots   —— lot_constraints 中互相引用的环内 lot 集合
#   fallback_used——不动点迭代 60 轮未收敛、已用"自然锚点回退"打破雪崩
_anchor_audit: dict = {"cycle_lots": set(), "fallback_used": False}

from models import (
    Lot, FlowStep, QTimeConstraint,
    ScheduleEntry, EqpScheduleEntry, QTimeAlert,
    StepTimeWindow, ShiftChangeTime, EqpConstraint, ManualAdjust, SpecialLotStep, SpecialEqp,
    LotConstraint,
)
from data_loader import get_product_flow_map, get_step_index_in_flow, get_step_ct

INF = float("inf")
FAR_FUTURE = datetime(2099, 12, 31, 0, 0)
SCHEDULE_WINDOW_DAYS = 365
TIGHT_CHAIN_THRESHOLD = 240  # 紧链判定阈值（分钟）
QTIGHT_SAFETY_MARGIN = 20.0  # 紧 Q-time 起点延迟的安全余量（百分比，按 Q 预算的 % 预留缓冲）
QTIGHT_MIN_MARGIN = 30.0     # 紧 Q-time 安全余量下限（分钟）：实际余量 = max(预算×%, 下限)
# 原因：百分比对短段不公平——240min 的 20% 仅 48min、120min 仅 24min，缓冲被压缩；
# 加绝对下限保证任何紧段至少预留 30min 缓冲，短段不易被击穿。
CROSS_SHIFT_AVOID = True  # 紧 Q 链不跨班次（用户规则）：紧链相邻步骤（如 PLASMA→DISPENSE）
# 的 Q 窗口若跨过班次切换时刻，把链首起点推后到班次之后，使整段链落在同一班次内。
# best-effort：推后会撑破上游紧链或不可行时保留原排程并输出跨班次告警。可配置关闭。

# 恒组批（together=true）等待凑批窗口（分钟）：到达同一"批次步骤"（如 CURE）的时间差在此
# 窗口内的不同 Lot 会合并进同一次同炉治程（最多 max_lots/max_qty）。窗口越大越倾向凑满一炉
# 减少设备次数，但会把先到 Lot 的出批时间推迟；应不超过该步骤常见紧 Q-time 预算，避免先到
# Lot 因等待被推出 Q-time（典型如 DISPENSE→CURE=240）。
BATCH_WAIT_WINDOW = 240

# 紧链整链块（_tight_chain_defer）单 Lot 连续 defer 上限：整链块放不下时按 +30min 步进等待
# 设备释放。若某一 Lot 连续达到该次数仍未成功，则放弃整链块（退回拆链/单步调度），保证调度
# 终止——避免在单机瓶颈（如 UF-CURE 只剩 PKPOV001 一台）下无限磨步把"计算超时"。
TIGHT_CHAIN_DEFER_MAX = 10

# ---- 约束展开缓存（纯函数 + 内容 key，跨两遍/跨迭代复用，不改变结果）----
# 三个展开函数只依赖约束数据内容与排程窗口，与 lot 顺序/迭代轮次无关；
# schedule_optimized 每轮构造都会调用 schedule() 两遍，这里缓存可避免 40 轮
# 重复展开同一批停机窗/时间窗（实测占生产路径约 1/3 耗时）。内容 key 保证
# 换数据后不会误命中旧结果。
_EXPAND_CACHE: dict = {}
_EXPAND_CACHE_MAX = 64


def _expand_cache_get(
    cache_name: str,
    constraints: list,
    schedule_start: datetime,
    schedule_end: datetime,
    thunk,
):
    if not constraints:
        return thunk()
    key = (cache_name, tuple(tuple(dataclasses.astuple(c)) for c in constraints),
           schedule_start, schedule_end)
    hit = _EXPAND_CACHE.get(key)
    if hit is not None:
        return hit
    out = thunk()
    if len(_EXPAND_CACHE) >= _EXPAND_CACHE_MAX:
        _EXPAND_CACHE.clear()
    _EXPAND_CACHE[key] = out
    return out


def _is_parallel_eqp(eqp_id: str, special_eqp_map: dict) -> bool:
    """together=false 的设备为"并行/多槽"型：可并发作业（受 max_lots/max_qty 限制），
    不独占、无需互斥排他，后续 Lot 到点即入，不需等待上一批结束。"""
    spec = special_eqp_map.get(eqp_id)
    return spec is not None and not spec.together


# ============================================================
# 辅助工具函数
# ============================================================

def _interval_sort_key(iv: tuple):
    return iv[0] if iv[0] is not None else datetime.min


def _add_machine_interval(
    machine_intervals: dict[str, list],
    eqp_id: str,
    interval: tuple[datetime, datetime],
) -> None:
    ivals = machine_intervals.setdefault(eqp_id, [])
    bisect.insort(ivals, interval, key=_interval_sort_key)


def _next_shift_after(dt: datetime, shift_times: list[tuple[int, int]]) -> datetime:
    # 空班次表防护：无班次概念时立即释放（否则 shift_times[0] 越界崩溃）
    if not shift_times:
        return dt
    for h, m in shift_times:
        candidate = dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > dt:
            return candidate
    return dt.replace(hour=shift_times[0][0], minute=shift_times[0][1], second=0, microsecond=0) + timedelta(days=1)


def _next_morning_shift(dt: datetime, shift_times: list[tuple[int, int]]) -> datetime:
    # 空班次表防护：无班次概念时立即释放
    if not shift_times:
        return dt
    h, m = shift_times[0]
    candidate = dt.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate > dt:
        return candidate
    return candidate + timedelta(days=1)


def get_step_wait_time(ext_priority: int, int_priority: int,
                       priority_wait_map: Optional[dict[tuple[int, int], int]] = None) -> int:
    if priority_wait_map:
        key = (ext_priority, int_priority)
        if key in priority_wait_map:
            val = priority_wait_map[key]
            return val if val is not None and val > 0 else 0
    return 10


def _reorder_eqp_ids_by_preference(
    eqp_ids: list[str],
    lot_name: str,
    step_name: str,
    eqp_preferences: Optional[dict[tuple[str, str], list[str]]],
) -> list[str]:
    """按 GA 设备偏好重排候选设备顺序（偏好在前，其余保持原序）。

    与主循环单步路径（_run_schedule_pass）的偏好语义保持一致，供链式整链块、
    前缀、反向链路径复用，避免"配置了偏好但链内路径被忽略"（探针 P7）。
    仅重排顺序、不改候选集合：偏好设备在同条件（可用时刻并列）时被优先选中。
    """
    if not eqp_preferences or len(eqp_ids) <= 1:
        return eqp_ids
    preferred = eqp_preferences.get((lot_name, step_name))
    if not preferred:
        return eqp_ids
    ordered = [p for p in preferred if p in eqp_ids]
    for e in eqp_ids:
        if e not in ordered:
            ordered.append(e)
    return ordered


# Q-time 链内步间等待的固定安全余量（分钟）：分摊预算时保留，避免刚好卡在临界
CHAIN_WAIT_SAFETY = 20


def _chain_gap_budget(
    chain_info: dict,
    ct_lookup: dict,
    lot: Optional[Lot],
    qtime_by_product: Optional[dict] = None,
    remaining: Optional[list] = None,
    step_offset: int = 0,
    special_lot_step_lookup: Optional[dict] = None,
) -> Optional[float]:
    """按链内每条 Q-time 规则反推该链允许的最大步间等待（分钟），取所有规则的最小值。

    借鉴 scheduler_before1 的 MIN_CHAIN_WAIT 动态压缩：
    对链内被某条 Q-time 规则 start→end 覆盖的区间，用
        max_gap = (max_duration - 中间步骤有效 CT) / 间隙数
    反推该规则允许的最大步间等待；取所有规则的最小值作为整链步间等待，再与
    CHAIN_WAIT_SAFETY 夹紧。这样紧链自动压紧、保证不超 Q，比"固定分摊 min_qtime"
    更贴合真实中间 CT。

    chain_info: _build_qtime_chains 产出的链信息。
    remaining: 当前 lot 的 remaining_steps（含 step_index 前的历史步骤，形参为完整列表）。
    step_offset: 链首 step 在 remaining 中的下标（链首不一定是 step_index）。
    返回单步间隙分钟数；数据不足时返回 None（由调用方回退到固定逻辑）。
    """
    if not chain_info or ct_lookup is None:
        return None
    chain_step_names = list(chain_info.get("chain_steps") or [])
    if len(chain_step_names) <= 1:
        return None
    # 构造 step_name → 链内下标与 CT
    chain_step_idx = {sn: i for i, sn in enumerate(chain_step_names)}
    chain_cts: list[float] = []
    for _i, _sn in enumerate(chain_step_names):
        _ct = 0.0
        if remaining and (step_offset + _i) < len(remaining):
            _s = remaining[step_offset + _i]
        else:
            # 回退：用 qtime 里的同名 step（性能可接受，当前链很短）
            _s = None
        if _s is not None:
            _ct = get_step_ct(ct_lookup, _s.product_name, _s.step_number, lot.qty if lot else 1)
            if special_lot_step_lookup and lot is not None:
                _sk = (lot.lot_name, _sn)
                if _sk in special_lot_step_lookup and special_lot_step_lookup[_sk].special_ct is not None:
                    _ct = special_lot_step_lookup[_sk].special_ct
        chain_cts.append(_ct)

    # 链上命中的 Q-time 规则（按 step_name 匹配，不依赖 product 字段即可复用）
    rules = []
    for _prod, _qs in (qtime_by_product or {}).items():
        if not _qs:
            continue
        for _q in _qs:
            if _q.start_step in chain_step_idx and _q.end_step in chain_step_idx:
                rules.append(_q)
    if not rules and remaining is None:
        # 无规则也无步序可用时无法反推
        return None

    per_gap_list = []
    chain_feasible = True
    for _q in rules:
        qs = chain_step_idx.get(_q.start_step)
        qe = chain_step_idx.get(_q.end_step)
        if qs is None or qe is None or qe < qs:
            continue
        D = _q.max_duration
        if D is None:
            continue
        sm = (_q.start_mod or "track in").strip()
        em = (_q.end_mod or "track out").strip()
        eff_start = qs if sm == "track in" else qs + 1
        eff_end = qe if em == "track out" else qe - 1
        if eff_start <= eff_end:
            extra_gaps = (1 if sm == "track out" else 0) + (1 if em == "track in" else 0)
            n_gaps = (eff_end - eff_start) + extra_gaps
            intermediate_ct = sum(chain_cts[eff_start:eff_end + 1])
            if n_gaps > 0:
                max_gap_for_q = (D - intermediate_ct) / n_gaps
                if max_gap_for_q <= 0:
                    chain_feasible = False
                    break
                per_gap_list.append(max_gap_for_q)
            elif intermediate_ct >= D:
                chain_feasible = False
                break
        else:
            # 相邻步骤（qe = qs + 1），只有 1 个间隙；时钟区间取决于起/止 mod：
            #   track out → track in：时钟只覆盖步间等待 → max_gap = D
            #   track in → track out：覆盖两端 CT + 等待 → D - ct_s - ct_e
            #   track in → track in：覆盖起点 CT + 等待 → D - ct_s
            #   track out → track out：覆盖终点 CT + 等待 → D - ct_e
            ct_s = chain_cts[qs]
            ct_e = chain_cts[qe]
            if sm == "track out" and em == "track in":
                max_gap = D
            elif sm == "track in" and em == "track out":
                max_gap = D - ct_s - ct_e
            elif sm == "track in":
                max_gap = D - ct_s
            else:
                max_gap = D - ct_e
            if max_gap > 0:
                per_gap_list.append(max_gap)
            else:
                chain_feasible = False

    if not chain_feasible:
        # 规则证明链本身不可行（中间 CT 已经吃满预算）：给极小间隙强行贴合
        return max(0.0, CHAIN_WAIT_SAFETY * 0.1)
    if not per_gap_list:
        return None
    min_gap = min(per_gap_list)
    # 夹紧到安全下限：避免极端情况下把间隙压成负数/过小（也不忘保留最小物理间隙）
    return max(0.0, min(min_gap, 720.0))


def _effective_chain_wait(
    lot: Lot,
    chain_info: Optional[dict],
    priority_wait_map: Optional[dict[tuple[int, int], int]] = None,
    ct_lookup: Optional[dict] = None,
    qtime_by_product: Optional[dict] = None,
    remaining: Optional[list] = None,
    step_offset: int = 0,
    special_lot_step_lookup: Optional[dict] = None,
) -> float:
    """Q-time 链内相邻步骤之间实际使用的等待时间。

    priority_wait 是"期望"的步间等待，但链内必须满足 Q-time（min_qtime）。
    若 priority_wait 大到会把链拉长到超过 Q-time，则按链内真实 CT + 各 Q-time 规则
    反推允许的最大步间等待（_chain_gap_budget），取较小者，保证链能紧凑排布，
    且在中间步骤 CT 吃紧时自动压紧（借鉴 scheduler_before1 的 MIN_CHAIN_WAIT 动态压缩）。
    """
    priority_wait = float(get_step_wait_time(lot.priority[0], lot.priority[1], priority_wait_map))
    if not chain_info:
        return priority_wait
    budget = _chain_gap_budget(
        chain_info, ct_lookup, lot, qtime_by_product, remaining,
        step_offset, special_lot_step_lookup)
    if budget is not None:
        return min(priority_wait, budget)
    # 回退到固定分摊
    min_qtime = chain_info.get("min_qtime")
    chain_steps = chain_info.get("chain_steps", [])
    n = len(chain_steps)
    if min_qtime is None or n <= 1:
        return priority_wait
    per_gap = max(0.0, (min_qtime - CHAIN_WAIT_SAFETY) / (n - 1))
    return min(priority_wait, per_gap)


# ============================================================
# 约束展开函数
# ============================================================

def _resolve_date_or_week(
    date_str: str, week: int,
    schedule_start: datetime, schedule_end: datetime,
) -> list[datetime]:
    target_dates: list[datetime] = []
    if date_str and date_str == "-1":
        d = schedule_start.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = schedule_end.replace(hour=0, minute=0, second=0, microsecond=0)
        while d <= end_day:
            target_dates.append(d)
            d += timedelta(days=1)
    elif date_str:
        try:
            specific_date = datetime.strptime(date_str, "%Y/%m/%d")
            target_dates.append(specific_date)
        except ValueError:
            pass
    if not target_dates and week and 1 <= week <= 7:
        target_weekday = week - 1
        d = schedule_start.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = schedule_end.replace(hour=0, minute=0, second=0, microsecond=0)
        while d <= end_day:
            if d.weekday() == target_weekday:
                target_dates.append(d)
            d += timedelta(days=1)
    return target_dates


def _expand_time_windows(
    windows: list[StepTimeWindow],
    schedule_start: datetime,
    schedule_end: Optional[datetime] = None,
) -> dict[str, list[tuple[datetime, datetime]]]:
    if schedule_end is None:
        schedule_end = schedule_start + timedelta(days=SCHEDULE_WINDOW_DAYS)

    def _impl() -> dict[str, list[tuple[datetime, datetime]]]:
        result: dict[str, list[tuple[datetime, datetime]]] = {}
        for w in windows:
            if not w.date_str and not w.week:
                continue
            try:
                sh, sm = map(int, w.start_time_str.split(":"))
                eh, em = map(int, w.end_time_str.split(":"))
            except (ValueError, AttributeError):
                continue
            target_dates = _resolve_date_or_week(w.date_str, w.week, schedule_start, schedule_end)
            for d in target_dates:
                start_dt = d.replace(hour=sh, minute=sm, second=0, microsecond=0)
                end_dt = d.replace(hour=eh, minute=em, second=0, microsecond=0)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                result.setdefault(w.step_name, []).append((start_dt, end_dt))
        for ivals in result.values():
            ivals.sort(key=lambda x: x[0])
        return result

    return _expand_cache_get("time_windows", windows, schedule_start, schedule_end, _impl)


def _expand_end_time_windows(
    windows: list[StepTimeWindow],
    schedule_start: datetime,
    schedule_end: Optional[datetime] = None,
) -> dict[str, list[tuple[datetime, datetime]]]:
    if schedule_end is None:
        schedule_end = schedule_start + timedelta(days=SCHEDULE_WINDOW_DAYS)

    def _impl() -> dict[str, list[tuple[datetime, datetime]]]:
        result: dict[str, list[tuple[datetime, datetime]]] = {}
        for w in windows:
            if not w.end_start_time_str or not w.end_end_time_str:
                continue
            if not w.end_date_str and not w.end_week:
                continue
            try:
                sh, sm = map(int, w.end_start_time_str.split(":"))
                eh, em = map(int, w.end_end_time_str.split(":"))
            except (ValueError, AttributeError):
                continue
            target_dates = _resolve_date_or_week(w.end_date_str, w.end_week, schedule_start, schedule_end)
            for d in target_dates:
                start_dt = d.replace(hour=sh, minute=sm, second=0, microsecond=0)
                end_dt = d.replace(hour=eh, minute=em, second=0, microsecond=0)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                result.setdefault(w.step_name, []).append((start_dt, end_dt))
        for ivals in result.values():
            ivals.sort(key=lambda x: x[0])
        return result

    return _expand_cache_get("end_windows", windows, schedule_start, schedule_end, _impl)


def _expand_shift_change_times(
    windows: list[ShiftChangeTime],
    schedule_start: datetime,
    schedule_end: Optional[datetime] = None,
) -> list[tuple[datetime, datetime]]:
    if schedule_end is None:
        schedule_end = schedule_start + timedelta(days=SCHEDULE_WINDOW_DAYS)

    def _impl() -> list[tuple[datetime, datetime]]:
        result: list[tuple[datetime, datetime]] = []
        for w in windows:
            if not w.date_str and not w.week:
                continue
            try:
                sh, sm = map(int, w.start_time_str.split(":"))
                eh, em = map(int, w.end_time_str.split(":"))
            except (ValueError, AttributeError):
                continue
            target_dates = _resolve_date_or_week(w.date_str, w.week, schedule_start, schedule_end)
            for d in target_dates:
                start_dt = d.replace(hour=sh, minute=sm, second=0, microsecond=0)
                end_dt = d.replace(hour=eh, minute=em, second=0, microsecond=0)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                result.append((start_dt, end_dt))
        result.sort(key=lambda x: x[0])
        return result

    return _expand_cache_get("shift_change", windows, schedule_start, schedule_end, _impl)


def _expand_eqp_constraints(
    constraints: list[EqpConstraint],
    schedule_start: datetime,
    schedule_end: Optional[datetime] = None,
) -> dict[str, list[tuple[datetime, datetime]]]:
    if schedule_end is None:
        schedule_end = schedule_start + timedelta(days=SCHEDULE_WINDOW_DAYS)

    def _impl() -> dict[str, list[tuple[datetime, datetime]]]:
        result: dict[str, list[tuple[datetime, datetime]]] = {}
        for c in constraints:
            if not c.date_str and not c.week:
                continue
            try:
                sh, sm = map(int, c.start_time_str.split(":"))
                eh, em = map(int, c.end_time_str.split(":"))
            except (ValueError, AttributeError):
                continue
            target_dates = _resolve_date_or_week(c.date_str, c.week, schedule_start, schedule_end)
            for d in target_dates:
                start_dt = d.replace(hour=sh, minute=sm, second=0, microsecond=0)
                end_dt = d.replace(hour=eh, minute=em, second=0, microsecond=0)
                if end_dt <= start_dt:
                    end_dt += timedelta(days=1)
                result.setdefault(c.eqp_name, []).append((start_dt, end_dt))
        return result

    return _expand_cache_get("eqp", constraints, schedule_start, schedule_end, _impl)


# ============================================================
# 步骤记录与约束释放
# ============================================================

def _record_step_entry(
    lot: Lot, step: FlowStep, used_eqp: str,
    start_time: datetime, end_time: datetime,
    ct: float, qtime_risk: str,
    lot_entries: list, eqp_entries: list,
) -> None:
    priority_str = f"{lot.priority[0]}-{lot.priority[1]}"
    lot_entries.append(ScheduleEntry(
        lot_name=lot.lot_name, priority=priority_str,
        product_name=lot.product_name, step_number=step.step_number,
        step_name=step.step_name, eqp_id=used_eqp,
        start_time=start_time, end_time=end_time, ct=ct,
        qtime_risk=qtime_risk, stage_name=step.stage_name))
    if used_eqp != "-":
        eqp_entries.append(EqpScheduleEntry(
            eqp_id=used_eqp, start_time=start_time, end_time=end_time,
            lot_name=lot.lot_name, step_name=step.step_name, qty=lot.qty))


def _release_refs_for_step(
    lot_name: str, step_name: str, end_time: datetime,
    reference_deps: dict, lot_state: dict,
    pending_refs: dict, ref_release_times: dict,
    shift_times: list,
) -> None:
    ref_key = (lot_name, step_name)
    if ref_key not in reference_deps:
        return
    for dep_lot_name in reference_deps[ref_key]:
        dep_state = lot_state.get(dep_lot_name)
        if dep_state is None:
            continue
        for ref in dep_state["lot"].references:
            if ref.reference_lot == lot_name and (ref.reference_step or "") == step_name:
                if ref.start_mod and ref.start_mod not in ("", "shift", "shift_day"):
                    try:
                        release_time = end_time + timedelta(hours=float(ref.start_mod))
                    except ValueError:
                        release_time = end_time
                elif ref.start_mod == "shift":
                    release_time = _next_shift_after(end_time, shift_times)
                elif ref.start_mod == "shift_day":
                    release_time = _next_morning_shift(end_time, shift_times)
                else:
                    release_time = end_time
                ref_release_times[dep_lot_name][ref_key] = release_time
                pending_refs[dep_lot_name].discard(ref_key)
                _update_blocked_ready_time(dep_lot_name, dep_state, pending_refs, ref_release_times)
                break
    del reference_deps[ref_key]


def _update_blocked_ready_time(
    dep_lot_name: str, dep_state: dict,
    pending_refs: dict, ref_release_times: dict,
) -> None:
    if not dep_state.get("refs_registered"):
        return
    ref_block_info = dep_state.get("ref_block_info", {})
    step_idx = dep_state["step_index"]
    pending = pending_refs.get(dep_lot_name, set())
    refs = ref_release_times.get(dep_lot_name, {})
    blocking_release_times = []
    blocked_by_pending = False
    for ref_key, block_idx in ref_block_info.items():
        if block_idx is not None and step_idx >= block_idx:
            if ref_key in refs:
                blocking_release_times.append(refs[ref_key])
            elif ref_key in pending:
                blocked_by_pending = True
                break
    if blocked_by_pending:
        dep_state["ready_time"] = FAR_FUTURE
    elif blocking_release_times:
        dep_state["ready_time"] = max(
            dep_state.get("_base_ready_time", dep_state["ready_time"]),
            max(blocking_release_times))


# ============================================================
# 手动调整与 Q-time 检查
# ============================================================

def _apply_manual_adjust(
    lot_name: str, step_name: str,
    start_time: datetime, end_time: datetime, ct: float,
    manual_adjust_lookup: dict,
    pin_lookup: dict = None,
    reapply: bool = False,
) -> tuple[datetime, datetime]:
    """应用手动调整约束。

    - manual_adjust_lookup: mode="delay"（不早于）语义
    - pin_lookup: mode="pin"（精确锁定）语义
      - 首次应用（reapply=False）：start_time 被精确设置为 pin_time
      - 约束解决后重应用（reapply=True）：仅保证不早于 pin_time（设备被占时允许顺延，
        避免把步骤拽回已被占用的时隙造成设备重叠）
    """
    if pin_lookup:
        for key in ((lot_name, step_name), (lot_name, None)):
            if key in pin_lookup:
                pin_time = pin_lookup[key]
                # 精确锁定：以 pin_time 为锚。贪心最早槽位早于 pin_time 时拉到 pin_time；
                # 晚于 pin_time（设备被占/锚点约束）则保持，避免拽回忙碌时隙。
                if reapply:
                    # 约束解决后：只保证不早于 pin_time，绝不拉回（避免设备重叠）
                    if start_time < pin_time:
                        start_time = pin_time
                        end_time = start_time + timedelta(minutes=ct)
                else:
                    if start_time < pin_time:
                        start_time = pin_time
                        end_time = start_time + timedelta(minutes=ct)
                return start_time, end_time
    if not manual_adjust_lookup:
        return start_time, end_time
    step_key = (lot_name, step_name)
    if step_key in manual_adjust_lookup:
        delay_to = manual_adjust_lookup[step_key]
        if delay_to > start_time:
            start_time = delay_to
            end_time = start_time + timedelta(minutes=ct)
    lot_key = (lot_name, None)
    if lot_key in manual_adjust_lookup:
        delay_to = manual_adjust_lookup[lot_key]
        if delay_to > start_time:
            start_time = delay_to
            end_time = start_time + timedelta(minutes=ct)
    return start_time, end_time


def _check_qtime_start(
    state: dict, step_name: str, product_qtimes: list,
    start_time: datetime, end_time: datetime,
) -> None:
    for q in product_qtimes:
        if q.start_step != step_name:
            continue
        qkey = (q.start_step, q.end_step)
        if qkey in state["qtime_tracker"]:
            continue
        start_mod = (q.start_mod or "track in").strip()
        if start_mod == "track in":
            qtime_start = start_time
        else:
            qtime_start = end_time
        state["qtime_tracker"][qkey] = {
            "start_step": q.start_step,
            "end_step": q.end_step,
            "start_mod": start_mod,
            "end_mod": (q.end_mod or "track out").strip(),
            "max_duration": q.max_duration,
            "qtime_start": qtime_start,
            "deadline": qtime_start + timedelta(minutes=q.max_duration),
        }


def _check_qtime_end_for_step(
    state: dict, step_name: str, product_qtimes: list,
    start_time: datetime, end_time: datetime,
    lot_name: str, qtime_alerts: list,
) -> str:
    qtime_risk = "-"
    ended_qtimes = []
    for qkey, tracker in list(state["qtime_tracker"].items()):
        if tracker["end_step"] != step_name or tracker["deadline"] is None:
            continue
        end_mod = tracker.get("end_mod", "track out")
        actual_end = start_time if end_mod == "track in" else end_time
        delta = (actual_end - tracker["deadline"]).total_seconds() / 60.0
        if delta > 0:
            qtime_risk = f"RISK: 超时{int(delta)}min"
            qtime_alerts.append(QTimeAlert(
                lot_name=lot_name,
                qtime_rule=f"{tracker['start_step']}→{step_name}",
                start_time=tracker["deadline"] - timedelta(minutes=tracker["max_duration"]),
                deadline=tracker["deadline"], actual_end=actual_end,
                over_minutes=int(delta), status="超时"))
        else:
            qtime_risk = "OK"
            qtime_alerts.append(QTimeAlert(
                lot_name=lot_name,
                qtime_rule=f"{tracker['start_step']}→{step_name}",
                start_time=tracker["deadline"] - timedelta(minutes=tracker["max_duration"]),
                deadline=tracker["deadline"], actual_end=actual_end,
                over_minutes=0, status="OK"))
        ended_qtimes.append(qkey)
    for qkey in ended_qtimes:
        if qkey in state["qtime_tracker"]:
            del state["qtime_tracker"][qkey]
    return qtime_risk


# ============================================================
# Q-time 前瞻：紧窗口起点延迟
# ============================================================

def _cross_shift_push_target(
    s_start: datetime,
    e_slot: datetime,
    shift_change_intervals: list,
) -> Optional[datetime]:
    """紧 Q 链不跨班次（用户规则）：S 若落在 E 所在班次的"紧邻前一个班次"内
    （即 S→E 的窗口跨过班次切换时刻、且两步骤同日相邻班次），返回应把 S 起点
    推后的目标时刻 = E 所在班次的开始（跳过交接班禁用窗，链首不落在交接班时刻）；
    否则返回 None（S 与 E 已同班次，或 S 距 E 超过一个班次——跨日/多日链不适用
    本规则，避免把链整体推迟一整天）。

    注意：返回的目标是"班次起点"，处于交接班禁用窗（如 08:30-09:30）之内时，
    调用方在 _resolve_constraints/_skip_shift_change 中会把起点继续推到窗后
    （09:30/21:30），保证链首不在交接班时刻开工。
    """
    if not shift_change_intervals:
        return None
    e_shift_start = None
    for _ws, _wse in shift_change_intervals:
        if _ws <= e_slot:
            e_shift_start = _ws  # e_slot 之前最近（最后）的班次边界 = E 所在班次开始
    if e_shift_start is None:
        return None
    if s_start < e_shift_start and (e_shift_start - s_start) <= timedelta(hours=12):
        return e_shift_start
    return None


def _q_target_margin(q) -> float:
    """紧 Q 规则的余量目标（分钟）：安全余量 = max(预算×%, 下限)。
    松链（预算 > 紧链阈值）不设余量目标（余量由自然排布决定）。
    """
    D = q.max_duration
    if D is None or D > TIGHT_CHAIN_THRESHOLD:
        return 0.0
    return max(D * QTIGHT_SAFETY_MARGIN / 100.0, QTIGHT_MIN_MARGIN)


def _tight_qtime_target_start(
    lot: Lot,
    state: dict,
    step: FlowStep,
    ct: float,
    product_qtimes: list,
    ready_time: datetime,
    s_start: datetime,
    machine_available: dict,
    machine_intervals: dict,
    pending_refs: dict,
    ref_release_times: dict,
    special_lot_step_lookup: dict,
    ct_lookup: dict,
    shift_change_intervals: list = None,
    step_windows: dict = None,
    end_windows: dict = None,
    priority_wait_map: dict = None,
    ref_release_forecast: dict = None,
    manual_adjust_lookup: dict = None,
    pin_lookup: dict = None,
) -> object:
    """计算当前 step（作为某 Q-time 起点）应被延迟到的目标开始时间（最晚可行）。

    关键点：Q-time 的端步骤 E 最终只能落在其"最早可行时刻"（考虑设备占用、
    换班窗口、时间窗、已释放的 reference release 时间）。若按当前自然开始时间调度
    起点 S，E 会被推过后导致 Q-time 超限（余量不足），则应把 S 推迟，
    使 E 落在其可行时段内并保留一定余量。

    E 的可行时刻按"链式相邻"估算：S 在 s_start 开始 → E 就绪时间 =
    s_start + ct + step_wait + 中间步骤(ct+wait)。再从该就绪时间找 E 的
    最早设备槽并解析换班/时间窗约束，得到 E 的真实可行开始。

    返回:
      - datetime: 延迟到的目标开始时间
      - None:     该 step 不是（需要前看的）Q-time 起点，无需延迟
      - ("DEFER", keys): 端步骤 reference 尚未释放，暂不能调度
    """
    target_time = None
    rem = state["remaining_steps"]
    si = state["step_index"]
    for q in product_qtimes or []:
        if q.start_step != step.step_name:
            continue
        D = q.max_duration
        if D is None:
            continue
        end_step = q.end_step
        e_idx = None
        for i in range(si, len(rem)):
            if rem[i].step_name == end_step:
                e_idx = i
                break
        if e_idx is None or e_idx <= si:
            continue
        end_flow = rem[e_idx]
        is_tight = D <= TIGHT_CHAIN_THRESHOLD

        # ---- 估算 E 的自然就绪时间（S 在 s_start 开始，沿链推进到 E） ----
        # 中间步骤也受设备占用/换班窗约束：若只按 CT+等待推进，会把 E 的可行开始
        # 估早，导致 S 延迟不足、E 实际落点靠后、余量被侵蚀到极限（用户反馈根因）。
        # 这里逐步用 _find_earliest_slot 模拟中间步骤的真实槽位（与其单步调度一致）。
        e_ready = s_start + timedelta(minutes=max(ct, 0))
        for i in range(si + 1, e_idx):
            mid_ct = get_step_ct(ct_lookup, rem[i].product_name, rem[i].step_number, lot.qty)
            if special_lot_step_lookup:
                sls_key = (lot.lot_name, rem[i].step_name)
                if sls_key in special_lot_step_lookup:
                    sls = special_lot_step_lookup[sls_key]
                    if sls.special_ct is not None:
                        mid_ct = sls.special_ct
            _mid_eqps = list(rem[i].eqp_ids) if rem[i].eqp_ids else ["-"]
            if _mid_eqps != ["-"] and machine_intervals:
                _mid_free = datetime.max
                for _meid in _mid_eqps:
                    _cand = _find_earliest_slot(
                        machine_intervals.get(_meid, []), e_ready, timedelta(minutes=mid_ct))
                    if _cand < _mid_free:
                        _mid_free = _cand
                if _mid_free != datetime.max and _mid_free > e_ready:
                    e_ready = _mid_free
            e_ready += timedelta(minutes=mid_ct + get_step_wait_time(
                lot.priority[0], lot.priority[1], priority_wait_map))

        # ---- E 的 reference 阻塞检查 ----
        defer_hold: set = set()
        ref_boost = None
        if state.get("refs_registered"):
            pending = pending_refs.get(lot.lot_name, set())
            rel = ref_release_times.get(lot.lot_name, {})
            for ref_key, block_idx in state.get("ref_block_info", {}).items():
                if block_idx is not None and e_idx >= block_idx:
                    if ref_key in rel:
                        if ref_boost is None or rel[ref_key] > ref_boost:
                            ref_boost = rel[ref_key]
                    elif ref_key in pending:
                        if ref_release_forecast and ref_key in ref_release_forecast:
                            # 未释放但第一遍已预测释放时刻：用预测锚点
                            if ref_boost is None or ref_release_forecast[ref_key] > ref_boost:
                                ref_boost = ref_release_forecast[ref_key]
                        else:
                            defer_hold.add(ref_key)
        # 端步骤 reference 未释放：本链末步骤（E）的粗排程锚点已含该引用跨 lot
        # 释放预测（含上游手动延迟级联）。取该锚点作为 E 最早可行开始的下界，
        # 反推链首起点——即使松链（窗口宽裕）也禁止把链首过早排开，
        # 否则端步骤被上游 reference 拉开，整条 Q-time 区间被拆散后单步各自锚定。
        # 这正是"跨 lot 有约束的链必须作为一个整体、不能拆散后单步调度"。
        ca_list = state.get("coarse_anchors") or []
        # 粗排程第 3 步的整链紧凑传播只对紧链生效，松链链首仍可能过早 → 这里用
        # 端步骤锚点与 Q-time 预算统一反推，作为链首目标时间的下界。
        _ca_end = None
        if state.get("refs_registered") and defer_hold:
            if e_idx < len(ca_list) and ca_list[e_idx] is not None:
                _ca_end = ca_list[e_idx]
        if defer_hold:
            if _ca_end is None:
                if is_tight:
                    return ("DEFER", defer_hold)
                # 松链但无端步骤锚点可依：端步骤时间未知，不影响起点先排，
                # 真实时间后续由 _update_blocked_ready_time 推进时保证
                continue
            # 有端步骤锚点：用它反推链首（在下方 e_base 分支并入 ref_boost）
            if ref_boost is None or _ca_end > ref_boost:
                ref_boost = _ca_end
        # 已释放 reference：E 至少等 ref_boost（还叠加链相邻的 e_ready）
        e_base = e_ready
        if ref_boost is not None and ref_boost > e_base:
            e_base = ref_boost
        # E 的手动约束（pin/delay 是 E 的最早开始下界，不影响 S 之前的步骤）：
        # 若不并入，前瞻会把 E 的可行开始估早（如 pin 把 DISPENSE 钉到 22:00，
        # 实际被设备推到次日 09:30），导致安全站点 S（如 PLASMA）延迟不足、
        # S→E 超 Q（test_pin_linkage pin PC2.UF-DISPENSE 场景根因）。
        if manual_adjust_lookup:
            _ma_e = manual_adjust_lookup.get((lot.lot_name, end_flow.step_name))
            if _ma_e is not None and _ma_e > e_base:
                e_base = _ma_e
        if pin_lookup:
            _pn_e = pin_lookup.get((lot.lot_name, end_flow.step_name))
            if _pn_e is not None and _pn_e > e_base:
                e_base = _pn_e

        # ---- E 的设备最早可用时间 + 约束解析 ----
        e_ct = get_step_ct(ct_lookup, end_flow.product_name, end_flow.step_number, lot.qty)
        if special_lot_step_lookup:
            sls_key = (lot.lot_name, end_flow.step_name)
            if sls_key in special_lot_step_lookup:
                sls = special_lot_step_lookup[sls_key]
                if sls.special_ct is not None:
                    e_ct = sls.special_ct
        e_eqp_ids = list(end_flow.eqp_ids) if end_flow.eqp_ids else ["-"]
        best_eid = None
        e_earliest = datetime.max
        for eid in e_eqp_ids:
            if eid == "-":
                cand = e_base
            else:
                cand = _find_earliest_slot(
                    machine_intervals.get(eid, []),
                    e_base,
                    timedelta(minutes=e_ct))
            if cand < e_earliest:
                e_earliest = cand
                best_eid = eid
        if e_earliest == datetime.max:
            continue

        # ---- 嵌套下游链约束：E 若本身又是下一 Q-time 链起点（如 DISPENSE→CURE），
        # 还受其下游端步骤设备约束。递归算出 E 真正可行的最早开始时刻，
        # 否则会误以为 E 能落在资源空闲的较早时刻，导致链首 S 延迟不足、整体超链。 ----
        def _nested_req_start(fs, base_t, _seen=None):
            """计算 fs 步骤真正可行的最早开始时刻。
            递归向下游传播：fs 必须不早于其自身设备最早可用时刻，且其所有下游
            端步骤（含再下游，即 fs 为起点到更远链末端的设备占用）的最早可行开始
            必须落在 Q-time 界内，反推 fs 的下界。这样当瓶颈在链末端设备时
            （如 DISPENSE 之后 CURE 满负荷），fs 也会被推后，链首延迟才足量。"""
            if _seen is None:
                _seen = set()
            if fs.step_name in _seen:
                return base_t
            _seen = _seen | {fs.step_name}
            fs_ct = get_step_ct(ct_lookup, fs.product_name, fs.step_number, lot.qty)
            if special_lot_step_lookup:
                _sk = (lot.lot_name, fs.step_name)
                if _sk in special_lot_step_lookup and special_lot_step_lookup[_sk].special_ct is not None:
                    fs_ct = special_lot_step_lookup[_sk].special_ct
            req = base_t
            # fs 自身设备的真实最早可用（否则更早开始根本不可行）——这是下游设备
            # 短板传播的基石：链末端的设备短板会沿 Q-time 界逐级向上推断到链首。
            fs_eqp = list(fs.eqp_ids) if fs.eqp_ids else ["-"]
            if fs_eqp != ["-"]:
                fs_free = datetime.max
                for _eid in fs_eqp:
                    _cand = _find_earliest_slot(
                        machine_intervals.get(_eid, []), base_t, timedelta(minutes=fs_ct))
                    if _cand < fs_free:
                        fs_free = _cand
                if fs_free != datetime.max and fs_free > req:
                    req = fs_free
            for qn in product_qtimes or []:
                if qn.start_step != fs.step_name:
                    continue
                e2 = None
                for ii in range(e_idx, len(rem)):
                    if rem[ii].step_name == qn.end_step:
                        e2 = rem[ii]
                        break
                if e2 is None:
                    continue
                dn = qn.max_duration
                if dn is None:
                    continue
                e2_ct = get_step_ct(ct_lookup, e2.product_name, e2.step_number, lot.qty)
                if special_lot_step_lookup:
                    _sk2 = (lot.lot_name, e2.step_name)
                    if _sk2 in special_lot_step_lookup and special_lot_step_lookup[_sk2].special_ct is not None:
                        e2_ct = special_lot_step_lookup[_sk2].special_ct
                # e2 的最早可行开始（其自身设备 + 再下游）
                e2_req = _nested_req_start(e2, base_t, _seen)
                em2 = (qn.end_mod or "track out").strip()
                e2_point = e2_req + (timedelta(minutes=e2_ct) if em2 != "track in" else timedelta(0))
                sm2 = (qn.start_mod or "track in").strip()
                off = e2_point - timedelta(minutes=dn)
                req_fs = off - timedelta(minutes=fs_ct) if sm2 == "track out" else off
                if req_fs > req:
                    req = req_fs
            return req

        # 解析 E 真实可行时刻（设备不可用/换班/时间窗）
        e_resolved = _resolve_constraints(
            e_earliest, e_ct, best_eid,
            machine_intervals, shift_change_intervals or [],
            step_windows or {}, end_windows or {},
            end_flow.step_name, max_iterations=10)
        if e_resolved == datetime.max:
            continue
        # E 还要不晚于（且不早于）其下游嵌套链设备允许的最早开始
        _down_req = _nested_req_start(end_flow, e_base)
        if _down_req > e_resolved:
            # _nested_req_start 用 _find_earliest_slot 估算，只看占用不看设备不可用窗
            #（如 PKUFD 22:00-08:30）。若直接采纳，E 可能落在不可用窗内，导致链首
            # 延迟不足、紧 Q-time 最终破裂。这里必须重新对设备约束做解析，把 E 推入
            # 真正可行的最新槽位，从而把约束正确传回链首。
            e_resolved = _resolve_constraints(
                _down_req, e_ct, best_eid,
                machine_intervals, shift_change_intervals or [],
                step_windows or {}, end_windows or {},
                end_flow.step_name, max_iterations=10)

        end_mod = (q.end_mod or "track out").strip()
        if end_mod == "track in":
            e_slot = e_resolved          # deadline 落在 E 开始
        else:
            e_slot = e_resolved + timedelta(minutes=e_ct)  # deadline 落在 E 结束

        start_mod = (q.start_mod or "track in").strip()
        # 距 S_end 的偏移（start 模式决定 deadline 的参考点）
        if start_mod == "track out":
            tgt = e_slot - timedelta(minutes=D) - timedelta(minutes=max(ct, 0))
        else:
            tgt = e_slot - timedelta(minutes=D)
        # 安全余量仅在紧链保留（松链保留余量易把起点推进设备不可用段）。
        # 实际余量 = max(预算 D 的百分比, 绝对下限)：短段也能保住最小缓冲。
        margin = (max(D * QTIGHT_SAFETY_MARGIN / 100.0, QTIGHT_MIN_MARGIN)
                  if is_tight else 0)
        tgt = tgt + timedelta(minutes=margin)
        # ---- 紧 Q 链不跨班次（用户规则）：Q 紧时链内相邻步骤不跨班次切换 ----
        # 用"生效起点"评估：max(自然起点, Q 目标)——Q 目标本身可能已把 S 推到 E 当日，
        # 此时再判断 S 是否落在 E 的紧邻前一个班次内。推后只缩短 S→E 窗口（Q 仍满足）；
        # 仅当不撑破 S 上游的紧链、且 S 的设备在班次边界时刻可用时生效（best-effort，
        # 否则保留原排程并输出跨班次告警）。设备不可用时跳过：避免多条链同时抢同一
        # 班次起点造成级联推挤（PEDES 2 台、4 条 PLASMA 链同抢 08:30 的实测根因）。
        if is_tight and CROSS_SHIFT_AVOID and shift_change_intervals:
            _eff_start = tgt if tgt > s_start else s_start
            _st = _cross_shift_push_target(_eff_start, e_slot, shift_change_intervals)
            if _st is not None:
                # 设备可用守卫（用户规则"整体后移"：不要求恰好卡在班次起点）：
                # 交接班禁用窗内设备不可开工，推后目标 _st 会在后续 _resolve_constraints
                # 中跳到窗后（09:30/21:30）。此处只需 S 的设备在"目标班次内"有可用槽
                # （早于下一班次边界），推后即有效——避免因设备在班次起点时刻被占就
                # 放弃整体后移、让紧链仍跨班次（用户反馈根因）。
                if step.eqp_ids:
                    _free_at_bnd = datetime.max
                    for _eid in step.eqp_ids:
                        _c = _find_earliest_slot(
                            machine_intervals.get(_eid, []), _st, timedelta(minutes=ct))
                        if _c < _free_at_bnd:
                            _free_at_bnd = _c
                    _next_bnd = None
                    for _ws2, _wse2 in shift_change_intervals:
                        if _ws2 > _st:
                            _next_bnd = _ws2
                            break
                    if (_free_at_bnd == datetime.max
                            or (_next_bnd is not None and _free_at_bnd >= _next_bnd)):
                        _st = None     # 班次内无可用槽：推后无意义，保留跨班次告警
                # 全程窗口保护：S 推后会把 E（及以 E 为终点的 Q 窗口终点）一起推后最多
                # delay。用 ready_time−步间等待 作为"上游锚点"估算各以 E 为终点的
                # Q 窗口当前占用：占用 + delay > 预算则放弃推后（避免把本就在
                # 临界/紧贴 1440 的宽松链推出窗外——手动调整/pin 场景实测根因）。
                # 跳过 S→E 自身规则（推后只缩短该窗口，不参与保护）。
                if _st is not None and ready_time is not None:
                    _delay = (_st - _eff_start)
                    if _delay > timedelta(0):
                        _win_start_ref = ready_time - timedelta(
                            minutes=get_step_wait_time(lot.priority[0], lot.priority[1],
                                                       priority_wait_map))
                        for _q3 in product_qtimes or []:
                            _D3 = _q3.max_duration
                            if (_q3.end_step != end_flow.step_name or not _D3
                                    or _q3.start_step == step.step_name):
                                continue
                            _usage = (e_slot - _win_start_ref).total_seconds() / 60.0
                            if _usage + _delay.total_seconds() / 60.0 > _D3:
                                _st = None
                                break
                for _q2 in product_qtimes or []:
                    _D2 = _q2.max_duration
                    # _st 可能被上方 951-965 的窗口保护置 None：须先判空再取差
                    if (_st is not None and _q2.end_step == step.step_name and _D2
                            and _D2 <= TIGHT_CHAIN_THRESHOLD
                            and (_st - ready_time).total_seconds() / 60.0 > _D2):
                        _st = None      # 推后会撑破上游紧链 → 放弃（保留跨班次告警）
                        break
                # E 可行性守卫：S 推后到 _st 后，E 的最早可行时刻（设备停机窗/
                # 占用/换班）必须仍落在 S.end + D 的 Q-time 预算内。若推后使 E
                # 只能落在预算之外（如 DISPENSE 被 22:00 停机窗推到次日 08:30、
                # S 又被推后到 21:30 → 预算被撑破），放弃推后（保留原排程+告警）——
                # 用户规则"尽量最早开始，约束不满足时整体后移"，而非把链首推后
                # 却把端步骤独自甩进停机窗。
                if _st is not None and best_eid is not None and best_eid != "-":
                    # 先用与主循环相同的约束解析（换班/设备窗）得到 S 的真实开始
                    _s_st_resolved = _st
                    if step.eqp_ids:
                        _s_st_resolved = _resolve_constraints(
                            _st, ct, step.eqp_ids[0], machine_intervals,
                            shift_change_intervals or [], step_windows or {}, end_windows or {},
                            step.step_name, max_iterations=10)
                    if _s_st_resolved != datetime.max:
                        _s_end_pushed = _s_st_resolved + timedelta(minutes=max(ct, 0))
                        _e_ready_pushed = _s_end_pushed + timedelta(minutes=get_step_wait_time(
                            lot.priority[0], lot.priority[1], priority_wait_map))
                        _e_after = _find_earliest_slot(
                            machine_intervals.get(best_eid, []), _e_ready_pushed,
                            timedelta(minutes=e_ct))
                        if _e_after != datetime.max:
                            _e_after = _resolve_constraints(
                                _e_after, e_ct, best_eid, machine_intervals,
                                shift_change_intervals or [], step_windows or {}, end_windows or {},
                                end_flow.step_name, max_iterations=10)
                        _deadline_pt = _s_end_pushed + timedelta(minutes=D)
                        _e_point = (_e_after + timedelta(minutes=e_ct)
                                    if end_mod == "track out" else _e_after)
                        if _e_after == datetime.max or _e_point > _deadline_pt:
                            _st = None
                if _st is not None and _st > tgt:
                    tgt = _st
        if tgt > s_start and (target_time is None or tgt > target_time):
            target_time = tgt

    return target_time


# ============================================================
# 设备可用性检查
# ============================================================

def _down_fit_span(down: list) -> timedelta:
    """停机窗之间最大可用间隙。

    判定某操作能否"完整落在某个可用时段内"：只要操作时长不超过相邻停机窗之间
    的最大间隙，就应该要求操作完整时长不跨越任何停机窗（设备不可用期间不得作业）；
    否则（长 CT，如 qty=25 的 FC-REFLOW CT≈925min > 每日 08:30-22:00 可用段 810min）
    退化为只约束操作起点不落在窗内，允许跨越继续运行，避免被推到远未来。
    """
    if len(down) < 2:
        return timedelta.max        # 单窗口/无窗口：总有可用时段可以放
    ds = sorted(down)
    best = timedelta(0)
    for i in range(1, len(ds)):
        gap = ds[i][0] - ds[i - 1][1]
        if gap > best:
            best = gap
    return best


def _down_push(
    c: datetime,
    duration: timedelta,
    down: list,
    can_fit: bool,
) -> Optional[datetime]:
    """判断 [c, c+duration) 与停机窗的关系，返回应把起点推到的时刻；None=当前可用。

    - 起点落在窗内：无论长短都推到窗口结束；
    - 完整时长跨越停机窗且操作能完整放入可用时段（can_fit）：推到窗口结束；
    - 长 CT（!can_fit）：只约束起点不落在窗内，允许跨越。
    """
    end = c + duration
    for ws, we in down:
        if end <= ws or c >= we:
            continue                # 不与该窗口相交
        if not can_fit:
            if ws <= c < we:
                return we           # 起点在窗内：推到窗口结束
            continue                # 长 CT：起点合法，允许跨越
        return we                   # 完整时长不得跨越停机窗
    return None


def _find_earliest_slot(
    intervals: list[tuple[Optional[datetime], Optional[datetime]]],
    lot_ready: datetime,
    duration: timedelta,
) -> datetime:
    # 停机窗：默认要求操作完整时长不得跨越（设备不可用期间不作业）；仅当操作
    # 时长超过相邻停机窗最大可用间隙（长 CT 无法在单个可用段内完成）时退化为
    # 只约束起点不落在窗内。普通占用区间要求完整时长不重叠。分开排序处理。
    down = [(iv[0] if iv[0] is not None else datetime.min,
             iv[1] if iv[1] is not None else datetime.max)
            for iv in intervals
            if len(iv) >= 3 and iv[2] == "down"]
    down.sort(key=lambda x: x[0])
    occupied = [(iv[0] if iv[0] is not None else datetime.min,
                 iv[1] if iv[1] is not None else datetime.max)
                for iv in intervals
                if not (len(iv) >= 3 and iv[2] == "down")
                and not (iv[0] is None and iv[1] is None)]
    occupied.sort(key=lambda x: x[0])
    can_fit = duration <= _down_fit_span(down)

    candidate = lot_ready
    ptr = 0            # 单调扫描指针：occupied[0:ptr] 均满足 iv_end <= candidate
    n = len(occupied)
    while True:
        # 完整时长不得与已占用区间重叠（指针只前进，结果与原全量重扫一致）
        end = candidate + duration
        bumped = False
        while ptr < n:
            iv_start, iv_end = occupied[ptr]
            if candidate >= iv_end:
                ptr += 1
                continue
            if end > iv_start:
                candidate = iv_end
                bumped = True
                break
            break       # 后续区间 start 更大，不可能再重叠
        if bumped:
            continue
        nb = _down_push(candidate, duration, down, can_fit)
        if nb is None:
            return candidate
        candidate = nb             # 起点在窗内 / 完整时长跨越停机窗：推到窗口结束
    return candidate


def _find_latest_slot(
    intervals: list[tuple[Optional[datetime], Optional[datetime]]],
    deadline: datetime,
    duration: timedelta,
) -> datetime:
    """在设备占用区间中，找到 deadline 之前最晚的可用时间槽，返回 start_time。
    用于链拆分后反向调度：从 Q-time deadline 往前找最晚可用时间。
    停机窗：默认要求操作完整时长不得跨越（can_fit 时）；长 CT 只约束起点。"""
    down = [(iv[0] if iv[0] is not None else datetime.min,
             iv[1] if iv[1] is not None else datetime.max)
            for iv in intervals
            if len(iv) >= 3 and iv[2] == "down"]
    down.sort(key=lambda x: x[0])
    valid = [(iv[0] if iv[0] is not None else datetime.min,
              iv[1] if iv[1] is not None else datetime.max)
             for iv in intervals
             if not (len(iv) >= 3 and iv[2] == "down")
             and not (iv[0] is None and iv[1] is None)]
    valid.sort(key=lambda x: x[0])
    can_fit = duration <= _down_fit_span(down)
    candidate_end = deadline
    candidate_start = candidate_end - duration
    # 从后往前检查占用区间
    for iv_start, iv_end in reversed(valid):
        if iv_end <= candidate_start:
            break
        if iv_start < candidate_end:
            candidate_end = iv_start
            candidate_start = candidate_end - duration
    # 停机窗：完整时长不得跨越（can_fit 时）；长 CT 只约束起点不落在窗内
    nb = _down_push(candidate_start, duration, down, can_fit)
    if nb is not None and nb > candidate_start:
        candidate_start = nb
        if candidate_start + duration > deadline:
            return datetime.min
    return max(candidate_start, datetime.min)


def _skip_unavailable(
    start: datetime,
    intervals: list[tuple[Optional[datetime], Optional[datetime]]],
    duration: timedelta,
) -> datetime:
    end = start + duration
    down = [iv for iv in intervals if len(iv) >= 3 and iv[2] == "down"]
    can_fit = duration <= _down_fit_span([
        (iv[0] if iv[0] is not None else datetime.min,
         iv[1] if iv[1] is not None else datetime.max)
        for iv in down])
    for interval in intervals:
        # 停机窗：默认要求操作完整时长不得跨越（设备不可用期间不作业）；
        # 仅当长 CT 无法完整放入任一可用时段（can_fit=False）时，退化为只约束
        # "新操作起点"不得落在窗内，允许跨越继续运行（如 qty=25 的 FC-REFLOW
        # CT≈925min 超过每日 08:30-22:00 运行窗 810min——fuzz seed 20260828001 根因）。
        if len(interval) >= 3 and interval[2] == "down":
            iv_start, iv_end = interval[0], interval[1]
            actual_start = iv_start if iv_start is not None else datetime.min
            actual_end = iv_end if iv_end is not None else datetime.max
            if start >= actual_end:
                continue
            if actual_start <= start < actual_end:
                start = actual_end
                end = start + duration
                continue
            if can_fit and end > actual_start:
                # 起点在窗外但完整时长跨越停机窗：推到窗口结束
                start = actual_end
                end = start + duration
            continue
        interval_start, interval_end = interval
        if interval_start is None and interval_end is None:
            continue
        actual_start = interval_start if interval_start is not None else datetime.min
        actual_end = interval_end if interval_end is not None else datetime.max
        if start >= actual_end:
            continue
        if end > actual_start:
            if interval_end is not None:
                start = interval_end
                end = start + duration
            else:
                return datetime.max
    return start


def _skip_shift_change(
    start: datetime,
    shift_change_intervals: list[tuple[datetime, datetime]],
    duration: Optional[timedelta] = None,
) -> datetime:
    if not shift_change_intervals:
        return start
    moved = True
    while moved:
        moved = False
        for ws, we in shift_change_intervals:
            if start >= we:
                continue
            if start >= ws:
                start = we
                moved = True
                break
            break
    return start


def _resolve_constraints(
    start: datetime, ct: float, eqp_id: str,
    machine_intervals: dict[str, list[tuple[Optional[datetime], Optional[datetime]]]],
    shift_change_intervals: list[tuple[datetime, datetime]],
    step_windows: dict[str, list[tuple[datetime, datetime]]],
    end_windows: dict[str, list[tuple[datetime, datetime]]],
    step_name: str,
    max_iterations: int = 10,
) -> datetime:
    duration = timedelta(minutes=ct)
    for _ in range(max_iterations):
        old_start = start
        if eqp_id != "-":
            start = _skip_unavailable(start, machine_intervals.get(eqp_id, []), duration)
            if start == datetime.max:
                return datetime.max
        if shift_change_intervals:
            start = _skip_shift_change(start, shift_change_intervals, duration)
        if step_name in step_windows:
            windows = step_windows[step_name]
            in_window = any(ws <= start < we for ws, we in windows)
            if not in_window:
                for ws, we in windows:
                    if ws > start:
                        start = ws
                        break
        end_time = start + duration
        if step_name in end_windows:
            ew = end_windows[step_name]
            in_end_window = any(ws <= end_time < we for ws, we in ew)
            if not in_end_window:
                for ws, we in ew:
                    if ws > end_time:
                        start = ws - duration
                        break
        if start == old_start:
            break
    return start


# ============================================================
# Q-time 链识别
# ============================================================

def _build_qtime_chains(
    flow_map: dict[str, list[FlowStep]],
    qtimes: list[QTimeConstraint],
) -> dict[str, dict[str, dict]]:
    """为每个 product 构建 Q-time 链信息。
    返回: {product_name: {step_name: {"chain_id": str, "chain_steps": [step_names], "is_chain_start": bool, "is_chain_end": bool, "is_tight": bool, "qtime_deadline_step": str}}}
    """
    result: dict[str, dict[str, dict]] = {}

    for product, steps in flow_map.items():
        product_qtimes = [q for q in qtimes if q.product_name == product]
        if not product_qtimes:
            continue

        step_name_to_idx = {s.step_name: i for i, s in enumerate(steps)}
        step_names = [s.step_name for s in steps]

        # 找出所有 Q-time 覆盖的步骤区间，合并重叠区间
        intervals = []
        for q in product_qtimes:
            if q.start_step in step_name_to_idx and q.end_step in step_name_to_idx:
                start_idx = step_name_to_idx[q.start_step]
                end_idx = step_name_to_idx[q.end_step]
                intervals.append((start_idx, end_idx, q.max_duration))

        if not intervals:
            continue

        # 合并重叠区间
        intervals.sort(key=lambda x: x[0])
        merged = [intervals[0]]
        for iv in intervals[1:]:
            if iv[0] <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], iv[1]),
                              min(merged[-1][2], iv[2]))
            else:
                merged.append(iv)

        product_chains = result.setdefault(product, {})
        for chain_id, (start_idx, end_idx, min_qtime) in enumerate(merged):
            chain_step_names = step_names[start_idx:end_idx + 1]
            is_tight = min_qtime <= TIGHT_CHAIN_THRESHOLD

            for i, sn in enumerate(chain_step_names):
                product_chains[sn] = {
                    "chain_id": f"{product}_chain{chain_id}",
                    "chain_steps": chain_step_names,
                    "chain_start_step": chain_step_names[0],
                    "chain_end_step": chain_step_names[-1],
                    "is_chain_start": (i == 0),
                    "is_chain_end": (i == len(chain_step_names) - 1),
                    "is_tight": is_tight,
                    "min_qtime": min_qtime,
                    "qtime_deadline_step": chain_step_names[-1],
                }

    return result


# ============================================================
# 特殊设备处理
# ============================================================

def _check_special_eqp_available(
    eqp_id: str,
    lot_qty: int,
    ready_time: datetime,
    ct: float,
    special_eqp_map: dict[str, SpecialEqp],
    eqp_batch_state: dict,
    machine_intervals: dict,
    cur_time: Optional[datetime] = None,
) -> tuple[bool, datetime]:
    """检查特殊设备是否可用，返回 (can_schedule, actual_start_time_or_wait_until)。

    cur_time: 当前调度时间（判断设备 busy_until 是否已过用）。为 None 时退化为
    用 lot 就绪时间判断（向后兼容整链块内调用——其 ready_time 即当前推进时间）。

    together=true:
      - 设备运行中锁定（busy_until 之前不可加入新 Lot）
      - 空闲时 Lot 可立即开始（单批或组批均可，最多 max_lots/max_qty）
      - 若当前已凑齐的批次超过 max_lots 或 max_qty，则需等待当前批次结束

    together=false:
      - 运行中可加入，但需检查 max_lots/max_qty 限制
      - 若超过限制，返回最早可加入时间（当前活跃 Lot 的最早结束时间）
    """
    spec = special_eqp_map.get(eqp_id)
    if spec is None:
        return True, ready_time

    batch_key = eqp_id
    if batch_key not in eqp_batch_state:
        eqp_batch_state[batch_key] = {
            "spec": spec,
            "waiting": [],       # (lot_name, qty, ready_time) — together=true 时等待加入批次的 Lot
            "active": [],        # (lot_name, qty, end_time) — 当前活跃的 Lot
            "busy_until": None,  # datetime or None — together=true 时设备锁定结束时间
        }

    bs = eqp_batch_state[batch_key]
    # 判断设备忙闲用的"当前时刻"：有 cur_time 用它，否则退回 lot 就绪时间
    now = cur_time if cur_time is not None else ready_time

    if spec.together:
        # ——— together=true：设备运行中锁定，不允许新 Lot 加入 ———
        # 清理已完成的活跃 Lot
        bs["active"] = [(ln, q, et) for ln, q, et in bs["active"] if et > now]
        bs["waiting"] = [(ln, q, rt) for ln, q, rt in bs["waiting"] if rt <= now or True]  # 保留所有等待中的

        if bs["busy_until"] is not None:
            # 批次已结束（busy_until 已过）时重置锁定，避免用过去的 busy_until 永久阻塞
            if now >= bs["busy_until"]:
                bs["busy_until"] = None
            elif now < bs["busy_until"]:
                # 设备正在运行，必须等待
                return False, bs["busy_until"]

        # 设备空闲，检查是否可加入当前批次
        current_lots = len(bs["waiting"]) + len(bs["active"])
        current_qty = sum(q for _, q, _ in bs["waiting"]) + sum(q for _, q, _ in bs["active"])

        if current_lots >= spec.max_lots:
            # 当前批次已满，需等待批次结束
            return False, bs["busy_until"] or ready_time
        if current_qty + lot_qty > spec.max_qty:
            # 加入后会超过 qty 限制，需等待批次结束
            return False, bs["busy_until"] or ready_time

        return True, ready_time

    else:
        # ——— together=false：运行中可加入，但需检查限制 ———
        # 容量检查以 lot 就绪时刻（ready_time）为基准判断活跃作业：只有"拟开始时刻
        # 之前已结束"的作业才算释放。不能用 cur_time（全局推进时间）清理——先排的
        # Lot 刚结束时 cur_time==其 end，会被误判已释放，导致后续 Lot 以重叠时刻
        # 开始（探针 P12 根因）。
        bs["active"] = [(ln, q, et) for ln, q, et in bs["active"] if et > ready_time]

        current_lots = len(bs["active"])
        current_qty = sum(q for _, q, _ in bs["active"])

        if current_lots >= spec.max_lots:
            # 当前活跃 Lot 数已达上限，等待最早结束的 Lot
            if bs["active"]:
                earliest_end = min(et for _, _, et in bs["active"])
                return False, earliest_end
            return False, ready_time  # 异常配置（active 为空仍达上限），防御空列表
        if current_qty + lot_qty > spec.max_qty:
            # 加入后会超过 qty 限制
            if bs["active"]:
                earliest_end = min(et for _, _, et in bs["active"])
                return False, earliest_end
            # 空炉 + 单 lot 本身超容量：放行单独运行（设备物理上必须处理该 lot），
            # 否则 _compute_batch_slot/_check_special_eqp_available 永久拒绝 →
            # 主循环空转（实测 fuzz 扰动出 20 片 vs 上限 15 后以 1min/轮 磨 20 万轮）。
            return True, ready_time

        return True, ready_time


def _register_special_eqp_usage(
    eqp_id: str,
    lot_name: str,
    lot_qty: int,
    start_time: datetime,
    end_time: datetime,
    special_eqp_map: dict[str, SpecialEqp],
    eqp_batch_state: dict,
) -> None:
    """注册特殊设备使用。

    together=true:
      - 将 Lot 加入活跃列表，锁定设备直到该 Lot（批次）结束
      - 清空等待队列（批次已开始）
    together=false:
      - 将 Lot 加入活跃列表（独立追踪，可与其他 Lot 并行）
    """
    spec = special_eqp_map.get(eqp_id)
    if spec is None:
        return

    batch_key = eqp_id
    if batch_key not in eqp_batch_state:
        return

    bs = eqp_batch_state[batch_key]

    if spec.together:
        # 设备锁定：设置 busy_until 为当前批次中所有 Lot 的最晚结束时间
        bs["active"].append((lot_name, lot_qty, end_time))
        # 清空等待队列（已进入批次）
        bs["waiting"] = []
        # busy_until = 批次中最晚的结束时间
        max_end = max(et for _, _, et in bs["active"])
        bs["busy_until"] = max_end
    else:
        # together=false：独立追踪
        bs["active"].append((lot_name, lot_qty, end_time))


def _compute_batch_slot(
    eqp_id: str,
    lot,
    step,
    ct: float,
    ready_time: datetime,
    spec,
    lot_state: dict,
    special_lot_step_lookup: dict,
    ct_lookup: dict,
    eqp_batch_state: dict,
    priority_wait_map: dict,
    wait_window: int = BATCH_WAIT_WINDOW,
    cur_time: Optional[datetime] = None,
    machine_intervals: Optional[dict] = None,
    special_eqp_map: Optional[dict] = None,
    lot_entries: Optional[list] = None,
):
    """恒组批（together=true）批次槽位：计算当前 Lot 在"批次步骤"（如 CURE）的槽位。

    核心：等待凑批 —— 预估其它 Lot 到达同一批次步骤的时间，若在窗口内则并入同一批次，
    批次统一在 max(本 Lot 就绪, 各成员预估到达) 时刻开始；成员后续到达时经 pending 记录
    加入同一批次，避免单炉只装一个 Lot 造成的设备串行与 Q-time 拉爆。

    cur_time: 调度器真实推进时间。busy_until 是否已过的判定必须用它，否则等待中的 Lot
    会一直拿着陈旧 ready_time，误判"批次仍在运行"而无限等待 / 或错过批次导致步骤丢失。

    返回 (can_use, slot)：
      - can_use=False 且 slot=busy_until：设备正被上一批次占用，需等待。
      - can_use=True 且 slot=T：本 Lot 槽位为 T（批次统一开始时间，可晚于 ready_time）。
    """
    batch_key = eqp_id
    if batch_key not in eqp_batch_state:
        eqp_batch_state[batch_key] = {
            "spec": spec,
            "waiting": [],
            "active": [],
            "busy_until": None,
            "pending": None,   # {start, end, members: {lot_name: est_ready}}
        }
    bs = eqp_batch_state[batch_key]
    now = cur_time if cur_time is not None else ready_time
    # 设备实际空闲时刻（上一批次结束时间）。若为 None 说明设备从未被本批次逻辑占用或
    # 已空闲；若在 now 之前则上一批刚结束。open_time 用它与 ready_time 取 max 即可，
    # 不能用全局 now：now 是调度器推进时钟，其它 Lot 的远未来锚点会把 now 抬到该 Lot
    # 就绪之后数小时，若批次以 now 开炉会把本 Lot 的 Q-time 拉爆（实测 PC1 的 CURE
    # 就绪 10:18、被 real1 的 PLASMA 锚点 17:56 拖到 18:03 才开炉超 Q 235min）。
    _eqp_free = bs["busy_until"]

    # 清理：已结束的活跃 Lot（busy_until 过期清理放到 pending 判定之后）
    bs["active"] = [(ln, q, et) for ln, q, et in bs["active"] if et > now]

    # 先尝试加入待凑批次：本 Lot 是成员且就绪不晚于批次开始 → 并入同一批次。
    # 必须放在"批次过期清理"之前：成员 Lot 可能因 reference 阻塞等原因晚于 cur_time
    # 才被拾取，若先按 cur_time 清掉 pending（now>=busy_until 恰好命中），成员会丢失
    # 资格、错过同批而另开新批次（如 PC1 批次在 11:39 等 real1，real1 17:13 才被拾取，
    # 此时 busy_until=17:13==now，pending 被误清 → real1 落单且被推到 17:13 超 Q）。
    pending = bs.get("pending")
    if pending is not None:
        if lot.lot_name in pending.get("members", {}) and ready_time <= pending["start"]:
            return True, pending["start"]
        # 非成员 / 已错过批次开始：若批次仍在运行则等待
        if bs["busy_until"] is not None and now < bs["busy_until"]:
            return False, bs["busy_until"]

    # 批次结束后清理
    if bs["busy_until"] is not None and now >= bs["busy_until"]:
        bs["busy_until"] = None
        bs["pending"] = None

    # 设备运行中（上一批次未结束）：等待，不允许新开批次
    if bs["busy_until"] is not None:
        return False, bs["busy_until"]

    # 容量检查（等待中 + 活跃中）
    in_lots = len(bs["waiting"]) + len(bs["active"])
    in_qty = (sum(q for _, q, _, _ in bs["waiting"]) + sum(q for _, q, _ in bs["active"]))
    if in_lots >= spec.max_lots:
        return False, bs["busy_until"] or ready_time
    if in_qty + lot.qty > spec.max_qty:
        # 单 lot 本身超容量（qty > max_qty，如 fuzz 扰动出的 20 片 vs 上限 15）：
        # 不能永久拒绝——设备物理上必须单独处理该 lot，否则会无限等待空转
        # （实测 real1 的 UF-CURE 被拒后主循环以 1min/轮 磨 20 万轮、模拟时间推到次年）。
        # 空炉时放行单批（宁可产生 1 条 max_qty 告警，也不让 lot 永远排不上）。
        if not (lot.qty > spec.max_qty and in_qty == 0 and in_lots == 0):
            return False, bs["busy_until"] or ready_time

    # 开新批次：本批次最早开炉时刻 = max(本 Lot 就绪, 设备实际空闲时刻)。
    # 不能用纯 ready_time：等待中的 Lot（如 CURE 等上一批次结束）ready_time 仍是旧的，
    # 若以它开新批次会落在上一批次运行期间（重叠超容量）。busy_until 记录设备真实空闲
    # 时刻（在 now 之前则上一批已结束、设备空闲），批次从 max(ready_time, busy_until)
    # 起才算真正可用，且不受全局 now 抬升的影响。
    open_time = ready_time if _eqp_free is None else max(ready_time, _eqp_free)

    # 当前 lot 在本步的 Q-time 硬截止：恒组批凑批不得把本 lot 的批次开始推迟到截止之后
    # （否则为等慢批次而牺牲本 lot 的 Q-time）。end_mod=track in 时 deadline 约束开始时间，
    # track out 时约束结束时间 → 反推开始时间的上限。
    hard_deadline = None
    _cur_tracker = lot_state.get(lot.lot_name, {}).get("qtime_tracker", {})
    for _tk in _cur_tracker.values():
        if _tk.get("end_step") != step.step_name or _tk.get("deadline") is None:
            continue
        _em = (_tk.get("end_mod") or "track out").strip()
        _limit = _tk["deadline"] if _em == "track in" else _tk["deadline"] - timedelta(minutes=ct)
        if hard_deadline is None or _limit < hard_deadline:
            hard_deadline = _limit

    _lot_suffix = step.step_name.split("-")[-1]
    _lot_stage = getattr(step, "stage_name", "")
    batch_start = open_time
    members = {}
    member_qty = lot.qty
    for other_name, other_state in lot_state.items():
        if len(members) >= spec.max_lots - 1:
            break
        if other_name == lot.lot_name or other_state["done"]:
            continue
        other_lot = other_state["lot"]
        other_remaining = other_state["remaining_steps"]
        other_idx = other_state["step_index"]
        if other_idx >= len(other_remaining):
            continue
        # 找到该 Lot 前方第一个"同批次步骤"：
        #   同 product → 精确同名；跨 product → 同工艺后缀 + 同 stage（如 AB1-UF）才可共炉，
        #   避免把 DAF-CURE 误配到 UF-CURE。
        target = None
        target_idx = -1
        for _j in range(other_idx, len(other_remaining)):
            _s = other_remaining[_j]
            if other_lot.product_name == lot.product_name:
                if _s.step_name == step.step_name:
                    target, target_idx = _s, _j
                    break
            else:
                if (_s.step_name.split("-")[-1] == _lot_suffix
                        and (not _lot_stage or _s.stage_name == _lot_stage)):
                    target, target_idx = _s, _j
                    break
        if target is None:
            continue  # 该 Lot 已越过本批次步骤，不再考虑
        # 该 Lot 的目标步骤必须能用此设备
        other_eqp_ids = list(target.eqp_ids) if target.eqp_ids else ["-"]
        if special_lot_step_lookup:
            sls_key = (other_lot.lot_name, target.step_name)
            if sls_key in special_lot_step_lookup:
                sls = special_lot_step_lookup[sls_key]
                if sls.special_eqp:
                    other_eqp_ids = list(sls.special_eqp)
        if eqp_id not in other_eqp_ids:
            continue
        # 估算该 Lot 到达目标步骤的时间。三路估计：
        #   1) est_act：沿中间步骤查询 lot_entries 中【已经排定的实际完成时刻】做锚点，
        #      命中则用真实完成时刻（精确度最高），未命中才退化为 wait+CT 累积。这是
        #      本轮 Fix1 的核心：不再纯靠估算，优先用"已排设备占用"回溯真实到达。
        #   2) est_net：纯 CT+wait 累积（乐观下界）。
        #   3) est_dev：设备感知累积（更贴近真实）。若中间某一步的设备槽位查询在排程窗口内
        #      找不到可用槽（返回 datetime.max），说明设备快照此刻的竞争信息不可靠/该链
        #      尚未推进（如 PC2 中间 PLASMA 找不到槽），此时不能拿 datetime.max 污染预估、
        #      把实际已就绪的成员（如 PC2 的 CURE 就绪 10:18）错误排除。
        _mem_entries = ({e.step_name: e for e in lot_entries}
                        if lot_entries else {})
        est_net = other_state["ready_time"]
        est_act = other_state["ready_time"]
        _act_anchor_used = False
        for _s in other_remaining[other_idx:target_idx]:
            _sct = get_step_ct(ct_lookup, _s.product_name, _s.step_number, other_lot.qty)
            _swait = get_step_wait_time(other_lot.priority[0], other_lot.priority[1], priority_wait_map)
            est_net += timedelta(minutes=_swait + _sct)
            # 已排定的中间步骤：用真实完成时刻作为锚点，替代估（精确度高）。
            _e = _mem_entries.get(_s.step_name) if lot_entries else None
            if _e is not None:
                est_act = _e.end_time
                _act_anchor_used = True
            else:
                est_act += timedelta(minutes=_swait + _sct)
        est_dev = other_state["ready_time"]
        _dev_ok = True
        for _s in other_remaining[other_idx:target_idx]:
            _sct = get_step_ct(ct_lookup, _s.product_name, _s.step_number, other_lot.qty)
            _swait = get_step_wait_time(other_lot.priority[0], other_lot.priority[1], priority_wait_map)
            est_dev += timedelta(minutes=_swait)
            if machine_intervals and _s.eqp_ids and _s.eqp_ids != ["-"]:
                _est_free = datetime.max
                for _eid in _s.eqp_ids:
                    if _eid in special_eqp_map and special_eqp_map[_eid].together:
                        # 恒组批步骤：沿用当前批次设备的 pending 槽位（有则用，无则按就绪）
                        _pe = eqp_batch_state.get(_eid, {}).get("pending")
                        _cand = _pe["start"] if _pe and _pe.get("start") else est_dev
                    else:
                        _cand = _find_earliest_slot(
                            machine_intervals.get(_eid, []), est_dev, timedelta(minutes=_sct))
                    if _cand < _est_free:
                        _est_free = _cand
                if _est_free == datetime.max:
                    _dev_ok = False
                    break  # 设备快照不可信：退回纯 CT 累积
                est_dev = _est_free
            est_dev += timedelta(minutes=_sct)
        # 三种估计都晚于窗口才排除（任一成立即可并入）。est_act 用真实完成时刻锚点，
        # 是三者中最可信的；est_net 为乐观下界；est_dev 为设备感知上界。
        if (est_net > open_time + timedelta(minutes=wait_window)
                and est_dev > open_time + timedelta(minutes=wait_window)
                and est_act > open_time + timedelta(minutes=wait_window)):
            continue
        # 成员到达时间：优先用含真实完成锚点的 est_act；否则设备感知（真实竞争），
        # 设备快照不可信时退回纯 CT 下界。注意 est_dev 可能已含 datetime.max 污染
        # （break 后未完成的累积），以 est_net 兜底。
        _maxv = datetime.max
        if _act_anchor_used and est_act < _maxv:
            est = est_act
        elif _dev_ok and est_dev < _maxv:
            est = est_dev
        else:
            est = est_net
        # 成员目标步骤的粗排程锚点（含引用/Q-time 约束，ref-aware）若远超窗口，
        # 说明其"实际到达"无法在窗口内兑现（如被引用阻塞推到次日）——不得并入，
        # 否则批次为该成员空等、把本 lot 的 Q-time 拉爆（PC2 的 CURE 锚点被 real2
        # 引用推到次日，est 却按无约束估算给出当天时间）。
        _oth_ca = other_state.get("coarse_anchors", [])
        if _oth_ca and target_idx < len(_oth_ca) and _oth_ca[target_idx] is not None:
            if _oth_ca[target_idx] > open_time + timedelta(minutes=wait_window):
                continue
        # 成员到达时间晚于本 lot 的 Q-time 截止：不得并入（否则批次被推迟到截止后）。
        # 注意：仅当成员到达【晚于当前批次开始时刻】且晚于截止时才排除——若成员虽然
        # 晚于截止、但早于批次开始时刻（如批次因设备占用已必然晚于截止开炉），并入
        # 不会让批次更晚，此时应放行共炉（fuzz 实测：PC1 的 CURE 批次因 PKPOV001
        # 忙 14:45 才开炉，real2 的 CURE 就绪 09:05 < 14:45，却被"晚于截止 08:02"
        # 错误排除 → real2 单开一炉到 20:18，DISPENSE→CURE 超 Q 240min；并入后
        # 批次仍 14:45 开炉、real2 间隔 222min 达标）。
        if hard_deadline is not None and est > hard_deadline and est > batch_start:
            continue
        if member_qty + other_lot.qty > spec.max_qty:
            continue
        member_qty += other_lot.qty
        members[other_name] = est
        if est > batch_start:
            batch_start = est

    # Q-time 硬截止兜底：批次开始不晚于本 lot 的 Q-time 截止（仅当截止在 open_time 之后；
    # 若截止早于 open_time，说明物理上已无法满足 Q-time，不得把批次压到 open_time 之前
    # 造成与运行中批次重叠）。宁可本 lot 单开一炉，也不让凑批把它拖到超 Q。
    if (hard_deadline is not None and open_time < hard_deadline
            and batch_start > hard_deadline):
        batch_start = hard_deadline

    # 记录待凑批次（含成员名单，供后续到达的成员加入同一批次）
    if members:
        bs["pending"] = {
            "start": batch_start,
            "members": {name: est for name, est in members.items()},
        }
    return True, batch_start


# ============================================================
# 链调度逻辑
# ============================================================

def _compute_reverse_placement(
    suffix_steps: list,
    deadline: datetime,
    lot: Lot,
    ct_lookup: dict,
    special_lot_step_lookup: dict,
    machine_intervals: dict,
    shift_change_intervals: list,
    step_windows: dict,
    end_windows: dict,
    manual_adjust_lookup: dict,
    resolve_max_iterations: int,
    chain_info: Optional[dict] = None,
    priority_wait_map: Optional[dict[tuple[int, int], int]] = None,
    eqp_preferences: Optional[dict[tuple[str, str], list[str]]] = None,
) -> tuple[list, list, list, datetime]:
    """计算链后缀的反向调度位置（不提交），返回 (starts, ends, eqps, suffix_start_time)。

    从 deadline 往前倒排每个步骤，不修改任何全局状态。
    返回 suffix_start_time 为第一个后缀步骤的开始时间。
    """
    n = len(suffix_steps)
    step_cts = []
    for step in suffix_steps:
        ct = get_step_ct(ct_lookup, step.product_name, step.step_number, lot.qty)
        if special_lot_step_lookup:
            sls_key = (lot.lot_name, step.step_name)
            if sls_key in special_lot_step_lookup:
                sls = special_lot_step_lookup[sls_key]
                if sls.special_ct is not None:
                    ct = sls.special_ct
        step_cts.append(ct)

    scheduled_starts = [None] * n
    scheduled_ends = [None] * n
    scheduled_eqps = [None] * n

    # 后缀相邻步骤之间的真实等待间隔（链内受 Q-time 预算限制）
    eff_wait = _effective_chain_wait(lot, chain_info, priority_wait_map)

    current_deadline = deadline
    for idx in range(n - 1, -1, -1):
        step = suffix_steps[idx]
        ct = step_cts[idx]

        eqp_ids = list(step.eqp_ids) if step.eqp_ids else ["-"]
        if special_lot_step_lookup:
            sls_key = (lot.lot_name, step.step_name)
            if sls_key in special_lot_step_lookup:
                sls = special_lot_step_lookup[sls_key]
                if sls.special_eqp:
                    eqp_ids = list(sls.special_eqp)
        eqp_ids = _reorder_eqp_ids_by_preference(eqp_ids, lot.lot_name, step.step_name,
                                                 eqp_preferences)

        best_eqp = None
        best_start = datetime.min

        for eqp_id in eqp_ids:
            if eqp_id == "-":
                candidate_start = current_deadline - timedelta(minutes=ct)
                if candidate_start > best_start:
                    best_start = candidate_start
                    best_eqp = "-"
                continue

            candidate_start = _find_latest_slot(
                machine_intervals.get(eqp_id, []),
                current_deadline,
                timedelta(minutes=ct))
            if candidate_start > best_start:
                best_start = candidate_start
                best_eqp = eqp_id

        if best_eqp is None:
            return None, None, None, None

        scheduled_starts[idx] = best_start
        scheduled_ends[idx] = best_start + timedelta(minutes=ct)
        scheduled_eqps[idx] = best_eqp
        # 反向放置：下一步（更早）须在上一步开始前留出真实等待间隔
        if idx > 0:
            current_deadline = best_start - timedelta(minutes=eff_wait)
        else:
            current_deadline = best_start

    suffix_start_time = scheduled_starts[0] if n > 0 else deadline
    return scheduled_starts, scheduled_ends, scheduled_eqps, suffix_start_time


def _precompute_whole_chain_block(
    lot: Lot,
    remaining: list,
    chain_range: list,
    step_idx: int,
    ct_lookup: dict,
    special_lot_step_lookup: dict,
    qtime_by_product: dict,
    machine_intervals: dict,
    machine_available: dict,
    special_eqp_map: dict,
    shift_change_intervals: list,
    step_windows: dict,
    end_windows: dict,
    manual_adjust_lookup: dict,
    priority_wait_map: dict,
    ref_release_times: dict,
    pending_refs: set,
    ref_block_info: dict,
    coarse_anchors: list,
    resolve_max_iterations: int,
    chain_first_ready: Optional[datetime] = None,
    now: Optional[datetime] = None,
    ref_release_forecast: Optional[dict] = None,
    cycle_forecast_keys: Optional[set] = None,
    eqp_preferences: Optional[dict[tuple[str, str], list[str]]] = None,
) -> Optional[dict]:
    """整链块调度（借鉴 scheduler_before1 方法 B）：对无 reference 阻塞的 Q-time 链，
    把整条链作为一块整体，做"从链尾倒排 + 整链后移重试"迭代：
      - 每一步都真实占用最早可用设备槽位（负载均衡：并列窗口选更空闲 eqp）
      - 逐条校验链内 Q-time，超时则反推更晚的链首，重试直到达标或无法达成
    成功返回 {剩余索引 i: (eqp, start, end)} 的精确调度计划；否则返回 None（调用方回退单步调度）。

    手动延迟按『最早』语义处理：手动延迟只作为该步的最早开始下限，不把链硬性拉开；
    整链块由 Q-time 预算与设备槽位共同决定紧凑排布，避免"改一步裸奔、链散开超 Q"。
    """
    steps = [(i, remaining[i]) for i in chain_range if i >= step_idx]
    if not steps:
        return None
    names = [s.step_name for _, s in steps]
    # 仅当整条链都不涉及特殊设备时启用块调度，避免与 special_eqp 批处理提交逻辑冲突
    for _i, s in steps:
        if s.eqp_ids:
            for e in s.eqp_ids:
                if e in special_eqp_map:
                    return None

    # 计算每步 CT
    cts = []
    for _i, s in steps:
        ct = get_step_ct(ct_lookup, s.product_name, s.step_number, lot.qty)
        if special_lot_step_lookup:
            sls = special_lot_step_lookup.get((lot.lot_name, s.step_name))
            if sls and sls.special_ct is not None:
                ct = sls.special_ct
        cts.append(ct)
    n = len(steps)

    # 链内 Q-time 规则（start/end 都在链内）
    chain_qs = []
    ns = set(names)
    for q in qtime_by_product.get(lot.product_name, []):
        if q.start_step in ns and q.end_step in ns:
            chain_qs.append(q)

    # 步间等待必须按 Q-time 预算反推（整链块要紧凑排布），不能用原始 priority_wait，
    # 否则松链（如 priority 9,9 的 322min）会把链内步骤硬性拉开到超 Q-time，
    # 导致整链块永远不达标、紧链 defer 无限循环（test07 退化根因）。
    step_wait = _effective_chain_wait(lot, {"chain_steps": names}, priority_wait_map,
                                      ct_lookup, qtime_by_product, remaining,
                                      steps[0][0], special_lot_step_lookup)
    step_wait = max(0.0, float(step_wait))
    total_duration = sum(cts) + (n - 1) * step_wait

    # 每步的硬性最早下限（coarse 锚点 / reference 释放 / 手动延迟『最早』语义）
    lower_bounds = {}
    for _i, s in steps:
        lb = None
        if coarse_anchors and _i < len(coarse_anchors):
            lb = coarse_anchors[_i]
        # 链首步不得早于 lot 当前 ready_time（上一步结束+等待）
        if _i == steps[0][0] and chain_first_ready is not None:
            lb = chain_first_ready if (lb is None or chain_first_ready > lb) else lb
        # 手动延迟：仅作为"最早"下限，不把链硬性拉开（整链紧凑由 Q-time 与设备决定）
        if manual_adjust_lookup:
            _md = manual_adjust_lookup.get((lot.lot_name, s.step_name))
            if _md is not None and (lb is None or _md > lb):
                lb = _md
        # reference 释放下限
        for rk, bi in (ref_block_info or {}).items():
            if bi is not None and steps[0][0] <= bi <= _i:
                # 环内"预测释放"的 reference（互相等待无确切顺序）：整链块反向放置
                # 会把"链尾锚点在次日、中间步被 ref 钉在当日"的跨天松链（如 FTF 链）
                # 通过整链后移推到更晚（max_gap 无限累计）——实测 MOUNT 被推到次日。
                # 预测释放时间已写入 ref_release_times（单步调度会用 coarse 锚点兜底
                # 正确等待），此处整链块直接放弃，交回单步/正向逐步骤调度。
                if cycle_forecast_keys and (lot.lot_name, rk) in cycle_forecast_keys:
                    return None
                rel = (ref_release_times.get(lot.lot_name, {}).get(rk, FAR_FUTURE)
                       if ref_release_times else FAR_FUTURE)
                # 尚未释放但有第一遍预测释放时刻：用预测锚点，让整链块提前紧凑锚定，
                # 避免等待实际释放导致链首过早、reference 释放后链内 Q-time 超时。
                if rel is FAR_FUTURE and ref_release_forecast and rk in ref_release_forecast:
                    rel = ref_release_forecast[rk]
                if rk in (pending_refs or set()) and rel is FAR_FUTURE:
                    return None  # release 时刻未知：交回单步/DEFER
                # release 已知（referenced lot 已排）：以释放时刻作为该步硬性下界锚点，
                # 让整链块从该锚点反向压紧，避免"前缀过早散开、端步骤超 Q"。
                if rel is not FAR_FUTURE and (lb is None or rel > lb):
                    lb = rel
        lower_bounds[_i] = lb

    # 初始链首：取链首步的硬性下限（coarse 锚点 / reference 释放），否则取链首步
    # 设备当前最早可坐时刻；再退化为全设备最早可用时刻。机器可用性未知则不启用块调度。
    sim_intervals = {k: list(v) for k, v in machine_intervals.items()}
    sim_avail = dict(machine_available)
    first_lb = lower_bounds.get(steps[0][0])
    _init_cs = first_lb
    if _init_cs is None:
        if steps[0][1].eqp_ids:
            _m_fast = [sim_avail.get(e) for e in steps[0][1].eqp_ids if sim_avail.get(e) is not None]
            _init_cs = min(_m_fast) if _m_fast else None
    if _init_cs is None:
        _all_fast = [t for t in sim_avail.values() if t is not None]
        _init_cs = min(_all_fast) if _all_fast else None
    if _init_cs is None:
        return None
    chain_start = _init_cs

    # ---- 正向贪心优先（安全站点模型）：链内步骤尽早做，Q-time 达标即采用 ----
    # 用户规则：做完当前站点就该继续往下做（机台空闲即做），不要为了后端步骤
    # 倒着排导致前面设备空闲而没做。先沿链正向逐步骤贪心找最早槽位：
    #   - 每步从 max(上一步结束+步间等待, 该步硬性下界) 起找最早可行槽位；
    #   - 链内 Q-time 全部达标且步骤顺序合法 → 直接采用（所有步骤尽量早开始）；
    #   - 不达标（紧链/设备竞争挤散）→ 回退下方"倒排 + 整链后移"保底（紧凑不超 Q）。
    # 这样宽松预算（如 10080min 的 FTF 段）不再被倒排整体推迟，中间步骤做完即走。
    _g_sim = {k: list(v) for k, v in machine_intervals.items()}
    _g_avail = dict(machine_available)
    _g_ok = True
    _g_starts: list = [None] * n
    _g_ends: list = [None] * n
    _g_eqps: list = [None] * n
    # pending reference 检查：链内步骤被"尚未释放且无预测锚点"的引用阻塞时，不能贪心
    # 早做（释放时刻未知，可能早于真实释放 → reference 违背），放弃正向贪心回退原逻辑。
    for _k in range(n):
        _i, _s = steps[_k]
        for _rk, _bix in (ref_block_info or {}).items():
            if _bix is None or _i < _bix:
                continue
            _relv = (ref_release_times.get(lot.lot_name, {}).get(_rk) or FAR_FUTURE)
            if _relv == FAR_FUTURE:
                if ref_release_forecast and _rk in ref_release_forecast:
                    continue  # 有预测锚点：可用预测时刻做下界
                if _rk in (pending_refs or set()):
                    _g_ok = False
                    break
        if not _g_ok:
            break
    if _g_ok:
        for _k in range(n):
            _i, _s = steps[_k]
            _ct = cts[_k]
            _lb = lower_bounds.get(_i)
            if _k == 0:
                _base = chain_start
                if _lb is not None and _lb > _base:
                    _base = _lb
            else:
                _base = _g_ends[_k - 1] + timedelta(minutes=step_wait)
                if _lb is not None and _lb > _base:
                    _base = _lb
            # ---- 紧 Q 链不跨班次（用户规则）：链内紧 Q 对 S→E 若跨班次，S 起点推后 ----
            # 保守估计 E 窗口终点 = S.start + CT_S + D（E 必不晚于该时刻）。推后只缩短
            # S→E 窗口（Q 仍满足）；若推后使链内/上游 Q 破裂，链尾校验会令正向贪心
            # 失败 → 回退倒排保底（保留跨班次告警）。
            if CROSS_SHIFT_AVOID and shift_change_intervals:
                for _q in chain_qs:
                    _Dq = _q.max_duration
                    if _Dq is None or _Dq > TIGHT_CHAIN_THRESHOLD:
                        continue
                    if _q.start_step != _s.step_name:
                        continue
                    _st_b = _cross_shift_push_target(
                        _base, _base + timedelta(minutes=_ct + _Dq), shift_change_intervals)
                    if _st_b is not None and _st_b > _base:
                        _base = _st_b
                    break
            if _s.eqp_ids:
                _cands = []
                for _e in _reorder_eqp_ids_by_preference(
                        list(_s.eqp_ids), lot.lot_name, _s.step_name, eqp_preferences):
                    _st = _find_earliest_slot(_g_sim.get(_e, []), _base, timedelta(minutes=_ct))
                    if _st == datetime.max:
                        continue
                    _st = _resolve_constraints(_st, _ct, _e, _g_sim, shift_change_intervals,
                                               step_windows, end_windows, _s.step_name, resolve_max_iterations)
                    if _st == datetime.max:
                        continue
                    _cands.append((_st, _e))
                if not _cands:
                    _g_ok = False
                    break
                _cands.sort(key=lambda x: (x[0], _g_avail.get(x[1], datetime.min)))
                _st, _e = _cands[0]
            else:
                _st = _resolve_constraints(_base, _ct, "-", _g_sim, shift_change_intervals,
                                           step_windows, end_windows, _s.step_name, resolve_max_iterations)
                if _st == datetime.max:
                    _g_ok = False
                    break
                _e = "-"
            _g_starts[_k], _g_ends[_k], _g_eqps[_k] = _st, _st + timedelta(minutes=_ct), _e
            _g_avail[_e] = _g_ends[_k]
            _g_sim.setdefault(_e, []).append((_st, _g_ends[_k]))
    if _g_ok:
        for _k in range(n - 1):
            if _g_starts[_k + 1] < _g_ends[_k]:
                _g_ok = False
                break
    if _g_ok:
        for _q in chain_qs:
            _qk = _chain_step_pos(names, _q.start_step)
            _qe = _chain_step_pos(names, _q.end_step)
            if _qk is None or _qe is None:
                continue
            _qas = _g_starts[_qk] if (_q.start_mod or "track in").strip() == "track in" else _g_ends[_qk]
            _qae = _g_starts[_qe] if (_q.end_mod or "track out").strip() == "track in" else _g_ends[_qe]
            # 余量目标：紧链不仅要"不超 Q"，还要留出用户设置的安全余量
            # （span <= D - safe）。贪心自然放置把端步骤 E 顶到极限（余量≈0）时，
            # 判定失败回退倒排——倒排把链首拉回贴近 E，余量恢复。
            _span_g = (_qae - _qas).total_seconds() / 60.0
            if _span_g > _q.max_duration - _q_target_margin(_q):
                _g_ok = False
                break
    if _g_ok:
        _gplan = {}
        for _k, (_i, _s) in enumerate(steps):
            _gplan[_i] = (_g_eqps[_k], _g_starts[_k], _g_ends[_k])
        return _gplan

    # ---- 迭代：向后倒排 + 整链后移，直至 Q-time 达标或无法再推迟 ----
    for _iter in range(12):
        # 每轮重置模拟状态（基于当前真实机器状态的副本 + 链自身占用）
        sim_intervals = {k: list(v) for k, v in machine_intervals.items()}
        sim_avail = dict(machine_available)
        starts = [None] * n
        ends = [None] * n
        eqps = [None] * n

        earliest_chain_end = chain_start + timedelta(minutes=total_duration)
        ok = True

        # 1. 最后一步：在 earliest_chain_end 之前/之后找最早可坐槽位（选择更空闲 eqp）
        last_i, last_s = steps[-1]
        last_ct = cts[-1]
        max_gap = timedelta(0)
        if last_s.eqp_ids:
            cands = []
            ideal = earliest_chain_end - timedelta(minutes=last_ct)
            for e in _reorder_eqp_ids_by_preference(
                    list(last_s.eqp_ids), lot.lot_name, last_s.step_name, eqp_preferences):
                st = _find_earliest_slot(sim_intervals.get(e, []), ideal, timedelta(minutes=last_ct))
                if st == datetime.max:
                    continue
                st = _resolve_constraints(st, last_ct, e, sim_intervals, shift_change_intervals,
                                          step_windows, end_windows, last_s.step_name, resolve_max_iterations)
                if st == datetime.max:
                    continue
                cands.append((st, e))
            if not cands:
                ok = False
            else:
                cands.sort(key=lambda x: (x[0], sim_avail.get(x[1], datetime.min)))
                st, e = cands[0]
                if lower_bounds.get(last_i) is not None and st < lower_bounds[last_i]:
                    st = lower_bounds[last_i]
                    st = _resolve_constraints(st, last_ct, e, sim_intervals, shift_change_intervals,
                                              step_windows, end_windows, last_s.step_name, resolve_max_iterations)
                    if st == datetime.max:
                        ok = False
                if ok:
                    if st > ideal:
                        max_gap = max(max_gap, st - ideal)
                    ends[-1] = st + timedelta(minutes=last_ct)
                    starts[-1] = st
                    eqps[-1] = e
                    sim_avail[e] = ends[-1]
                    sim_intervals.setdefault(e, []).append((st, ends[-1]))
        else:
            ideal = earliest_chain_end - timedelta(minutes=last_ct)
            st = ideal
            if lower_bounds.get(last_i) is not None:
                st = max(st, lower_bounds[last_i])
            st = _resolve_constraints(st, last_ct, "-", sim_intervals, shift_change_intervals,
                                      step_windows, end_windows, last_s.step_name, resolve_max_iterations)
            if st == datetime.max:
                ok = False
            else:
                if st > ideal:
                    max_gap = max(max_gap, st - ideal)
                ends[-1] = st + timedelta(minutes=last_ct)
                starts[-1] = st
                eqps[-1] = "-"

        # 2. 向后依次放置其余步（不原地平移；记录步间缺口，整体推迟 chain_start 后重排）
        if ok:
            for k in range(n - 2, -1, -1):
                i, s = steps[k]
                ct = cts[k]
                ideal_end = starts[k + 1] - timedelta(minutes=step_wait)
                ideal_start = ideal_end - timedelta(minutes=ct)
                if lower_bounds.get(i) is not None and ideal_start < lower_bounds[i]:
                    ideal_start = lower_bounds[i]
                if s.eqp_ids:
                    cands = []
                    for e in _reorder_eqp_ids_by_preference(
                            list(s.eqp_ids), lot.lot_name, s.step_name, eqp_preferences):
                        st = _find_earliest_slot(sim_intervals.get(e, []), ideal_start, timedelta(minutes=ct))
                        if st == datetime.max:
                            continue
                        st = _resolve_constraints(st, ct, e, sim_intervals, shift_change_intervals,
                                                  step_windows, end_windows, s.step_name, resolve_max_iterations)
                        if st == datetime.max:
                            continue
                        cands.append((st, e))
                    if not cands:
                        ok = False
                        break
                    cands.sort(key=lambda x: (x[0], sim_avail.get(x[1], datetime.min)))
                    st, e = cands[0]
                    if st > ideal_start:
                        max_gap = max(max_gap, st - ideal_start)
                    ends[k] = st + timedelta(minutes=ct)
                    starts[k] = st
                    eqps[k] = e
                    sim_avail[e] = ends[k]
                    sim_intervals.setdefault(e, []).append((st, ends[k]))
                else:
                    st = _resolve_constraints(ideal_start, ct, "-", sim_intervals, shift_change_intervals,
                                              step_windows, end_windows, s.step_name, resolve_max_iterations)
                    if st == datetime.max:
                        ok = False
                        break
                    if st > ideal_start:
                        max_gap = max(max_gap, st - ideal_start)
                    ends[k] = st + timedelta(minutes=ct)
                    starts[k] = st
                    eqps[k] = "-"

        if not ok:
            if os.environ.get("QFAIL"):
                print(f"[QFAIL] {lot.lot_name} {chain_start} iter={_iter} fail_ok names={names[:6]}", flush=True)
            return None  # 设备/约束不可行：回退单步调度

        # ---- 整链整体后移重排：某步被设备/约束推晚时，推迟 chain_start 后整体重排，
        # 保证链内背靠背且每步都经 _find_earliest_slot 校验（避免原地平移把端步骤移进
        # 已占用槽位、提交时再被顶开的"散开超 Q"退化，test09 根因）。
        if max_gap > timedelta(0) and _iter < 11:
            chain_start += max_gap
            continue

        # ---- 顺序校验（仅收敛/耗尽后）：链内每步不得早于前一步结束 ----
        # 设备时间窗可能导致反向放置把后一步排到前一步之前（如 FC-REFLOW 与 FC-DEFLUX
        # 同撞窗口开启时刻 09:30），此时 Q-time 跨度可能为负而"看似达标"。只有重排已
        # 收敛（max_gap==0）或迭代耗尽后仍顺序违例，才拒绝该畸形计划（test12 根因），
        # 交回单步调度（单步会保持步骤顺序）；瞬态违例已由上面的整体后移重排修复。
        if ok:
            for _k in range(n - 1):
                if starts[_k] is None or starts[_k + 1] is None:
                    ok = False
                    break
                if starts[_k + 1] < ends[_k]:
                    if os.environ.get("QFAIL"):
                        print(f"[QFAIL] {lot.lot_name} {chain_start} iter={_iter} ORDER "
                              f"{names[_k]}@{starts[_k].strftime('%m/%d %H:%M')} -> "
                              f"{names[_k + 1]}@{starts[_k + 1].strftime('%m/%d %H:%M')}", flush=True)
                    ok = False
                    break
        if not ok:
            if os.environ.get("QFAIL"):
                print(f"[QFAIL] {lot.lot_name} {chain_start} iter={_iter} order_fail names={names[:6]}", flush=True)
            return None  # 畸形（顺序违例）计划不可返回：回退单步调度

        # 3. 逐条校验链内 Q-time 与跨班次
        # 余量目标：紧链 span 必须 <= D - safe（留出用户安全余量）。倒排收敛后链已
        # 紧凑（max_gap==0），span 为链自身固有长度，继续推链首只会让整链同步平移、
        # span 不变（余量不变）→ 视为"可接受的最佳"直接采用（余量缺口由校验/优化器
        # 报告）；仅当真·超 Q（span > D）或跨班次（推后能改变位置、span 不变也有效）
        # 时才反推链首重试。
        violation = None
        cs_target = None          # 跨班次推后目标（链首起点）
        margin_only = False       # 仅余量缺口（span<=D 但 < D-safe）：紧凑链不可改善
        for q in chain_qs:
            qk = _chain_step_pos(names, q.start_step)
            qe = _chain_step_pos(names, q.end_step)
            if qk is None or qe is None:
                continue
            q_astart = starts[qk] if (q.start_mod or "track in").strip() == "track in" else ends[qk]
            q_aend = starts[qe] if (q.end_mod or "track out").strip() == "track in" else ends[qe]
            _span_m = (q_aend - q_astart).total_seconds() / 60.0
            if _span_m > q.max_duration:
                violation = q
                break
            # 紧 Q 对跨班次（用户规则）：窗口跨过班次切换时刻 → 链首整体后移。
            # 先于余量缺口判断：紧凑链的余量缺口不可推改善，但跨班次可通过整链平移
            # 到班次起点解决（span 不变、位置改变）。
            if CROSS_SHIFT_AVOID and shift_change_intervals:
                _Dq = q.max_duration
                if _Dq is not None and _Dq <= TIGHT_CHAIN_THRESHOLD:
                    _cs = _cross_shift_push_target(q_astart, q_aend, shift_change_intervals)
                    if _cs is not None and _cs > q_astart:
                        violation = q
                        cs_target = _cs
                        break
            if _span_m > q.max_duration - _q_target_margin(q):
                margin_only = True
                if max_gap <= timedelta(0):
                    # 紧凑链的固有跨度就超余量目标：推后无效，接受（最佳余量）
                    continue
                violation = q
                break
        if violation is None:
            # 成功：装配计划
            plan = {}
            for k, (i, s) in enumerate(steps):
                plan[i] = (eqps[k], starts[k], ends[k])
            return plan

        # 4. Q-time 超时/余量不足/跨班次：反推更晚的链首，重试
        qk = _chain_step_pos(names, violation.start_step)
        qe = _chain_step_pos(names, violation.end_step)
        q_astart = starts[qk] if (violation.start_mod or "track in").strip() == "track in" else ends[qk]
        q_aend = starts[qe] if (violation.end_mod or "track out").strip() == "track in" else ends[qe]
        if cs_target is not None:
            # 跨班次：链首直接推到班次起点（跳过交接窗），整体后移
            target_start = cs_target
        else:
            # 余量缺口（非紧凑链，可推）：span 目标 = D - safe；真·超 Q：仅保证 span<=D
            _m2 = _q_target_margin(violation) if margin_only else 0.0
            target_start = q_aend - timedelta(minutes=violation.max_duration - _m2)
        if os.environ.get("QFAIL"):
            _st_strs = [(st.strftime("%m/%d %H:%M") if st else None) for st in starts]
            print(f"[QFAIL] {lot.lot_name} chain_start={chain_start} iter={_iter} "
                  f"viol={violation.start_step}->{violation.end_step} span={(q_aend-q_astart).total_seconds()/60:.0f} "
                  f"max={violation.max_duration} aend={q_aend} target_start={target_start} "
                  f"starts={_st_strs}", flush=True)
        # 反推链首：链首 = target_start - 该步前(含边界)的耗时
        prefix_ct = 0.0
        for k in range(qk):
            prefix_ct += cts[k]
            if k > 0:
                prefix_ct += step_wait
        if (violation.start_mod or "track in").strip() != "track in":
            prefix_ct += cts[qk]
        target_chain_start = target_start - timedelta(minutes=prefix_ct)
        # 不能早于链首硬性下限；且必须比当前更晚（否则无法达成）
        lb0 = lower_bounds.get(steps[0][0])
        if lb0 is not None and target_chain_start < lb0:
            target_chain_start = lb0
        if target_chain_start <= chain_start:
            if os.environ.get("QFAIL"):
                print(f"[QFAIL] {lot.lot_name} {chain_start} iter={_iter} no_progress "
                      f"viol={violation.start_step}->{violation.end_step} names={names[:6]}", flush=True)
            return None
        chain_start = target_chain_start
    if os.environ.get("QFAIL"):
        print(f"[QFAIL] {lot.lot_name} {chain_start} iter_exhausted names={names[:6]}", flush=True)
    return None


def _chain_step_pos(names: list, step_name: str) -> Optional[int]:
    for idx, nm in enumerate(names):
        if nm == step_name:
            return idx
    return None


def _try_schedule_chain_forward(
    lot: Lot,
    state: dict,
    chain_info: dict,
    flow_map: dict,
    ct_lookup: dict,
    qtime_by_product: dict,
    machine_intervals: dict,
    machine_available: dict,
    special_eqp_map: dict,
    eqp_batch_state: dict,
    special_lot_step_lookup: dict,
    shift_change_intervals: list,
    step_windows: dict,
    end_windows: dict,
    manual_adjust_lookup: dict,
    pin_lookup: dict,
    resolve_max_iterations: int,
    lot_entries: list,
    eqp_entries: list,
    qtime_alerts: list,
    lot_order_rank: dict,
    pending_refs: dict,
    ref_block_info: dict,
    shift_times: list,
    reference_deps: dict,
    lot_state: dict,
    ref_release_times: dict,
    priority_wait_map: dict,
    product_qtimes: list = None,
    chain_placement: str = "compact",
    ref_release_forecast: dict = None,
    cur_time: Optional[datetime] = None,
    cycle_forecast_keys: Optional[set] = None,
    eqp_preferences: Optional[dict[tuple[str, str], list[str]]] = None,
) -> tuple[bool, int]:
    """尝试从前往后调度一个 Q-time 链段。
    如果遇到 reference 阻塞或设备不可用，则拆链：
    - 紧链：不拆分，返回 False 等待
    - 松链：拆链 —— 先从后往前调度后缀（从 deadline 倒排），再从前往后调度前缀（截止到后缀开始时间），
            确保拆分后的两段紧密相邻

    chain_placement: "compact"（默认，reference 锚点紧凑放置）/ "early"（最早放置，供迭代探索）

    返回 (全部调度完成, 已调度的步骤数)
    """
    remaining = state["remaining_steps"]
    step_idx = state["step_index"]
    chain_steps_names = chain_info["chain_steps"]
    is_tight = chain_info.get("is_tight", False)

    # ---- 手动调整：整链锚点（在任何路径前生效） ----
    # 链内若有手动延后的 step X（delay_to），链首若按最早排，X 之后会被手动
    # 硬性推迟导致链首过早、前段 Q-time 被拉长。这里按"链内自然位置"反推整链
    # 应整体后移的量，使 X 恰好落在 delay_to，链内保持紧凑。
    # pin 精确锁定同样按整链锚点处理：使被 pin 的 step 恰好落在 pin_time。
    if manual_adjust_lookup or pin_lookup:
        _chain_start_ready = state["ready_time"]
        _acc = 0.0
        _positions: dict[str, float] = {}
        for _k, _sn in enumerate(chain_steps_names):
            _positions[_sn] = _acc
            if _k < len(chain_steps_names) - 1:
                _s = next((s for s in remaining if s.step_name == _sn), None)
                if _s is not None:
                    _ct = get_step_ct(ct_lookup, _s.product_name, _s.step_number, lot.qty)
                    if special_lot_step_lookup:
                        _slsk = (lot.lot_name, _sn)
                        if _slsk in special_lot_step_lookup and special_lot_step_lookup[_slsk].special_ct is not None:
                            _ct = special_lot_step_lookup[_slsk].special_ct
                    _acc += _ct + get_step_wait_time(lot.priority[0], lot.priority[1], priority_wait_map)
        _chain_manual_offset = 0.0
        for (_mlot, _mstep), _delay in manual_adjust_lookup.items():
            if _mlot == lot.lot_name and _mstep and _mstep in _positions:
                _offset = (_delay - _chain_start_ready).total_seconds() / 60.0 - _positions[_mstep]
                if _offset > _chain_manual_offset:
                    _chain_manual_offset = _offset
        if pin_lookup:
            for (_mlot, _mstep), _pintime in pin_lookup.items():
                if _mlot == lot.lot_name and _mstep and _mstep in _positions:
                    _offset = (_pintime - _chain_start_ready).total_seconds() / 60.0 - _positions[_mstep]
                    if _offset > _chain_manual_offset:
                        _chain_manual_offset = _offset
        if _chain_manual_offset > 0:
            # 链首只需平移"宽松段吸收不掉"的残余延迟：被钉步骤之前的 Q-time 段若预算宽裕
            # （如 BAKE→DISPENSE 1440min），其自然时长远小于预算，可吸收部分/全部手动延迟，
            # 链首无需整体平移。否则会把链首人为推迟数小时、制造设备空窗并拖累引用该链的
            # lot（实测 real1 的 UF-CURE 钉到 20:00 后，BAKE 被从 03:33 平移 12h 到 15:00，
            # 导致 PC1 的 UF-DISPENSE 因引用 real1 的 BAKE 而推后、PLASMA→DISPENSE 超 Q）。
            _absorb = 0.0
            _pin_pos = _positions.get(_mstep)
            if _pin_pos is not None:
                for _q in (product_qtimes or []):
                    _qs = _positions.get(_q.start_step)
                    _qe = _positions.get(_q.end_step)
                    if _qs is None or _qe is None or _qe > _pin_pos:
                        continue
                    _budget = _q.max_duration
                    if _budget is None or _budget <= TIGHT_CHAIN_THRESHOLD:
                        continue  # 紧段不吸收（保持链内紧凑，由链调度压实保 Q）
                    _seg_nat = 0.0
                    for _s2 in remaining:
                        if _s2.step_name == _q.end_step:
                            _end_ct = get_step_ct(ct_lookup, _s2.product_name,
                                                  _s2.step_number, lot.qty)
                            _seg_nat = _positions[_q.end_step] + _end_ct - _positions[_q.start_step]
                            break
                    _absorb += max(0.0, _budget - _seg_nat)
            _chain_manual_offset = max(0.0, _chain_manual_offset - _absorb)
            if _chain_manual_offset > 0:
                state["ready_time"] = state["ready_time"] + timedelta(minutes=_chain_manual_offset)
                state["_base_ready_time"] = state["ready_time"]

    # 找到链段在当前 lot 的 remaining_steps 中的范围
    chain_start_in_remaining = None
    chain_end_in_remaining = None
    for i, s in enumerate(remaining[step_idx:], start=step_idx):
        if s.step_name == chain_info["chain_start_step"] and chain_start_in_remaining is None:
            chain_start_in_remaining = i
        if s.step_name in chain_steps_names:
            chain_end_in_remaining = i

    if chain_start_in_remaining is None:
        return False, 0

    # 收集链段中需要调度的步骤
    chain_range = list(range(chain_start_in_remaining, chain_end_in_remaining + 1))

    # ---- 预扫描：找到第一个阻塞点 ----
    split_point = None  # 阻塞发生的步骤索引（在 chain_range 中）
    ref_anchor = None  # 已释放 reference 的锚点（释放时刻）
    pending_keys: set = set()
    for i in chain_range:
        if i < step_idx:
            continue
        step = remaining[i]

        # 检查 reference 阻塞
        if state.get("refs_registered"):
            pending = pending_refs.get(lot.lot_name, set())
            rel = ref_release_times.get(lot.lot_name, {})
            for ref_key in pending:
                block_idx = ref_block_info.get(ref_key)
                if block_idx is not None and i >= block_idx:
                    # 整链块重排：无论紧链/松链，release 已知的 reference 都不在此处拆链，
                    # 交给整链块调度（方法B以释放时刻为下界锚点反向压紧整链）。
                    # 尚未释放但第一遍已预测释放时刻（ref_release_forecast）也按已释放
                    # 处理——整链块以预测时刻为锚提前紧凑放置，避免等到实际释放时链首
                    # 已过早开排、链内 Q-time 超时。
                    if (ref_key in rel and rel[ref_key] != FAR_FUTURE) or (
                            ref_release_forecast and ref_key in ref_release_forecast):
                        pending_keys.add(ref_key)
                        continue
                    # release 未知：紧链不拆分，交单步 DEFER（预测锚点延迟起点）；
                    # 松链则以该阻塞点拆链逆向调度后缀。
                    if is_tight:
                        return False, 0
                    else:
                        split_point = i
                        pending_keys.add(ref_key)
                        # 若该 reference 已释放（兜底），取释放时刻作为紧凑锚点
                        if ref_key in rel and (ref_anchor is None or rel[ref_key] > ref_anchor):
                            ref_anchor = rel[ref_key]
                    break
        if split_point is not None:
            break

    # ---- 处理拆链：先反向调度后缀，再正向调度前缀 ----
    if split_point is not None:
        if pending_keys and not ref_anchor:
            # 后缀被"未释放"的 reference 阻塞：端步骤位置未知，若现在按链 deadline
            # 反向提前放置后缀，会早于 reference 释放时刻，造成 reference 违背。
            # 与紧链一致：不拆排，交给单步调度/DEFER 机制，待 reference 释放后再延迟整链。
            return False, 0
        # 后缀步骤：从 split_point 到 chain_end
        suffix_steps = remaining[split_point:chain_end_in_remaining + 1]
        deadline = _get_chain_deadline(state, chain_info, product_qtimes)
        # 若 reference 已释放，以释放时刻作为后缀的放置锚点：
        # 后缀从锚点开始正向紧凑排（第一个后缀步骤从锚点开始，后续紧挨），
        # 等价于以 (锚点 + 后缀总CT) 作为反向放置的截止时间。
        if ref_anchor is not None and chain_placement != "early":
            _suffix_ct_sum = 0.0
            for _s in suffix_steps:
                _s_ct = get_step_ct(ct_lookup, _s.product_name, _s.step_number, lot.qty)
                if special_lot_step_lookup:
                    _sls_key = (lot.lot_name, _s.step_name)
                    if _sls_key in special_lot_step_lookup:
                        _sls = special_lot_step_lookup[_sls_key]
                        if _sls.special_ct is not None:
                            _s_ct = _sls.special_ct
                _suffix_ct_sum += _s_ct
            deadline = ref_anchor + timedelta(minutes=_suffix_ct_sum)

        # 后缀步骤：从 split_point 反向调度位置
        rev_starts, rev_ends, rev_eqps, suffix_start_time = _compute_reverse_placement(
            suffix_steps, deadline, lot, ct_lookup, special_lot_step_lookup,
            machine_intervals, shift_change_intervals, step_windows, end_windows,
            manual_adjust_lookup, resolve_max_iterations,
            chain_info=chain_info, priority_wait_map=priority_wait_map)

        if rev_starts is None:
            # 无法为后缀找到位置，整个链拆失败
            state["chain_reverse_pending"] = {
                "chain_start_step": suffix_steps[0].step_name,
                "chain_end_step": chain_info["chain_end_step"],
                "chain_steps": suffix_steps,
                "deadline": deadline,
            }
            return False, 0

        # 2. 正向调度前缀步骤（chain_start 到 split_point-1），截止到 suffix_start_time
        scheduled_count = 0
        prefix_range = list(range(chain_start_in_remaining, split_point))
        for i in prefix_range:
            if i < step_idx:
                continue

            step = remaining[i]
            ct = get_step_ct(ct_lookup, step.product_name, step.step_number, lot.qty)
            if special_lot_step_lookup:
                sls_key = (lot.lot_name, step.step_name)
                if sls_key in special_lot_step_lookup:
                    sls = special_lot_step_lookup[sls_key]
                    if sls.special_ct is not None:
                        ct = sls.special_ct
            if i == 0 and state.get("first_step_ct_adj", 0) > 0:
                ct = max(0, ct - state["first_step_ct_adj"])

            eqp_ids = list(step.eqp_ids) if step.eqp_ids else ["-"]
            if special_lot_step_lookup:
                sls_key = (lot.lot_name, step.step_name)
                if sls_key in special_lot_step_lookup:
                    sls = special_lot_step_lookup[sls_key]
                    if sls.special_eqp:
                        eqp_ids = list(sls.special_eqp)
            eqp_ids = _reorder_eqp_ids_by_preference(eqp_ids, lot.lot_name, step.step_name,
                                                     eqp_preferences)

            ready_time = state["ready_time"] if scheduled_count == 0 else (
                lot_entries[-1].end_time if lot_entries else state["ready_time"])

            # 引用释放下界（已释放的 reference）：当前步骤不得早于释放时刻。
            # 主循环单步路径经 _update_blocked_ready_time 处理；链内逐步骤放置同样需要，
            # 否则链中间步骤（如 real1 的 UF-DISPENSE 等 PC1 的 UF-DISPENSE 释放 13:46）
            # 会被 lot_entries[-1].end_time 提前放行 → reference 违背（fuzz seed 20260828166）。
            if state.get("refs_registered"):
                for _rk5, _bix5 in ref_block_info.items():
                    if _bix5 is not None and i >= _bix5:
                        _rv5 = ref_release_times.get(lot.lot_name, {}).get(_rk5)
                        if _rv5 is not None and _rv5 != FAR_FUTURE and _rv5 > ready_time:
                            ready_time = _rv5

            # 前缀的最后一步不能超过后缀开始时间（留出步间等待间隔）
            if i == prefix_range[-1]:
                _junction_wait = _effective_chain_wait(lot, chain_info, priority_wait_map)
                prefix_deadline = suffix_start_time - timedelta(minutes=_junction_wait)
            else:
                prefix_deadline = FAR_FUTURE

            best_eqp = None
            best_start = datetime.max
            if eqp_ids == ["-"]:
                candidate = ready_time
                if candidate + timedelta(minutes=ct) <= prefix_deadline:
                    best_start = candidate
                    best_eqp = "-"
            else:
                _conflicts = _eqp_conflict_scores(eqp_ids, lot, lot_state, qtime_by_product, flow_map)
                for eqp_id in eqp_ids:
                    if eqp_id in special_eqp_map and special_eqp_map[eqp_id].together:
                        can_use, adj_time = _compute_batch_slot(
                            eqp_id, lot, step, ct, ready_time, special_eqp_map[eqp_id],
                            lot_state, special_lot_step_lookup, ct_lookup,
                            eqp_batch_state, priority_wait_map,
                            wait_window=BATCH_WAIT_WINDOW,
                            cur_time=cur_time, machine_intervals=machine_intervals, special_eqp_map=special_eqp_map,
                            lot_entries=lot_entries)
                        if not can_use:
                            continue
                        check_time = adj_time
                        avail = check_time
                    else:
                        if eqp_id in special_eqp_map:
                            can_use, adj_time = _check_special_eqp_available(
                                eqp_id, lot.qty, ready_time, ct,
                                special_eqp_map, eqp_batch_state, machine_intervals)
                            if not can_use:
                                if adj_time > state["ready_time"]:
                                    state["ready_time"] = adj_time
                                    state["_base_ready_time"] = adj_time
                                continue
                            ready_time = max(ready_time, adj_time)
                        check_time = ready_time
                        if _is_parallel_eqp(eqp_id, special_eqp_map):
                            avail = check_time
                        else:
                            avail = _find_earliest_slot(
                                machine_intervals.get(eqp_id, []),
                                check_time,
                                timedelta(minutes=ct))
                    if avail + timedelta(minutes=ct) <= prefix_deadline:
                        _c = _conflicts.get(eqp_id, 0)
                        if (avail < best_start) or (avail == best_start and best_eqp is not None
                                                    and _c < _conflicts.get(best_eqp, 0)):
                            best_start = avail
                            best_eqp = eqp_id

            if best_eqp is None:
                state["chain_reverse_pending"] = {
                    "chain_start_step": suffix_steps[0].step_name,
                    "chain_end_step": chain_info["chain_end_step"],
                    "chain_steps": suffix_steps,
                    "deadline": deadline,
                }
                return False, scheduled_count

            start_time = best_start
            _ca = state.get("coarse_anchors", [])
            if _ca and i < len(_ca) and _ca[i] > start_time:
                start_time = _ca[i]
            end_time = start_time + timedelta(minutes=ct)
            start_time, end_time = _apply_manual_adjust(
                lot.lot_name, step.step_name, start_time, end_time, ct, manual_adjust_lookup,
                pin_lookup=pin_lookup)
            start_time = _resolve_constraints(
                start_time, ct, best_eqp,
                machine_intervals, shift_change_intervals,
                step_windows, end_windows,
                step.step_name, max_iterations=resolve_max_iterations)
            if start_time == datetime.max:
                state["chain_reverse_pending"] = {
                    "chain_start_step": suffix_steps[0].step_name,
                    "chain_end_step": chain_info["chain_end_step"],
                    "chain_steps": suffix_steps,
                    "deadline": deadline,
                }
                return False, scheduled_count

            end_time = start_time + timedelta(minutes=ct)
            start_time, end_time = _apply_manual_adjust(
                lot.lot_name, step.step_name, start_time, end_time, ct, manual_adjust_lookup,
                pin_lookup=pin_lookup, reapply=True)

            product_qtimes_local = qtime_by_product.get(lot.product_name, [])
            _check_qtime_start(state, step.step_name, product_qtimes_local, start_time, end_time)
            qtime_risk = _check_qtime_end_for_step(
                state, step.step_name, product_qtimes_local,
                start_time, end_time, lot.lot_name, qtime_alerts)

            _record_step_entry(
                lot, step, best_eqp, start_time, end_time, ct, qtime_risk,
                lot_entries, eqp_entries)

            if best_eqp != "-":
                _is_par = _is_parallel_eqp(best_eqp, special_eqp_map)
                _is_tog = best_eqp in special_eqp_map and special_eqp_map[best_eqp].together
                if not _is_par and not _is_tog:
                    _add_machine_interval(machine_intervals, best_eqp, (start_time, end_time))
                machine_available[best_eqp] = end_time
                if best_eqp in special_eqp_map:
                    _register_special_eqp_usage(
                        best_eqp, lot.lot_name, lot.qty, start_time, end_time,
                        special_eqp_map, eqp_batch_state)

            _release_refs_for_step(
                lot.lot_name, step.step_name, end_time,
                reference_deps, lot_state, pending_refs, ref_release_times, shift_times)

            state["step_index"] = i + 1
            scheduled_count += 1

            if state["step_index"] >= len(remaining):
                state["done"] = True
                # 后缀没有被调度，标记为待反向调度
                state["chain_reverse_pending"] = {
                    "chain_start_step": suffix_steps[0].step_name,
                    "chain_end_step": chain_info["chain_end_step"],
                    "chain_steps": suffix_steps,
                    "deadline": deadline,
                }
                return True, scheduled_count

            step_wait = _effective_chain_wait(lot, chain_info, priority_wait_map)
            state["ready_time"] = end_time + timedelta(minutes=step_wait)
            state["_base_ready_time"] = state["ready_time"]

        # 3. 提交后缀步骤（使用预计算的时间）
        for idx in range(len(suffix_steps)):
            step = suffix_steps[idx]
            ct = get_step_ct(ct_lookup, step.product_name, step.step_number, lot.qty)
            if special_lot_step_lookup:
                sls_key = (lot.lot_name, step.step_name)
                if sls_key in special_lot_step_lookup:
                    sls = special_lot_step_lookup[sls_key]
                    if sls.special_ct is not None:
                        ct = sls.special_ct
            start_time = rev_starts[idx]
            end_time = rev_ends[idx]
            eqp = rev_eqps[idx]

            start_time, end_time = _apply_manual_adjust(
                lot.lot_name, step.step_name, start_time, end_time, ct, manual_adjust_lookup,
                pin_lookup=pin_lookup)
            resolved = _resolve_constraints(
                start_time, ct, eqp,
                machine_intervals, shift_change_intervals,
                step_windows, end_windows,
                step.step_name, max_iterations=resolve_max_iterations)
            if resolved == datetime.max:
                resolved = start_time
            start_time = resolved
            end_time = start_time + timedelta(minutes=ct)
            start_time, end_time = _apply_manual_adjust(
                lot.lot_name, step.step_name, start_time, end_time, ct, manual_adjust_lookup,
                pin_lookup=pin_lookup, reapply=True)

            product_qtimes_local = qtime_by_product.get(lot.product_name, [])
            _check_qtime_start(state, step.step_name, product_qtimes_local, start_time, end_time)
            qtime_risk = _check_qtime_end_for_step(
                state, step.step_name, product_qtimes_local,
                start_time, end_time, lot.lot_name, qtime_alerts)

            _record_step_entry(
                lot, step, eqp, start_time, end_time, ct, qtime_risk,
                lot_entries, eqp_entries)

            if eqp != "-":
                _is_par = _is_parallel_eqp(eqp, special_eqp_map)
                _is_tog = eqp in special_eqp_map and special_eqp_map[eqp].together
                if not _is_par and not _is_tog:
                    _add_machine_interval(machine_intervals, eqp, (start_time, end_time))
                machine_available[eqp] = end_time
                if eqp in special_eqp_map:
                    _register_special_eqp_usage(
                        eqp, lot.lot_name, lot.qty, start_time, end_time,
                        special_eqp_map, eqp_batch_state)

            _release_refs_for_step(
                lot.lot_name, step.step_name, end_time,
                reference_deps, lot_state, pending_refs, ref_release_times, shift_times)

            for j, s in enumerate(remaining):
                if s.step_name == step.step_name and j >= state["step_index"]:
                    state["step_index"] = j + 1
                    break
            scheduled_count += 1

            if state["step_index"] >= len(remaining):
                state["done"] = True
                return True, scheduled_count

            if idx < len(suffix_steps) - 1:
                step_wait = _effective_chain_wait(lot, chain_info, priority_wait_map)
                state["ready_time"] = end_time + timedelta(minutes=step_wait)
                state["_base_ready_time"] = state["ready_time"]

        return True, scheduled_count

    # ---- 无阻塞点：正向调度整条链 ----
    scheduled_count = 0

    # 整链块调度（方法B）先手：把整条无阻塞 Q-time 链作为一块整体做"倒排+整链后移+Q-time
    # 校验"。仅当链首确实处于该 lot 的当前前沿（无未排前序步骤）时启用，避免破坏 lot 内部
    # 步骤顺序；链首下限取"本 lot 上一步真实结束时间 + 步间等待"（即 state["ready_time"]），
    # 保证不早于本 lot 就绪。注意不能用 lot_entries[-1]（全局最后一条）：它属于其它 Lot，
    # 会把链首强行推迟到别的 Lot 的最新结束时刻——例如 real1.FC-REFLOW（PKCON001，本可
    # 12:17 开工）被 PC1.UF-PLASMA(04:26) 拖到次日凌晨，整链级联后移、reference 释放变晚、
    # 后续紧 Q-time 超时。
    _frontier_ok = (step_idx == chain_range[0]) and not os.environ.get("NO_WCB")
    _first_ready = state["ready_time"]
    _plan = None
    if _frontier_ok:
        _plan = _precompute_whole_chain_block(
            lot, remaining, chain_range, step_idx, ct_lookup,
            special_lot_step_lookup, qtime_by_product, machine_intervals, machine_available,
            special_eqp_map, shift_change_intervals, step_windows, end_windows,
            manual_adjust_lookup, priority_wait_map,
            ref_release_times, pending_refs.get(lot.lot_name, set()), ref_block_info,
            state.get("coarse_anchors", []), resolve_max_iterations,
            _first_ready, ref_release_forecast=ref_release_forecast,
            cycle_forecast_keys=cycle_forecast_keys, eqp_preferences=eqp_preferences)

    # ---- 紧链整链块失败：不可回退单步（会散开超 Q），延迟 ready_time 重试 ----
    # 整链块调度失败说明当前时间点设备/约束不足以把整条链紧凑放下。若回退单步贪婪，
    # 链首（如 BAKE）被过早排好，中段抢不到 DISPENSE/PLASMA 等设备，端步骤（CURE）被
    # 推出 Q-time 预算（test09 退化根因）。这里参照 baseline：对紧链计算链内设备最早
    # 可用时间，把 ready_time 延迟到 max(最早可用, +30min)，等待设备释放后整链重试。
    # 特殊设备链走单步批处理路径（_precompute 提前返回 None），不在此处 defer。
    if _frontier_ok and _plan is None and is_tight:
        _any_special = False
        for _sn in chain_steps_names:
            _s = next((s for s in remaining if s.step_name == _sn), None)
            if _s is not None and _s.eqp_ids:
                if any(e in special_eqp_map for e in _s.eqp_ids):
                    _any_special = True
                    break
        if not _any_special:
            _chain_eqp_ids: set = set()
            for _sn in chain_steps_names:
                _s = next((s for s in remaining if s.step_name == _sn), None)
                if _s is not None and _s.eqp_ids:
                    _chain_eqp_ids.update(_s.eqp_ids)
            _ea = max((machine_available.get(e, state["ready_time"]) for e in _chain_eqp_ids),
                      default=state["ready_time"])
            # 终止保护：连续 defer 超上限（如单机瓶颈长期放不下整链块）时不再 defer，
            # 直接退回下方逐步骤调度（接受可能出现的 Q-time 短时超限告警，换取停止挂死）。
            state["_tight_chain_defers"] = state.get("_tight_chain_defers", 0) + 1
            if state["_tight_chain_defers"] <= TIGHT_CHAIN_DEFER_MAX:
                _defer = max(_ea, state["ready_time"] + timedelta(minutes=30))
                if os.environ.get("SCHED_TRACE") == "1":
                    print(f"[TDEFER] {lot.lot_name} {chain_steps_names[0]} ready={state['ready_time']} "
                          f"ea={_ea} defer={_defer} eqps={sorted(_chain_eqp_ids)}")
                state["ready_time"] = _defer
                state["_base_ready_time"] = _defer
                state["_tight_chain_defer"] = True
                return False, 0
            if os.environ.get("SCHED_TRACE") == "1":
                print(f"[TDEFER-FORCE] {lot.lot_name} {chain_steps_names[0]} defers={state['_tight_chain_defers']} "
                      f"ea={_ea} eqps={sorted(_chain_eqp_ids)} -> 退单步以保证终止")

    for i in chain_range:
        if i < step_idx:
            continue

        step = remaining[i]
        ct = get_step_ct(ct_lookup, step.product_name, step.step_number, lot.qty)

        if special_lot_step_lookup:
            sls_key = (lot.lot_name, step.step_name)
            if sls_key in special_lot_step_lookup:
                sls = special_lot_step_lookup[sls_key]
                if sls.special_ct is not None:
                    ct = sls.special_ct

        if i == 0 and state.get("first_step_ct_adj", 0) > 0:
            ct = max(0, ct - state["first_step_ct_adj"])

        # ---- 检查 reference 阻塞：当前步骤在待释放的 reference 阻塞点之后，停止链式调度 ----
        # 整链块计划已覆盖的步骤除外（方法B已用 release 时刻作下界锚点校验过）。
        if _plan is not None and i in _plan:
            pass
        elif state.get("refs_registered"):
            _pending = pending_refs.get(lot.lot_name, set())
            for _ref_key in _pending:
                _block_idx = ref_block_info.get(_ref_key)
                if _block_idx is not None and i >= _block_idx:
                    # 有预测锚点则拆链紧凑排；否则交给单步调度（会执行 _qtime_hold/等待）
                    if ref_release_forecast and _ref_key in ref_release_forecast:
                        # 退回预扫描拆链路径（用预测锚点）
                        return False, scheduled_count
                    return False, scheduled_count

        eqp_ids = list(step.eqp_ids) if step.eqp_ids else ["-"]
        if special_lot_step_lookup:
            sls_key = (lot.lot_name, step.step_name)
            if sls_key in special_lot_step_lookup:
                sls = special_lot_step_lookup[sls_key]
                if sls.special_eqp:
                    eqp_ids = list(sls.special_eqp)
        eqp_ids = _reorder_eqp_ids_by_preference(eqp_ids, lot.lot_name, step.step_name,
                                                 eqp_preferences)

        ready_time = state["ready_time"] if scheduled_count == 0 else (
            lot_entries[-1].end_time if lot_entries else state["ready_time"]
        )
        # 引用释放下界（已释放的 reference）：链内逐步骤回退路径同样需钳制
        # 释放时刻（与 2157 处前缀循环一致），否则链中间步骤被 lot_entries[-1]
        # 提前放行 → reference 违背。
        if state.get("refs_registered"):
            for _rk6, _bix6 in ref_block_info.items():
                if _bix6 is not None and i >= _bix6:
                    _rv6 = ref_release_times.get(lot.lot_name, {}).get(_rk6)
                    if _rv6 is not None and _rv6 != FAR_FUTURE and _rv6 > ready_time:
                        ready_time = _rv6

        best_eqp = None
        best_start = datetime.max
        _planned = _plan.get(i) if _plan else None
        if _planned is not None:
            # 整链块计划：直接沿用其精确槽位（已完成设备/约束/Q-time 校验）
            best_eqp, best_start, _end_plan = _planned
        elif eqp_ids == ["-"]:
            best_eqp = "-"
            best_start = ready_time
        else:
            _conflicts = _eqp_conflict_scores(eqp_ids, lot, lot_state, qtime_by_product, flow_map)
            for eqp_id in eqp_ids:
                if eqp_id in special_eqp_map and special_eqp_map[eqp_id].together:
                    # 恒组批（together=true）：批次统一槽位（等待凑批/加入已开批次）
                    can_use, adj_time = _compute_batch_slot(
                        eqp_id, lot, step, ct, ready_time, special_eqp_map[eqp_id],
                        lot_state, special_lot_step_lookup, ct_lookup,
                        eqp_batch_state, priority_wait_map,
                        wait_window=BATCH_WAIT_WINDOW,
                        cur_time=cur_time, machine_intervals=machine_intervals, special_eqp_map=special_eqp_map,
                        lot_entries=lot_entries)
                    if not can_use:
                        continue
                    avail = adj_time
                else:
                    if eqp_id in special_eqp_map:
                        can_use, adj_time = _check_special_eqp_available(
                            eqp_id, lot.qty, ready_time, ct,
                            special_eqp_map, eqp_batch_state, machine_intervals)
                        if not can_use:
                            if adj_time > state["ready_time"]:
                                state["ready_time"] = adj_time
                                state["_base_ready_time"] = adj_time
                            continue
                        ready_time = max(ready_time, adj_time)
                    if _is_parallel_eqp(eqp_id, special_eqp_map):
                        # 并行型特殊设备（together=false）：到点即入，不需互斥排他（容量限制已由
                        # _check_special_eqp_available 校验），直接以就绪时刻为开始时间，避免被
                        # machine_intervals 里并行 Lot 的占用区间串行化。
                        avail = ready_time
                    else:
                        avail = _find_earliest_slot(
                            machine_intervals.get(eqp_id, []),
                            ready_time,
                            timedelta(minutes=ct))
                _c = _conflicts.get(eqp_id, 0)
                if (avail < best_start) or (avail == best_start and best_eqp is not None
                                            and _c < _conflicts.get(best_eqp, 0)):
                    best_start = avail
                    best_eqp = eqp_id

        if best_eqp is None:
            return False, scheduled_count

        start_time = best_start
        # 粗排程锚点下限（已由整链块计划计入时不重复强制）
        _ca = state.get("coarse_anchors", [])
        if _planned is None and _ca and i < len(_ca) and _ca[i] > start_time:
            start_time = _ca[i]
        end_time = start_time + timedelta(minutes=ct)

        # ---- 紧 Q-time 窗口前瞻：延迟起点，避免端步骤超限（整链块计划步已在校验中处理）----
        prod_qtimes = qtime_by_product.get(lot.product_name, [])
        if prod_qtimes and _planned is None:
            qh = _tight_qtime_target_start(
                lot, state, step, ct, prod_qtimes, state["ready_time"],
                start_time,
                machine_available, machine_intervals,
                pending_refs, ref_release_times, special_lot_step_lookup, ct_lookup,
                shift_change_intervals, step_windows, end_windows, priority_wait_map,
                state.get("ref_release_forecast"),
                manual_adjust_lookup=manual_adjust_lookup, pin_lookup=pin_lookup)
            if isinstance(qh, tuple) and qh[0] == "DEFER":
                # 端步骤 reference 未释放：推迟整个链，交给单步调度处理
                return False, scheduled_count
            if isinstance(qh, datetime) and qh > start_time:
                start_time = qh
                end_time = start_time + timedelta(minutes=ct)

        start_time, end_time = _apply_manual_adjust(
            lot.lot_name, step.step_name, start_time, end_time, ct, manual_adjust_lookup,
            pin_lookup=pin_lookup)
        start_time = _resolve_constraints(
            start_time, ct, best_eqp,
            machine_intervals, shift_change_intervals,
            step_windows, end_windows,
            step.step_name, max_iterations=resolve_max_iterations)
        if start_time == datetime.max:
            return False, scheduled_count

        end_time = start_time + timedelta(minutes=ct)
        start_time, end_time = _apply_manual_adjust(
            lot.lot_name, step.step_name, start_time, end_time, ct, manual_adjust_lookup,
            pin_lookup=pin_lookup, reapply=True)

        product_qtimes_local = qtime_by_product.get(lot.product_name, [])
        _check_qtime_start(state, step.step_name, product_qtimes_local, start_time, end_time)
        qtime_risk = _check_qtime_end_for_step(
            state, step.step_name, product_qtimes_local,
            start_time, end_time, lot.lot_name, qtime_alerts)

        _record_step_entry(
            lot, step, best_eqp, start_time, end_time, ct, qtime_risk,
            lot_entries, eqp_entries)

        if best_eqp != "-":
            _is_par = _is_parallel_eqp(best_eqp, special_eqp_map)
            _is_tog = best_eqp in special_eqp_map and special_eqp_map[best_eqp].together
            if not _is_par and not _is_tog:
                # 并行型（together=false）与恒组批（together=true）特殊设备：批次内多条
                # 占用区间相互重叠是正常且允许的，不写入排他 machine_intervals（否则会把
                # 同批 Lot 误判为相互阻塞/重叠违规）。
                _add_machine_interval(machine_intervals, best_eqp, (start_time, end_time))
            machine_available[best_eqp] = end_time
            if best_eqp in special_eqp_map:
                _register_special_eqp_usage(
                    best_eqp, lot.lot_name, lot.qty, start_time, end_time,
                    special_eqp_map, eqp_batch_state)

        _release_refs_for_step(
            lot.lot_name, step.step_name, end_time,
            reference_deps, lot_state, pending_refs, ref_release_times, shift_times)

        state["step_index"] = i + 1
        scheduled_count += 1

        if state["step_index"] >= len(remaining):
            state["done"] = True
            return True, scheduled_count

        step_wait = _effective_chain_wait(lot, chain_info, priority_wait_map)
        state["ready_time"] = end_time + timedelta(minutes=step_wait)
        state["_base_ready_time"] = state["ready_time"]

    return True, scheduled_count


# ============================================================
# 粗排程：约束关系锚点传播（先确定约束导致的先后关系）
# ============================================================

def _effective_running_ct(ct_lookup, special_lot_step_lookup, lot, step):
    """计算当前步骤的“已运行抵扣”（分钟）。

    规则（running_time 单位为分钟）：
      - 只有填了 running_time 才抵扣；
      - 若 running_time >= 当前步骤 CT（已做超时或数据错误），按 0 处理（不抵扣、不做负时间）。
    """
    if not lot.running_time:
        return 0.0
    ct = get_step_ct(ct_lookup, step.product_name, step.step_number, lot.qty)
    if special_lot_step_lookup:
        sls_key = (lot.lot_name, step.step_name)
        if sls_key in special_lot_step_lookup:
            sls = special_lot_step_lookup[sls_key]
            if sls.special_ct is not None:
                ct = sls.special_ct
    if lot.running_time >= ct:
        return 0.0
    return float(lot.running_time)


def _coarse_earliest_anchors(
    lots: list,
    flow_map: dict,
    ct_lookup: dict,
    special_lot_step_lookup: dict,
    priority_wait_map: dict,
    schedule_start: datetime,
    qtimes: list = None,
    manual_adjusts: list = None,
    eqp_constraints: list = None,
    shift_times: Optional[list] = None,
) -> dict[str, list[datetime]]:
    """粗排程第一遍：忽略设备竞争，只按 constraints 计算每个 lot 每个 step 的
    "最早可行开始时间"锚点。

    传播规则：
    - 自然推进：step[i] >= lot_ready + Σ(之前步骤 CT+wait)
    - reference：lot 的 start_step 不得早于 reference 步骤结束时间(+start_mod 偏移)；
      一旦某 step 被 reference 推迟，其后所有步骤整体平移（保持内部紧凑）。
    - 紧 Q-time 链：链尾被 reference 推迟时，链首及链内步骤整体后移，
      保证整条链紧凑（不因链首过早排而拉长 Q-time）。
    - 手动延迟：手动延后的 step 作为该 step 及之后步骤的硬性最早锚点，
      再沿 reference 网级联传播给相互引用的 lot（保证跨 lot 链整体平移）。
    - 设备停机窗（eqp_constraints，Fix2）：步骤所用设备若在预估开始时处于停机窗
      （如 PKCON/PKFCV 22:00-08:30），把该步及其后步骤整体推到窗口结束之后，
      使粗排锚点与实际设备可用时间对齐。

    返回 {lot_name: [每个 remaining step 的最早开始时间]}
    """
    from data_loader import get_step_index_in_flow
    anchors: dict[str, list[datetime]] = {}
    lot_by_name = {l.lot_name: l for l in lots}

    # 设备停机窗：{eqp: [(start,end)]}，用于把落在停机窗的步骤锚点推到窗口结束之后
    down_map = _expand_eqp_constraints(eqp_constraints or [], schedule_start)
    # shift_times 用于 start_mod="shift"/"shift_day" 的 reference 释放计算。
    # 缺省给一个默认班次，避免空列表导致 _next_shift_after/_next_morning_shift 越界。
    shift_times = shift_times or [(8, 30)]

    def _resolve_eqp_downtime(_eqps: list, _t: datetime) -> datetime:
        """若 _t 落在任一可用设备的停机窗内，推到窗口结束；返回最早的合法开始时刻。"""
        if _eqps == ["-"]:
            return _t
        low = _t
        while True:
            blocked = False
            for _eid in _eqps:
                for _ws, _we in down_map.get(_eid, []):
                    if _ws <= low < _we:
                        low = _we
                        blocked = True
            if not blocked:
                return low

    # 手动延迟锚：{(lot, step): delay_to}
    manual_delay_map: dict[tuple[str, str], datetime] = {}
    if manual_adjusts:
        for ma in manual_adjusts:
            if not (ma.lot_name and ma.step_name and ma.delay_to):
                continue
            key = (ma.lot_name, ma.step_name)
            if key not in manual_delay_map or ma.delay_to > manual_delay_map[key]:
                manual_delay_map[key] = ma.delay_to

    # ---- 0. Q-time 链信息（用于链内步间等待分摊） ----
    qtime_chain_map = _build_qtime_chains(flow_map, qtimes or [])

    # ---- 1. 自然推进（仅 CT + wait，无设备竞争） ----
    for lot in lots:
        product_flow = flow_map.get(lot.product_name)
        if not product_flow:
            continue
        try:
            cur_idx = get_step_index_in_flow(product_flow, lot.current_step_name)
        except ValueError:
            cur_idx = 0
        remaining = product_flow[cur_idx:]
        # 第一步的开始时间不因 running_time 推迟（running 中的 step 已在跑，从当前时刻算起）；
        # running_time 只用于缩短第一步的剩余 CT（见下方 i==0 的 ct - eff），
        # 即计算“当前正在 running 的 lot 何时结束”，从而决定下一个 step 的开始时间。
        ready = lot.start_time if lot.start_time is not None else schedule_start
        lst = []
        t = ready
        product_cmap = qtime_chain_map.get(lot.product_name, {})
        for i, s in enumerate(remaining):
            ct = get_step_ct(ct_lookup, s.product_name, s.step_number, lot.qty)
            if special_lot_step_lookup:
                sls_key = (lot.lot_name, s.step_name)
                if sls_key in special_lot_step_lookup:
                    sls = special_lot_step_lookup[sls_key]
                    if sls.special_ct is not None:
                        ct = sls.special_ct
            if i == 0 and lot.running_time:
                eff = _effective_running_ct(ct_lookup, special_lot_step_lookup, lot, s)
                ct = max(0.0, ct - eff)
            # Fix2：设备停机窗。若步骤预估开始时刻落在其设备的停机窗内，推到窗口结束，
            # 使粗排锚点与实际设备可用时间对齐（如 PKCON/PKFCV 22:00-08:30 停机，
            # 落到当晚的步骤被推到次日 08:30，real1.BAKE 锚点≈实际）。
            if down_map:
                _eqps = list(getattr(s, "eqp_ids") or ["-"]) if getattr(s, "eqp_ids", None) else ["-"]
                if special_lot_step_lookup:
                    _sls_key = (lot.lot_name, s.step_name)
                    _sls = special_lot_step_lookup.get(_sls_key)
                    if _sls is not None and _sls.special_eqp:
                        _eqps = list(_sls.special_eqp)
                if _eqps != ["-"]:
                    t = _resolve_eqp_downtime(_eqps, t)
            lst.append(t)
            # 链内步间等待受 Q-time 预算限制（避免把链拉长到超 Q）
            wait = get_step_wait_time(lot.priority[0], lot.priority[1], priority_wait_map)
            cinfo = product_cmap.get(s.step_name)
            if cinfo:
                wait = _effective_chain_wait(lot, cinfo, priority_wait_map)
            t += timedelta(minutes=ct + wait)
        anchors[lot.lot_name] = lst

    # ---- 2. reference 传播（松弛迭代直到稳定） ----
    refs = []
    for lot in lots:
        for r in lot.references or []:
            if r.reference_lot and r.start_step:
                refs.append((lot.lot_name, r))

    # 检测 reference 依赖环（如 IOU1↔IOU1-f 互相等待对方某步）。
    # 环内 lot 的锚点互相引用会形成正反馈：A 被 B 推后 → B 又被 A 推后 →
    # 迭代不收敛、锚点雪崩式推迟到未来几天（实测 60 轮推到 09-03，整条 FTF 链
    # 被拖 2 天）。修复：环内 lot 作为 reference 源时，取"自然推进锚点"
    #（第 1 步无引用传播的结果），只传播环外释放时间，打破雪崩。
    _ref_graph: dict = {}
    for _ln, _r in refs:
        _ref_graph.setdefault(_ln, set()).add(_r.reference_lot)
    _cycle_lots: set = set()
    _visited: set = set()
    _stack: list = []

    def _dfs_cycle(n: str):
        if n in _visited:
            return
        _visited.add(n)
        _stack.append(n)
        for _m in _ref_graph.get(n, ()):
            if _m in _stack:  # 找到环
                _idx = _stack.index(_m)
                for _x in _stack[_idx:]:
                    _cycle_lots.add(_x)
            else:
                _dfs_cycle(_m)
        _stack.pop()

    for _n in list(_ref_graph):
        _dfs_cycle(_n)

    def _apply_manual_delay(target: dict = None) -> bool:
        """手动延迟作为硬性最早锚：把该 step 及其后步骤整体平移到 delay_to。
        target 缺省为 anchors（正式传播目标）；传 _natural_anchors 时把手动延迟
        也并入"自然锚点"，保证打破环后手动延迟（硬约束）仍对环外 lot 生效。"""
        if target is None:
            target = anchors
        changed = False
        for (lot_name, step_name), delay in manual_delay_map.items():
            if lot_name not in target:
                continue
            lot_obj = lot_by_name[lot_name]
            lot_flow = flow_map.get(lot_obj.product_name)
            if not lot_flow:
                continue
            try:
                cur_idx = get_step_index_in_flow(lot_flow, lot_obj.current_step_name)
            except ValueError:
                cur_idx = 0
            try:
                m_idx = get_step_index_in_flow(lot_flow, step_name)
            except ValueError:
                continue
            rel = m_idx - cur_idx
            if rel < 0 or rel >= len(target[lot_name]):
                continue
            lst = target[lot_name]
            if delay > lst[rel]:
                shift = delay - lst[rel]
                for j in range(rel, len(lst)):
                    lst[j] = lst[j] + shift
                changed = True
        return changed

    # 自然推进锚点（无引用传播，含自身手动延迟），供环内引用源使用：
    # 环内 lot 的锚点互相引用会形成正反馈（A 被 B 推后 → B 又被 A 推后 → 迭代
    # 不收敛、锚点雪崩式推迟数天）。打破方式：引用源取"自然锚点"（该 lot 自己
    # 能最早到达的时间），只传播环外释放，环内互相等待收敛到各自自然就绪时刻。
    _natural_anchors = {_ln: list(_lst) for _ln, _lst in anchors.items()}
    if manual_delay_map:
        _apply_manual_delay(_natural_anchors)

    def _propagate_refs_once(use_natural_for_cycle: bool = False) -> bool:
        """单轮 reference 传播：被约束 lot 的 start_step 不得早于 reference 释放时刻。
        返回该轮是否发生了变化。
        use_natural_for_cycle=True 时，环内 lot 作为引用源取"自然锚点"（见
        _natural_anchors 说明），用于打破真·循环引用（步骤间直接互相等待）造成的
        雪崩——仅在不动点迭代 60 轮未收敛时才启用（普通场景保持实际锚点传播，
        保证与贪婪排程阶段的实际引用释放时间一致）。"""
        changed = False
        for lot_name, r in refs:
            ref_lot_name = r.reference_lot
            if lot_name not in anchors or ref_lot_name not in anchors:
                continue
            ref_lot = lot_by_name[ref_lot_name]
            ref_flow = flow_map.get(ref_lot.product_name)
            if not ref_flow:
                continue
            ref_step = r.reference_step or ""
            try:
                ref_flow_idx = get_step_index_in_flow(ref_flow, ref_step) if ref_step else None
            except ValueError:
                ref_flow_idx = None
            if ref_flow_idx is None:
                continue
            try:
                ref_cur_idx = get_step_index_in_flow(ref_flow, ref_lot.current_step_name)
            except ValueError:
                ref_cur_idx = 0
            ref_rel = ref_flow_idx - ref_cur_idx
            if ref_rel < 0 or ref_rel >= len(anchors[ref_lot_name]):
                # reference 步骤在当前 lot 剩余流程之前（如已完成）：跳过
                continue
            else:
                if use_natural_for_cycle and ref_lot_name in _cycle_lots:
                    ref_anchor_t = _natural_anchors[ref_lot_name][ref_rel]
                else:
                    ref_anchor_t = anchors[ref_lot_name][ref_rel]
                ref_ct = get_step_ct(ct_lookup, ref_flow[ref_flow_idx].product_name,
                                     ref_flow[ref_flow_idx].step_number, ref_lot.qty)
                release = _ref_release_offset(ref_anchor_t + timedelta(minutes=ref_ct), r.start_mod, shift_times)

            lot_obj = lot_by_name[lot_name]
            lot_flow = flow_map.get(lot_obj.product_name)
            if not lot_flow:
                continue
            try:
                start_idx = get_step_index_in_flow(lot_flow, r.start_step)
            except ValueError:
                continue
            try:
                cur_idx = get_step_index_in_flow(lot_flow, lot_obj.current_step_name)
            except ValueError:
                cur_idx = 0
            rel_start = max(0, start_idx - cur_idx)
            target_list = anchors[lot_name]
            if rel_start >= len(target_list):
                continue
            if release > target_list[rel_start]:
                shift = release - target_list[rel_start]
                for j in range(rel_start, len(target_list)):
                    target_list[j] = target_list[j] + shift
                changed = True
        return changed

    # 初次 reference 传播
    for _ in range(50):
        if not _propagate_refs_once():
            break

    # ---- 2.5 手动延迟级联：先手动平移，再沿 reference 网级联，直到稳定 ----
    # 跨 lot 相互引用（如 PC↔real 的 UF 链）要求相关所有 Q-time 区间 step
    # 作为一个整体锚定，不能把链拆散后单步各自贴最早/最晚槽位。
    # 手动延后的 step 会沿 reference 网级联给相互引用的 lot（如 PC 的 UF-BAKE 被
    # 手动延后 → PC.UF-DISPENSE→real.UF-DISPENSE 整体后移 → real 的 bake 锚点也随之
    # 由整链 Q-time 预算反推后移），避免 real bake 过早、链首与端步骤被拉开超 Q。
    # 仅在存在手动延迟时启动；_apply_manual_delay / _propagate_refs_once 均为
    # 单调递增平移，50 轮内收敛，无振荡风险。
    if manual_delay_map:
        for _ in range(50):
            m_changed = _apply_manual_delay()
            r_changed = _propagate_refs_once()
            if not (m_changed or r_changed):
                break

    # ---- 3. 紧 Q-time 链紧凑传播：仅当链逼近 Q-time 预算上限时，才把链收压 ----
    # 老实现无条件把整条链前缀压成背靠背，会把"链内某步被钉晚"反向传导给链首；
    # 配合跨 lot 循环引用（IOU1↔IOU1-f、PC↔real 的相互等待）形成正反馈雪崩。
    # 现改为：链内有序化总是保证；反向背靠背压实只在"整链时长逼近 min_qtime
    # 预算"时启用（紧链如 UF 240min 需要压实保 Q；FTF 10080min 无需压实，
    # 链首保持自然锚点）。手动延迟按『最早』语义：链尾锚点只作下界。
    def _compact_chain_blocks() -> bool:
        """整链块有序化 + 受 Q-time 预算约束的紧凑压实。

        实现（单向后移，保证不动点收敛）：
        1) 正向顺延：被 reference / 手动延迟钉住的中间步骤会把它之后的步骤带后；
        2) 判定整链总时长是否已逼近 min_qtime（合并后最紧的 Q-time 预算）上限；
        3) 仅当逼近预算时，才做反向背靠背压实：从链尾锚点逐 k 前推
           pos[k] = pos[k+1] - CT[k] - wait[k]，把链重新收进预算；
        4) 再正向顺延一次保持链内有序；链尾后移时，链尾之后的步骤同步平移。
        不逼近预算时保持各步自身锚点，避免把中间步骤的延后反向传导给链首
        （这正是跨 lot 循环引用下"整链被推到未来数天"雪崩的根因）。
        """
        changed = False
        for lot in lots:
            if lot.lot_name not in anchors:
                continue
            cmap = qtime_chain_map.get(lot.product_name, {})
            if not cmap:
                continue
            lst = anchors[lot.lot_name]
            lot_flow = flow_map.get(lot.product_name)
            if not lot_flow:
                continue
            try:
                cur_idx = get_step_index_in_flow(lot_flow, lot.current_step_name)
            except ValueError:
                cur_idx = 0
            for sname, info in cmap.items():
                if not info.get("is_chain_start"):
                    continue
                chain_steps = info["chain_steps"]
                chain_end_name = info["chain_end_step"]
                try:
                    s_idx = get_step_index_in_flow(lot_flow, chain_steps[0])
                    e_idx = get_step_index_in_flow(lot_flow, chain_end_name)
                except ValueError:
                    continue
                s_rel = s_idx - cur_idx
                e_rel = e_idx - cur_idx
                if s_rel < 0 or e_rel >= len(lst):
                    continue
                n = e_rel - s_rel + 1
                cts = []
                waits = []
                for j in range(s_rel, e_rel + 1):
                    step_obj = lot_flow[cur_idx + j]
                    ct = get_step_ct(ct_lookup, step_obj.product_name,
                                     step_obj.step_number, lot.qty)
                    if special_lot_step_lookup:
                        sls_key = (lot.lot_name, step_obj.step_name)
                        if sls_key in special_lot_step_lookup:
                            sls = special_lot_step_lookup[sls_key]
                            if sls.special_ct is not None:
                                ct = sls.special_ct
                    cts.append(ct)
                    if j < e_rel:
                        w = get_step_wait_time(lot.priority[0], lot.priority[1], priority_wait_map)
                        step_cinfo = cmap.get(step_obj.step_name)
                        if step_cinfo:
                            w = _effective_chain_wait(lot, step_cinfo, priority_wait_map)
                        waits.append(w)
                # ---- 整链有序化 + 仅受 Q-time 预算约束的反向压实 ----
                # 顺序：先正向顺延（被 reference / 手动延迟钉住的中间步骤会把它
                # 之后的步骤带后）；再用链的 min_qtime（合并后最紧的 Q-time 预算）
                # 判定"整链是否已被拉长到接近预算上限"——只有此时才把前缀步骤做
                # 背靠背反向压实，把链重新收进预算（PC↔real 的 UF 链 min_qtime=240
                # 属于紧链，需要压实保 Q）。
                # 关键改动：不再无条件把整条链前缀压成背靠背——那会把"链内某步被
                # 钉晚"反向传导给链首（如 FTF 被钉晚 → FTF-OPUT-MOUNT 被拉晚），
                # 在跨 lot 循环引用（IOU1↔IOU1-f 互相等待）下形成正反馈雪崩，
                # 实测整条 FTF 链被推到未来 2 天（09/01 → 09/03）。FTF 链
                # min_qtime=10080（7 天），链内间隙远小于预算，无需压实，
                # MOUNT 保持在自然锚点（不再被动推迟）。
                # 反向压实只作用于"紧 Q-time 段"（max_duration<=TIGHT_CHAIN_THRESHOLD
                # 的规则覆盖的步骤）：merge 后 min_qtime 取的是链内最紧规则的值
                # （如 UF 链 BAKE→DISPENSE 1440 / PLASMA→DISPENSE 240 / DISPENSE→CURE 240
                # → min=240），但它只约束 DISPENSE→CURE 段，BAKE→DISPENSE 有 1440min 余量。
                # 若把整条链都压成背靠背，会把链首（BAKE）人为推迟十多个小时、制造设备
                # 空窗并拖累其它 lot 的就绪调度（实测 PC2 的 UF-BAKE 被从 08/18 12:58
                # 推到 08/19 04:31，导致 PC1 的 UF-CURE 被饿死超 Q 813min）。
                # ---- 预编译：链内 Q-time 段（一次扫描，同时服务"紧段判定"与"预算守卫"）----
                # 每个 qtime 规则在链内定位 start/end 相对索引并解析起/终报告时刻修正
                # （start/end_mod：track in=起点，track out=终点）。一份列表供：
                #   a) _pinnable：反向压实只作用于紧 Q-time 段内的步骤
                #      （max_duration<=TIGHT_CHAIN_THRESHOLD，如 UF 链 PLASMA→DISPENSE
                #       240 / DISPENSE→CURE 240；BAKE→DISPENSE 1440 为宽松段不压实）。
                #   b) 通用 Q 段预算守卫：对任意跨度/任一 Q 模型的超预算做链内后移兜底，
                #      避免反向压实或手动钉晚把某步拉晚、使其前"报告步骤"撑破 Q。
                _qsegs = []
                for _q0 in (qtimes or []):
                    if _q0.product_name != lot.product_name or _q0.max_duration is None:
                        continue
                    _qa = _qb = None
                    for _j in range(n):
                        _sn = lot_flow[cur_idx + s_rel + _j].step_name
                        if _sn == _q0.start_step and _qa is None:
                            _qa = _j
                        if _sn == _q0.end_step:
                            _qb = _j
                    if _qa is None or _qb is None or _qa >= _qb:
                        continue
                    _budget = float(_q0.max_duration)
                    _sm = (_q0.start_mod or "track in").strip()
                    _em = (_q0.end_mod or "track out").strip()
                    _qsegs.append({
                        "start_j": _qa,
                        "end_j": _qb,
                        "a_off": cts[_qa] if _sm == "track out" else 0.0,
                        "b_off": cts[_qb] if _em == "track out" else 0.0,
                        "budget": _budget,
                        "is_tight": _budget <= TIGHT_CHAIN_THRESHOLD,
                    })
                _qsegs.sort(key=lambda x: x["end_j"], reverse=True)  # end 相对索引降序

                def _pinnable(_k: int) -> bool:
                    # 步骤 _k 是否处于某条紧 Q-time 规则覆盖区间内（start<=k<end）
                    return any(s["is_tight"] and s["start_j"] <= _k < s["end_j"] for s in _qsegs)

                pos = list(lst[s_rel:e_rel + 1])
                # 1) 正向顺延：保证链内有序
                for k in range(1, n):
                    need = pos[k - 1] + timedelta(minutes=cts[k - 1] + waits[k - 1])
                    if pos[k] < need:
                        pos[k] = need
                # 2) 整链总时长是否逼近 min_qtime 预算（含余量：max(预算×%, 下限)）
                chain_budget = float(info.get("min_qtime") or 0)
                total_dur = (pos[n - 1] - pos[0]).total_seconds() / 60.0
                _pull_margin = max(chain_budget * QTIGHT_SAFETY_MARGIN / 100.0,
                                   QTIGHT_MIN_MARGIN)
                need_pull = (chain_budget > 0
                             and total_dur > chain_budget - _pull_margin)
                # 3) 反向背靠背压实（仅当整链逼近预算上限时；后移只缩小链内间隙，
                #    不违反任何规则，也不会把"钉晚"反向传导给链首）。
                #    只压实紧 Q-time 段内的步骤：宽松段（如 BAKE→DISPENSE 1440min）
                #    保持自身锚点，避免人为推迟链首、浪费设备空窗。
                if need_pull:
                    for k in range(n - 2, -1, -1):
                        if not _pinnable(k):
                            continue
                        bk = pos[k + 1] - timedelta(minutes=cts[k] + waits[k])
                        if bk > pos[k]:
                            pos[k] = bk
                # 3.5) 通用 Q 段预算守卫：复用上面一次预编译的 _qsegs，对每段
                #    若实际用时（q_end - q_start）超预算，把其前序"报告步骤"
                #    pos[start_j] 整体后移以满足预算。仅真实超预算时触发，位移受
                #    预算界约束；按 end 索引降序处理，段间位移向链首自然级联，不雪崩。
                #    q_end - q_start = pos[b]+b_off - (pos[a]+a_off) <= budget
                #    => pos[a] >= pos[b] + b_off - a_off - budget
                for _s in _qsegs:
                    _lo = pos[_s["end_j"]] + timedelta(minutes=_s["b_off"] - _s["a_off"] - _s["budget"])
                    if pos[_s["start_j"]] < _lo:
                        pos[_s["start_j"]] = _lo
                # 4) 再次正向顺延：被压实/后移步骤可能顶动其后步骤
                for k in range(1, n):
                    need = pos[k - 1] + timedelta(minutes=cts[k - 1] + waits[k - 1])
                    if pos[k] < need:
                        pos[k] = need
                # 链尾后移：其后步骤同步平移，保持 lot 内顺序
                end_delta = None
                if pos[n - 1] > lst[e_rel]:
                    end_delta = pos[n - 1] - lst[e_rel]
                # 只增不减地应用
                applied = False
                for k in range(n):
                    if pos[k] > lst[s_rel + k]:
                        lst[s_rel + k] = pos[k]
                        applied = True
                if end_delta is not None:
                    for j in range(e_rel + 1, len(lst)):
                        lst[j] = lst[j] + end_delta
                    applied = True
                if applied:
                    changed = True
        return changed

    # ---- 2.5 + 3 不动点迭代：手动延迟 / reference 传播 / 链块压实 单调前移直至收敛 ----
    # 若 60 轮未收敛，说明存在"真·循环引用"（如 A.FTF 等 B.FTF 结束、B.FTF 又等
    # A.FTF 结束，步骤间直接互相等待），互相推后无下界 → 雪崩。此时启用回退：
    # 环内 lot 锚点重置为自然锚点、引用源改取自然锚点重新迭代，打破雪崩
    # （环外释放时间仍正常传播；该回退同时被 schedule() 的合理性检测识别并告警）。
    def _fixed_point(use_natural_for_cycle: bool) -> bool:
        for _it in range(60):
            _fp_changed = False
            if manual_delay_map:
                _fp_changed |= _apply_manual_delay()
            _fp_changed |= _propagate_refs_once(use_natural_for_cycle)
            _fp_changed |= _compact_chain_blocks()
            if not _fp_changed:
                return True
        return False

    _converged = _fixed_point(False)
    if not _converged and _cycle_lots:
        # 雪崩回退：环内 lot 复位到自然锚点后重跑
        for _ln in list(_cycle_lots):
            if _ln in anchors:
                anchors[_ln] = list(_natural_anchors[_ln])
        _fixed_point(True)

    # 供 schedule() 做智能告警
    _anchor_audit["cycle_lots"] = set(_cycle_lots)
    _anchor_audit["fallback_used"] = (not _converged and bool(_cycle_lots))
    return anchors


def _detect_schedule_anomalies(
    le: list,
    lots: list,
    flow_map: dict,
    ct_lookup: dict,
    priority_wait_map: dict,
    anchor_audit: dict = None,
) -> list[str]:
    """排程结果"合理性"智能检测，返回人类可读的告警列表。

    识别以下不合理情形（不改变排程结果，只做告警）：
    1. lot_constraints 存在引用环（A↔B 互相等待）——互相等待可能被迭代无限推迟；
    2. 引用环雪崩已发生并被"自然锚点回退"自动打破（粗排程 60 轮未收敛）；
    3. 引用步骤在源流程中不存在（数据错误 → 引用永不释放 → 必然死锁）。
    """
    warnings: list[str] = []

    # ---- 1/2. 引用环与雪崩回退（数据级 + 粗排程审计） ----
    ref_graph: dict[str, set[str]] = {}
    for lot in lots:
        for r in lot.references or []:
            if r.reference_lot and r.start_step and not r.lead_id:  # lead 内部边不进引用环
                ref_graph.setdefault(lot.lot_name, set()).add(r.reference_lot)
    _visited: set[str] = set()
    _stack: list[str] = []
    _cycle_lots: set[str] = set()

    def _dfs(n: str):
        if n in _visited:
            return
        _visited.add(n)
        _stack.append(n)
        for m in ref_graph.get(n, ()):
            if m in _stack:
                _idx = _stack.index(m)
                for x in _stack[_idx:]:
                    _cycle_lots.add(x)
            else:
                _dfs(m)
        _stack.pop()

    for n in list(ref_graph):
        _dfs(n)

    if _cycle_lots:
        names = sorted(_cycle_lots)
        warnings.append(
            f"引用环检测：lot_constraints 中 {', '.join(names)} 互相引用（互相等待对方某步），"
            "若不满足收敛条件会被无限推迟；请核对配置是否确实需要互为前置。")
        if (anchor_audit or {}).get("fallback_used"):
            warnings.append(
                f"引用环雪崩已发生：{', '.join(names)} 的相互等待导致锚点被无限推迟，"
                "已自动采用'自然就绪时刻'回退打破雪崩（结果按各 lot 自身最早可达时间排程）。")

    # ---- 3. 引用步骤在源流程中不存在（数据错误 → 引用永不释放 → 必然死锁） ----
    lot_by_name = {l.lot_name: l for l in lots}
    _missing_ref_steps: list[str] = []
    for _lot in lots:
        for _r in _lot.references or []:
            if not (_r.reference_lot and _r.reference_step):
                continue
            _src_lot = lot_by_name.get(_r.reference_lot)
            _src_flow = flow_map.get(_src_lot.product_name) if _src_lot else None
            if not (_src_lot and _src_flow):
                continue
            try:
                get_step_index_in_flow(_src_flow, _r.reference_step)
            except ValueError:
                _missing_ref_steps.append(
                    f"{_lot.lot_name} 引用了 {_r.reference_lot} 的步骤 "
                    f"{_r.reference_step}，但该步骤在 {_src_lot.product_name} 流程中不存在——"
                    "该引用永远不会释放，相关 lot 将被永久阻塞")
    if _missing_ref_steps:
        warnings.append("引用配置错误：" + "；".join(_missing_ref_steps[:3]))

    # ---- 4. lead 数据体检（成环/异构/热启动，设计文档 §3.2） ----
    try:
        from data_loader import health_check_lead
        warnings.extend(health_check_lead(lots, flow_map))
    except Exception:  # 体检失败不影响排程结果
        pass
    return warnings


def _ref_release_offset(ref_end: datetime, start_mod, shift_times):
    """计算 reference 释放时间（含 start_mod 偏移）。"""
    if not start_mod or start_mod in ("", "0"):
        return ref_end
    if start_mod in ("shift",):
        return _next_shift_after(ref_end, shift_times)
    if start_mod in ("shift_day",):
        return _next_morning_shift(ref_end, shift_times)
    try:
        return ref_end + timedelta(hours=float(start_mod))
    except (ValueError, TypeError):
        return ref_end


def _collect_ref_release_forecast(
    lot_entries: list,
    lots: list,
    shift_times: list,
) -> dict[tuple[str, str], datetime]:
    """从一次完整排程的条目中，提取每个 reference 键的实际释放时刻。

    供两遍排程第二遍使用：pending reference 未释放时，
    用第一遍的"预测锚点"避免链首过早调度。
    """
    forecast: dict[tuple[str, str], datetime] = {}
    by_lot_step: dict[tuple[str, str], object] = {}
    for e in lot_entries:
        by_lot_step[(e.lot_name, e.step_name)] = e
    for lot in lots:
        for r in lot.references or []:
            key = (r.reference_lot, r.reference_step or "")
            ref_entry = by_lot_step.get(key)
            if ref_entry is None:
                continue
            release = _ref_release_offset(ref_entry.end_time, r.start_mod, shift_times)
            if key not in forecast or release > forecast[key]:
                forecast[key] = release
    return forecast


def _count_ref_violations(
    lot_entries: list,
    lots: list,
    shift_times: list,
) -> int:
    """统计一次排程结果中 reference 约束违背的数量（供两遍取优比较）。

    由 lots 自带的 references 重建约束列表，复用 validation._check_references。
    """
    from validation import _check_references
    cons = []
    for l in lots:
        for ref in l.references or []:
            # lead 上游对齐引用（lead_id 带 "-u" 后缀）是相位对齐软目标，不计入
            # "闸A/普通引用"硬违背（由 _count_lead_u_violations 单独统计、取优时
            # 置于 Q-time 之后——Q-time 红线优先于对齐软约束）。
            if getattr(ref, "lead_id", "") and str(ref.lead_id).endswith("-u"):
                continue
            cons.append(LotConstraint(
                lot_name=l.lot_name, reference_lot=ref.reference_lot,
                reference_step=ref.reference_step, start_mod=ref.start_mod,
                start_step=ref.start_step, hold_periods=ref.hold_periods))
    if not cons:
        return 0
    return len(_check_references(lot_entries, cons, shift_times or []))


def _count_lead_u_violations(
    lot_entries: list,
    lots: list,
    shift_times: list,
) -> int:
    """统计 lead 上游对齐（-u 软引用）违背数（取优第 5 优先级，排在 Q-time 之后）。"""
    from validation import _check_references
    cons = []
    for l in lots:
        for ref in l.references or []:
            if not (getattr(ref, "lead_id", "") and str(ref.lead_id).endswith("-u")):
                continue
            cons.append(LotConstraint(
                lot_name=l.lot_name, reference_lot=ref.reference_lot,
                reference_step=ref.reference_step, start_mod=ref.start_mod,
                start_step=ref.start_step, hold_periods=ref.hold_periods))
    if not cons:
        return 0
    return len(_check_references(lot_entries, cons, shift_times or []))


def _count_missing_steps(lot_entries, lots, flows) -> int:
    """统计一次排程结果中"缺失步骤"总数（供两遍取优比较）。

    与 validation 的缺失步骤判定一致：从 lot.current_step_name 起、到 target_step
    （无则流程末尾）为止的应有步骤集合，减去实际已排步骤。
    两遍取优时缺失步骤是最硬性指标：丢步骤的排程无论 Q-time 多优都是非法结果
    （且丢步通常会使 Q-time 告警变少——步骤都没排何来超时——会反向骗过择优逻辑）。
    """
    from data_loader import get_step_index_in_flow as _gsi
    flow_map = get_product_flow_map(flows)
    by_lot: dict[str, set] = {}
    for e in lot_entries:
        by_lot.setdefault(e.lot_name, set()).add(e.step_name)
    missing = 0
    for lot in lots:
        pf = flow_map.get(lot.product_name)
        if not pf:
            continue
        try:
            start_idx = _gsi(pf, lot.current_step_name)
        except ValueError:
            continue
        end_idx = len(pf)
        if lot.target_step:
            try:
                end_idx = _gsi(pf, lot.target_step) + 1
            except ValueError:
                pass
        expected = set(s.step_name for s in pf[start_idx:end_idx])
        missing += len(expected - by_lot.get(lot.lot_name, set()))
    return missing


def _chain_ref_anchor(
    state: dict,
    lot: Lot,
    chain_info: dict,
    ref_block_info: dict,
    pending_refs: dict,
    ref_release_times: dict,
) -> tuple[Optional[int], Optional[datetime], set]:
    """查找链内被 reference 阻塞的步骤，并计算其锚点（释放时刻）。

    返回:
      - block_idx: 链内最先被 reference 阻塞的步骤索引（在 remaining 中）
      - anchor:     已释放 reference 的释放时刻（最晚者），未释放则为 None
      - pending_keys: 尚未释放且阻塞链内步骤的 reference 键集合
    """
    remaining = state["remaining_steps"]
    chain_steps = set(chain_info["chain_steps"])
    pending = pending_refs.get(lot.lot_name, set())
    rel = ref_release_times.get(lot.lot_name, {})

    best_idx: Optional[int] = None
    best_anchor: Optional[datetime] = None
    pending_keys: set = set()

    for ref_key, bi in ref_block_info.items():
        if bi is None:
            continue
        # 找到该 reference 阻塞的第一个链内步骤
        for i, s in enumerate(remaining):
            if s.step_name in chain_steps and i >= bi:
                if ref_key in rel:
                    a = rel[ref_key]
                    if best_anchor is None or a > best_anchor:
                        best_anchor = a
                        best_idx = i
                elif ref_key in pending:
                    pending_keys.add(ref_key)
                break
    return best_idx, best_anchor, pending_keys


def _eqp_conflict_scores(
    eqp_ids: list,
    lot: Lot,
    lot_state: dict,
    qtime_by_product: dict,
    flow_map: dict,
) -> dict:
    """计算候选设备被"其他 Lot 未来紧 Q-time 端步骤唯一需要"的冲突分。

    冲突分 = 其他 Lot 尚未完成的、且作为紧 Q-time 端步骤、且只能使用该设备的步骤数。
    用于设备选择时优先避开会被其他 Lot 紧 Q-time 独占的设备。
    """
    scores = {eid: 0 for eid in eqp_ids if eid != "-"}
    if not scores:
        return scores
    for name, st in lot_state.items():
        if name == lot.lot_name or st.get("done"):
            continue
        product = st["lot"].product_name
        qs = qtime_by_product.get(product, [])
        if not qs:
            continue
        # 该 lot 的紧 Q-time 端步骤集合
        tight_ends = {q.end_step for q in qs
                      if q.max_duration is not None and q.max_duration <= TIGHT_CHAIN_THRESHOLD}
        if not tight_ends:
            continue
        remaining = st["remaining_steps"]
        step_idx = st["step_index"]
        for i, s in enumerate(remaining):
            if i < step_idx:
                continue
            if s.step_name not in tight_ends:
                continue
            eids = list(s.eqp_ids) if s.eqp_ids else ["-"]
            eids = [e for e in eids if e != "-"]
            if len(eids) == 1 and eids[0] in scores:
                scores[eids[0]] += 1
    return scores


def _get_chain_deadline(state: dict, chain_info: dict,
                        product_qtimes: list = None) -> datetime:
    """获取链后缀的 Q-time deadline。

    优先从活跃的 Q-time tracker 中查找（end_step 在链内的任一 tracker），
    若找不到则从 Q-time 约束中推断（基于链的 min_qtime）。
    """
    tracker = state.get("qtime_tracker", {})
    chain_steps = set(chain_info.get("chain_steps", []))
    chain_end = chain_info.get("chain_end_step", "")

    # 1. 从活跃 tracker 中查找：任何 end_step 在链内的 tracker
    earliest = FAR_FUTURE
    for t in tracker.values():
        end_step = t.get("end_step", "")
        if end_step in chain_steps and t.get("deadline"):
            if t["deadline"] < earliest:
                earliest = t["deadline"]

    if earliest != FAR_FUTURE:
        return earliest

    # 2. 回退：从 Q-time 约束推断
    # 链的 min_qtime 是合并后的最小 Q-time 时长
    # 若 Q-time 起点步骤已被调度，则 deadline = 起点时刻 + max_duration
    if product_qtimes:
        for q in product_qtimes:
            if q.end_step == chain_end or q.end_step in chain_steps:
                # 查找该 Q-time 的 start_step 是否在 tracker 中
                for t in tracker.values():
                    if t.get("start_step") == q.start_step and t.get("qtime_start"):
                        return t["qtime_start"] + timedelta(minutes=q.max_duration)

    # 3. 仍无法确定：使用链的 min_qtime 作为保守估计
    #    从链中最后一个已调度的步骤时间 + min_qtime 作为 deadline
    min_qtime = chain_info.get("min_qtime", 0)
    if min_qtime > 0 and tracker:
        # 取所有 tracker 中最早的 qtime_start + min_qtime
        for t in tracker.values():
            if t.get("qtime_start") and t.get("end_step") in chain_steps:
                candidate = t["qtime_start"] + timedelta(minutes=min_qtime)
                if candidate < earliest:
                    earliest = candidate
        if earliest != FAR_FUTURE:
            return earliest

    return FAR_FUTURE


def _try_schedule_chain_reverse(
    lot: Lot,
    state: dict,
    flow_map: dict,
    ct_lookup: dict,
    qtime_by_product: dict,
    machine_intervals: dict,
    machine_available: dict,
    special_eqp_map: dict,
    eqp_batch_state: dict,
    special_lot_step_lookup: dict,
    shift_change_intervals: list,
    step_windows: dict,
    end_windows: dict,
    manual_adjust_lookup: dict,
    pin_lookup: dict,
    resolve_max_iterations: int,
    lot_entries: list,
    eqp_entries: list,
    qtime_alerts: list,
    pending_refs: dict,
    ref_block_info: dict,
    shift_times: list,
    reference_deps: dict,
    lot_state: dict,
    ref_release_times: dict,
    priority_wait_map: dict,
    chain_info: Optional[dict] = None,
    eqp_preferences: Optional[dict[tuple[str, str], list[str]]] = None,
) -> bool:
    """从后往前反向调度链后缀步骤。
    从 Q-time deadline 往前倒排，确保后缀步骤紧贴 deadline 之前。
    返回 True 表示全部调度完成。
    """
    reverse_info = state.pop("chain_reverse_pending", None)
    if reverse_info is None:
        return False

    suffix_steps = reverse_info["chain_steps"]
    deadline = reverse_info.get("deadline", FAR_FUTURE)

    if not suffix_steps:
        return True

    remaining = state["remaining_steps"]

    # 使用 _compute_reverse_placement 计算位置
    rev_starts, rev_ends, rev_eqps, _ = _compute_reverse_placement(
        suffix_steps, deadline, lot, ct_lookup, special_lot_step_lookup,
        machine_intervals, shift_change_intervals, step_windows, end_windows,
        manual_adjust_lookup, resolve_max_iterations,
        eqp_preferences=eqp_preferences)

    if rev_starts is None:
        return False

    # 从前往后提交调度（时间已经确定）
    for idx in range(len(suffix_steps)):
        step = suffix_steps[idx]
        ct = get_step_ct(ct_lookup, step.product_name, step.step_number, lot.qty)
        if special_lot_step_lookup:
            sls_key = (lot.lot_name, step.step_name)
            if sls_key in special_lot_step_lookup:
                sls = special_lot_step_lookup[sls_key]
                if sls.special_ct is not None:
                    ct = sls.special_ct
        start_time = rev_starts[idx]
        end_time = rev_ends[idx]
        eqp = rev_eqps[idx]

        # 应用约束
        resolved = _resolve_constraints(
            start_time, ct, eqp,
            machine_intervals, shift_change_intervals,
            step_windows, end_windows,
            step.step_name, max_iterations=resolve_max_iterations)
        if resolved == datetime.max:
            resolved = start_time
        start_time = resolved
        end_time = start_time + timedelta(minutes=ct)

        # 手动调整
        start_time, end_time = _apply_manual_adjust(
            lot.lot_name, step.step_name, start_time, end_time, ct, manual_adjust_lookup,
            pin_lookup=pin_lookup, reapply=True)

        # Q-time 检查
        product_qtimes = qtime_by_product.get(lot.product_name, [])
        _check_qtime_start(state, step.step_name, product_qtimes, start_time, end_time)
        qtime_risk = _check_qtime_end_for_step(
            state, step.step_name, product_qtimes,
            start_time, end_time, lot.lot_name, qtime_alerts)

        # 记录
        _record_step_entry(
            lot, step, eqp, start_time, end_time, ct, qtime_risk,
            lot_entries, eqp_entries)

        # 更新设备占用
        if eqp != "-":
            _is_par = _is_parallel_eqp(eqp, special_eqp_map)
            _is_tog = eqp in special_eqp_map and special_eqp_map[eqp].together
            if not _is_par and not _is_tog:
                _add_machine_interval(machine_intervals, eqp, (start_time, end_time))
            machine_available[eqp] = end_time
            if eqp in special_eqp_map:
                _register_special_eqp_usage(
                    eqp, lot.lot_name, lot.qty, start_time, end_time,
                    special_eqp_map, eqp_batch_state)

        # 释放 reference
        _release_refs_for_step(
            lot.lot_name, step.step_name, end_time,
            reference_deps, lot_state, pending_refs, ref_release_times, shift_times)

        # 更新步骤索引
        for i, s in enumerate(remaining):
            if s.step_name == step.step_name and i >= state["step_index"]:
                state["step_index"] = i + 1
                break

        if state["step_index"] >= len(remaining):
            state["done"] = True

        # 步骤间等待（链内受 Q-time 预算限制，使用 _effective_chain_wait）
        if idx < len(suffix_steps) - 1:
            step_wait = _effective_chain_wait(lot, chain_info, priority_wait_map)
            state["ready_time"] = end_time + timedelta(minutes=step_wait)
            state["_base_ready_time"] = state["ready_time"]

    return True


# ============================================================
# 主调度函数
# ============================================================

def _max_forward_shift(
    entries: list,
    target_shift: timedelta,
    other_int: dict,
    lower_bound: datetime,
    step_min: Optional[dict] = None,
    down_check: Optional[callable] = None,
) -> timedelta:
    """计算整批可**前移**（提前）的最大位移，用于回拉跟随批贴齐领导批。

    约束：
      1. 任何步骤前移后不早于 lower_bound（跟随批的 start_time）；
      2. step_min（如 lead 闸A 门步：前移后不得早于领导批对应 step 完成）；
      3. 前移后的设备占用不与其它批次（other_int）冲突；
      4. down_check：前移后的占用不得落入设备停机窗（设备不可用期间不作业）。
    返回实际可用的前移量（0 = 无法前移）。
    """
    shift_d = target_shift
    if shift_d <= timedelta(0):
        return timedelta(0)
    # 1/2. 受 start_time 与闸A 门步下界截断
    for e in entries:
        if e.eqp_id == "-":
            continue
        m = step_min.get(e.step_name, lower_bound) if step_min else lower_bound
        if e.start_time - shift_d < m:
            shift_d = min(shift_d, e.start_time - m)
    if shift_d <= timedelta(0):
        return timedelta(0)
    # 3/4. 设备冲突 + 停机窗：从可用位移线性收缩（步进 5min，冲突窗口通常很小）
    while shift_d > timedelta(0):
        ok = True
        for e in entries:
            if e.eqp_id == "-":
                continue
            ns, ne = e.start_time - shift_d, e.end_time - shift_d
            m = step_min.get(e.step_name, lower_bound) if step_min else lower_bound
            if ns < m:
                ok = False
                break
            for (os_, oe_) in other_int.get(e.eqp_id, []):
                if ns < oe_ and os_ < ne:
                    ok = False
                    break
            if ok and down_check is not None and not down_check(e, ns, ne):
                ok = False
                break
            if not ok:
                break
        if ok:
            return shift_d
        shift_d -= timedelta(minutes=5)
    return timedelta(0)


def _inject_lead_upstream_refs(lots: list, flow_map: dict) -> None:
    """lead 上游对齐（设计文档 §4.1 第二条内部阻塞，等效用户原先的"双向普通引用"）：
    领导批 step1 **等 跟随批 step2 的紧邻上一步完成**——让领导批不跑太快，
    两批在衔接步处真正背靠背（跟随批的链相位对齐，PC1 的 DISPENSE 不会早于 real1 的 PLASMA/BAKE）。

    生成的引用带 lead_id 标记（"-u" 后缀），环检测/死锁判定按 lead 内部边跳过。
    幂等：已存在同键引用时跳过（多 seed 反复调用不会重复叠加）。
    """
    by_name = {l.lot_name: l for l in lots}
    for lot in lots:
        for lp in lot.lead_pairs or []:
            if lp.lot1 != lot.lot_name:
                continue                       # 只处理挂在领导批上的 LeadPair
            follow = by_name.get(lp.lot2)
            if not follow:
                continue
            ff = flow_map.get(follow.product_name)
            if not ff:
                continue
            idx = next((i for i, s in enumerate(ff) if s.step_name == lp.step2), -1)
            if idx <= 0:
                continue                       # 跟随步是流程首步，无上游可对齐
            up_step = ff[idx - 1].step_name
            if lot.references is None:
                lot.references = []
            if any(r.reference_lot == follow.lot_name
                   and r.reference_step == up_step
                   and getattr(r, "lead_id", "") for r in lot.references):
                continue
            lot.references.append(LotConstraint(
                lot_name=lot.lot_name,
                reference_lot=follow.lot_name,
                reference_step=up_step,
                start_step=lp.step1,
                start_mod=None,
                lead_id=lp.lead_id + "-u"))


def _lead_back_shift(
    lots: list[Lot],
    lot_entries: list[ScheduleEntry],
    eqp_entries: list[EqpScheduleEntry],
    flows: list[FlowStep],
    window_end: datetime,
    eqp_constraints: Optional[list] = None,
    schedule_start: Optional[datetime] = None,
) -> tuple[list[ScheduleEntry], list[EqpScheduleEntry]]:
    """lead 背靠背回拉（back-shift alignment，设计文档 §4.2 / §5.3 Pass B）。

    对每条 lead (lot1.step1, lot2.step2)：若 lot1.step1 完成后与 lot2.step2 开始之间
    存在空闲带（lot1 提前做完、在紧 Q 链入口前空等），则把 lot1 的全部已排步骤整体
    顺延，使 lot1.step1.end 贴近 lot2.step2.start（背靠背）。

    整批统一顺延 → lot1 内部相邻步骤与各 Q-time 间隔原样保留（不新增 Q 风险），
    "空闲等待"被推迟到紧 Q 计时尚未启动的位置。只校验：
      1. 顺延后的 lot1 各设备占用不与其它批次占用冲突；
      2. 不越过排程窗口末端；
      3. 不落入设备停机窗（设备不可用期间不作业）。
    无可优化 lead / 已背靠背 / 顺延与设备冲突时，该 lead 保持原样（软退化为最小间隙）。

    注意：某 lot 同时是某 lead 的 lot1 又是另一 lead 的 lot2 时，按声明顺序依次处理，
    先到者的结果被后到者看到（load_loader 已归组，顺序确定、可复现）。
    """
    lead_pairs = [lp for lot in lots for lp in (lot.lead_pairs or [])]
    if not lead_pairs or not lot_entries:
        return lot_entries, eqp_entries

    # 停机窗：{eqp: [(ws, we)]}——回拉/顺延后的占用不得落入停机窗
    _down_map: dict[str, list[tuple[datetime, datetime]]] = {}
    if eqp_constraints:
        _ss = schedule_start or min((e.start_time for e in lot_entries), default=datetime.now())
        _expanded = _expand_eqp_constraints(eqp_constraints, _ss)
        _down_map = {k: [(ws, we) for ws, we in v] for k, v in _expanded.items()}

    def _down_ok(e: ScheduleEntry, ns: datetime, ne: datetime) -> bool:
        if e.eqp_id == "-" or e.eqp_id not in _down_map:
            return True
        for ws, we in _down_map[e.eqp_id]:
            if ne > ws and ns < we:
                return False
        return True

    lot_by_name = {l.lot_name: l for l in lots}
    le = list(lot_entries)
    ee = list(eqp_entries)

    # lot -> step -> entry
    by_lot_step: dict[str, dict[str, ScheduleEntry]] = {}
    for e in le:
        by_lot_step.setdefault(e.lot_name, {})[e.step_name] = e

    # flow 内步骤顺序（用于稳定排序 lot1 步骤、判定 step1 是否可排）
    flow_map = get_product_flow_map(flows)
    _flow_order: dict[str, dict[str, int]] = {}
    for _p, _fs in flow_map.items():
        _flow_order[_p] = {s.step_name: i for i, s in enumerate(_fs)}

    def _other_intervals(_lot1: str) -> dict:
        d: dict[str, list] = {}
        for e in ee:
            if e.eqp_id == "-" or e.lot_name == _lot1:
                continue
            d.setdefault(e.eqp_id, []).append((e.start_time, e.end_time))
        return d

    def _shift_ok(lot1_entries: list, shift_d: timedelta, other_int: dict) -> bool:
        for e in lot1_entries:
            if e.eqp_id == "-":
                continue
            ns, ne = e.start_time + shift_d, e.end_time + shift_d
            if not _down_ok(e, ns, ne):
                return False
            for (os_, oe_) in other_int.get(e.eqp_id, []):
                if ns < oe_ and os_ < ne:      # 区间重叠
                    return False
            if ne > window_end:
                return False
        return True

    for lp in lead_pairs:
        lot1 = lot_by_name.get(lp.lot1)
        if not lot1:
            continue
        e1 = by_lot_step.get(lp.lot1, {}).get(lp.step1)
        e2 = by_lot_step.get(lp.lot2, {}).get(lp.step2)
        if e1 is None or e2 is None:
            continue
        # 闸A 必须以实际排程为准；此处只做"背靠背"贴齐（lot2 不可早于 lot1 由闸A保证）
        gap_min = (e2.start_time - e1.end_time).total_seconds() / 60.0
        if gap_min < 2.0:
            continue                            # 已背靠背（差距≤2min）或 lot1 更迟
        _ord1 = _flow_order.get(lot1.product_name, {})
        if lp.step1 not in _ord1:
            continue
        lot1_es = [e for e in le if e.lot_name == lp.lot1 and e.step_name in _ord1]
        lot1_es.sort(key=lambda x: _ord1[x.step_name])
        if len(lot1_es) < 1:
            continue

        # ---- 分支1：回拉跟随批（lot2）——把 step2 及之前步骤整体前移，贴齐领导批完成 ----
        # 适用于"跟随批被 wait/设备拖慢、领导批早已完成"（用户主场景）。
        lot2 = lot_by_name.get(lp.lot2)
        if lot2:
            _ord2 = _flow_order.get(lot2.product_name, {})
            if lp.step2 in _ord2:
                lot2_es = [e for e in le if e.lot_name == lp.lot2 and e.step_name in _ord2]
                lot2_es.sort(key=lambda x: _ord2[x.step_name])
                if lot2_es:
                    other2 = _other_intervals(lp.lot2)
                    lower = lot2.start_time or datetime.min
                    # 闸A 门步下界：跟随批上每条 lead 引用（带 lead_id）的衔接步，
                    # 前移后不得早于对应领导批 step 完成时刻——防止整批前移把早期
                    # 衔接步拽到领导批完成之前（违反闸A）。
                    step_min: dict = {}
                    for _r in (lot2.references or []):
                        if not (getattr(_r, "lead_id", "") and _r.start_step):
                            continue
                        _src_e = by_lot_step.get(_r.reference_lot, {}).get(_r.reference_step or "")
                        if _src_e is not None:
                            step_min[_r.start_step] = _src_e.end_time
                    fshift = _max_forward_shift(lot2_es, timedelta(minutes=gap_min), other2, lower, step_min,
                                                down_check=_down_ok)
                    if fshift > timedelta(0):
                        for e in le:
                            if e.lot_name == lp.lot2:
                                e.start_time = e.start_time - fshift
                                e.end_time = e.end_time - fshift
                        for e in ee:
                            if e.lot_name == lp.lot2:
                                e.start_time = e.start_time - fshift
                                e.end_time = e.end_time - fshift
                        continue                # 已回拉，处理下一条 lead

        # ---- 分支2：顺延领导批（lot1）——lot1 提前做完产生空闲带，整体后移贴齐 lot2 ----
        if not lot1.lead_pairs:
            continue
        other = _other_intervals(lp.lot1)
        shift_d = timedelta(minutes=gap_min)
        if not _shift_ok(lot1_es, shift_d, other):
            continue                            # 顺延与设备冲突 → 软退化，保持最小间隙
        # 提交：lot1 全部已排步骤与设备条目统一顺延
        for e in le:
            if e.lot_name == lp.lot1:
                e.start_time = e.start_time + shift_d
                e.end_time = e.end_time + shift_d
        for e in ee:
            if e.lot_name == lp.lot1:
                e.start_time = e.start_time + shift_d
                e.end_time = e.end_time + shift_d
    return le, ee


def _detect_qtime_cross_shift(
    lot_entries: list,
    qtimes: list,
    shift_times: list,
) -> list[str]:
    """紧 Q 链跨班次告警：Q-time ≤ 紧链阈值 的相邻步骤若跨过班次切换时刻
    （如 PLASMA 在班次 A 末完成、DISPENSE 排到班次 B 初），输出风险提示。

    仅告警，不改变排程结果（跨班次约束机制待定义，见设计文档 §8）。
    """
    warns: list[str] = []
    if not shift_times:
        return warns
    st_mins = sorted(s[0] * 60 + s[1] for s in shift_times)

    def _crosses_shift(a: datetime, b: datetime) -> bool:
        if b <= a:
            return False
        day = a.date()
        cur = a
        for _ in range(3):                       # 最多查 3 天
            for m in st_mins:
                t = datetime(day.year, day.month, day.day, m // 60, m % 60)
                if t > cur:
                    return t < b                 # 切换时刻落在 (a, b) 内 = 跨班次
            day = day + timedelta(days=1)
            cur = datetime(day.year, day.month, day.day, 0, 0)
            if cur >= b:
                return False
        return False

    by_lot: dict[str, list] = {}
    for e in lot_entries:
        by_lot.setdefault(e.lot_name, []).append(e)
    for q in qtimes:
        if q.max_duration is None or q.max_duration > TIGHT_CHAIN_THRESHOLD:
            continue
        for lot, es in by_lot.items():
            se = next((e for e in es if e.step_name == q.start_step), None)
            ee = next((e for e in es if e.step_name == q.end_step), None)
            if se is None or ee is None:
                continue
            if _crosses_shift(se.end_time, ee.start_time):
                warns.append(
                    f"紧 Q 链跨班次: {lot} {q.start_step}→{q.end_step} "
                    f"{se.end_time:%m/%d %H:%M} → {ee.start_time:%m/%d %H:%M} "
                    f"(预算 {q.max_duration}min)")
    return warns[:10]


def schedule(
    lots: list[Lot],
    flows: list[FlowStep],
    ct_lookup: dict,
    qtimes: list[QTimeConstraint],
    shift_times: list[tuple[int, int]],
    ftf_qty_change: Optional[dict[str, tuple[int, int, str]]] = None,
    special_lot_step_lookup: Optional[dict[tuple[str, str], SpecialLotStep]] = None,
    priority_wait_map: Optional[dict[tuple[int, int], int]] = None,
    eqp_constraints: Optional[list[EqpConstraint]] = None,
    step_time_window_constraints: Optional[list[StepTimeWindow]] = None,
    shift_change_times: Optional[list[ShiftChangeTime]] = None,
    manual_adjusts: Optional[list[ManualAdjust]] = None,
    special_eqp_map: Optional[dict[str, SpecialEqp]] = None,
    resolve_max_iterations: int = 10,
    verbose: bool = False,
    lot_order: Optional[list[str]] = None,
    eqp_preferences: Optional[dict[tuple[str, str], list[str]]] = None,
    chain_placement: str = "compact",
    ref_release_forecast: Optional[dict] = None,
    tight_chain_threshold: Optional[int] = None,
    qtight_safety_margin: Optional[float] = None,
    qtight_min_margin: Optional[float] = None,
    chain_wait_safety: Optional[int] = None,
    cross_shift_avoid: Optional[bool] = None,
    batch_wait_window: Optional[int] = None,
    out_warnings: Optional[list] = None,
) -> tuple[list[ScheduleEntry], list[EqpScheduleEntry], list[QTimeAlert]]:
    """两遍排程（Fix3）：把"第一遍实际释放时刻"回喂第二遍作为预测锚点。

    第一遍不带预测跑出真实排程，从中提取各个 reference 键的实际释放时刻
    （_collect_ref_release_forecast），作为第二遍的 ref_release_forecast 传入，
    使等待中、尚未在第二遍里实际释放的 reference 也能以真实释放锚点提前紧凑放置
    整链块，避免链首因缺锚而过早调度、拖垮整链 Q-time。

    若调用方已显式传入 ref_release_forecast，则以调用方的为准（更高优先级）。
    第二遍在深拷贝的 lots 上运行，避免第一遍 FTF 数量变换把 qty 二次放大。
    """
    import copy as _copy
    # lead 上游对齐：为领导批补"step1 等 跟随批 step2 紧邻上一步完成"的内部引用边
    # （等效用户原先的双向普通引用；幂等，多 seed 反复调用不会叠加）。
    try:
        _inject_lead_upstream_refs(lots, get_product_flow_map(flows))
    except Exception:
        pass  # 注入失败不影响排程主流程
    # 保留调用方 lots 的原始快照，供第二遍使用（第一遍可能在 FTF 步骤改动 lot.qty）
    _orig_lots = _copy.deepcopy(lots)
    # 第一遍：不带预测，跑出真实释放时刻
    _le1, _ee1, _qa1 = _run_schedule_pass(
        lots, flows, ct_lookup, qtimes, shift_times,
        ftf_qty_change=ftf_qty_change,
        special_lot_step_lookup=special_lot_step_lookup,
        priority_wait_map=priority_wait_map,
        eqp_constraints=eqp_constraints,
        step_time_window_constraints=step_time_window_constraints,
        shift_change_times=shift_change_times,
        manual_adjusts=manual_adjusts,
        special_eqp_map=special_eqp_map,
        resolve_max_iterations=resolve_max_iterations,
        verbose=verbose, lot_order=lot_order,
        eqp_preferences=eqp_preferences,
        chain_placement=chain_placement,
        ref_release_forecast=None,
        tight_chain_threshold=tight_chain_threshold,
        qtight_safety_margin=qtight_safety_margin,
        qtight_min_margin=qtight_min_margin,
        chain_wait_safety=chain_wait_safety,
        cross_shift_avoid=cross_shift_avoid,
        batch_wait_window=batch_wait_window,
        out_warnings=None)
    _forecast = _collect_ref_release_forecast(_le1, lots, shift_times)
    if ref_release_forecast:
        _forecast = {**_forecast, **ref_release_forecast}
    # 第二遍：携带预测锚点重新排程（用原始快照，深拷贝隔离）
    _le2, _ee2, _qa2 = _run_schedule_pass(
        _orig_lots, flows, ct_lookup, qtimes, shift_times,
        ftf_qty_change=ftf_qty_change,
        special_lot_step_lookup=special_lot_step_lookup,
        priority_wait_map=priority_wait_map,
        eqp_constraints=eqp_constraints,
        step_time_window_constraints=step_time_window_constraints,
        shift_change_times=shift_change_times,
        manual_adjusts=manual_adjusts,
        special_eqp_map=special_eqp_map,
        resolve_max_iterations=resolve_max_iterations,
        verbose=verbose, lot_order=lot_order,
        eqp_preferences=eqp_preferences,
        chain_placement=chain_placement,
        ref_release_forecast=_forecast,
        tight_chain_threshold=tight_chain_threshold,
        qtight_safety_margin=qtight_safety_margin,
        qtight_min_margin=qtight_min_margin,
        chain_wait_safety=chain_wait_safety,
        cross_shift_avoid=cross_shift_avoid,
        batch_wait_window=batch_wait_window,
        out_warnings=out_warnings)
    # 两遍都比，取更优者。择优键为四元组，优先级从高到低：
    #   1) 缺失步骤数（最硬性：丢步骤的排程非法；且丢步会让 Q-time 告警变少，
    #      单独比 Q-time 会被"缺步者"反向骗赢——实测两遍都缺步时无法区分）；
    #   2) reference 违背数（约束合法性，宁可少一项 Q-time 收益也要约束合法）；
    #   3) Q-time 超时条目数；
    #   4) 总超时分钟。
    # 保证回喂预测的副作用不会把原本更优的第一遍结果劣化（例如优化器反复重排的
    # 手动调整场景）。
    _r1 = _count_ref_violations(_le1, _orig_lots, shift_times)
    _r2 = _count_ref_violations(_le2, _orig_lots, shift_times)
    _u1 = _count_lead_u_violations(_le1, _orig_lots, shift_times)
    _u2 = _count_lead_u_violations(_le2, _orig_lots, shift_times)
    _m1 = _count_missing_steps(_le1, _orig_lots, flows)
    _m2 = _count_missing_steps(_le2, _orig_lots, flows)
    _n1 = len([a for a in _qa1 if a.status != "OK"])
    _n2 = len([a for a in _qa2 if a.status != "OK"])
    _o1 = sum(getattr(a, "over_minutes", 0) for a in _qa1 if a.status != "OK")
    _o2 = sum(getattr(a, "over_minutes", 0) for a in _qa2 if a.status != "OK")
    if (_m1, _r1, _n1, _o1, _u1) <= (_m2, _r2, _n2, _o2, _u2):
        _best_le, _best_ee, _best_qa = _le1, _ee1, _qa1
    else:
        _best_le, _best_ee, _best_qa = _le2, _ee2, _qa2
    # ---- lead 背靠背回拉（Pass B：把 lot1 顺延贴齐 lot2.step2.start）----
    _win = datetime.min
    for _e in _best_le:
        if _e.end_time > _win:
            _win = _e.end_time
    if _win == datetime.min:
        _win = datetime.now()
    _best_le, _best_ee = _lead_back_shift(
        lots=_orig_lots, lot_entries=_best_le, eqp_entries=_best_ee,
        flows=flows, window_end=_win + timedelta(days=2),
        eqp_constraints=eqp_constraints)
    # ---- 超 Q 后修复（尽力而为）：长 Q 链首被排得过早、端步骤被延后导致超 Q 时，
    #      把链首后移到满足 Q 预算的位置（如 BAKE→DISPENSE 1445min>1440min 场景）。
    #      只后移不前置；设备冲突 / 上游 Q 被撑爆 / 校验违规增加时整体回滚。
    try:
        _ss_win = min((e.start_time for e in _best_le), default=datetime.now())
        _fix_qtime_overflow_pull_chain_start(
            _best_le, _best_ee, _best_qa, _orig_lots, flows, qtimes, shift_times,
            shift_change_intervals=(
                _expand_shift_change_times(shift_change_times, _ss_win)
                if shift_change_times else None))
    except Exception:
        pass
    # ---- 紧 Q 链跨班次风险告警（仅提示，不改变结果）----
    try:
        _cs = _detect_qtime_cross_shift(_best_le, qtimes, shift_times)
        for _w in _cs:
            if verbose or _WARN_ENV:
                logger.warning("[跨班次风险] %s", _w)
        if out_warnings is not None:
            out_warnings.extend(_cs)
    except Exception:
        pass
    return _best_le, _best_ee, _best_qa


def _prev_step_name(flows: list, product_name: str, step_name: str) -> Optional[str]:
    """返回某产品流程中指定步骤的前一个步骤名（按 step_number 升序）；无则 None。"""
    steps = [f for f in flows if f.product_name == product_name]
    def _key(f):
        try:
            return (float(f.step_number), f.step_name)
        except (TypeError, ValueError):
            return (0.0, f.step_name)
    steps.sort(key=_key)
    for i, f in enumerate(steps):
        if f.step_name == step_name and i > 0:
            return steps[i - 1].step_name
    return None


def _fix_qtime_overflow_pull_chain_start(le, ee, qa, lots, flows, qtimes,
                                         shift_times=None,
                                         shift_change_intervals=None) -> int:
    """超 Q 后修复（尽力而为）。

    场景：长 Q-time 段的链首步骤（如 real1 的 BAKE）被自然排得很早，端步骤
    （如 DISPENSE）随后被手动延后 / 设备挤兑推晚，导致整段间隔超过 Q 预算
    （如 BAKE→DISPENSE 1445min > 1440min）。此时链首之后通常有大段空缺，
    把链首步骤后移即可恢复合规。

    策略（只后移、不前置，失败即放弃保持原样）：
      1) 对每个"超时" Q-time 告警，定位链首 entry A 与端步骤 entry B；
      2) 目标：A 的参考时刻 ≥ B 参考 - (预算 - 安全余量)。参考点规则：
         start_mod 决定 A 侧（track out=结束 / track in=开始），end_mod 决定 B 侧；
         紧链按用户安全余量（span ≤ D - safe）而非"刚好卡线 D-1min"（余量 1min
         的用户反馈根因）；跨班次时优先把链首整体后移到 B 所在班次（跳过交接窗）；
      3) 用设备占用区间找 ≥ (A.start + 需后移量) 的最早可用槽，保序（不越过 B）；
      4) 上游 Q 保护：A 后移不得撑爆 (prev→A) 段预算（否则放弃，见已知限制）；
      5) 通过则同步更新 lot/设备维度 entry，告警标记 OK；
      6) 全部尝试后统一 validate 复查：违规数比修复前增加则整体回滚。

    已知限制（链式后移尝试结论）：PC1 型场景（超 Q 段上游是紧链且链首设备被其他
    Lot 排他占用）无法单层后移——尝试过"链式传播逐层后移"（第 16 轮实验）会破坏
    real1/PC2 的修复（整链精确平移撞设备占用、参考点错位），已回退单层。此类场景
    属上游紧链 + 设备竞争的真实约束冲突，保留告警更安全。
    """
    if not qa or not qtimes:
        return 0
    try:
        from validation import validate_schedule
    except Exception:
        validate_schedule = None

    prod_map = {l.lot_name: l.product_name for l in lots}
    entry_by_lot_step: dict[str, dict[str, ScheduleEntry]] = {}
    for e in le:
        entry_by_lot_step.setdefault(e.lot_name, {})[e.step_name] = e
    qrule_by_key: dict[tuple, object] = {}
    for q in qtimes:
        qrule_by_key[(q.product_name, q.start_step, q.end_step)] = q

    before_n = len(validate_schedule(le, ee, qa, lots, flows, qtimes)) if validate_schedule else 0
    moves = []  # 记录已移动条目，供整体回滚

    for alert in list(qa):
        try:
            if alert.status != "超时" or "→" not in alert.qtime_rule:
                continue
            ss, es = (x.strip() for x in alert.qtime_rule.split("→", 1))
            prod = prod_map.get(alert.lot_name)
            q = qrule_by_key.get((prod, ss, es))
            if q is None or not q.max_duration:
                continue
            A = entry_by_lot_step.get(alert.lot_name, {}).get(ss)
            B = entry_by_lot_step.get(alert.lot_name, {}).get(es)
            if A is None or B is None or A.eqp_id == "-":
                continue
            budget = float(q.max_duration)
            start_mod = (q.start_mod or "track in").strip()   # A 侧参考
            end_mod = (q.end_mod or "track out").strip()      # B 侧参考
            b_ref = B.start_time if end_mod == "track in" else B.end_time
            # 链首参考时刻的合规下限：间隔 = b_ref - a_ref，要求 ≤ 预算 - 安全余量
            # → a_ref ≥ b_ref - (预算 - 安全余量)。a_ref 越小（链首排得越早）间隔越大越易超时。
            # 安全余量（用户规则）：紧链不只"不超 Q"，还要留出 max(预算×%, 下限) 的缓冲，
            # 不再把链首刚好卡在 D-1min（余量 1min，用户反馈根因）。松链不设余量目标。
            _target_span = budget - _q_target_margin(q)
            a_ref_min = b_ref - timedelta(minutes=_target_span)
            a_ref_now = A.end_time if start_mod == "track out" else A.start_time
            if a_ref_now >= a_ref_min:
                continue  # 链首已够晚，间隔合规
            # 需后移分钟数（+1min 避免浮点误差刚好卡线）
            shift_min = (a_ref_min - a_ref_now).total_seconds() / 60.0 + 1.0
            lo = A.start_time + timedelta(minutes=shift_min)
            # 上一步约束：后移不得早于上一步完成
            prev_step = _prev_step_name(flows, prod, ss)
            prev_e = entry_by_lot_step.get(alert.lot_name, {}).get(prev_step) if prev_step else None
            if prev_e is not None and lo < prev_e.end_time:
                lo = prev_e.end_time
            # 设备占用区间（剔除 A 自身；含"down"标记不影响：占用均视为普通占用）
            occ = [[e2.start_time, e2.end_time] for e2 in ee
                   if e2.eqp_id == A.eqp_id
                   and not (e2.lot_name == A.lot_name and e2.step_name == A.step_name)]
            new_start = _find_earliest_slot(occ, lo, timedelta(minutes=A.ct))
            # 保序：链首后移不得越过端步骤
            if new_start + timedelta(minutes=A.ct) > B.start_time:
                continue
            # 跨班次整链后移（用户规则）：后移目标若落在 B 所在班次的紧邻前一个班次
            #（即 A→B 窗口跨过班次切换），把 A 起点推到 B 所在班次起点（跳过交接窗），
            # 使整段链落在同一班次内；若推后越序（A 越过 B）则放弃跨班次对齐，保留
            # 按余量目标的后移（余量优先于跨班次，跨班次由告警提示）。
            if shift_change_intervals:
                _cs_lo = _cross_shift_push_target(new_start, B.start_time,
                                                  shift_change_intervals)
                if _cs_lo is not None and _cs_lo > new_start:
                    # 班次起点落在交接班禁用窗内：解析到窗后（09:30/21:30）再落槽，
                    # 避免把链首排进交接班时段
                    _cs_resolved = _skip_shift_change(_cs_lo, shift_change_intervals)
                    _cs_start = _find_earliest_slot(occ, _cs_resolved,
                                                    timedelta(minutes=A.ct))
                    if (_cs_start != datetime.max
                            and _cs_start + timedelta(minutes=A.ct) <= B.start_time):
                        new_start = _cs_start
            # 上游 Q 检查：A 作为 end 步骤（规则 prev_step→ss），移动不得撑爆上游预算
            if prev_e is not None and prev_step:
                upq = qrule_by_key.get((prod, prev_step, ss))
                if upq is not None and upq.max_duration:
                    up_start_mod = (upq.start_mod or "track in").strip()
                    up_end_mod = (upq.end_mod or "track out").strip()
                    up_ref = (new_start + timedelta(minutes=A.ct)
                              if up_end_mod == "track out" else new_start)
                    prev_ref = (prev_e.end_time
                                if up_start_mod == "track out" else prev_e.start_time)
                    if (up_ref - prev_ref).total_seconds() / 60.0 > float(upq.max_duration):
                        continue  # 会撑爆上游（PC1 型），保守放弃
            # 应用移动
            old_start, old_end = A.start_time, A.end_time
            A.start_time = new_start
            A.end_time = new_start + timedelta(minutes=A.ct)
            eqp_match = None
            for e2 in ee:
                if (e2.eqp_id == A.eqp_id and e2.lot_name == A.lot_name
                        and e2.step_name == A.step_name and e2.start_time == old_start):
                    eqp_match = e2
                    break
            if eqp_match is not None:
                eqp_match.start_time = new_start
                eqp_match.end_time = A.end_time
            moves.append((A, old_start, old_end, eqp_match))
            alert.status = "OK"
            alert.over_minutes = 0
        except Exception:
            continue

    if not moves:
        return 0
    # 整体回滚保护：修复后违规数不得比修复前增加
    if validate_schedule:
        after_n = len(validate_schedule(le, ee, qa, lots, flows, qtimes))
        if after_n > before_n:
            for A, old_start, old_end, eqp_match in moves:
                A.start_time = old_start
                A.end_time = old_end
                if eqp_match is not None:
                    eqp_match.start_time = old_start
                    eqp_match.end_time = old_end
            return 0
    return len(moves)


def _run_schedule_pass(
    lots: list[Lot],
    flows: list[FlowStep],
    ct_lookup: dict,
    qtimes: list[QTimeConstraint],
    shift_times: list[tuple[int, int]],
    ftf_qty_change: Optional[dict[str, tuple[int, int, str]]] = None,
    special_lot_step_lookup: Optional[dict[tuple[str, str], SpecialLotStep]] = None,
    priority_wait_map: Optional[dict[tuple[int, int], int]] = None,
    eqp_constraints: Optional[list[EqpConstraint]] = None,
    step_time_window_constraints: Optional[list[StepTimeWindow]] = None,
    shift_change_times: Optional[list[ShiftChangeTime]] = None,
    manual_adjusts: Optional[list[ManualAdjust]] = None,
    special_eqp_map: Optional[dict[str, SpecialEqp]] = None,
    resolve_max_iterations: int = 10,
    verbose: bool = False,
    lot_order: Optional[list[str]] = None,
    eqp_preferences: Optional[dict[tuple[str, str], list[str]]] = None,
    chain_placement: str = "compact",
    ref_release_forecast: Optional[dict] = None,
    tight_chain_threshold: Optional[int] = None,
    qtight_safety_margin: Optional[float] = None,
    qtight_min_margin: Optional[float] = None,
    chain_wait_safety: Optional[int] = None,
    cross_shift_avoid: Optional[bool] = None,
    batch_wait_window: Optional[int] = None,
    out_warnings: Optional[list] = None,
) -> tuple[list[ScheduleEntry], list[EqpScheduleEntry], list[QTimeAlert]]:
    """执行启发式排程。

    Args:
        lots: Lot 列表
        flows: 流程步骤列表
        ct_lookup: CT 查找表
        qtimes: Q-time 约束列表
        shift_times: 班次开始时间列表
        special_eqp_map: 特殊设备配置 {eqp_name: SpecialEqp}
        lot_order: Lot 调度顺序，None 时按优先级排序
        eqp_preferences: 设备偏好
        qtight_safety_margin: 紧 Q-time 安全余量（百分比 0-100，默认 20%）
        qtight_min_margin: 紧 Q-time 安全余量下限（分钟，默认 30）

    Returns:
        (lot_entries, eqp_entries, qtime_alerts)
    """
    if verbose:
        logger.setLevel(logging.DEBUG)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
            logger.addHandler(handler)
    else:
        logger.setLevel(logging.WARNING)

    if eqp_preferences is None:
        eqp_preferences = {}
    if special_eqp_map is None:
        special_eqp_map = {}
    if special_lot_step_lookup is None:
        special_lot_step_lookup = {}

    global TIGHT_CHAIN_THRESHOLD, QTIGHT_SAFETY_MARGIN, QTIGHT_MIN_MARGIN, CHAIN_WAIT_SAFETY, CROSS_SHIFT_AVOID, BATCH_WAIT_WINDOW
    # 每次调用都写入"生效值"（参数为 None 时恢复模块默认），
    # 避免上一次调用残留的自定义值污染后续以默认参数运行的调用
    # （同进程多请求 / 测试先后调用场景）。
    TIGHT_CHAIN_THRESHOLD = tight_chain_threshold if tight_chain_threshold is not None else 240
    # 安全余量为百分比（0-100，默认 20%）：按紧链 Q 预算 D 的占比预留起点缓冲
    QTIGHT_SAFETY_MARGIN = qtight_safety_margin if qtight_safety_margin is not None else 20.0
    # 安全余量下限（分钟，默认 30）：实际余量 = max(预算×%, 下限)
    QTIGHT_MIN_MARGIN = qtight_min_margin if qtight_min_margin is not None else 30.0
    CHAIN_WAIT_SAFETY = chain_wait_safety if chain_wait_safety is not None else 20
    CROSS_SHIFT_AVOID = cross_shift_avoid if cross_shift_avoid is not None else True
    # 恒组批等待凑批窗口（分钟，默认 240）
    BATCH_WAIT_WINDOW = batch_wait_window if batch_wait_window is not None else 240

    now = datetime.now()

    # ---- 1. 计算每个 lot 的 ready_time 和全局 schedule_start ----
    flow_map = get_product_flow_map(flows)

    lot_ready_map: dict[str, datetime] = {}
    for lot in lots:
        product_flow = flow_map.get(lot.product_name)
        if not product_flow:
            raise ValueError(f"未找到产品 {lot.product_name} 的流程")

        # start_time 为空 → 从现在开始
        if lot.start_time is not None:
            ready = lot.start_time
        else:
            ready = now

        # 注意：running_time 只用于计算“当前正在 running 的 step 何时结束”
        # （第一步剩余 CT = CT - running_time，见 first_step_ct_adj），
        # 不再把开始时间推迟到 now + running_time。因此这里不调整 ready。

        if lot.hold_periods:
            for hs, he in lot.hold_periods:
                if he is None:
                    continue
                if ready < he:
                    ready = max(ready, he)

        lot_ready_map[lot.lot_name] = ready

    # 全局起点：取各 lot 就绪时刻的最小值（历史数据下确定、可复现）。
    # 未填 start_time 的 lot 就绪时刻 = 现在（见上），故：
    #   - 全部有明确 start_time 时，起点 = 最早批次开始时间（与数据时间线一致）；
    #   - 存在无 start_time 的批次时，若其为最早则起点 = 现在（“从现在开始”）。
    # 不要用 datetime.now() 作为无条件全局起点：会让结果随运行时刻漂移、
    # 与历史数据的时间线错位，导致粗排锚点与设备约束展开基准错误。
    schedule_start = min(lot_ready_map.values()) if lot_ready_map else now

    schedule_end = schedule_start + timedelta(days=SCHEDULE_WINDOW_DAYS)

    # ---- 2. 初始化设备 ----
    all_eqp_ids: set[str] = set()
    for f in flows:
        all_eqp_ids.update(f.eqp_ids)

    machine_available: dict[str, datetime] = {}
    machine_intervals: dict[str, list[tuple[Optional[datetime], Optional[datetime]]]] = {}
    for eqp_id in all_eqp_ids:
        machine_available[eqp_id] = schedule_start
        machine_intervals[eqp_id] = []

    if eqp_constraints:
        expanded = _expand_eqp_constraints(eqp_constraints, schedule_start, schedule_end)
        for eqp_name, intervals in expanded.items():
            # 停机窗标记为 3 元组 (start, end, "down")：调度时只约束"新操作起点"
            # 不得落在停机窗内，允许长 CT 操作跨越停机窗继续运行（如 qty=25 的
            # FC-REFLOW CT≈925min 超过每日 08:30-22:00 运行窗 810min，若按"完整
            # 时长不得跨越停机窗"处理会被推到 365 天窗口末尾、产生 2027 年排程——
            # fuzz 实测 seed 20260828001 的远未来排程根因）。
            if eqp_name in machine_intervals:
                machine_intervals[eqp_name].extend([(s, e, "down") for s, e in intervals])
            else:
                machine_intervals[eqp_name] = [(s, e, "down") for s, e in intervals]
                machine_available[eqp_name] = schedule_start
            machine_intervals[eqp_name].sort(key=lambda x: x[0] if x[0] else datetime.min)

    step_windows_expanded: dict[str, list[tuple[datetime, datetime]]] = {}
    end_windows_expanded: dict[str, list[tuple[datetime, datetime]]] = {}
    if step_time_window_constraints:
        step_windows_expanded = _expand_time_windows(step_time_window_constraints, schedule_start, schedule_end)
        end_windows_expanded = _expand_end_time_windows(step_time_window_constraints, schedule_start, schedule_end)

    shift_change_intervals: list[tuple[datetime, datetime]] = []
    if shift_change_times:
        shift_change_intervals = _expand_shift_change_times(shift_change_times, schedule_start, schedule_end)

    manual_adjust_lookup: dict[tuple[str, Optional[str]], datetime] = {}
    pin_lookup: dict[tuple[str, Optional[str]], datetime] = {}
    if manual_adjusts:
        for ma in manual_adjusts:
            if not ma.delay_to:
                continue
            key = (ma.lot_name, ma.step_name)
            mode = getattr(ma, "mode", "delay") or "delay"
            if mode == "pin":
                if key not in pin_lookup or ma.delay_to > pin_lookup[key]:
                    pin_lookup[key] = ma.delay_to
            else:
                if key not in manual_adjust_lookup or ma.delay_to > manual_adjust_lookup[key]:
                    manual_adjust_lookup[key] = ma.delay_to

    # ---- 3. 构建 Q-time 链信息 ----
    qtime_chain_map = _build_qtime_chains(flow_map, qtimes)

    # ---- 4. 初始化 lot 状态 ----
    qtime_by_product: dict[str, list[QTimeConstraint]] = {}
    for q in qtimes:
        qtime_by_product.setdefault(q.product_name, []).append(q)

    lot_order_rank: dict[str, int] = {}
    if lot_order:
        for rank, name in enumerate(lot_order):
            lot_order_rank[name] = rank
    else:
        sorted_lots = sorted(lots, key=lambda l: (l.priority, l.lot_name))
        for rank, lot in enumerate(sorted_lots):
            lot_order_rank[lot.lot_name] = rank

    lot_state: dict[str, dict] = {}
    reference_deps: dict[tuple[str, str], list[str]] = {}
    pending_refs: dict[str, set[tuple[str, str]]] = {}
    ref_release_times: dict[str, dict[tuple[str, str], datetime]] = {}

    # 粗排程第一遍：计算约束关系导致的各 step 最早可行开始时间锚点
    coarse_anchors = _coarse_earliest_anchors(
        lots, flow_map, ct_lookup, special_lot_step_lookup,
        priority_wait_map or {}, schedule_start, qtimes,
        manual_adjusts=manual_adjusts,
        eqp_constraints=eqp_constraints,
        shift_times=shift_times)

    # ---- 引用环内的 reference 预测释放 ----
    # 环内 lot 互相等待（A 等 B 完成、B 又等 A 完成）时，贪婪排程阶段无法得到
    # "确切的释放顺序"：源 lot 自己也卡在等待里，release 永远是 FAR_FUTURE →
    # 所有未完成 lot 互相阻塞 → 死锁、后续步骤全部缺失。
    # 用粗排锚点（自然就绪时刻，已由 _coarse_earliest_anchors 的雪崩回退打破环）
    # 预测释放时间：预测 = 源 lot 该步骤锚点 + CT。预填后本 lot 到该步时
    # ready_time = max(自然就绪, 预测)，不再硬阻塞；源 lot 真实完成时
    # _release_refs_for_step 会以真实完成时间覆盖（_update_blocked_ready_time
    # 取 max，真实更晚则自动修正）。
    _cycle_forecast: dict[tuple[str, tuple[str, str]], datetime] = {}
    _cycle_forecast_keys: set = set()
    _cycle_lots_now = set(_anchor_audit.get("cycle_lots") or ())
    lot_by_name = {l.lot_name: l for l in lots}
    # 各 lot 在流程中的当前步骤索引（判定"引用源步骤已被源 lot 越过"用）
    _src_cur_idx: dict[str, int] = {}
    for _l in lots:
        _lpf = flow_map.get(_l.product_name)
        if _lpf:
            try:
                _src_cur_idx[_l.lot_name] = get_step_index_in_flow(_lpf, _l.current_step_name)
            except ValueError:
                _src_cur_idx[_l.lot_name] = 0
    # 仅当粗排程 60 轮未收敛（真·循环引用、已触发自然锚点回退）时才启用预测释放：
    # 收敛的环（IOU1↔IOU1-f、PC↔real）真实释放会正常到来，预测反而可能偏早/偏晚，
    # 干扰设备占用导致 Q-time 超时（PC↔real 强制延迟实测 2→7 错误）；只有真·环
    # （A.FTF 等 B.FTF 完成、B.FTF 又等 A.FTF 完成）真实释放永远不会到来，才需要
    # 用"自然就绪时刻"预测打破互相阻塞死锁。
    # 死锁环判定：把环内 lot 按引用图【连通分量】分组，仅对"真·死锁分量"启用
    # 预测释放。真·死锁分量 = 分量内至少一个 lot 的首步被同分量引用阻塞
    # （block_idx==0）——该 lot 无法启动，而分量内其它 lot 又可能依赖它的释放，
    # 形成闭合等待、真实释放永远不会到来。收敛分量（如 PC↔real：PC1 等
    # real1.UF-BAKE、real1 等 PC1.FC-REFLOW，但两者的首步都未被同分量引用阻塞）
    # 真实释放会正常到来——若也用自然锚点预测，预测会偏早（忽略设备/班次延迟），
    # 下游 lot 提前调度产生 reference 违背（fuzz 实测 seed 20260828035：real1.
    # UF-BAKE 实际 08/19 09:05 完成，自然锚点预测却给 08/18 03:30 → PC1.
    # UF-DISPENSE 08/18 09:35 提前开工，违反引用约束；而同 seed 的 PC2↔real2
    # 是真·死锁分量——PC2 首步等 real2.DAF-SAT，real2 又等 PC2.FC-REFLOW）。
    _deadlock_lots: set = set()
    if _cycle_lots_now and _anchor_audit.get("fallback_used"):
        # 环内引用图的无向连通分量
        _adj: dict[str, set[str]] = {}
        for _cln in _cycle_lots_now:
            _adj.setdefault(_cln, set())
        for _cl in lots:
            if _cl.lot_name not in _cycle_lots_now:
                continue
            for _r in _cl.references or []:
                if _r.reference_lot in _cycle_lots_now and _r.reference_lot != _cl.lot_name:
                    _adj[_cl.lot_name].add(_r.reference_lot)
                    _adj.setdefault(_r.reference_lot, set()).add(_cl.lot_name)
        _seen_c: set = set()
        for _cln in _cycle_lots_now:
            if _cln in _seen_c:
                continue
            _comp: set = set()
            _stack = [_cln]
            while _stack:
                _n = _stack.pop()
                if _n in _seen_c:
                    continue
                _seen_c.add(_n)
                _comp.add(_n)
                _stack.extend(_adj.get(_n, ()))
            # 判断该分量是否真·死锁（任一 lot 首步被同分量引用阻塞）
            _comp_deadlock = False
            for _cl2 in _comp:
                _cl2_lot = lot_by_name.get(_cl2)
                if not _cl2_lot:
                    continue
                _cl2f = flow_map.get(_cl2_lot.product_name)
                if not _cl2f:
                    continue
                try:
                    _cl2_cur = get_step_index_in_flow(_cl2f, _cl2_lot.current_step_name)
                except ValueError:
                    _cl2_cur = 0
                _cl2_rem = _cl2f[_cl2_cur:]
                for _r2 in _cl2_lot.references or []:
                    if _r2.reference_lot not in _comp:
                        continue
                    if _r2.start_step:
                        _bi2 = next((i for i, _s in enumerate(_cl2_rem) if _s.step_name == _r2.start_step), None)
                    else:
                        _bi2 = 0
                    if _bi2 == 0:
                        _comp_deadlock = True
                        break
                if _comp_deadlock:
                    break
            if _comp_deadlock:
                _deadlock_lots |= _comp
    if _deadlock_lots:
        for _lot in lots:
            _pf = flow_map.get(_lot.product_name)
            if not _pf:
                continue
            for _r in _lot.references or []:
                if not (_r.reference_lot and _r.start_step):
                    continue
                if _r.reference_lot not in _deadlock_lots or _lot.lot_name not in _deadlock_lots:
                    continue  # 源/本 lot 不在真·死锁分量：真实释放会正常到来，无需预测
                _src_lot = lot_by_name.get(_r.reference_lot)
                _src_flow = flow_map.get(_src_lot.product_name) if _src_lot else None
                if not (_src_lot and _src_flow):
                    continue
                try:
                    _src_step_idx = get_step_index_in_flow(_src_flow, _r.reference_step or "")
                    _src_cur = get_step_index_in_flow(_src_flow, _src_lot.current_step_name)
                except ValueError:
                    continue
                _rel = _src_step_idx - _src_cur
                if _rel < 0 or _rel >= len(coarse_anchors.get(_r.reference_lot, [])):
                    continue
                _anchor_t = coarse_anchors[_r.reference_lot][_rel]
                _ct = get_step_ct(ct_lookup, _src_flow[_src_step_idx].product_name,
                                  _src_flow[_src_step_idx].step_number, _src_lot.qty)
                _cycle_forecast[(_lot.lot_name, (_r.reference_lot, _r.reference_step or ""))] = \
                    _anchor_t + timedelta(minutes=_ct)
                _cycle_forecast_keys.add((_lot.lot_name, (_r.reference_lot, _r.reference_step or "")))

    for lot in lots:
        product_flow = flow_map.get(lot.product_name)
        current_idx = get_step_index_in_flow(product_flow, lot.current_step_name)
        if lot.target_step:
            target_idx = get_step_index_in_flow(product_flow, lot.target_step)
            remaining = product_flow[current_idx:target_idx + 1]
        else:
            remaining = product_flow[current_idx:]

        ready_time = lot_ready_map.get(lot.lot_name, schedule_start)
        has_refs = bool(lot.references)

        ref_block_info: dict[tuple[str, str], int] = {}
        if has_refs:
            for ref in lot.references:
                ref_key = (ref.reference_lot, ref.reference_step or "")
                if ref.start_step:
                    for i, s in enumerate(remaining):
                        if s.step_name == ref.start_step:
                            ref_block_info[ref_key] = i
                            break
                else:
                    ref_block_info[ref_key] = 0

        base_ready_time = ready_time
        if has_refs:
            active_refs = lot.references
            # 判定"本排程内永远不会释放"的引用（继续等待必然死锁 → 后续步骤全部缺失）：
            #   a) 源 lot 不在本轮排程列表（外部 lot，其步骤永远不会被本 schedule 排到）；
            #   b) 源 lot 在本轮，但其 reference_step 已在源 lot 当前步骤之前（源已越过该步，
            #      该步骤不会被再次排程，释放永远不会到来）；
            #   c) 源 lot 设置了 target_step 且其位置在 reference_step 之前（源只排到
            #      target 为止，reference 步骤超出源的目标，同样永远不会被排到）。
            # 此类引用视为"调度开始时已满足"，立即预释放（release 基于源 lot 就绪时刻 /
            # schedule_start 施加 start_mod 偏移），避免下游整链阻塞。这是 fuzz 扰动
            # （把 current_step 后移 / 改引用 / 设 target 截断）实测最大的缺失步骤来源：
            # PC2 等 real2.UF-BAKE、real1 等 PC1.BG2-INSP2-REV、PC1 等 real1.AB1IQC-INSP-REV
            # 全部卡死 → all_blocked 死锁 break → 每 lot 缺 70~100 步。
            never_release_keys: set = set()
            for ref in active_refs:
                ref_key = (ref.reference_lot, ref.reference_step or "")
                src = lot_by_name.get(ref.reference_lot)
                if src is None:
                    never_release_keys.add(ref_key)  # (a) 外部源
                    continue
                src_flow = flow_map.get(src.product_name)
                if not src_flow:
                    never_release_keys.add(ref_key)
                    continue
                try:
                    _sref = get_step_index_in_flow(src_flow, ref.reference_step or "")
                except ValueError:
                    _sref = -1
                if _sref < 0:
                    continue
                _scur = _src_cur_idx.get(src.lot_name, 0)
                if _sref < _scur:
                    never_release_keys.add(ref_key)  # (b) 源已越过该步
                    continue
                if src.target_step:
                    try:
                        _stgt = get_step_index_in_flow(src_flow, src.target_step)
                    except ValueError:
                        _stgt = -1
                    if _stgt >= 0 and _sref > _stgt:
                        never_release_keys.add(ref_key)  # (c) 源 target 截断在该步之前
                        continue

            first_blocked = any(
                ref_block_info.get((r.reference_lot, r.reference_step or "")) == 0
                for r in active_refs
                if (r.reference_lot, r.reference_step or "") not in never_release_keys)
            if first_blocked:
                # 首步即被引用阻塞：若该引用在环内已有预测释放，用
                # max(自然就绪, 预测释放) 作为 ready_time（不硬阻塞，避免死锁）；
                # 否则置 FAR_FUTURE 等待真实释放。
                _blocked_keys = [
                    (r.reference_lot, r.reference_step or "")
                    for r in active_refs
                    if (ref_block_info.get((r.reference_lot, r.reference_step or "")) == 0
                        and (r.reference_lot, r.reference_step or "") not in never_release_keys)]
                _blocked_fc = [
                    _cycle_forecast[(lot.lot_name, k)] for k in _blocked_keys
                    if (lot.lot_name, k) in _cycle_forecast]
                if _blocked_fc and len(_blocked_fc) == len(_blocked_keys):
                    ready_time = max([ready_time] + _blocked_fc)
                else:
                    ready_time = FAR_FUTURE

            pending_refs[lot.lot_name] = set()
            ref_release_times[lot.lot_name] = {}
            for ref in active_refs:
                ref_key = (ref.reference_lot, ref.reference_step or "")
                if ref_key in never_release_keys:
                    # 预释放：基于源 lot 就绪时刻（源已越过该步）或 schedule_start（外部源）
                    _src = lot_by_name.get(ref.reference_lot)
                    _rel_base = schedule_start
                    if _src is not None and _src.start_time is not None:
                        _rel_base = max(schedule_start, _src.start_time)
                    ref_release_times[lot.lot_name][ref_key] = _ref_release_offset(
                        _rel_base, ref.start_mod, shift_times)
                    logger.warning(
                        "引用 %s -> %s.%s 源步骤已越过/不在排程，视为已满足（预释放 %s）",
                        lot.lot_name, ref.reference_lot, ref.reference_step or "",
                        ref_release_times[lot.lot_name][ref_key])
                    continue
                pending_refs[lot.lot_name].add(ref_key)
                reference_deps.setdefault(ref_key, []).append(lot.lot_name)
                # 环内预测释放：预先视为已释放，避免互相等待死锁
                fc = _cycle_forecast.get((lot.lot_name, ref_key))
                if fc is not None:
                    ref_release_times[lot.lot_name][ref_key] = fc
                    pending_refs[lot.lot_name].discard(ref_key)

        # FTF qty 变化
        ftf_rule = None
        if ftf_qty_change:
            rule = ftf_qty_change.get(lot.product_name)
            if rule:
                input_num, output_num, change_step = rule
                for i, s in enumerate(product_flow):
                    if s.step_name == change_step and current_idx < i:
                        ftf_rule = (input_num, output_num, change_step)
                        break

        lot_state[lot.lot_name] = {
            "lot": lot,
            "remaining_steps": remaining,
            "ready_time": ready_time,
            "qtime_tracker": {},
            "done": not remaining,
            "step_index": 0,
            "first_step_ct_adj": 0 if not remaining else (
                _effective_running_ct(ct_lookup, special_lot_step_lookup, lot, remaining[0])),
            "ftf_rule": ftf_rule,
            "ref_block_info": ref_block_info,
            "refs_registered": has_refs,
            "_base_ready_time": base_ready_time,
            "chain_reverse_pending": None,  # 链拆后待反向调度的后缀
            "_qtime_hold": None,  # 紧 Q-time 窗口挂起等待的 reference 集合
            "coarse_anchors": coarse_anchors.get(lot.lot_name, []),  # 约束锚点
            "ref_release_forecast": ref_release_forecast or {},  # 第一遍预测锚点
            "_tight_chain_defers": 0,  # 紧链整链块连续 defer 计数（触发终止保护）
        }

    lot_entries: list[ScheduleEntry] = []
    eqp_entries: list[EqpScheduleEntry] = []
    qtime_alerts: list[QTimeAlert] = []

    # 特殊设备批处理状态
    eqp_batch_state: dict = {}

    # ---- 5. 逐步调度主循环 ----
    current_time = schedule_start
    _safety_iters = 0
    import os as _os
    _TRACE = _os.environ.get("SCHED_TRACE") == "1"

    while not all(state["done"] for state in lot_state.values()):
        _safety_iters += 1
        if _safety_iters > 200000:
            stall = [n for n, s in lot_state.items() if not s["done"]]
            detail = "; ".join(
                f"{n}(idx={lot_state[n]['step_index']}/{len(lot_state[n]['remaining_steps'])},ready={lot_state[n]['ready_time']})"
                for n in stall)
            logger.error("调度疑似死循环，中止。未完成: %s", detail)
            break
        # 收集就绪的 lot
        # 先处理 Q-time 挂起恢复：若等待的 reference 已释放则恢复
        for name, state in lot_state.items():
            if state["done"] or not state.get("_qtime_hold"):
                continue
            held = state["_qtime_hold"]
            remaining_pending = pending_refs.get(name, set())
            if held.isdisjoint(remaining_pending):
                state["_qtime_hold"] = None

        ready_lot_names = []
        for name, state in lot_state.items():
            if state["done"]:
                continue
            if state.get("_qtime_hold"):
                # 紧 Q-time 窗口挂起等待中
                continue
            blocked = False
            if state.get("refs_registered"):
                ref_block_info = state.get("ref_block_info", {})
                step_idx = state["step_index"]
                pending = pending_refs.get(name, set())
                for ref_key in pending:
                    block_idx = ref_block_info.get(ref_key)
                    if block_idx is not None and step_idx >= block_idx:
                        blocked = True
                        break
            if not blocked and state["ready_time"] <= current_time:
                ready_lot_names.append(name)

        if not ready_lot_names:
            # 检查死锁
            all_blocked = True
            next_unblocked_time = datetime.max
            for name, state in lot_state.items():
                if state["done"]:
                    continue
                blocked = False
                # Q-time 挂起等待（reference 未释放）也算阻塞：若所有未完成 Lot 都在等
                # 彼此释放 reference，则构成循环等待死锁，应中止而非 1min/轮空转
                if state.get("_qtime_hold"):
                    blocked = True
                if not blocked and state.get("refs_registered"):
                    ref_block_info = state.get("ref_block_info", {})
                    step_idx = state["step_index"]
                    pending = pending_refs.get(name, set())
                    for ref_key in pending:
                        block_idx = ref_block_info.get(ref_key)
                        if block_idx is not None and step_idx >= block_idx:
                            blocked = True
                            break
                if not blocked:
                    all_blocked = False
                    if state["ready_time"] < next_unblocked_time:
                        next_unblocked_time = state["ready_time"]

            if all_blocked:
                blocked_names = [n for n, s in lot_state.items() if not s["done"]]
                # 死锁：所有未完成 Lot 均被 pending reference 阻塞。若能找到
                # "源也在未完成且被阻塞"的引用（构成循环等待——源自己卡在等待里、
                # 无法到达释放步骤，真实释放永远不会到来），则用预测释放打破死锁
                # 后继续，避免整链缺步骤（fuzz 实测：PC2 等 real2.DAF-SAT、
                # real2 又等 PC2.FC-REFLOW，PC2 首步被钉死 → all_blocked → break
                # → 每 lot 缺几十步）。预测释放 = 源该步骤粗排锚点 + CT。
                _relaxed = False
                for _dname in list(blocked_names):
                    _dst = lot_state[_dname]
                    if not _dst.get("refs_registered"):
                        continue
                    for _rk in list(pending_refs.get(_dname, set())):
                        _src_name, _src_step = _rk
                        _src_st = lot_state.get(_src_name)
                        if _src_st is None or _src_st["done"] or _src_name not in blocked_names:
                            continue  # 源已完成/不在死锁环：真实释放会到来，不放松
                        # 预测释放时刻：源该步骤粗排锚点 + CT；无锚点则退回 schedule_start
                        _pred = schedule_start
                        _ca = _dst.get("coarse_anchors") or []
                        _src_flow = flow_map.get(_src_st["lot"].product_name) if _src_st else None
                        _rel_idx = None
                        _src_step_idx = None
                        if _src_flow and _src_step:
                            try:
                                _src_step_idx = get_step_index_in_flow(_src_flow, _src_step)
                            except ValueError:
                                _src_step_idx = None
                            if _src_step_idx is not None:
                                _rel_idx = _src_step_idx - _src_cur_idx.get(_src_name, 0)
                        if _rel_idx is not None and 0 <= _rel_idx < len(_ca) and _ca[_rel_idx] is not None:
                            _ct2 = get_step_ct(ct_lookup, _src_flow[_src_step_idx].product_name,
                                               _src_flow[_src_step_idx].step_number,
                                               _src_st["lot"].qty)
                            _pred = max(schedule_start, _ca[_rel_idx] + timedelta(minutes=_ct2))
                        ref_release_times[_dname][_rk] = _pred
                        pending_refs[_dname].discard(_rk)
                        _relaxed = True
                if _relaxed:
                    logger.warning("死锁环已用预测释放打破: %s", blocked_names)
                    # 重算各 lot 的 blocked ready_time，并清除已满足的 qtime 挂起
                    for _n2, _s2 in lot_state.items():
                        if not _s2["done"]:
                            _update_blocked_ready_time(_n2, _s2, pending_refs, ref_release_times)
                    for _n2, _s2 in lot_state.items():
                        if _s2["done"] or not _s2.get("_qtime_hold"):
                            continue
                        if _s2["_qtime_hold"].isdisjoint(pending_refs.get(_n2, set())):
                            _s2["_qtime_hold"] = None
                    continue
                logger.warning("所有未完成 Lot 均被 reference 阻塞，疑似死锁: %s", blocked_names)
                break

            next_time = next_unblocked_time
            for t in machine_available.values():
                if t > current_time and t < next_time:
                    next_time = t
            if next_time == datetime.max:
                break
            if next_time <= current_time:
                current_time += timedelta(minutes=1)
            else:
                current_time = next_time
            continue

        # 按 lot_order 排序就绪队列，Q-time 感知优先级提升
        def _ready_sort_key(name: str):
            state = lot_state[name]
            base_rank = lot_order_rank.get(name, 999999)
            # 有活跃 Q-time tracker 的 Lot 优先级提升
            has_active_qtime = len(state.get("qtime_tracker", {})) > 0
            qtime_boost = -1000 if has_active_qtime else 0
            # 当前步的粗排程锚点若远在未来（被引用/Q-time 链推到将来，如 PC2 的 PLASMA
            # 因 DISPENSE 等 real2 的 BAKE 而被迫排到次日），则降优先级排在后面，
            # 让当前时间点就能排的 lot 先走——否则这个"远未来步骤"会把 current_time 抬升
            # 数小时，饿死其它已就绪且可立即排的 lot（实测 real1 的 UF 段因此被拖 17 小时）。
            _anchor_pen = 0
            _ca = state.get("coarse_anchors", [])
            _si = state["step_index"]
            if _ca and _si < len(_ca) and _ca[_si] is not None:
                _diff_h = (_ca[_si] - current_time).total_seconds() / 3600.0
                if _diff_h > 6.0:
                    _anchor_pen = 2000
            # 恒组批批次步骤（together=true，如 CURE）：会等凑批并把设备占用数小时，
            # 若排在前面会把 current_time 大幅前推、饿死其它可立即排的 lot（实测 real2 的
            # DISPENSE 因此被拖 6.7h 超 Q）。批次步骤降一级优先级，让非批次步骤先走。
            _batch_pen = 0
            _cur_step = state["remaining_steps"][_si]
            if _cur_step.eqp_ids:
                _batch_pen = 300 if any(
                    e in special_eqp_map and special_eqp_map[e].together
                    for e in _cur_step.eqp_ids) else 0
            return (base_rank + qtime_boost + _anchor_pen + _batch_pen, name)

        ready_lot_names.sort(key=_ready_sort_key)

        # ---- 全链设备可通行性前瞻 + defer（借鉴 scheduler_before1 L837-959）----
        # 对即将进入某 Q-time start_step 的 lot，在临时机器可用性副本上模拟整条
        # start→end 链逐占一台最早可用设备。若中间步骤（如 PLASMA/DISPENSE）当前积压、
        # 无法在 max_duration 内完成全链，则本轮回合不选它（defer），让其它可通行的
        # lot 先排，从而避免"过早打开链首 → 中段卡设备 → 端步骤超 Q"（real bake 太靠前）。
        # 若所有就绪 lot 都被 defer 则退化为正常排序，保证推进不死锁。
        deferred = set()
        for _dname in ready_lot_names:
            _dst = lot_state[_dname]
            _dlt = _dst["lot"]
            _dsp = _dst["remaining_steps"][_dst["step_index"]]
            # 当前步粗排程锚点远在未来（>3h，被引用/Q-time 推到将来）：本轮不选，
            # 让当前时刻即可排/即将就绪的 lot 先走——否则该"远未来步骤"会把 current_time
            # 抬升数小时，饿死其它就绪 lot（实测 real1 的 PLASMA 锚点被 CURE 手动延迟
            # 推到 17:56，在 12:05 被选中后 end=18:03 直接跳过 PC1 的 CURE 就绪 10:18）。
            _ca_d = _dst.get("coarse_anchors", [])
            _csi = _dst["step_index"]
            if _ca_d and _csi < len(_ca_d) and _ca_d[_csi] is not None:
                if (_ca_d[_csi] - current_time).total_seconds() / 3600.0 > 6.0:
                    deferred.add(_dname)
                    continue
            _dpqs = qtime_by_product.get(_dlt.product_name, [])
            if not _dpqs:
                continue
            for _q in _dpqs:
                if _q.start_step != _dsp.step_name:
                    continue
                _steps_rem = _dst["remaining_steps"][_dst["step_index"]:]
                _idx_end = next((i for i, s in enumerate(_steps_rem)
                                 if s.step_name == _q.end_step), None)
                if _idx_end is None:
                    continue
                try:
                    _ct0 = get_step_ct(ct_lookup, _dsp.product_name, _dsp.step_number, _dlt.qty)
                except Exception:
                    _ct0 = 0
                # start_step 最快能上的一台机器
                if _dsp.eqp_ids:
                    _cands = [(machine_available.get(e, current_time), e) for e in _dsp.eqp_ids]
                    _cands.sort()
                    _a0, _ue0 = _cands[0]
                    _s0_start = max(current_time, machine_available.get(_ue0, current_time))
                else:
                    _ue0 = "-"
                    _s0_start = current_time
                _s0_end = _s0_start + timedelta(minutes=max(_ct0, 1 if _ue0 != "-" else 0))
                _q_start = _s0_start if _q.start_mod == "track in" else _s0_end
                _q_deadline = _q_start + timedelta(minutes=int(_q.max_duration))
                # 全链模拟：start→end 每一步占用一台最早可用设备
                _t = _s0_end
                _temp_avail = dict(machine_available)
                if _ue0 != "-":
                    _temp_avail[_ue0] = _s0_end
                _feasible = True
                for _i in range(1, _idx_end + 1):
                    _s = _steps_rem[_i]
                    try:
                        _cs = get_step_ct(ct_lookup, _s.product_name, _s.step_number, _dlt.qty)
                    except Exception:
                        _cs = 0
                    if not _s.eqp_ids:
                        _t += timedelta(minutes=max(_cs, 0))
                        if _t > _q_deadline:
                            _feasible = False
                            break
                        continue
                    _c2 = [( _temp_avail.get(e, _t), e) for e in _s.eqp_ids]
                    _c2.sort()
                    _uav, _ue = _c2[0]
                    _cstart = max(_t, _uav)
                    _cend = _cstart + timedelta(minutes=max(_cs, 1))
                    _temp_avail[_ue] = _cend
                    _t = _cend
                    _ref_end = _cstart if (_i == _idx_end and _q.end_mod == "track in") else _cend
                    if _ref_end > _q_deadline:
                        _feasible = False
                        break
                if not _feasible:
                    deferred.add(_dname)
                # 第二层：关键中间设备串行化进入（借鉴 scheduler_before1 L930-955）。
                # 仅当"A：另一 lot 已把同一 start→end 链打开且未跑完" 且 "B：中间关键
                # 设备当前仍被占用(最早可得 > 现在)" 时才 defer。避免"一下开多个
                # BAKE，后面的抢不到 DISPENSE/PLASMA 中间设备、整链 Q-time 拉爆"。
                if _dname not in deferred:
                    _open_other = 0
                    _crit = set()
                    for _i in range(1, _idx_end):
                        _crit.update(_steps_rem[_i].eqp_ids or [])
                    if _crit:
                        for _os_state in lot_state.values():
                            if _os_state is _dst:
                                continue
                            for _tk in _os_state.get("qtime_tracker", {}).values():
                                if (_tk.get("start_step") == _q.start_step
                                        and _tk.get("end_step") == _q.end_step
                                        and _tk.get("deadline") is not None):
                                    _open_other += 1
                        if _open_other >= 1:
                            _earliest_release = min((machine_available.get(e, current_time)
                                                    for e in _crit), default=current_time)
                            if _earliest_release > current_time:
                                deferred.add(_dname)
                                if _TRACE:
                                    print(f"[DEFER2] t={current_time} {_dname} step={_dsp.step_name} "
                                          f"open_other={_open_other} crit={sorted(_crit)} "
                                          f"release={_earliest_release}")
                break  # 任一 Q-time 不可行就 defer，不再看其他
        not_deferred = [n for n in ready_lot_names if n not in deferred]
        if not not_deferred and deferred and ready_lot_names:
            _next_ready = datetime.max
            for _s2n, _s2 in lot_state.items():
                if _s2["done"] or _s2.get("_qtime_hold"):
                    continue
                if _s2n in deferred:
                    continue  # 远未来锚点，本轮不选，不作为推进目标
                if _s2["ready_time"] <= current_time:
                    continue  # 已就绪的 lot 不在推进目标内（它们在就绪队列里）
                if _s2["ready_time"] < _next_ready:
                    _next_ready = _s2["ready_time"]
            if _next_ready < current_time + timedelta(hours=6):
                current_time = max(current_time + timedelta(minutes=1), _next_ready)
                continue
        # 优先排非 defer 的 lot；全 defer 时退化按原排序推进，保证不死锁
        pick_list = not_deferred if not_deferred else ready_lot_names
        name = pick_list[0]
        state = lot_state[name]
        lot = state["lot"]
        step = state["remaining_steps"][state["step_index"]]
        product_qtimes = qtime_by_product.get(lot.product_name, [])
        if _TRACE:
            print(f"[TRACE] t={current_time} ready={ready_lot_names} pick={name} step={step.step_name} "
                  f"ready_time={state['ready_time']} chain_rev={bool(state.get('chain_reverse_pending'))} "
                  f"hold={state.get('_qtime_hold')}")

        # ---- 检查是否需要链式调度或反向调度 ----
        product_chain_map = qtime_chain_map.get(lot.product_name, {})
        chain_info = product_chain_map.get(step.step_name)

        # 检查是否有待反向调度的链后缀
        if state.get("chain_reverse_pending"):
            _try_schedule_chain_reverse(
                lot, state, flow_map, ct_lookup, qtime_by_product,
                machine_intervals, machine_available,
                special_eqp_map, eqp_batch_state,
                special_lot_step_lookup,
                shift_change_intervals, step_windows_expanded, end_windows_expanded,
                manual_adjust_lookup, pin_lookup, resolve_max_iterations,
                lot_entries, eqp_entries, qtime_alerts,
                pending_refs, state.get("ref_block_info", {}), shift_times,
                reference_deps, lot_state, ref_release_times, priority_wait_map,
                chain_info, eqp_preferences=eqp_preferences)
            current_time = max(current_time, lot_entries[-1].end_time if lot_entries else current_time)
            continue

        # 链式调度
        chain_scheduled = False
        # FTF qty 变化步骤存在时禁用链式：链式路径不消费 ftf_rule（qty 变换会静默丢失），
        # 走单步路径让 5416 行的 FTF 执行逻辑生效（链内 Q 紧凑由链压实兜底）。
        if chain_info and chain_info.get("is_chain_start") and not state.get("ftf_rule"):
            chain_scheduled, scheduled_count = _try_schedule_chain_forward(
                lot, state, chain_info, flow_map, ct_lookup, qtime_by_product,
                machine_intervals, machine_available,
                special_eqp_map, eqp_batch_state,
                special_lot_step_lookup,
                shift_change_intervals, step_windows_expanded, end_windows_expanded,
                manual_adjust_lookup, pin_lookup, resolve_max_iterations,
                lot_entries, eqp_entries, qtime_alerts,
                lot_order_rank, pending_refs, state.get("ref_block_info", {}), shift_times,
                reference_deps, lot_state, ref_release_times, priority_wait_map,
                product_qtimes,  # 传入 product_qtimes 用于 deadline 计算
                chain_placement,  # 链放置策略
                state.get("ref_release_forecast"),  # 第一遍预测锚点
                current_time,  # 真实推进时间（恒组批 busy_until 判定用）
                _cycle_forecast_keys,  # 环内预测释放的 ref key 集合
                eqp_preferences=eqp_preferences,
            )
            if chain_scheduled or state.get("chain_reverse_pending"):
                current_time = max(current_time, lot_entries[-1].end_time if lot_entries else current_time)
                continue
            if scheduled_count > 0:
                # 链部分调度（如 BAKE..DISPENSE 已排好、端步骤 CURE 因设备忙未排）：
                # 链函数已把 step_index 推进到端步骤，但本次 pick 的 step 变量仍指向链首，
                # 若继续落单步会用陈旧 step 重复记录已排步骤并跳过端步骤（CURE 丢失）。
                # 此处重新拾取，让下一轮以更新后的 step_index 处理端步骤。
                current_time = max(current_time, lot_entries[-1].end_time if lot_entries else current_time)
                continue

        # 链调度已设置 hold（reference 未释放）：等待，不单步调度
        if state.get("_qtime_hold"):
            continue

        # 紧链整链块失败已延迟 ready_time：等待下一轮重试，不回退单步（避免散开超 Q）
        if state.pop("_tight_chain_defer", False):
            continue

        # ---- 单步调度（非链首或链调度失败） ----
        ct = get_step_ct(ct_lookup, step.product_name, step.step_number, lot.qty)

        if special_lot_step_lookup:
            sls_key = (lot.lot_name, step.step_name)
            if sls_key in special_lot_step_lookup:
                sls = special_lot_step_lookup[sls_key]
                if sls.special_ct is not None:
                    ct = sls.special_ct

        if state["step_index"] == 0 and state["first_step_ct_adj"] > 0:
            ct = max(0, ct - state["first_step_ct_adj"])

        # 设备选择
        eqp_ids = list(step.eqp_ids) if step.eqp_ids else ["-"]
        if special_lot_step_lookup:
            sls_key = (lot.lot_name, step.step_name)
            if sls_key in special_lot_step_lookup:
                sls = special_lot_step_lookup[sls_key]
                if sls.special_eqp:
                    eqp_ids = list(sls.special_eqp)

        # GA 设备偏好
        pref_key = (lot.lot_name, step.step_name)
        if pref_key in eqp_preferences and len(eqp_ids) > 1:
            preferred = eqp_preferences[pref_key]
            ordered = []
            for p in preferred:
                if p in eqp_ids:
                    ordered.append(p)
            for e in eqp_ids:
                if e not in ordered:
                    ordered.append(e)
            eqp_ids = ordered

        best_eqp = None
        best_start = datetime.max
        # 精确锁定（pin）：目标时刻 = pin_time（若该 step 被锁定）
        _pin_t = None
        if pin_lookup:
            _pin_t = pin_lookup.get((lot.lot_name, step.step_name))
            if _pin_t is None:
                _pin_t = pin_lookup.get((lot.lot_name, None))
        if eqp_ids == ["-"]:
            best_eqp = "-"
            best_start = state["ready_time"]
        else:
            # 设备冲突分：避免抢占其他 Lot 紧 Q-time 端步骤的唯一设备
            _conflicts = _eqp_conflict_scores(eqp_ids, lot, lot_state, qtime_by_product, flow_map)
            for eqp_id in eqp_ids:
                # 特殊设备检查
                ready = state["ready_time"]
                if eqp_id in special_eqp_map and special_eqp_map[eqp_id].together:
                    # 恒组批（together=true）：批次统一槽位（等待凑批/加入已开批次）
                    can_use, adj_time = _compute_batch_slot(
                        eqp_id, lot, step, ct, ready, special_eqp_map[eqp_id],
                        lot_state, special_lot_step_lookup, ct_lookup,
                        eqp_batch_state, priority_wait_map,
                        wait_window=BATCH_WAIT_WINDOW,
                        cur_time=current_time, machine_intervals=machine_intervals, special_eqp_map=special_eqp_map,
                        lot_entries=lot_entries)
                    if not can_use:
                        continue
                    avail = adj_time
                    _c = _conflicts.get(eqp_id, 0)
                    if (avail < best_start) or (avail == best_start and best_eqp is not None
                                                and _c < _conflicts.get(best_eqp, 0)):
                        best_start = avail
                        best_eqp = eqp_id
                    continue
                if eqp_id in special_eqp_map:
                    can_use, adj_time = _check_special_eqp_available(
                        eqp_id, lot.qty, ready, ct,
                        special_eqp_map, eqp_batch_state, machine_intervals,
                        cur_time=current_time)
                    if not can_use:
                        # 容量/锁定未释放：把 lot 就绪推进到最早释放时刻，避免
                        # 每轮以旧 ready 重试导致主循环空转（探针 P12 时序）。
                        if adj_time > state["ready_time"]:
                            state["ready_time"] = adj_time
                            state["_base_ready_time"] = adj_time
                        continue
                    ready = max(ready, adj_time)

                if _is_parallel_eqp(eqp_id, special_eqp_map):
                    # 并行型特殊设备（together=false）：到点即入，不需互斥排他
                    avail = ready
                    _c = _conflicts.get(eqp_id, 0)
                    if (avail < best_start) or (avail == best_start and best_eqp is not None
                                                and _c < _conflicts.get(best_eqp, 0)):
                        best_start = avail
                        best_eqp = eqp_id
                    continue

                if _pin_t is not None:
                    # pin：优先寻找恰好可从 pin_time 开始的空闲槽位（精确命中）
                    avail = _find_earliest_slot(
                        machine_intervals.get(eqp_id, []),
                        max(ready, _pin_t),
                        timedelta(minutes=ct))
                    # 精确命中 pin_time 的槽位优先于其他设备的最早槽位
                    _c = _conflicts.get(eqp_id, 0)
                    _exact = (avail == max(ready, _pin_t))
                    _prev_exact = (best_eqp is not None and best_start == max(
                        state["ready_time"], _pin_t))
                    if _exact and not _prev_exact:
                        best_start = avail
                        best_eqp = eqp_id
                    elif _exact and _prev_exact:
                        if (_c < _conflicts.get(best_eqp, 0)) or (
                                _c == _conflicts.get(best_eqp, 0) and avail < best_start):
                            best_start = avail
                            best_eqp = eqp_id
                    elif not _exact and not _prev_exact:
                        if (avail < best_start) or (avail == best_start and best_eqp is not None
                                                    and _c < _conflicts.get(best_eqp, 0)):
                            best_start = avail
                            best_eqp = eqp_id
                else:
                    avail = _find_earliest_slot(
                        machine_intervals.get(eqp_id, []),
                        ready,
                        timedelta(minutes=ct))
                    # 同槽位更早时优先选冲突分更低的设备
                    _c = _conflicts.get(eqp_id, 0)
                    if (avail < best_start) or (avail == best_start and best_eqp is not None
                                                and _c < _conflicts.get(best_eqp, 0)):
                        best_start = avail
                        best_eqp = eqp_id

        if best_eqp is None:
            # 设备全忙 / 恒组批批次忙（busy_until）/ 并行上限未到释放时刻：
            # 不能原地空转——把时钟推进到"本步骤候选设备"的最早可用未来时刻，
            # 否则同一 ready lot 每轮被选却无法落步，主循环磨满 20 万轮（实测
            # real1 的 UF-CURE 在 PKPOV001 批次忙时被无限重试，DISPENSE 做完 CURE 一直等）。
            _wait_until = None
            for _eid in eqp_ids:
                _bs = eqp_batch_state.get(_eid)
                if _bs is not None:
                    _bu = _bs.get("busy_until")
                    if _bu is not None and _bu > current_time:
                        _wait_until = _bu if _wait_until is None else min(_wait_until, _bu)
                    # 待凑批次尚未开炉（busy_until 未设）：等到 pending.start
                    _pend = _bs.get("pending")
                    if _pend is not None:
                        _ps = _pend.get("start")
                        if _ps is not None and _ps > current_time:
                            _wait_until = _ps if _wait_until is None else min(_wait_until, _ps)
                    for _aln, _aq, _aet in _bs.get("active", []):
                        if _aet > current_time:
                            _wait_until = _aet if _wait_until is None else min(_wait_until, _aet)
                _ma = machine_available.get(_eid)
                if _ma is not None and _ma > current_time:
                    _wait_until = _ma if _wait_until is None else min(_wait_until, _ma)
            if _wait_until is None:
                _wait_until = current_time + timedelta(minutes=1)
            current_time = _wait_until
            continue

        start_time = best_start
        # 粗排程锚点下限：约束关系（reference/Q-time 链）决定该 step 的最早可行开始，
        # 若贪心选到的槽位早于锚点，则推迟到锚点（避免链被拉长 / reference 未满足）
        _ca = state.get("coarse_anchors", [])
        if _ca and state["step_index"] < len(_ca) and _ca[state["step_index"]] > start_time:
            start_time = _ca[state["step_index"]]
        end_time = start_time + timedelta(minutes=ct)

        # ---- 紧 Q-time 窗口前瞻：延迟起点，避免端步骤超限 ----
        if product_qtimes:
            qh = _tight_qtime_target_start(
                lot, state, step, ct, product_qtimes,
                state["ready_time"], start_time,
                machine_available, machine_intervals,
                pending_refs, ref_release_times, special_lot_step_lookup, ct_lookup,
                shift_change_intervals, step_windows_expanded, end_windows_expanded,
                priority_wait_map, state.get("ref_release_forecast"),
                manual_adjust_lookup=manual_adjust_lookup, pin_lookup=pin_lookup)
            if isinstance(qh, tuple) and qh[0] == "DEFER":
                # 端步骤 reference 未释放，暂不能调度：挂起本 lot，等待释放
                state["_qtime_hold"] = set(qh[1])
                continue
            if isinstance(qh, datetime):
                if qh > start_time:
                    start_time = qh
                    end_time = start_time + timedelta(minutes=ct)

        # 手动调整
        start_time, end_time = _apply_manual_adjust(
            lot.lot_name, step.step_name, start_time, end_time, ct, manual_adjust_lookup,
            pin_lookup=pin_lookup)

        # 约束
        start_time = _resolve_constraints(
            start_time, ct, best_eqp,
            machine_intervals, shift_change_intervals,
            step_windows_expanded, end_windows_expanded,
            step.step_name, max_iterations=resolve_max_iterations)

        if start_time == datetime.max:
            if verbose or _WARN_ENV:
                logger.warning("Lot %s step %s 无法满足约束，跳过", lot.lot_name, step.step_name)
            continue

        end_time = start_time + timedelta(minutes=ct)

        # 重新应用手动调整
        start_time, end_time = _apply_manual_adjust(
            lot.lot_name, step.step_name, start_time, end_time, ct, manual_adjust_lookup,
            pin_lookup=pin_lookup, reapply=True)

        # Q-time 检查
        _check_qtime_start(state, step.step_name, product_qtimes, start_time, end_time)
        qtime_risk = _check_qtime_end_for_step(
            state, step.step_name, product_qtimes,
            start_time, end_time, lot.lot_name, qtime_alerts)

        # 记录
        _record_step_entry(
            lot, step, best_eqp, start_time, end_time, ct, qtime_risk,
            lot_entries, eqp_entries)

        # 更新设备占用
        if best_eqp != "-":
            _is_par = _is_parallel_eqp(best_eqp, special_eqp_map)
            _is_tog = best_eqp in special_eqp_map and special_eqp_map[best_eqp].together
            if not _is_par and not _is_tog:
                # 并行型（together=false）与恒组批（together=true）特殊设备：批次内多条
                # 占用区间相互重叠是正常且允许的，不写入排他 machine_intervals。
                _add_machine_interval(machine_intervals, best_eqp, (start_time, end_time))
            machine_available[best_eqp] = end_time
            if best_eqp in special_eqp_map:
                _register_special_eqp_usage(
                    best_eqp, lot.lot_name, lot.qty, start_time, end_time,
                    special_eqp_map, eqp_batch_state)

        # 释放 reference
        _release_refs_for_step(
            lot.lot_name, step.step_name, end_time,
            reference_deps, lot_state, pending_refs, ref_release_times, shift_times)

        # FTF qty 变化
        if state.get("ftf_rule") and step.step_name == state["ftf_rule"][2]:
            input_num, output_num, _change_step = state["ftf_rule"]
            new_qty = math.ceil(lot.qty * input_num / output_num)
            lot.qty = new_qty
            state["ftf_rule"] = None

        # 推进到下一步骤
        state["step_index"] += 1
        if state["step_index"] >= len(state["remaining_steps"]):
            state["done"] = True
        else:
            step_wait = _effective_chain_wait(lot, chain_info, priority_wait_map)
            # lead 跟随批的衔接步：lot 已在等待领导批完成，满足条件即开始，不再额外等待
            # （wait time 是 lot 内部相邻 step 的机制，衔接步本身除外——衔接由引用边驱动）
            _nxt = state["remaining_steps"][state["step_index"]].step_name \
                if state["step_index"] < len(state["remaining_steps"]) else None
            if _nxt and any(getattr(r, "lead_id", "") and r.start_step == _nxt
                            for r in (lot.references or [])):
                step_wait = 0.0
            base_ready = end_time + timedelta(minutes=step_wait)
            state["_base_ready_time"] = base_ready
            state["ready_time"] = base_ready

            # Per-reference blocking（含已释放但尚未到达的 release 时间）
            if state.get("refs_registered"):
                _update_blocked_ready_time(name, state, pending_refs, ref_release_times)

        if _os.environ.get("SCHED_DBG4") == "1" and end_time - current_time > timedelta(minutes=120) and current_time >= datetime(2026,8,18,14,0):
            print(f"[DBG5] jump current={current_time.strftime('%m/%d %H:%M')} -> {end_time.strftime('%m/%d %H:%M')} by {name}:{step.step_name} eqp={best_eqp}", flush=True)
        current_time = max(current_time, end_time)

    # ---- 排程合理性智能检测（不改变结果，只告警） ----
    try:
        _warns = _detect_schedule_anomalies(
            lot_entries, lots, flow_map, ct_lookup, priority_wait_map or {},
            anchor_audit=dict(_anchor_audit))
        for _w in _warns:
            if verbose or _WARN_ENV:
                logger.warning("[排程合理性] %s", _w)
        if out_warnings is not None:
            out_warnings.extend(_warns)
    except Exception as _e:  # 检测失败不影响排程结果
        logger.warning("排程合理性检测异常: %s", _e)

    return lot_entries, eqp_entries, qtime_alerts