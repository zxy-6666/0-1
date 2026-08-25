"""Flow 原始数据导入与转换模块

支持将原始 flow 文件（含 stage_name, step_name, large_ct, middle_ct, small_ct, 多列可用eqp）
转换为系统使用的 flow.csv 和 step_ct.csv 格式。

原始 flow 文件格式（Tab/逗号分隔）：
    Stage_name  step_name  large_ct  middle_ct  small_ct  可用eqp  可用eqp  ...
    （列名支持多种变体，见下方的 PATTERNS 常量）

用法：
    from flow_importer import import_flow_files, RawFlowParseResult
    results = import_flow_files("/workspace/data/flow_import/")
"""

import os
import pandas as pd
from typing import Optional
from dataclasses import dataclass, field


# ── 数据类 ──

@dataclass
class RawFlowStep:
    """原始 flow 单步数据"""
    product_name: str
    step_number: str
    step_name: str
    stage_name: str = ""           # 所属阶段名称（仅用于 step_number 生成）
    eqp_ids: list[str] = field(default_factory=list)  # 可用的设备列表
    ct_3: Optional[float] = None   # 3pcs 作业 CT (small_ct)
    ct_8: Optional[float] = None   # 8pcs 作业 CT (middle_ct)
    ct_13: Optional[float] = None  # 13pcs 作业 CT (large_ct)


@dataclass
class RawFlowParseResult:
    """单个原始 flow 文件的解析结果"""
    product_name: str
    filename: str
    steps: list[RawFlowStep] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    success: bool = True


@dataclass
class ImportResult:
    """批量导入结果"""
    total_files: int = 0
    success_files: list[str] = field(default_factory=list)
    error_files: list[str] = field(default_factory=list)
    products_updated: list[str] = field(default_factory=list)   # 覆盖更新的产品
    products_added: list[str] = field(default_factory=list)     # 新增的产品
    flow_rows_added: int = 0
    flow_rows_removed: int = 0
    step_ct_rows_added: int = 0
    step_ct_rows_removed: int = 0
    warnings: list[str] = field(default_factory=list)


# ── 列名检测 ──

# CT 列名映射 (qty → 可能的列名)
# large_ct=13pcs, middle_ct=8pcs, small_ct=3pcs
CT_COLUMN_PATTERNS = {
    3:  ["small_ct", "SMALL_CT", "CT_3", "ct_3", "CT_3pcs", "ct_3pcs", "little_ct", "LITTLE_CT", "CT_L", "ct_l", "3pcs", "3PCS", "little", "LITTLE", "small", "SMALL", "Small CT"],
    8:  ["middle_ct", "MIDDLE_CT", "CT_8", "ct_8", "CT_8pcs", "ct_8pcs", "CT_M", "ct_m", "8pcs", "8PCS", "middle", "MIDDLE", "Middle CT"],
    13: ["large_ct", "LARGE_CT", "CT_13", "ct_13", "CT_13pcs", "ct_13pcs", "CT_H", "ct_h", "13pcs", "13PCS", "large", "LARGE", "Large CT"],
}

# 阶段列名
STAGE_NAME_PATTERNS = ["stage_name", "STAGE_NAME", "Stage_name", "stage", "STAGE", "Stage", "phase", "PHASE", "阶段", "工段"]

# 步骤列名
STEP_NAME_PATTERNS = ["step_name", "STEP_NAME", "step", "STEP", "process", "PROCESS", "工序", "步骤", "原Step", "原step"]
STEP_NUMBER_PATTERNS = ["step_number", "STEP_NUMBER", "step_no", "STEP_NO", "step_id", "STEP_ID", "seq", "SEQ", "序号", "顺序"]

# 设备列名（单列模式）
EQP_PATTERNS = ["eqp_name", "EQP_NAME", "eqp_id", "EQP_ID", "eqp", "EQP", "available_eqp", "AVAILABLE_EQP", "设备", "可用eqp", "可用设备"]


