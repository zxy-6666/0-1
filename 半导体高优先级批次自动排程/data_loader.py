"""数据加载与校验"""
import os
import pandas as pd
from datetime import datetime
from typing import Optional
from models import Lot, LotConstraint, LeadPair, EqpConstraint, FlowStep, StepCT, QTimeConstraint, SpecialLotStep, StepTimeWindow, ShiftChangeTime, ShiftConfig, ManualAdjust, SpecialEqp

DATETIME_FORMAT = "%Y/%m/%d %H:%M"

# 由 load_lot_constraints 收集的 lead 关系（每次加载时清除并重建）。
# lead 是"lot2.step2 尾随 lot1.step1"的关系声明，见 LeadPair 与 lead 设计文档。
LEAD_PAIRS: list[LeadPair] = []


def _read_csv(filepath: str, sep: str = "\t") -> pd.DataFrame:
    """读取 CSV 文件，自动尝试多种编码和分隔符。
    如果文件不存在或为空，返回空 DataFrame（列从文件头读取）。"""
    if not os.path.exists(filepath):
        return pd.DataFrame()
    
    # 检查文件是否为空
    file_size = os.path.getsize(filepath)
    if file_size == 0:
        return pd.DataFrame()
    
    for encoding in ["utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030", "latin-1"]:
        for delimiter in [sep, ",", ";"]:
            try:
                df = pd.read_csv(filepath, sep=delimiter, dtype=str, encoding=encoding)
                # 要求至少解析出 2 列才认为分隔符正确；
                # 否则可能是"用 tab 读逗号文件"只得到 1 列的错误解析
                if len(df.columns) >= 2:
                    return df
            except (UnicodeDecodeError, UnicodeError):
                continue
            except pd.errors.EmptyDataError:
                return pd.DataFrame()
            except Exception:
                continue
    # 兜底：分隔符自动嗅探（可正确处理单列文件 / 混合分隔符）
    try:
        return pd.read_csv(filepath, sep=None, engine="python", dtype=str, encoding="utf-8")
    except Exception:
        pass
    raise Exception(f"无法读取文件 {filepath}，已尝试所有常见编码和分隔符")


def parse_datetime(s: Optional[str]) -> Optional[datetime]:
    """解析日期时间字符串，空值返回 None"""
    if pd.isna(s) or s is None or str(s).strip() == "":
        return None
    return datetime.strptime(str(s).strip(), DATETIME_FORMAT)


def parse_priority(s: str) -> tuple[int, int]:
    """解析优先级 "2-1" → (2, 1)"""
    parts = str(s).strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"优先级格式错误: '{s}'，应为 '外部-内部' 如 '2-1'")
    return (int(parts[0]), int(parts[1]))


def _safe_int(row, col: str, default: int = 0) -> int:
    """安全读取整数字段，空值返回 default"""
    val = row.get(col)
    if pd.isna(val) or str(val).strip() == "":
        return default
    return int(float(str(val).strip()))


def _safe_str(row, col: str, default: str = "") -> str:
    """安全读取字符串字段，空值返回 default"""
    val = row.get(col)
    if pd.isna(val) or str(val).strip() == "":
        return default
    return str(val).strip()


