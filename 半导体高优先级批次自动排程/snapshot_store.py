"""多方案快照存储与对比

每次排程结果（含原始设备/批次条目，供甘特图与 Excel 复用）可保存为一个 JSON 快照，
支持列出 / 加载 / 删除 / 导出。快照存于 output/snapshots/ 目录。

快照内容:
  - response:      完整报表响应（统计、汇总、告警、步骤明细等，可直接供前端渲染）
  - lot_entries / eqp_entries / qtime_alerts: 原始排程对象（恢复甘特图与 Excel 用）
  - shift_times:   班次时间（甘特图班次背景用）
"""
import json
import os
from datetime import datetime

from models import ScheduleEntry, EqpScheduleEntry, QTimeAlert
from paths import SNAPSHOT_DIR


def _safe_id(snap_id: str) -> bool:
    """快照 id 仅允许字母数字、下划线、横线、井号与点，防止路径穿越"""
    if not snap_id:
        return False
    return os.path.basename(snap_id) == snap_id and all(
        c.isalnum() or c in "_-.#" for c in snap_id)


def _new_snapshot_id(seed) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{ts}_s{seed}"
    path = os.path.join(SNAPSHOT_DIR, f"{base}.json")
    i = 1
    while os.path.exists(path):
        path = os.path.join(SNAPSHOT_DIR, f"{base}_{i}.json")
        i += 1
    return os.path.splitext(os.path.basename(path))[0]


def _se_dict(e: ScheduleEntry) -> dict:
    return {
        "lot_name": e.lot_name, "priority": e.priority,
        "product_name": e.product_name, "step_number": e.step_number,
        "step_name": e.step_name, "eqp_id": e.eqp_id,
        "start_time": e.start_time.isoformat(), "end_time": e.end_time.isoformat(),
        "ct": e.ct, "qtime_risk": e.qtime_risk, "stage_name": e.stage_name,
    }


def _ee_dict(e: EqpScheduleEntry) -> dict:
    return {
        "eqp_id": e.eqp_id,
        "start_time": e.start_time.isoformat(), "end_time": e.end_time.isoformat(),
        "lot_name": e.lot_name, "step_name": e.step_name, "qty": e.qty,
    }


def _qa_dict(a: QTimeAlert) -> dict:
    return {
        "lot_name": a.lot_name, "qtime_rule": a.qtime_rule,
        "start_time": a.start_time.isoformat(), "deadline": a.deadline.isoformat(),
        "actual_end": a.actual_end.isoformat(),
        "over_minutes": a.over_minutes, "status": a.status,
    }


def save_snapshot(response: dict, lot_entries, eqp_entries, qtime_alerts,
                  shift_times, seed) -> str:
    """保存快照，返回 snapshot_id"""
    snap_id = _new_snapshot_id(seed)
    data = {
        "snapshot_id": snap_id,
        "created_at": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        "seed": seed,
        "response": response,
        "lot_entries": [_se_dict(e) for e in lot_entries],
        "eqp_entries": [_ee_dict(e) for e in eqp_entries],
        "qtime_alerts": [_qa_dict(a) for a in (qtime_alerts or [])],
        "shift_times": [list(t) for t in (shift_times or [])],
    }
    with open(os.path.join(SNAPSHOT_DIR, f"{snap_id}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return snap_id


def list_snapshots() -> list:
    """列出全部快照摘要（按创建时间倒序）"""
    out = []
    for fn in sorted(os.listdir(SNAPSHOT_DIR)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(SNAPSHOT_DIR, fn)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        out.append(_summary(d))
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


def _summary(d: dict) -> dict:
    resp = d.get("response") or {}
    stats = resp.get("stats") or {}
    lot_summaries = resp.get("lot_summaries") or {}
    ve = resp.get("validation_errors")
    return {
        "snapshot_id": d.get("snapshot_id", ""),
        "created_at": d.get("created_at", ""),
        "seed": d.get("seed"),
        "score": stats.get("best_score"),
        "min_qtime_margin": stats.get("min_qtime_margin"),
        "qtime_alerts": stats.get("qtime_alerts", 0),
        "validation_errors": stats.get("validation_errors", 0),
        "validation_error_list": (ve if isinstance(ve, list) else [])[:20],
        "valid_iterations": stats.get("valid_iterations", 0),
        "total_iterations": stats.get("total_iterations", 0),
        "lot_entries": stats.get("lot_entries", 0),
        "eqp_entries": stats.get("eqp_entries", 0),
        "lots": len((lot_summaries or {}).get("rows", [])) if isinstance(lot_summaries, dict) else 0,
        "warning": resp.get("warning"),
    }


def load_snapshot(snap_id: str):
    """加载快照，返回 dict(response, lot_entries, eqp_entries, qtime_alerts, shift_times, seed)；
    不存在返回 None"""
    if not _safe_id(snap_id):
        return None
    path = os.path.join(SNAPSHOT_DIR, f"{snap_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    lot_entries = [
        ScheduleEntry(
            lot_name=e.get("lot_name", ""), priority=e.get("priority", "9-9"),
            product_name=e.get("product_name", ""), step_number=e.get("step_number", ""),
            step_name=e.get("step_name", ""), eqp_id=e.get("eqp_id", "-"),
            start_time=datetime.fromisoformat(e["start_time"]),
            end_time=datetime.fromisoformat(e["end_time"]),
            ct=e.get("ct", 0.0), qtime_risk=e.get("qtime_risk", ""),
            stage_name=e.get("stage_name", ""),
        )
        for e in d.get("lot_entries", [])
    ]
    eqp_entries = [
        EqpScheduleEntry(
            eqp_id=e.get("eqp_id", "-"),
            start_time=datetime.fromisoformat(e["start_time"]),
            end_time=datetime.fromisoformat(e["end_time"]),
            lot_name=e.get("lot_name", ""), step_name=e.get("step_name", ""),
            qty=e.get("qty", 0),
        )
        for e in d.get("eqp_entries", [])
    ]
    qtime_alerts = [
        QTimeAlert(
            lot_name=a.get("lot_name", ""), qtime_rule=a.get("qtime_rule", ""),
            start_time=datetime.fromisoformat(a["start_time"]),
            deadline=datetime.fromisoformat(a["deadline"]),
            actual_end=datetime.fromisoformat(a["actual_end"]),
            over_minutes=a.get("over_minutes", 0), status=a.get("status", "OK"),
        )
        for a in d.get("qtime_alerts", [])
    ]
    return {
        "response": d.get("response", {}),
        "lot_entries": lot_entries,
        "eqp_entries": eqp_entries,
        "qtime_alerts": qtime_alerts,
        "shift_times": [tuple(t) for t in d.get("shift_times", [])],
        "seed": d.get("seed"),
    }


def delete_snapshot(snap_id: str) -> bool:
    if not _safe_id(snap_id):
        return False
    path = os.path.join(SNAPSHOT_DIR, f"{snap_id}.json")
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True