def _interpolate_ct(ct_3: Optional[float], ct_8: Optional[float], ct_13: Optional[float]) -> dict[int, float]:
    """根据 3/8/13 三个锚点的 CT 值，线性插值出 1-13 片所有 CT

    假设 CT 与片数呈线性关系，已知三个锚点：
      - (3, ct_3): 3pcs 作业时间
      - (8, ct_8): 8pcs 作业时间
      - (13, ct_13): 13pcs 作业时间

    插值策略：
      - qty 1-2: 外推 (3, ct_3) → (8, ct_8) 的直线
      - qty 3-8: 内插 (3, ct_3) → (8, ct_8)
      - qty 9-13: 内插 (8, ct_8) → (13, ct_13)

    如果部分锚点缺失，使用可用锚点进行线性拟合。
    如果只有一个或零个锚点，全部默认返回 0（无 CT 步骤）。
    """
    # 收集可用锚点
    anchors = []
    if ct_3 is not None:
        anchors.append((3, ct_3))
    if ct_8 is not None:
        anchors.append((8, ct_8))
    if ct_13 is not None:
        anchors.append((13, ct_13))

    if len(anchors) == 0:
        # 所有 CT 值都为空，默认返回 0（无 CT 步骤，如 REPORT、MERGE-MAP-CHECK 等）
        return {qty: 0.0 for qty in range(1, 14)}

    if len(anchors) < 2:
        # 只有一个锚点，所有 qty 都用该值
        return {qty: anchors[0][1] for qty in range(1, 14)}

    result: dict[int, float] = {}

    for qty in range(1, 14):
        if qty <= anchors[0][0]:
            # 左端外推：用前两个锚点
            a, b = anchors[0], anchors[1]
            val = a[1] + (b[1] - a[1]) / (b[0] - a[0]) * (qty - a[0])
        elif qty >= anchors[-1][0]:
            # 右端外推：用后两个锚点
            a, b = anchors[-2], anchors[-1]
            val = a[1] + (b[1] - a[1]) / (b[0] - a[0]) * (qty - a[0])
        else:
            # 内插：找到 qty 所在的区间
            for i in range(len(anchors) - 1):
                if anchors[i][0] <= qty <= anchors[i + 1][0]:
                    a, b = anchors[i], anchors[i + 1]
                    val = a[1] + (b[1] - a[1]) / (b[0] - a[0]) * (qty - a[0])
                    break
            else:
                val = anchors[0][1]  # fallback

        result[qty] = round(val, 4)

    return result


def _detect_column(df_columns: list[str], patterns: list[str]) -> Optional[str]:
    """在 DataFrame 列名中匹配，返回第一个匹配的列名"""
    for col in df_columns:
        if col in patterns:
            return col
    # 大小写不敏感匹配
    for col in df_columns:
        for pat in patterns:
            if col.lower() == pat.lower():
                return col
    return None


def _detect_all_columns(df_columns: list[str], patterns: list[str]) -> list[str]:
    """检测所有匹配的列名"""
    result = []
    # 精确匹配
    for col in df_columns:
        if col in patterns:
            result.append(col)
    # 大小写不敏感匹配
    for col in df_columns:
        if col not in result:
            for pat in patterns:
                if col.lower() == pat.lower():
                    result.append(col)
                    break
    return result


def _detect_ct_columns(df_columns: list[str]) -> dict[int, str]:
    """检测 CT 列名，返回 {qty: column_name}"""
    result = {}
    for qty, patterns in CT_COLUMN_PATTERNS.items():
        col = _detect_column(df_columns, patterns)
        if col:
            result[qty] = col
    return result


def _looks_like_eqp_id(val: str) -> bool:
    """判断一个值是否看起来像设备 ID（字母数字组合，非纯数字）"""
    if not val or val in ("", "nan", "None", "-"):
        return False
    # 设备 ID 通常是字母数字混合，如 PGBGL001、PMHOI015
    # 排除纯数字（可能是 CT 值误读）
    try:
        float(val)
        return False  # 纯数字不太可能是设备 ID
    except ValueError:
        pass
    return len(val) >= 2