def _parse_constraints_file(filepath: str) -> tuple[list[LotConstraint], list[LeadPair]]:
    """解析 lot_constraints.csv（五列关系声明：lot1 | step1 | lot2 | step2 | mod）。

    列语义（设计文档 §3.0）：
      - lot1：被约束/领导批次（等价旧列 lot_name）；
      - step1：lot1 的衔接 step（等价旧列 start_step）；
      - lot2：参照/配套批次（等价旧列 reference_lot）；
      - step2：lot2 的对应 step（等价旧列 reference_step）；
      - mod：关系修饰（等价旧列 start_mod），见 §3.1。

    mod 含义：
      - 空/0：普通引用——lot1.step1 在 lot2.step2 完成之后才释放；
      - N（小时）：普通引用 + 偏移 N 小时；
      - shift / shift_day：普通引用，等到下一班次/下一白班释放；
      - lead：领导衔接——**lot2 为领导批（leading lot，在前面跑）**，
        lot1 为跟随批（主点）背靠背尾随：lot1.step1 紧跟 lot2.step2 之后开始，
        并把领导批 lot2 上游链按 Q-time 回拉对齐（不超任何区间 Q、不在紧 Q 窗口空等）。

      加载时对 mod=lead 自动生成（内部统一为"领导批在前、配套批尾随"）：
        - 闸A 内部引用边（挂到跟随批 lot1）：lot1.step1 在 lot2.step2 完成之后才能开始，
          带 lead_id 标记（环检测/死锁判定跳过）。
        - LeadPair 记录：lot1=用户声明的 lot2（领导批）、lot2=用户声明的 lot1（跟随批），
          供回拉与终检使用。

    兼容旧表头（lot_name/start_step/reference_lot/reference_step/start_mod），
    hold_period_N_start/end 多段扣留时段仍受支持（活跃功能）。

    返回 (constraints, lead_pairs)：lead_pairs 为本次文件解析出的 LeadPair，
    由调用方持有，不依赖模块级全局（避免跨调用历史泄漏导致排程不确定）。
    """
    df = _read_csv(filepath)
    constraints: list[LotConstraint] = []
    lead_pairs: list[LeadPair] = []
    col_names = list(df.columns)

    def _col(row, names, default=""):
        for n in names:
            if n in row and pd.notna(row.get(n)) and str(row.get(n)).strip() != "":
                return str(row[n]).strip()
        return default

    lead_idx = 0
    for _, row in df.iterrows():
        lot1 = _col(row, ["lot1", "lot_name"])
        if not lot1:
            continue

        modv = _col(row, ["mod", "start_mod"])
        step1 = _col(row, ["step1", "start_step"])
        lot2 = _col(row, ["lot2", "reference_lot"])
        step2 = _col(row, ["step2", "reference_step"])

        if modv == "lead":
            # lead 声明（角色反转，用户视角）：lot1 为"主点"（主要关注批次）、尾随在前跑的 lot2。
            #   lot2（leading lot）在前面跑：lot2.step2 先完成；
            #   lot1（跟随批）背靠背尾随：lot1.step1 紧接着开始。
            # 内部统一为"领导批在前、配套批尾随"（LeadPair.lot1=领导批）：
            #   LeadPair.lot1 ← 用户声明的 lot2（领导批），LeadPair.lot2 ← 用户声明的 lot1（跟随批）。
            if lot2 and step2 and step1:
                lead_id = f"lead{lead_idx}"; lead_idx += 1
                # 闸A：跟随批 lot1 等 领导批 lot2.step2 完成之后才释放 lot1.step1
                constraints.append(LotConstraint(
                    lot_name=lot1,               # 挂到跟随批（lot1）
                    reference_lot=lot2,          # 等领导批（lot2）
                    reference_step=step2,        # lot2 的 step2
                    start_mod=None,              # 空 mod = lot2.step2 完成时刻之后释放
                    start_step=step1,            # lot1 的 step1
                    lead_id=lead_id,
                ))
                lead_pairs.append(LeadPair(
                    lot1=lot2, step1=step2,      # 领导批（内部 lot1）= 用户声明的 lot2
                    lot2=lot1, step2=step1,      # 配套批（内部 lot2）= 用户声明的 lot1
                    lead_id=lead_id))
            continue  # lead 行本身不产生普通引用条目

        reference_lot = lot2 or None
        reference_step = step2 or None
        start_mod = modv or None
        start_step = step1 or None

        # 解析 hold_periods: 多列对 hold_period_N_start, hold_period_N_end
        hold_periods = []
        hold_cols = [c for c in col_names if c.startswith("hold_period_") and c.endswith("_start")]
        for start_col in hold_cols:
            end_col = start_col.replace("_start", "_end")
            if end_col in col_names:
                hs = parse_datetime(row.get(start_col))
                he = parse_datetime(row.get(end_col))
                if hs is not None or he is not None:
                    hold_periods.append((hs, he))

        constraints.append(LotConstraint(
            lot_name=lot1,
            reference_lot=reference_lot,
            reference_step=reference_step,
            start_mod=start_mod,
            start_step=start_step,
            hold_periods=hold_periods,
        ))
    return constraints, lead_pairs


def load_lot_constraints(filepath: str) -> list[LotConstraint]:
    """加载 lot_constraints.csv，返回普通引用约束列表。

    同时把本次解析出的 LeadPair 写入模块级 LEAD_PAIRS（兼容旧调用方直接读全局；
    新代码应使用 _parse_constraints_file 的返回，不依赖全局）。
    """
    global LEAD_PAIRS
    constraints, pairs = _parse_constraints_file(filepath)
    LEAD_PAIRS = pairs
    return constraints


