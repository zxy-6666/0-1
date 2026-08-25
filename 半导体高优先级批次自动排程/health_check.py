"""排程前数据体检（health check）

在排程前检查数据一致性问题，避免排程中途报错或产出畸形结果。
所有检查只读数据，不做任何修改；返回分级结果供前端展示。

分级:
  - errors:   会直接导致排程失败或结果错误的问题（需修复）
  - warnings: 可能导致结果偏差/缺失的问题（建议修复）
"""
from __future__ import annotations

from data_loader import get_step_index_in_flow, get_product_flow_map


def _flow_step_names(flow_steps) -> set:
    return {s.step_name for s in flow_steps}


def check_data(
    lots,
    flows,
    step_cts,
    qtimes,
    priority_wait_map=None,
    special_lot_step_lookup=None,
    lot_constraints=None,
    manual_adjusts=None,
    special_eqp_map=None,
    ftf_qty_change=None,
) -> dict:
    """执行全部数据体检项，返回 {"errors": [...], "warnings": [...]}"""
    errors: list[str] = []
    warnings: list[str] = []

    flow_map = get_product_flow_map(flows)
    product_names = set(flow_map.keys())

    # ---- 1. 重复 lot ----
    seen_lots: dict[str, int] = {}
    for lot in lots:
        seen_lots[lot.lot_name] = seen_lots.get(lot.lot_name, 0) + 1
    for name, cnt in seen_lots.items():
        if cnt > 1:
            errors.append(f"Lot [{name}] 在 lot_list.csv 中出现 {cnt} 次（重复）")

    # ---- 2. lot 无流程 / 当前步骤不在流程 ----
    for lot in lots:
        if lot.product_name not in product_names:
            errors.append(f"Lot [{lot.lot_name}] 产品 [{lot.product_name}] 在 flow.csv 中无流程")
            continue
        flow = flow_map[lot.product_name]
        names = _flow_step_names(flow)
        if lot.current_step_name not in names:
            errors.append(f"Lot [{lot.lot_name}] 当前步骤 [{lot.current_step_name}] 不在其产品流程中")
        if lot.target_step and lot.target_step not in names:
            warnings.append(f"Lot [{lot.lot_name}] 目标步骤 [{lot.target_step}] 不在其产品流程中（将排到底）")

    # ---- 3. 流程步骤缺少 CT ----
    ct_keys = {(c.product_name, c.step_number) for c in step_cts}
    for f in flows:
        if (f.product_name, f.step_number) not in ct_keys:
            warnings.append(
                f"步骤 [{f.product_name}/{f.step_name}] 在 step_ct.csv 无记录"
                f"（排程时会自动插值补 CT）")

    # ---- 4. Q-time 检查 ----
    for q in qtimes or []:
        if q.product_name not in product_names:
            errors.append(f"Q-time 规则 [{q.start_step}→{q.end_step}] 产品 [{q.product_name}] 无流程")
            continue
        names = _flow_step_names(flow_map[q.product_name])
        if q.start_step not in names:
            errors.append(f"Q-time 规则产品 [{q.product_name}] 起始步骤 [{q.start_step}] 不在流程")
        if q.end_step not in names:
            errors.append(f"Q-time 规则产品 [{q.product_name}] 结束步骤 [{q.end_step}] 不在流程")

    # ---- 5. 优先级等待缺项 ----
    if priority_wait_map:
        missing_pri = set()
        for lot in lots:
            if lot.priority not in priority_wait_map:
                missing_pri.add(lot.priority)
        for pri in sorted(missing_pri):
            warnings.append(
                f"优先级 [{pri[0]}-{pri[1]}] 在 priority_wait.csv 中无等待时间（使用默认）")

    # ---- 6. special_lot_step 的 lot/step 不存在 ----
    for (lot_name, step_name), _sls in (special_lot_step_lookup or {}).items():
        lot_obj = next((l for l in lots if l.lot_name == lot_name), None)
        if lot_obj is None:
            errors.append(f"special_lot_step.csv 中 Lot [{lot_name}] 不存在于 lot_list.csv")
            continue
        flow = flow_map.get(lot_obj.product_name, [])
        names = _flow_step_names(flow)
        if step_name not in names:
            errors.append(f"special_lot_step.csv 中步骤 [{lot_name}/{step_name}] 不在其产品流程中")

    # ---- 7. 手动调整的 lot/step 不存在 ----
    for ma in manual_adjusts or []:
        lot_obj = next((l for l in lots if l.lot_name == ma.lot_name), None)
        if lot_obj is None:
            errors.append(f"manual_adjust.csv 中 Lot [{ma.lot_name}] 不存在于 lot_list.csv")
            continue
        if ma.step_name:
            flow = flow_map.get(lot_obj.product_name, [])
            names = _flow_step_names(flow)
            if ma.step_name not in names:
                warnings.append(f"manual_adjust.csv 中步骤 [{ma.lot_name}/{ma.step_name}] 不在其产品流程中")

    # ---- 8. lot_constraints 的 reference 不存在 ----
    for c in lot_constraints or []:
        if not c.reference_lot or not c.reference_step:
            continue
        ref_lot = next((l for l in lots if l.lot_name == c.reference_lot), None)
        if ref_lot is None:
            errors.append(f"lot_constraints.csv 中 reference_lot [{c.reference_lot}] 不存在")
            continue
        ref_flow = flow_map.get(ref_lot.product_name, [])
        if c.reference_step not in _flow_step_names(ref_flow):
            errors.append(
                f"lot_constraints.csv 中 reference_step [{c.reference_lot}/{c.reference_step}] 不在其产品流程中")
        if c.start_step:
            lot_obj = next((l for l in lots if l.lot_name == c.lot_name), None)
            if lot_obj and c.start_step not in _flow_step_names(flow_map.get(lot_obj.product_name, [])):
                errors.append(
                    f"lot_constraints.csv 中 start_step [{c.lot_name}/{c.start_step}] 不在其产品流程中")

    # ---- 9. 特殊设备引用的设备不在任何流程 ----
    if special_eqp_map:
        all_eqp = {e for f in flows for e in f.eqp_ids}
        for eqp_name in special_eqp_map:
            if eqp_name not in all_eqp:
                warnings.append(f"special_eqp.csv 中设备 [{eqp_name}] 未在任何流程步骤中出现")

    # ---- 10. FTF qty 变化 ----
    for product, (inp, outp, change_step) in (ftf_qty_change or {}).items():
        if product not in product_names:
            errors.append(f"ftf_qty_change.csv 中产品 [{product}] 无流程")
            continue
        if change_step and change_step not in _flow_step_names(flow_map[product]):
            warnings.append(f"ftf_qty_change.csv 中步骤 [{product}/{change_step}] 不在其产品流程中")

    return {"errors": errors, "warnings": warnings}