def _detect_eqp_columns(df_columns: list[str], known_cols: set[str], df: pd.DataFrame) -> list[str]:
    """检测设备列，支持多列模式

    策略：
    1. 先找明确匹配的设备列名
    2. 再扫描剩余列（列名不在 known_cols 中并非已知设备列），找值看起来像设备 ID 的列
    3. 合并去重返回
    """
    eqp_cols = []

    # 步骤1：明确匹配
    explicit = _detect_all_columns(df_columns, EQP_PATTERNS)
    eqp_cols.extend(explicit)

    # 步骤2：扫描剩余列（列名不在 known_cols 中，也不在已找到的 eqp_cols 中）
    for col in df_columns:
        if col in known_cols or col in eqp_cols:
            continue
        # 检查该列的值是否像设备 ID
        values = df[col].dropna().astype(str).str.strip()
        valid = values[values.apply(_looks_like_eqp_id)]
        if len(valid) > 0 and len(valid) >= len(values) * 0.5:
            eqp_cols.append(col)

    return eqp_cols


def _collect_eqp_ids(row, eqp_cols: list[str]) -> list[str]:
    """从多列中收集设备 ID，保持顺序去重"""
    seen = set()
    result = []
    for col in eqp_cols:
        if col not in row.index:
            continue
        val = str(row[col]).strip() if pd.notna(row[col]) else ""
        if val and val.lower() not in ("nan", "none", ""):
            if val not in seen:
                seen.add(val)
                result.append(val)
    return result


def _forward_fill_stage_name(steps: list[RawFlowStep]) -> None:
    """处理合并单元格：将 stage_name 向下填充到空行

    Excel 导出 CSV 时，合并单元格只在第一行有值，后续行留空。
    此函数将上一行的 stage_name 填充到当前空行。
    """
    current_stage = ""
    for s in steps:
        if s.stage_name and s.stage_name not in ("", "nan", "None"):
            current_stage = s.stage_name
        else:
            s.stage_name = current_stage


def _auto_generate_step_numbers(steps: list[RawFlowStep]) -> list[RawFlowStep]:
    """根据 stage_name 自动生成 step_number

    规则：
    - 每个不同的 stage_name 对应一个 stage 序号（10, 20, 30...）
    - 同一 stage 内的 step 按顺序编号（001, 002, 003...）
    - 格式：{stage_num}.{step_seq:03d}
    - 如果已有 step_number，保持原值
    """
    # 检查是否已有 step_number
    has_numbers = any(s.step_number for s in steps)
    if has_numbers:
        return steps

    # 按 stage_name 分组
    stage_order: list[str] = []
    seen_stages = set()
    for s in steps:
        if s.stage_name and s.stage_name not in seen_stages:
            seen_stages.add(s.stage_name)
            stage_order.append(s.stage_name)

    if not stage_order:
        # 没有 stage_name，全部按顺序编号
        for i, s in enumerate(steps):
            s.step_number = f"10.{i + 1:03d}"
        return steps

    # 为每个 stage 分配序号
    stage_num = 10
    stage_seq: dict[str, int] = {}
    stage_counters: dict[str, int] = {}

    for sn in stage_order:
        stage_seq[sn] = stage_num
        stage_counters[sn] = 0
        stage_num += 10

    for s in steps:
        stage = s.stage_name if s.stage_name in stage_seq else (stage_order[0] if stage_order else "")
        if stage in stage_seq:
            stage_counters[stage] += 1
            s.step_number = f"{stage_seq[stage]}.{stage_counters[stage]:03d}"
        else:
            # fallback
            stage_counters.setdefault("_fallback", 0)
            stage_counters["_fallback"] += 1
            s.step_number = f"10.{stage_counters['_fallback']:03d}"

    return steps