def load_lot_list(filepath: str, constraints_filepath: Optional[str] = None) -> list[Lot]:
    """加载 lot_list.csv（含 start_time 列），约束字段从 lot_constraints.csv 合并"""
    df = _read_csv(filepath)

    # 加载约束（lead_pairs 取自本次文件解析，不依赖全局 LEAD_PAIRS——
    # 否则同进程第二次 load_lot_list（无约束文件）会带上上一次的 lead 关系，
    # 导致排程结果依赖调用历史，非确定。）
    all_constraints = []
    lead_pairs: list[LeadPair] = []
    if constraints_filepath:
        all_constraints, lead_pairs = _parse_constraints_file(constraints_filepath)

    # lead 关系按领导批 lot1 归组（供回拉与终检）
    lead_pairs_by_lot1: dict[str, list[LeadPair]] = {}
    for _lp in lead_pairs:
        lead_pairs_by_lot1.setdefault(_lp.lot1, []).append(_lp)

    # 按 lot_name 分组约束
    constraints_by_lot: dict[str, list[LotConstraint]] = {}
    for c in all_constraints:
        if c.lot_name not in constraints_by_lot:
            constraints_by_lot[c.lot_name] = []
        constraints_by_lot[c.lot_name].append(c)

    lots = []
    for _, row in df.iterrows():
        lot_name = _safe_str(row, "lot_name")
        target = _safe_str(row, "target_step")
        if not target:
            target = None

        # 合并该 lot 的约束（hold_periods 去重，防止重复扣留同一时段）
        lot_constraints = constraints_by_lot.get(lot_name, [])
        references = []
        merged_start_time = None
        merged_hold_periods: list[tuple] = []
        seen_hold: set = set()

        # 1. 优先从 lot_list.csv 读取 start_time
        lot_start_time = parse_datetime(row.get("start_time")) if "start_time" in row.index else None
        if lot_start_time is not None:
            merged_start_time = lot_start_time

        for c in lot_constraints:
            # reference 依赖（保留每个 reference 的 start_step）
            if c.reference_lot:
                references.append(c)
            # hold_periods: 合并（去重）
            for hp in c.hold_periods:
                if hp not in seen_hold:
                    seen_hold.add(hp)
                    merged_hold_periods.append(hp)

        # start_step 不再合并到 lot 级别，保留在每个 reference 的 LotConstraint 中
        # 排程引擎会按 per-reference start_step 处理阻塞逻辑

        lots.append(Lot(
            lot_name=lot_name,
            priority=parse_priority(str(row["priority"])),
            qty=int(row["qty"]),
            carrier_id=_safe_str(row, "carrier_id"),
            current_step_name=_safe_str(row, "step_name"),
            product_name=_safe_str(row, "product_name"),
            target_step=target,
            lot_state=_safe_str(row, "lot_state"),
            running_time=_safe_int(row, "running_time"),
            references=references,
            lead_pairs=lead_pairs_by_lot1.get(lot_name, []),
            start_time=merged_start_time,
            start_step=None,  # per-reference start_step 在每个 reference 中
            hold_periods=merged_hold_periods,
            planned_end=parse_datetime(row.get("planned_end")) if "planned_end" in row.index else None,
        ))
    return lots


def load_flow(filepath: str) -> list[FlowStep]:
    """加载 flow.csv，自动合并同 product+step_number 的 eqp_id"""
    df = _read_csv(filepath)
    # 使用 (product_name, step_number) 去重合并 eqp_ids
    merged: dict[tuple[str, str], tuple[str, list[str]]] = {}
    stage_names: dict[tuple[str, str], str] = {}
    for _, row in df.iterrows():
        product_name = str(row["product_name"]).strip()
        step_number = str(row["step_number"]).strip()
        step_name = str(row["step_name"]).strip()
        stage_name = _safe_str(row, "stage_name") if "stage_name" in row.index else ""
        eqp_str = str(row.get("eqp_id", "")).strip() if pd.notna(row.get("eqp_id")) else ""
        # 空值必须用空列表（"" 会让下游 `e in raw_allowed` 退化为子串匹配）
        eqp_ids = [e.strip() for e in eqp_str.split(",") if e.strip()] if eqp_str else []

        key = (product_name, step_number)
        if key in merged:
            # 合并 eqp_ids，保持顺序去重
            existing_name, existing_eqps = merged[key]
            for eid in eqp_ids:
                if eid not in existing_eqps:
                    existing_eqps.append(eid)
        else:
            merged[key] = (step_name, eqp_ids)
            if stage_name:
                stage_names[key] = stage_name

    steps = []
    for (product_name, step_number), (step_name, eqp_ids) in merged.items():
        key = (product_name, step_number)
        steps.append(FlowStep(
            product_name=product_name,
            step_number=step_number,
            step_name=step_name,
            stage_name=stage_names.get(key, ""),
            eqp_ids=eqp_ids,
        ))
    return steps


def load_step_ct(filepath: str) -> list[StepCT]:
    """加载 step_ct.csv"""
    df = _read_csv(filepath)
    cts = []
    for _, row in df.iterrows():
        cts.append(StepCT(
            product_name=str(row["product_name"]).strip(),
            step_number=str(row["step_number"]).strip(),
            step_name=str(row["step_name"]).strip(),
            qty=int(row["qty"]),
            step_ct=float(row["step_ct"]),
        ))
    return cts


def load_qtime(filepath: str) -> list[QTimeConstraint]:
    """加载 qtime.csv"""
    df = _read_csv(filepath)
    constraints = []
    for _, row in df.iterrows():
        constraints.append(QTimeConstraint(
            product_name=str(row["product_name"]).strip(),
            start_step=str(row["Q-time_start"]).strip(),
            end_step=str(row["Q-time_end"]).strip(),
            start_mod=str(row["Q-time_start_mod"]).strip(),
            end_mod=str(row["Q-time_end_mod"]).strip(),
            max_duration=int(row["Q-time"]),
        ))
    return constraints


