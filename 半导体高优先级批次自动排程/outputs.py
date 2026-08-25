"""输出格式化 —— Lot 视图 + 设备视图 + Q-time 风险"""
import pandas as pd
from models import ScheduleEntry, EqpScheduleEntry, QTimeAlert


def to_lot_dataframe(entries: list[ScheduleEntry]) -> pd.DataFrame:
    """生成 Lot 维度 DataFrame"""
    if not entries:
        return pd.DataFrame(columns=[
            "lot_name", "priority", "product_name", "stage_name", "step_number", "step_name",
            "eqp_id", "start_time", "end_time", "ct(min)", "qtime_risk"
        ])

    data = []
    for e in entries:
        data.append({
            "lot_name": e.lot_name,
            "priority": e.priority,
            "product_name": e.product_name,
            "stage_name": e.stage_name,
            "step_number": e.step_number,
            "step_name": e.step_name,
            "eqp_id": e.eqp_id,
            "start_time": e.start_time.strftime("%m/%d %H:%M"),
            "end_time": e.end_time.strftime("%m/%d %H:%M"),
            "ct(min)": e.ct,
            "qtime_risk": e.qtime_risk,
        })

    df = pd.DataFrame(data)
    # 按 lot_name + start_time 排序
    df = df.sort_values(["lot_name", "start_time"]).reset_index(drop=True)
    return df


def to_eqp_dataframe(entries: list[EqpScheduleEntry]) -> pd.DataFrame:
    """生成设备维度 DataFrame"""
    if not entries:
        return pd.DataFrame(columns=[
            "eqp_id", "start_time", "end_time", "lot_name", "step_name", "qty"
        ])

    data = []
    for e in entries:
        data.append({
            "eqp_id": e.eqp_id,
            "start_time": e.start_time.strftime("%m/%d %H:%M"),
            "end_time": e.end_time.strftime("%m/%d %H:%M"),
            "lot_name": e.lot_name,
            "step_name": e.step_name,
            "qty": e.qty,
        })

    df = pd.DataFrame(data)
    df = df.sort_values(["eqp_id", "start_time"]).reset_index(drop=True)
    return df


def to_qtime_dataframe(alerts: list[QTimeAlert]) -> pd.DataFrame:
    """生成 Q-time 风险 DataFrame"""
    if not alerts:
        return pd.DataFrame(columns=[
            "lot_name", "qtime_rule", "start_time", "deadline", "actual_end", "over_min", "status"
        ])

    data = []
    for a in alerts:
        data.append({
            "lot_name": a.lot_name,
            "qtime_rule": a.qtime_rule,
            "start_time": a.start_time.strftime("%m/%d %H:%M"),
            "deadline": a.deadline.strftime("%m/%d %H:%M"),
            "actual_end": a.actual_end.strftime("%m/%d %H:%M"),
            "over_min": a.over_minutes,
            "status": a.status,
        })

    df = pd.DataFrame(data)
    return df


def to_csv(df: pd.DataFrame) -> str:
    """DataFrame 转 CSV 字符串"""
    return df.to_csv(index=False)


def to_tsv(df: pd.DataFrame) -> str:
    """DataFrame 转 TSV 字符串"""
    return df.to_csv(index=False, sep="\t")