def parse_raw_flow(filepath: str, product_name: str) -> RawFlowParseResult:
    """解析单个原始 flow 文件

    支持格式：
        Stage_name  step_name  large_ct  middle_ct  small_ct  可用eqp  [可用eqp2] ...
        或
        step_name  step_number  eqp_name  CT_3  CT_8  CT_13

    Args:
        filepath: 原始 flow 文件路径
        product_name: 产品名称（从文件名提取）

    Returns:
        RawFlowParseResult 包含解析后的步骤列表
    """
    result = RawFlowParseResult(product_name=product_name, filename=os.path.basename(filepath))

    if not os.path.exists(filepath):
        result.success = False
        result.errors.append(f"文件不存在: {filepath}")
        return result

    # 读取文件
    try:
        df = _read_file_flexible(filepath)
    except Exception as e:
        result.success = False
        result.errors.append(f"读取文件失败: {e}")
        return result

    if df.empty:
        result.success = False
        result.errors.append("文件为空")
        return result

    columns = list(df.columns)

    # 检测各列
    stage_name_col = _detect_column(columns, STAGE_NAME_PATTERNS)
    step_name_col = _detect_column(columns, STEP_NAME_PATTERNS)
    step_number_col = _detect_column(columns, STEP_NUMBER_PATTERNS)
    ct_cols = _detect_ct_columns(columns)

    # 收集已知列名，用于后续检测 eqp 列
    known_cols = set()
    if stage_name_col:
        known_cols.add(stage_name_col)
    if step_name_col:
        known_cols.add(step_name_col)
    if step_number_col:
        known_cols.add(step_number_col)
    for col in ct_cols.values():
        known_cols.add(col)

    # 检测设备列（支持多列）
    eqp_cols = _detect_eqp_columns(columns, known_cols, df)

    # 校验
    if not step_name_col:
        result.success = False
        result.errors.append(f"未找到步骤名列（step_name），可用列: {columns}")
        return result

    if not eqp_cols:
        result.warnings.append(f"未找到设备列，可用列: {columns}。步骤将没有设备绑定。")
    if not ct_cols:
        result.warnings.append(f"未找到 CT 列（large_ct/middle_ct/small_ct），可用列: {columns}。将不生成 CT 数据。")

    # 解析每一行
    for _, row in df.iterrows():
        step_name = str(row[step_name_col]).strip()
        if not step_name or step_name.lower() in ("nan", "none", ""):
            continue

        stage_name = ""
        if stage_name_col:
            stage_name = str(row[stage_name_col]).strip() if pd.notna(row[stage_name_col]) else ""

        step_number = ""
        if step_number_col:
            step_number = str(row[step_number_col]).strip() if pd.notna(row[step_number_col]) else ""

        # 收集设备 ID（从多列）
        eqp_ids = _collect_eqp_ids(row, eqp_cols)

        # 解析 CT（原始单位：小时 → 转换为分钟）
        ct_3 = _safe_float(row, ct_cols.get(3))
        ct_8 = _safe_float(row, ct_cols.get(8))
        ct_13 = _safe_float(row, ct_cols.get(13))
        if ct_3 is not None:
            ct_3 = round(ct_3 * 60, 4)
        if ct_8 is not None:
            ct_8 = round(ct_8 * 60, 4)
        if ct_13 is not None:
            ct_13 = round(ct_13 * 60, 4)

        result.steps.append(RawFlowStep(
            product_name=product_name,
            step_number=step_number,
            step_name=step_name,
            stage_name=stage_name,
            eqp_ids=eqp_ids,
            ct_3=ct_3,
            ct_8=ct_8,
            ct_13=ct_13,
        ))

    if not result.steps:
        result.success = False
        result.errors.append("未解析到任何步骤数据")
        return result

    # 处理合并单元格：stage_name 列只在第一行有值，后续空行需要 forward-fill
    if stage_name_col:
        _forward_fill_stage_name(result.steps)
        result.warnings.append("stage_name 已自动填充合并单元格（forward-fill）")

    # 自动生成 step_number（如果缺失）
    _auto_generate_step_numbers(result.steps)

    return result