def build_ct_lookup(step_cts: list[StepCT]) -> dict[tuple[str, str, int], float]:
    """构建 CT 查找表: (product, step_number, qty) → ct"""
    lookup = {}
    for ct in step_cts:
        lookup[(ct.product_name, ct.step_number, ct.qty)] = ct.step_ct
    return lookup


def auto_repair_step_ct(flows: list[FlowStep], step_cts: list[StepCT],
                        step_ct_filepath: str | None = None) -> list[StepCT]:
    """修复 bug: 当用户手动编辑 flow.csv（比如为 step 增删设备或新增 step）
    但未同步 step_ct.csv 时，schedule() 会因找不到 CT 报错"没有 flow / 找不到 CT 数据"。

    策略：
      1. 找出所有在 flow.csv 中存在、但在 step_ct.csv 中 (product, step_number)
         组合完全没有记录的 steps。
      2. 对缺失的 step，采用同 product 同 stage 的已有 step 的 3/8/13 片 CT
         做锚点均值，再线性插值生成 1-13 片的全部 CT，追加回 step_cts。
      3. 若 step_ct_filepath 不为 None，还会把补全后的内容回写 step_ct.csv，
         下次直接读就不会再"要去 step_ct 保存一次"了。
    """
    from collections import defaultdict

    existing_keys: set[tuple[str, str]] = set()
    for ct in step_cts:
        existing_keys.add((ct.product_name, ct.step_number))

    missing_steps: list[FlowStep] = []
    for s in flows:
        if (s.product_name, s.step_number) not in existing_keys:
            missing_steps.append(s)

    if not missing_steps:
        return step_cts

    print(f"[data_loader.auto_repair_step_ct] 检测到 step_ct.csv 缺失 {len(missing_steps)} 个步骤，")
    print("    （例如用户刚改完 flow.csv 但没保存 step_ct.csv）自动生成 CT 并回写：")
    for s in missing_steps:
        print(f"      - {s.product_name} / {s.step_name} ({s.step_number})")

    # 收集同 product 同 stage 的锚点(3,8,13) -> ct
    anchors_by_group: dict[tuple[str, str], dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for s in flows:
        for ct_qty in (3, 8, 13):
            v = next((c.step_ct for c in step_cts
                      if c.product_name == s.product_name
                      and c.step_number == s.step_number
                      and c.qty == ct_qty), None)
            if v is not None:
                anchors_by_group[(s.product_name, s.stage_name or "")][ct_qty].append(v)

    def _simple_interp(ct3: float | None, ct8: float | None, ct13: float | None) -> dict[int, float]:
        anchors = []
        if ct3 is not None: anchors.append((3, ct3))
        if ct8 is not None: anchors.append((8, ct8))
        if ct13 is not None: anchors.append((13, ct13))
        if not anchors:
            return {qty: 0.0 for qty in range(1, 14)}
        if len(anchors) == 1:
            return {qty: anchors[0][1] for qty in range(1, 14)}
        result = {}
        for qty in range(1, 14):
            if qty <= anchors[0][0]:
                a, b = anchors[0], anchors[1]
                val = a[1] + (b[1] - a[1]) / (b[0] - a[0]) * (qty - a[0])
            elif qty >= anchors[-1][0]:
                a, b = anchors[-2], anchors[-1]
                val = a[1] + (b[1] - a[1]) / (b[0] - a[0]) * (qty - a[0])
            else:
                for i in range(len(anchors) - 1):
                    if anchors[i][0] <= qty <= anchors[i + 1][0]:
                        a, b = anchors[i], anchors[i + 1]
                        val = a[1] + (b[1] - a[1]) / (b[0] - a[0]) * (qty - a[0])
                        break
                else:
                    val = anchors[-1][1]
            result[qty] = round(max(0.0, val), 2)
        return result

    new_cts: list[StepCT] = list(step_cts)
    for s in missing_steps:
        grp = anchors_by_group.get((s.product_name, s.stage_name or ""))
        ct3 = round(sum(grp[3]) / len(grp[3]), 2) if grp and grp[3] else None
        ct8 = round(sum(grp[8]) / len(grp[8]), 2) if grp and grp[8] else None
        ct13 = round(sum(grp[13]) / len(grp[13]), 2) if grp and grp[13] else None
        # 如果 stage 下没任何已知 step，回退整个 product 的平均
        if ct3 is None and ct8 is None and ct13 is None:
            fallback = defaultdict(list)
            for ct in step_cts:
                if ct.product_name == s.product_name and ct.qty in (3, 8, 13):
                    fallback[ct.qty].append(ct.step_ct)
            ct3 = round(sum(fallback[3]) / len(fallback[3]), 2) if fallback[3] else None
            ct8 = round(sum(fallback[8]) / len(fallback[8]), 2) if fallback[8] else None
            ct13 = round(sum(fallback[13]) / len(fallback[13]), 2) if fallback[13] else None
        ct_map = _simple_interp(ct3, ct8, ct13)
        for qty in range(1, 14):
            new_cts.append(StepCT(
                product_name=s.product_name,
                step_number=s.step_number,
                step_name=s.step_name,
                qty=qty,
                step_ct=ct_map[qty],
            ))

    if step_ct_filepath:
        # 回写到 step_ct.csv，保证下次读取直接有。
        # 容错：文件被占用（Excel 锁）/无写权限时只告警，内存补全仍然生效。
        # 只**追加**缺失步骤的补全行，保留原有行的原始精度与列（避免整表重写导致
        # 已有 CT 被四舍五入、用户自定义列丢失——历史 bug）。
        new_rows = []
        for c in new_cts:
            if (c.product_name, c.step_number) in existing_keys:
                continue  # 原有行不动
            new_rows.append({
                "product_name": c.product_name,
                "step_number": c.step_number,
                "step_name": c.step_name,
                "qty": str(c.qty),
                "step_ct": f"{c.step_ct:.2f}",
            })
        if not new_rows:
            return new_cts
        try:
            records = []
            if os.path.exists(step_ct_filepath) and os.path.getsize(step_ct_filepath) > 0:
                _existing_df = _read_csv(step_ct_filepath)
                if not _existing_df.empty:
                    records = _existing_df.fillna("").to_dict(orient="records")
            records.extend(new_rows)
            cols = list(records[0].keys())
            df = pd.DataFrame(records, columns=cols)
            df.to_csv(step_ct_filepath, index=False, sep="\t")
            print(f"    ✅ 已自动回写 step_ct.csv（新增 {len(new_rows)} 行，原有行保持不变），下次无需手动再保存")
        except (PermissionError, OSError) as e:
            print(f"    ⚠ 回写 {step_ct_filepath} 失败（{e}），本次仅内存补全")

    return new_cts


def get_step_ct(lookup: dict, product: str, step_number: str, qty: int) -> float:
    """
    查找 CT，支持精确匹配和最近 qty 插值
    """
    key = (product, step_number, qty)
    if key in lookup:
        return lookup[key]

    # 查找该 product+step 的所有 qty
    available_qties = sorted([k[2] for k in lookup if k[0] == product and k[1] == step_number])
    if not available_qties:
        raise ValueError(f"未找到 CT 数据: product={product}, step={step_number}")

    if qty < available_qties[0]:
        return lookup[(product, step_number, available_qties[0])]
    if qty > available_qties[-1]:
        # 超过最大 qty（13）时，按边际 CT 外推：CT(N) = CT(13) + (N-13) * (CT(13)-CT(12))
        max_qty = available_qties[-1]
        ct_max = lookup[(product, step_number, max_qty)]
        if len(available_qties) >= 2:
            prev_qty = available_qties[-2]
            ct_prev = lookup[(product, step_number, prev_qty)]
            per_piece_ct = (ct_max - ct_prev) / (max_qty - prev_qty)
        else:
            # 只有一个数据点，按平均 CT/pcs 外推
            per_piece_ct = ct_max / max_qty
        return round(ct_max + (qty - max_qty) * per_piece_ct, 2)

    # 找最近的两个 qty 进行线性插值
    for i in range(len(available_qties) - 1):
        if available_qties[i] <= qty <= available_qties[i + 1]:
            q1, q2 = available_qties[i], available_qties[i + 1]
            ct1 = lookup[(product, step_number, q1)]
            ct2 = lookup[(product, step_number, q2)]
            if q1 == q2:
                return ct1
            return round(ct1 + (ct2 - ct1) * (qty - q1) / (q2 - q1), 2)

    return lookup[(product, step_number, available_qties[-1])]


def get_product_flow_map(flows: list[FlowStep]) -> dict[str, list[FlowStep]]:
    """构建产品流程映射: product_name → 排序后的 FlowStep 列表"""
    flow_map = {}
    for f in flows:
        if f.product_name not in flow_map:
            flow_map[f.product_name] = []
        flow_map[f.product_name].append(f)

    def _step_sort_key(s: FlowStep):
        # 防御：step_number 为空 / 非数字（如缺单元格读成 "nan"）时避免 int() 崩溃
        try:
            return tuple(int(n) for n in s.step_number.split("."))
        except (ValueError, TypeError, AttributeError):
            return (0, 0)

    # 按 step_number 排序
    for product in flow_map:
        flow_map[product].sort(key=_step_sort_key)
    return flow_map


def get_step_index_in_flow(flow_steps: list[FlowStep], step_name: str) -> int:
    """根据 step_name 查找在 flow 中的索引"""
    for i, step in enumerate(flow_steps):
        if step.step_name == step_name:
            return i
    raise ValueError(f"未在 flow 中找到步骤: {step_name}")


def health_check_lead(lots: list[Lot], flow_map: dict[str, list[FlowStep]]) -> list[str]:
    """lead 数据体检（设计文档 §3.2），返回人类可读的违规/标注列表（空=通过）。

    检查项：
    1. 存在性：lead 的 lot1（领导批）、lot2（配套批）都必须在 lot_list 中。
    2. 流程异构：step1 必须在 lot1 流程、step2 必须在 lot2 流程。
    3. 成环：lead 边（lot2→lot1，配套依赖领导）与普通引用边（lot→reference_lot）
       构成的有向图不得成环（A 等 B 且 B 等 A）。lead 内部边（即闸A 那一条，
       带 lead_id、挂在 lot2 上）已在 _detect_schedule_anomalies 的引用环中跳过，
       这里同样不把带 lead_id 的引用计入。
    4. 热启动太靠后：lot1 当前已排在 step1 之后 → lead 对 lot1 侧回拉失效，
       仅保留闸A（lot2.step2 不早于 lot1.step1 完成），结果标注。
    """
    warns: list[str] = []
    lot_by_name = {l.lot_name: l for l in lots}
    lead_pairs = [lp for lot in lots for lp in (lot.lead_pairs or [])]
    if not lead_pairs:
        return warns

    # ---- 1. 存在性 ----
    for lp in lead_pairs:
        if lp.lot1 not in lot_by_name:
            warns.append(
                f"lead 数据体检：领导批 {lp.lot1} 在 lot_list 中不存在，"
                f"lead（{lp.lot2}.{lp.step2} 尾随 {lp.lot1}.{lp.step1}）失效")
        if lp.lot2 not in lot_by_name:
            warns.append(
                f"lead 数据体检：配套批 {lp.lot2} 在 lot_list 中不存在，"
                f"lead（{lp.lot2}.{lp.step2} 尾随 {lp.lot1}.{lp.step1}）失效")

    # ---- 2. 流程异构 ----
    for lp in lead_pairs:
        lot1 = lot_by_name.get(lp.lot1)
        lot2 = lot_by_name.get(lp.lot2)
        if lot1:
            f1 = flow_map.get(lot1.product_name) or []
            if not any(s.step_name == lp.step1 for s in f1):
                warns.append(
                    f"lead 数据体检：{lp.lot1} 的流程 {lot1.product_name} 中不存在衔接步 {lp.step1}")
        if lot2:
            f2 = flow_map.get(lot2.product_name) or []
            if not any(s.step_name == lp.step2 for s in f2):
                warns.append(
                    f"lead 数据体检：{lp.lot2} 的流程 {lot2.product_name} 中不存在衔接步 {lp.step2}")

    # ---- 3. 成环（lead + 普通引用，DAG）----
    graph: dict[str, set[str]] = {l.lot_name: set() for l in lots}
    for lp in lead_pairs:
        graph.setdefault(lp.lot2, set()).add(lp.lot1)          # 配套依赖领导
    for lot in lots:
        for r in lot.references or []:
            # 跳过 lead 内部边（带 lead_id）；lead 语义已由 LeadPair 入图
            if r.reference_lot and r.start_step and not r.lead_id:
                graph.setdefault(lot.lot_name, set()).add(r.reference_lot)
    involved = set()
    for lp in lead_pairs:
        involved.add(lp.lot1)
        involved.add(lp.lot2)

    def _reach(n, seen):
        for m in graph.get(n, ()):
            if m not in seen:
                seen.add(m)
                _reach(m, seen)

    expanded = set(involved)
    for n in list(involved):
        _reach(n, expanded)
    _visited: set[str] = set()
    _stack: list[str] = []
    _cyc: set[str] = set()

    def _dfs(n):
        if n in _visited or n not in graph:
            return
        _visited.add(n)
        _stack.append(n)
        for m in graph.get(n, ()):
            if m in _stack:
                _idx = _stack.index(m)
                for x in _stack[_idx:]:
                    _cyc.add(x)
            elif m in expanded:
                _dfs(m)
        _stack.pop()

    for n in expanded:
        _dfs(n)
    if _cyc:
        warns.append(
            f"lead 数据体检：{'、'.join(sorted(_cyc))} 的 lead/引用关系成环"
            "（A 等 B 且 B 等 A），相互依赖会互相拖住，请核对配置")

    # ---- 4. 热启动太靠后：lot1 当前已排在 step1 之后 ----
    for lp in lead_pairs:
        lot1 = lot_by_name.get(lp.lot1)
        if not lot1:
            continue
        f1 = flow_map.get(lot1.product_name) or []
        idx_cur = next((i for i, s in enumerate(f1) if s.step_name == lot1.current_step_name), -1)
        idx_step1 = next((i for i, s in enumerate(f1) if s.step_name == lp.step1), -1)
        if idx_cur >= 0 and idx_step1 >= 0 and idx_cur > idx_step1:
            warns.append(
                f"lead 数据体检：{lp.lot1} 当前已在其流程的 [{lot1.current_step_name}]"
                f"（位于衔接步 {lp.step1} 之后），lead 对 {lp.lot1} 侧回拉失效，"
                f"仅保留闸A（{lp.lot2}.{lp.step2} 不早于完成）")
    return warns


def load_ftf_qty_change(filepath: str) -> dict[str, tuple[int, int, str]]:
    """加载 FTF qty 变化表，返回 {product_name: (input_number, output_number, change_step)}"""
    df = _read_csv(filepath)
    result = {}
    for _, row in df.iterrows():
        product = str(row["product_name"]).strip()
        input_num = int(row["input_number"])
        output_num = int(row["output_number"])
        change_step = _safe_str(row, "change_step", "FTF-INPUT-TO-OUTPUT")
        result[product] = (input_num, output_num, change_step)
    return result


def load_special_lot_step(filepath: str) -> dict[tuple[str, str], SpecialLotStep]:
    """加载 Lot 级特殊 CT/设备覆盖表 (special_lot_step.csv)
    返回 {(lot_name, step_name): SpecialLotStep}
    字段: lot_name, step_name, special_ct, special_eqp
    - special_ct: 空=使用原 CT, 数值=覆盖 CT（分钟）
    - special_eqp: 空=不限制设备, 逗号分隔的设备ID=限定可用设备
    """
    df = _read_csv(filepath)
    result = {}
    for _, row in df.iterrows():
        lot_name = str(row["lot_name"]).strip()
        step_name = str(row["step_name"]).strip()
        # special_ct: 空或 0 表示缺省（用数值判断，兼容 "0.00"、"0.000" 等写法）
        ct_str = _safe_str(row, "special_ct")
        special_ct = None
        if ct_str:
            try:
                _v = float(ct_str)
                if _v > 0:
                    special_ct = _v
            except ValueError:
                pass
        # special_eqp: 逗号分隔的设备列表
        eqp_str = _safe_str(row, "special_eqp")
        special_eqp = [e.strip() for e in eqp_str.split(",") if e.strip()] if eqp_str else []
        result[(lot_name, step_name)] = SpecialLotStep(
            lot_name=lot_name,
            step_name=step_name,
            special_ct=special_ct,
            special_eqp=special_eqp,
        )
    return result


def load_priority_wait(filepath: str) -> dict[tuple[int, int], int]:
    """加载优先级-Wait时间映射表，返回 {(ext_priority, int_priority): wait_time}
    支持两种格式：
    1. 三列: ext_priority, int_priority, wait_time（精确到内部优先级）
    2. 两列: ext_priority, wait_time（兼容旧格式，内部优先级不区分）
    """
    df = _read_csv(filepath)
    result: dict[tuple[int, int], int] = {}
    has_int_priority = "int_priority" in df.columns
    for _, row in df.iterrows():
        ext_priority = int(row["ext_priority"])
        wait_time = int(row["wait_time"])
        if has_int_priority:
            int_priority = int(row["int_priority"])
            result[(ext_priority, int_priority)] = wait_time
        else:
            # 旧格式：所有内部优先级使用相同 wait_time
            for ip in range(1, 10):
                result[(ext_priority, ip)] = wait_time
    return result


def load_eqp_constraints(filepath: str) -> list[EqpConstraint]:
    """加载设备不可用约束 eqp_constraint.csv
    字段: eqp_name, no_used_start_time, no_used_end_time, date, week
    - date: 空=无效, -1=每天, yyyy/mm/dd=指定日期
    - week: 1-7(周一到周日), 空=无约束
    - date 和 week 都填以 date 为准
    """
    df = _read_csv(filepath)
    constraints = []
    for _, row in df.iterrows():
        eqp_name = _safe_str(row, "eqp_name")
        if not eqp_name:
            continue

        start_time = _safe_str(row, "no_used_start_time")
        end_time = _safe_str(row, "no_used_end_time")
        if not start_time or not end_time:
            continue  # 时间段无效，跳过

        date_str = _safe_str(row, "date")
        week_str = _safe_str(row, "week")

        # date 和 week 都为空 → 无效，跳过
        if not date_str and not week_str:
            continue

        week = None
        if week_str:
            try:
                week = int(week_str)
                if week < 1 or week > 7:
                    week = None
            except ValueError:
                week = None

        constraints.append(EqpConstraint(
            eqp_name=eqp_name,
            start_time_str=start_time,
            end_time_str=end_time,
            date_str=date_str if date_str else None,
            week=week,
        ))
    return constraints


def load_step_time_windows(filepath: str) -> list[StepTimeWindow]:
    """加载步骤可作业时间窗口 step_time_window.csv
    字段: step_name, start_time, end_time, day, end_start_time, end_end_time, end_day
    - day / end_day: 空=无效, -1=每天, yyyy/mm/dd=指定日期, 1-7=周几
    """
    df = _read_csv(filepath)
    windows = []
    for _, row in df.iterrows():
        step_name = _safe_str(row, "step_name")
        if not step_name:
            continue
        start_time = _safe_str(row, "start_time")
        end_time = _safe_str(row, "end_time")
        if not start_time or not end_time:
            continue
        day_str = _safe_str(row, "day")
        if not day_str:
            continue

        week = None
        if day_str not in ("-1", "") and not day_str.startswith("20"):
            try:
                w = int(day_str)
                if 1 <= w <= 7:
                    week = w
            except ValueError:
                pass

        # 结束时间窗
        end_start_time = _safe_str(row, "end_start_time")
        end_end_time = _safe_str(row, "end_end_time")
        end_day_str = _safe_str(row, "end_day")

        end_week = None
        if end_day_str and end_day_str not in ("-1", "") and not end_day_str.startswith("20"):
            try:
                w = int(end_day_str)
                if 1 <= w <= 7:
                    end_week = w
            except ValueError:
                pass

        windows.append(StepTimeWindow(
            step_name=step_name,
            start_time_str=start_time,
            end_time_str=end_time,
            date_str=day_str if day_str else None,
            week=week,
            end_start_time_str=end_start_time if end_start_time else None,
            end_end_time_str=end_end_time if end_end_time else None,
            end_date_str=end_day_str if end_day_str else None,
            end_week=end_week,
        ))
    return windows


def load_shift_config(filepath: str) -> list[ShiftConfig]:
    """加载班次配置 shift_config.csv
    字段: shift_name, start_time (hh:mm)
    """
    df = _read_csv(filepath)
    shifts = []
    for _, row in df.iterrows():
        shift_name = _safe_str(row, "shift_name")
        start_time = _safe_str(row, "start_time")
        if not shift_name or not start_time:
            continue
        shifts.append(ShiftConfig(
            shift_name=shift_name,
            start_time_str=start_time,
        ))
    return shifts


def load_shift_change_times(filepath: str) -> list[ShiftChangeTime]:
    """加载换班时间窗口 shift_change_time.csv
    字段: start_time, end_time, day
    - day: 空=无效, -1=每天, yyyy/mm/dd=指定日期, 1-7=周几
    """
    df = _read_csv(filepath)
    windows = []
    for _, row in df.iterrows():
        start_time = _safe_str(row, "start_time")
        end_time = _safe_str(row, "end_time")
        if not start_time or not end_time:
            continue
        day_str = _safe_str(row, "day")
        if not day_str:
            continue

        week = None
        if day_str not in ("-1", "") and not day_str.startswith("20"):
            try:
                w = int(day_str)
                if 1 <= w <= 7:
                    week = w
            except ValueError:
                pass

        windows.append(ShiftChangeTime(
            start_time_str=start_time,
            end_time_str=end_time,
            date_str=day_str if day_str else None,
            week=week,
        ))
    return windows


def load_manual_adjusts(filepath: str) -> list[ManualAdjust]:
    """加载手动调整约束 manual_adjust.csv
    字段: lot_name, step_name, delay_to, mode
    - step_name 为空 = 该 Lot 所有未排步骤延迟
    - delay_to: yyyy/mm/dd HH:MM 格式
    - mode: delay=不早于（最早）/ pin=精确锁定
    """
    df = _read_csv(filepath)
    adjusts = []
    for _, row in df.iterrows():
        lot_name = _safe_str(row, "lot_name")
        if not lot_name:
            continue
        step_name = _safe_str(row, "step_name")
        delay_str = _safe_str(row, "delay_to")
        delay_to = parse_datetime(delay_str) if delay_str else None
        if delay_to is None:
            continue
        mode = _safe_str(row, "mode", "delay").lower()
        if mode not in ("delay", "pin"):
            mode = "delay"
        adjusts.append(ManualAdjust(
            lot_name=lot_name,
            step_name=step_name if step_name else None,
            delay_to=delay_to,
            mode=mode,
        ))
    return adjusts


def load_special_eqp(filepath: str) -> dict[str, SpecialEqp]:
    """加载特殊设备批处理配置 special_eqp.csv
    字段: eqp_name, max_lots, max_qty, together
    - together: true/false，是否要求同时开始并锁定设备
    返回 {eqp_name: SpecialEqp}
    """
    df = _read_csv(filepath)
    result = {}
    for _, row in df.iterrows():
        eqp_name = _safe_str(row, "eqp_name")
        if not eqp_name:
            continue
        max_lots = _safe_int(row, "max_lots", 1)
        max_qty = _safe_int(row, "max_qty", 999999)
        together_str = _safe_str(row, "together", "false").lower()
        together = together_str in ("true", "1", "yes")
        result[eqp_name] = SpecialEqp(
            eqp_name=eqp_name,
            max_lots=max_lots,
            max_qty=max_qty,
            together=together,
        )
    return result