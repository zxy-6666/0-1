"""种子随机局部搜索优化器

基于启发式构造解 schedule() 的迭代择优：
- 每轮用 rng 生成一组变化：(lot_order 加权洗牌, eqp_preferences 部分随机,
  chain_placement 随机选 "compact"/"early")
- 调 scheduler.schedule() 构造完整解
- validation.validate_schedule() 全量校验（Q-time / reference / 设备 / 顺序）
- 合法解用 compute_objective() 打分，比当前 best 更优则替换
- 非法解直接丢弃（回退），下一轮用新策略

特性：
- 可复现：相同 seed + 相同 max_iterations → 结果一致
- 变化性：不同 seed → 不同批次顺序/设备偏好/链策略
- 若所有轮都无合法解，返回"违规最少"的解并标记 warning

目标（用户确认）：
1. 主目标：加权总完工时间最短（所有批次都考虑，高优先级权重更大）
2. 硬约束：0 Q-time 超限 + 0 reference 违背 + 设备不重叠 + 顺序正确
3. 次目标：Q-time 剩余余量尽量大（由构造解本身的紧凑链保证）
"""
from __future__ import annotations

import copy
import random
from datetime import datetime

from scheduler import schedule
from validation import validate_schedule, compute_objective


def _weighted_shuffle(names: list[str], rng: random.Random,
                      priority_rank: dict = None, n_shuffles: int = 2) -> list[str]:
    """对批次顺序做少量加权随机交换：高优先级（rank 小）批次被选中参与交换的
    概率更低、更可能保持在靠前位置，低优先级批次可后移/试探，提供搜索多样性。

    priority_rank: {lot_name: rank}，rank 0 = 最高优先级；缺省时按原顺序编号。
    """
    order = list(names)
    n = len(order)
    if n < 2:
        return order
    if priority_rank is None:
        priority_rank = {nm: i for i, nm in enumerate(order)}
    for _ in range(n_shuffles):
        # 权重 w = 1 / (rank + 1)：rank 越大（优先级越低）越容易被抽中交换
        weights = [1.0 / (priority_rank.get(nm, n) + 1) for nm in order]
        total = sum(weights)

        def _pick():
            r = rng.random() * total
            acc = 0.0
            for k in range(n):
                acc += weights[k]
                if r <= acc:
                    return k
            return n - 1

        i = _pick()
        j = _pick()
        order[i], order[j] = order[j], order[i]
    return order


def _sample_eqp_preferences(lots, flows, rng: random.Random, n: int = 8) -> dict:
    """随机抽取部分 (lot, step) 的设备偏好（用于探索不同设备分配）。

    仅对多设备步骤采样，生成一个随机的设备优先序，交给 scheduler 尝试；
    换 seed / 每轮迭代都重新随机，提供搜索多样性。
    """
    prefs: dict = {}
    flow_map: dict[str, list] = {}
    for f in flows:
        flow_map.setdefault(f.product_name, []).append(f)
    candidates = []
    for lot in lots:
        fl = flow_map.get(lot.product_name, [])
        for s in fl:
            if len(s.eqp_ids) > 1:
                candidates.append((lot.lot_name, s.step_name, list(s.eqp_ids)))
    rng.shuffle(candidates)
    for lot_name, step_name, eqps in candidates[:n]:
        shuffled = list(eqps)
        rng.shuffle(shuffled)
        prefs[(lot_name, step_name)] = shuffled
    return prefs