def _read_file_flexible(filepath: str) -> pd.DataFrame:
    """灵活读取文件，自动尝试编码和分隔符，支持 CSV 和 Excel"""
    ext = os.path.splitext(filepath)[1].lower()

    # Excel 文件
    if ext in (".xlsx", ".xls"):
        try:
            df = pd.read_excel(filepath, dtype=str)
            return df
        except Exception as e:
            raise Exception(f"无法读取 Excel 文件 {filepath}: {e}")

    # CSV 文件
    for encoding in ["utf-8", "utf-8-sig", "gbk", "gb2312", "gb18030", "latin-1"]:
        for sep in ["\t", ",", ";"]:
            try:
                df = pd.read_csv(filepath, sep=sep, dtype=str, encoding=encoding)
                if len(df.columns) >= 2:
                    return df
            except (UnicodeDecodeError, UnicodeError):
                continue
            except pd.errors.EmptyDataError:
                return pd.DataFrame()
            except Exception:
                continue
    raise Exception(f"无法读取文件 {filepath}")


def _safe_float(row, col: Optional[str]) -> Optional[float]:
    """安全读取浮点数"""
    if col is None or col not in row.index:
        return None
    val = row[col]
    if pd.isna(val) or str(val).strip() in ("", "nan", "None", "-"):
        return None
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


def _read_system_csv(filepath: str) -> pd.DataFrame:
    """读取系统 CSV 文件（tab 分隔），不存在返回空 DataFrame"""
    if not os.path.exists(filepath):
        return pd.DataFrame()
    try:
        # 先尝试 tab 分隔
        df = pd.read_csv(filepath, sep="\t", dtype=str)
        return df
    except Exception:
        return pd.DataFrame()


def convert_and_merge(
    parsed_results: list[RawFlowParseResult],
    data_dir: str,
) -> ImportResult:
    """将解析结果转换为 flow.csv 和 step_ct.csv 格式，并与现有数据合并

    Args:
        parsed_results: 解析结果列表
        data_dir: 数据目录路径

    Returns:
        ImportResult 包含导入统计信息
    """
    import_result = ImportResult()
    import_result.total_files = len(parsed_results)

    flow_path = os.path.join(data_dir, "flow.csv")
    step_ct_path = os.path.join(data_dir, "step_ct.csv")

    # 读取现有数据
    existing_flow = _read_system_csv(flow_path)
    existing_step_ct = _read_system_csv(step_ct_path)

    # 收集所有需要导入的产品
    all_product_names = set()
    all_flow_steps: list[RawFlowStep] = []
    for pr in parsed_results:
        if not pr.success:
            import_result.error_files.append(pr.filename)
            import_result.warnings.extend(pr.errors)
            continue
        import_result.success_files.append(pr.filename)
        all_product_names.add(pr.product_name)
        all_flow_steps.extend(pr.steps)

    if not all_flow_steps:
        import_result.warnings.append("没有成功解析的数据可以导入")
        return import_result

    # 判断是新增还是更新
    if not existing_flow.empty and "product_name" in existing_flow.columns:
        existing_products = set(existing_flow["product_name"].dropna().unique())
        for pn in all_product_names:
            if pn in existing_products:
                import_result.products_updated.append(pn)
            else:
                import_result.products_added.append(pn)
    else:
        import_result.products_added = list(all_product_names)

    # ── 生成 flow.csv 数据 ──
    # 按 (product_name, step_number) 合并 eqp_ids 和 stage_name
    flow_merged: dict[tuple[str, str], tuple[str, str, list[str]]] = {}
    for step in all_flow_steps:
        key = (step.product_name, step.step_number)
        if key in flow_merged:
            existing_name, existing_stage, existing_eqps = flow_merged[key]
            for eid in step.eqp_ids:
                if eid not in existing_eqps:
                    existing_eqps.append(eid)
        else:
            flow_merged[key] = (step.step_name, step.stage_name, list(step.eqp_ids))

    new_flow_rows = []
    for (product_name, step_number), (step_name, stage_name, eqp_ids) in flow_merged.items():
        new_flow_rows.append({
            "product_name": product_name,
            "step_number": step_number,
            "step_name": step_name,
            "stage_name": stage_name,
            "eqp_id": ",".join(eqp_ids) if eqp_ids else "",
        })

    new_flow_df = pd.DataFrame(new_flow_rows, columns=["product_name", "step_number", "step_name", "stage_name", "eqp_id"])

    # 合并：删除旧产品数据，追加新数据
    if not existing_flow.empty and "product_name" in existing_flow.columns:
        import_result.flow_rows_removed = len(existing_flow[existing_flow["product_name"].isin(all_product_names)])
        existing_flow = existing_flow[~existing_flow["product_name"].isin(all_product_names)]
        merged_flow = pd.concat([existing_flow, new_flow_df], ignore_index=True)
    else:
        import_result.flow_rows_removed = 0
        merged_flow = new_flow_df

    import_result.flow_rows_added = len(new_flow_df)

    # 保存
    merged_flow.to_csv(flow_path, index=False, sep="\t")

    # ── 生成 step_ct.csv 数据 ──
    new_ct_rows = []
    zero_ct_steps: list[str] = []  # 记录 CT 全为空的步骤
    for step in all_flow_steps:
        # 线性插值生成 1-13 片 CT
        ct_map = _interpolate_ct(step.ct_3, step.ct_8, step.ct_13)
        if step.ct_3 is None and step.ct_8 is None and step.ct_13 is None:
            zero_ct_steps.append(f"{step.product_name} / {step.step_name} ({step.step_number})")
        for qty, ct_val in ct_map.items():
            new_ct_rows.append({
                "product_name": step.product_name,
                "step_number": step.step_number,
                "step_name": step.step_name,
                "qty": str(qty),
                "step_ct": str(ct_val),
            })

    if zero_ct_steps:
        import_result.warnings.append(
            f"以下 {len(zero_ct_steps)} 个步骤的 CT 值为空，已默认设为 0：\n    " +
            "\n    ".join(zero_ct_steps)
        )

    new_ct_df = pd.DataFrame(new_ct_rows, columns=["product_name", "step_number", "step_name", "qty", "step_ct"])

    if not existing_step_ct.empty and "product_name" in existing_step_ct.columns:
        import_result.step_ct_rows_removed = len(existing_step_ct[existing_step_ct["product_name"].isin(all_product_names)])
        existing_step_ct = existing_step_ct[~existing_step_ct["product_name"].isin(all_product_names)]
        merged_ct = pd.concat([existing_step_ct, new_ct_df], ignore_index=True)
    else:
        import_result.step_ct_rows_removed = 0
        merged_ct = new_ct_df

    import_result.step_ct_rows_added = len(new_ct_df)

    # 保存
    merged_ct.to_csv(step_ct_path, index=False, sep="\t")

    return import_result


def import_flow_files(import_dir: str, data_dir: str) -> ImportResult:
    """批量导入原始 flow 文件

    扫描 import_dir 下所有 CSV 文件，解析并转换后合并到系统数据中。
    文件名作为 product_name。

    Args:
        import_dir: 原始 flow 文件目录
        data_dir: 系统数据目录

    Returns:
        ImportResult 包含详细导入统计
    """
    if not os.path.exists(import_dir):
        return ImportResult(total_files=0, warnings=[f"导入目录不存在: {import_dir}"])

    # 扫描所有 CSV 文件
    files = sorted([f for f in os.listdir(import_dir) if f.lower().endswith((".csv", ".xlsx", ".xls"))])
    if not files:
        return ImportResult(total_files=0, warnings=[f"导入目录中没有文件: {import_dir}"])

    parsed_results = []
    for filename in files:
        filepath = os.path.join(import_dir, filename)
        # 文件名去掉扩展名作为 product_name
        product_name = os.path.splitext(filename)[0]
        result = parse_raw_flow(filepath, product_name)
        parsed_results.append(result)

    return convert_and_merge(parsed_results, data_dir)