def schedule_optimized(
    lots,
    flows,
    ct_lookup,
    qtimes,
    shift_times,
    ftf_qty_change=None,
    special_lot_step_lookup=None,
    priority_wait_map=None,
    eqp_constraints=None,
    step_time_window_constraints=None,
    shift_change_times=None,
    manual_adjusts=None,
    special_eqp_map=None,
    lot_constraints=None,
    resolve_max_iterations: int = 10,
    max_iterations: int = 40,
    seed: int = 0,
    weight_by_priority: bool = True,
    verbose: bool = False,
    early_stop_patience: int = 0,
    # ---- 算法旋钮（None 使用调度器默认） ----
    tight_chain_threshold: int = None,
    qtight_safety_margin: int = None,
    chain_wait_safety: int = None,
    # ---- SA+Tabu 细调层（借鉴 meta_heuristic_before） ----
    refine_enabled: bool = True,
    refine_max_iterations: int = 60,
    tabu_tenure: int = 8,
    sa_temperature_start: float = 200.0,
    sa_temperature_end: float = 2.0,
    target_accept_rate: float = 0.3,
    adapt_window: int = 20,
):
    """多 seed 构造择优 + SA+Tabu 细调，返回 (best_lot_entries, best_eqp_entries,
    best_alerts, meta)。

    meta 含: best_score, valid_iterations, total_iterations, warning, seed,
             best_lot_order, completion_times
    """
    rng = random.Random(seed)
    base_names = [l.lot_name for l in lots]
    # 优先级排序稳定：按 (priority, name) 排序作为默认尝试顺序与权重基准
    sorted_names = [l.lot_name for l in sorted(lots, key=lambda l: (l.priority, l.lot_name))]
    priority_rank = {name: i for i, name in enumerate(sorted_names)}

    best = None
    best_score = None
    best_margin = None  # 同分时用 Q-time 余量（越大越好）做次目标
    best_meta = None
    valid_iterations = 0
    best_violating = None
    best_violating_count = None
    best_violating_meta = {"iter": None, "errors": []}

    # schedule_start 由 schedule() 内部计算，这里用 min start_time 近似打分基准
    schedule_start = min((l.start_time for l in lots if l.start_time is not None),
                         default=datetime.now())

    no_improve = 0  # 连续无改进轮数（自适应早停）
    iters_done = 0

    for it in range(max_iterations):
        iters_done = it + 1
        if it == 0:
            lot_order = list(sorted_names)
        else:
            lot_order = _weighted_shuffle(base_names, rng, priority_rank=priority_rank)

        eqp_prefs = _sample_eqp_preferences(lots, flows, rng)
        chain_placement = rng.choice(["compact", "early"]) if it > 0 else "compact"

        # 每轮用 lots 的浅拷贝：scheduler 内 FTF 数量变化会写回 lot.qty，
        # 直接复用同一列表会导致跨轮污染、结果不可复现（历史 bug）。
        iter_lots = [copy.copy(l) for l in lots]

        iter_warnings: list[str] = []
        try:
            le, ee, qa = schedule(
                lots=iter_lots, flows=flows, ct_lookup=ct_lookup, qtimes=qtimes,
                shift_times=shift_times,
                ftf_qty_change=ftf_qty_change,
                special_lot_step_lookup=special_lot_step_lookup,
                priority_wait_map=priority_wait_map,
                eqp_constraints=eqp_constraints,
                step_time_window_constraints=step_time_window_constraints,
                shift_change_times=shift_change_times,
                manual_adjusts=manual_adjusts,
                special_eqp_map=special_eqp_map,
                resolve_max_iterations=resolve_max_iterations,
                lot_order=lot_order,
                eqp_preferences=eqp_prefs,
                chain_placement=chain_placement,
                tight_chain_threshold=tight_chain_threshold,
                qtight_safety_margin=qtight_safety_margin,
                chain_wait_safety=chain_wait_safety,
                out_warnings=iter_warnings,
            )
        except Exception as e:
            if verbose:
                print(f"  [iter {it}] schedule 异常: {e}")
            continue

        errors = validate_schedule(
            le, ee, qa, iter_lots, flows, qtimes,
            lot_constraints=lot_constraints, shift_times=shift_times,
            special_eqp_map=special_eqp_map)

        if not errors:
            valid_iterations += 1
            obj = compute_objective(le, iter_lots, schedule_start, weight_by_priority,
                                    qtimes=qtimes)
            score = obj["score"]
            margin = obj.get("min_qtime_margin")  # None 表示无 Q-time
            # 主目标：score 更小优先；同分时次目标：Q-time 余量更大优先
            better = False
            if best is None:
                better = True
            elif score < best_score - 1e-6:
                better = True
            elif abs(score - best_score) <= 1e-6:
                if margin is None and best_margin is None:
                    better = False
                elif margin is None:
                    better = False
                elif best_margin is None:
                    better = True
                elif margin > best_margin + 1e-6:
                    better = True
            if better:
                best = (le, ee, qa)
                best_score = score
                best_margin = margin
                best_meta = {
                    "iter": it, "lot_order": lot_order,
                    "eqp_prefs": eqp_prefs,
                    "chain_placement": chain_placement,
                    "score": score,
                    "min_qtime_margin": margin,
                    "completion_times": obj["completion_times"],
                    "schedule_warnings": list(iter_warnings),
                }
                no_improve = 0
            else:
                no_improve += 1
        else:
            # 记录违规最少的解（兜底）
            if best_violating is None or len(errors) < best_violating_count:
                best_violating = (le, ee, qa)
                best_violating_count = len(errors)
                best_violating_meta = {"iter": it, "errors": list(errors),
                                       "schedule_warnings": list(iter_warnings)}

        # 自适应早停：已得合法解后连续 N 轮无改进则提前终止（0=关闭）
        if (early_stop_patience > 0 and best is not None
                and no_improve >= early_stop_patience):
            break

    # ============================================================
    # SA+Tabu 细调层（借鉴 meta_heuristic_before，仅在已得合法解叠加）
    # ============================================================
    if (refine_enabled and refine_max_iterations > 0 and best is not None
            and best_meta is not None):
        import math

        def _eval_solution(lo, ep, ch):
            """构造并全量校验，返回 (errors, obj, le, ee, qa)；异常或非法返回 None-ish。"""
            try:
                eval_lots = [copy.copy(l) for l in lots]  # 避免 FTF qty 写回污染
                rle, ree, rqa = schedule(
                    lots=eval_lots, flows=flows, ct_lookup=ct_lookup, qtimes=qtimes,
                    shift_times=shift_times, ftf_qty_change=ftf_qty_change,
                    special_lot_step_lookup=special_lot_step_lookup,
                    priority_wait_map=priority_wait_map,
                    eqp_constraints=eqp_constraints,
                    step_time_window_constraints=step_time_window_constraints,
                    shift_change_times=shift_change_times,
                    manual_adjusts=manual_adjusts, special_eqp_map=special_eqp_map,
                    resolve_max_iterations=resolve_max_iterations,
                    lot_order=lo, eqp_preferences=ep, chain_placement=ch,
                    tight_chain_threshold=tight_chain_threshold,
                    qtight_safety_margin=qtight_safety_margin,
                    chain_wait_safety=chain_wait_safety)
            except Exception:
                return None
            errs = validate_schedule(
                rle, ree, rqa, eval_lots, flows, qtimes,
                lot_constraints=lot_constraints, shift_times=shift_times,
                special_eqp_map=special_eqp_map)
            if errs:
                return (errs, None, rle, ree, rqa)
            obj = compute_objective(rle, eval_lots, schedule_start, weight_by_priority,
                                    qtimes=qtimes)
            return (errs, obj, rle, ree, rqa)

        def _better(a_score, a_margin, b_score, b_margin):
            if b_score is None:
                return True
            if a_score < b_score - 1e-6:
                return True
            if abs(a_score - b_score) <= 1e-6:
                if a_margin is None and b_margin is not None:
                    return False
                if b_margin is None:
                    return a_margin is not None
                return a_margin > b_margin + 1e-6
            return False

        # 当前解决策变量
        cur_lo = list(best_meta["lot_order"])
        cur_ep = {k: list(v) for k, v in (best_meta.get("eqp_prefs") or {}).items()}
        cur_ch = best_meta.get("chain_placement", "compact")

        best_ref_obj_score = best_score
        best_ref_margin = best_margin
        best_ref_le, best_ref_ee, best_ref_qa = best
        best_ref_meta = dict(best_meta)

        alpha = math.exp(math.log(max(sa_temperature_end, 1e-9) / max(sa_temperature_start, 1e-9))
                         / max(refine_max_iterations - 1, 1))
        T = max(sa_temperature_start, 1e-9)
        tabu: dict[str, int] = {}
        op_names = ["order_swap", "order_shuffle", "eqp_swap", "eqp_shuffle"]
        op_weights = {n: 1.0 / len(op_names) for n in op_names}
        op_contrib = {n: [] for n in op_names}
        op_used = {n: 0 for n in op_names}
        accept_window: list[bool] = []
        _t_iters = 0

        for _it in range(refine_max_iterations):
            _t_iters += 1
            # 选算子（轮盘赌）
            r_roll = rng.random() * sum(op_weights.values())
            _acc = 0.0
            for n in op_names:
                _acc += op_weights[n]
                if r_roll <= _acc:
                    op = n
                    break
            op_used[op] += 1

            # 生成邻域
            nb_lo = list(cur_lo)
            nb_ep = {k: list(v) for k, v in cur_ep.items()}
            nb_ch = cur_ch
            move_id = None
            nbl = len(nb_lo)
            if op in ("order_swap", "order_shuffle") and nbl >= 2:
                i = rng.randrange(nbl)
                j = rng.randrange(nbl)
                if op == "order_swap":
                    nb_lo[i], nb_lo[j] = nb_lo[j], nb_lo[i]
                    move_id = f"os:{i},{j}"
                else:
                    a, b = sorted((i, j))
                    seg = nb_lo[a:b + 1]
                    rng.shuffle(seg)
                    nb_lo[a:b + 1] = seg
                    move_id = f"sh:{a},{b}"
            elif op in ("eqp_swap", "eqp_shuffle") and nb_ep:
                keys = list(nb_ep.keys())
                if op == "eqp_shuffle":
                    k = keys[rng.randrange(len(keys))]
                    rng.shuffle(nb_ep[k])
                    move_id = f"eqsh:{k[1] if isinstance(k, tuple) else k}"
                else:
                    if len(keys) >= 2:
                        k1 = keys[rng.randrange(len(keys))]
                        k2 = keys[rng.randrange(len(keys))]
                        nb_ep[k1], nb_ep[k2] = nb_ep[k2], nb_ep[k1]
                        move_id = f"eqsw:{id(k1)}"
            if move_id is None:
                # 兜底：切换链放置
                nb_ch = "early" if cur_ch != "early" else "compact"
                move_id = "chain_toggle"
                op = "chain_toggle"
                op_used[op] = op_used.get(op, 0) + 1

            is_tabu = move_id in tabu and tabu[move_id] > _t_iters
            res = _eval_solution(nb_lo, nb_ep, nb_ch)
            if res is None:
                accept_window.append(False)
                T *= alpha
                continue
            _errs, _obj, _nle, _nee, _nqa = res
            if _obj is None:
                # 非法邻域：直接回退，不采纳
                accept_window.append(False)
                T *= alpha
                continue
            nb_score = _obj["score"]
            nb_margin = _obj.get("min_qtime_margin")
            delta = nb_score - best_ref_obj_score
            # 记录算子贡献（delta<0 时贡献 = -delta）
            if delta < 0:
                op_contrib[op].append(-delta)

            accepted = False
            improve = False
            aspiration = False
            if is_tabu and _better(nb_score, nb_margin, best_ref_obj_score, best_ref_margin):
                aspiration = True
            if delta <= 1e-6:
                accepted = True
                if _better(nb_score, nb_margin, best_ref_obj_score, best_ref_margin):
                    improve = True
            else:
                prob = math.exp(-delta / T)
                if rng.random() < prob:
                    accepted = True
            if is_tabu and not aspiration:
                accepted = False
            accept_window.append(accepted)

            if accepted:
                cur_lo = nb_lo
                cur_ep = nb_ep
                cur_ch = nb_ch
                if move_id is not None:
                    tabu[move_id] = _t_iters + tabu_tenure
                if improve:
                    best_ref_obj_score = nb_score
                    best_ref_margin = nb_margin
                    best_ref_le, best_ref_ee, best_ref_qa = _nle, _nee, _nqa
                    best_ref_meta = {"iter": 10000 + _t_iters, "lot_order": nb_lo,
                                     "eqp_prefs": nb_ep, "chain_placement": nb_ch,
                                     "score": nb_score, "min_qtime_margin": nb_margin,
                                     "completion_times": _obj["completion_times"],
                                     "schedule_warnings": list(iter_warnings)}

            # 清理过期 tabu
            for k in [k for k, v in tabu.items() if v <= _t_iters]:
                del tabu[k]

            # 温度自适应
            if len(accept_window) >= adapt_window:
                recent = accept_window[-adapt_window:]
                rate = sum(1 for b in recent if b) / len(recent)
                rate_diff = rate - target_accept_rate
                adapt_factor = 1.0 - 0.4 * rate_diff
                adapt_factor = max(0.5, min(2.0, adapt_factor))
                T = T * alpha * adapt_factor
            else:
                T *= alpha

            # 算子权重自适应
            if _t_iters % max(adapt_window, 1) == 0:
                contrib_sum = 0.0
                for n in op_names:
                    c = sum(op_contrib[n][-adapt_window:]) if op_contrib[n] else 0.0
                    op_weights[n] = c if c > 0 else 0.0
                total = sum(op_weights.values())
                if total > 0:
                    for n in op_names:
                        op_weights[n] = max(0.05, min(0.8, op_weights[n] / total))
                else:
                    for n in op_names:
                        op_weights[n] = 1.0 / len(op_names)
                tot2 = sum(op_weights.values())
                for n in op_names:
                    op_weights[n] = op_weights[n] / tot2

        # 采纳细调更好解
        if _better(best_ref_obj_score, best_ref_margin, best_score, best_margin):
            best = (best_ref_le, best_ref_ee, best_ref_qa)
            best_score = best_ref_obj_score
            best_margin = best_ref_margin
            best_meta = best_ref_meta

    if best is None:
        warning = True
        if best_violating is None:
            # 所有迭代都未产出任何解（构造层全部异常）
            best_violating = ([], [], [])
        le, ee, qa = best_violating
        meta = {
            "best_score": None,
            "valid_iterations": 0,
            "total_iterations": iters_done,
            "warning": "未找到完全合法解，已返回违规最少的解",
            "violations": (best_violating_meta or {}).get("errors", []),
            "schedule_warnings": list((best_violating_meta or {}).get("schedule_warnings", [])),
            "seed": seed,
        }
    else:
        warning = False
        le, ee, qa = best
        meta = {
            "best_score": best_score,
            "min_qtime_margin": best_margin,
            "valid_iterations": valid_iterations,
            "total_iterations": iters_done,
            "warning": None,
            "seed": seed,
            **best_meta,
        }

    meta["weight_by_priority"] = weight_by_priority
    return le, ee, qa, meta
