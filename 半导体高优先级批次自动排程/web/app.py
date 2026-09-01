import os
import sys
import io
import json
import threading
import time as _time
from datetime import datetime, time, timedelta as td
from flask import Flask, request, jsonify, render_template, send_file, send_from_directory
import pandas as pd

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

from paths import DATA_DIR, OUTPUT_DIR, STATIC_DIR, TEMPLATES_DIR  # noqa: E402
from snapshot_store import save_snapshot, list_snapshots, load_snapshot, delete_snapshot  # noqa: E402

# 模板/静态目录：源码运行时用 web 下真实目录；打包运行时用捆绑目录/根目录 static
app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.json.sort_keys = False  # 保持 JSON key 的原始顺序，不按字母排序

# CORS 支持（仅允许本机页面跨源访问，防止任意网页读取/篡改本地数据）
from urllib.parse import urlparse


def _origin_allowed(origin):
    """仅允许 127.0.0.1 / localhost 源；无 Origin（curl/脚本等非浏览器客户端）放行"""
    if not origin:
        return True
    try:
        host = (urlparse(origin).netloc or "").split(":")[0]
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost")


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if _origin_allowed(origin):
        response.headers['Access-Control-Allow-Origin'] = origin or '*'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@app.route('/')
def index():
    return render_template('index.html')


def _safe_table_name(name):
    """表名必须是 .csv 结尾的纯文件名，防止路径穿越（历史 bug）"""
    return bool(name) and name.endswith(".csv") \
        and os.path.basename(name) == name and "/" not in name and "\\" not in name


@app.route('/api/upload', methods=['POST'])
def upload():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file provided'}), 400

    filename = os.path.basename((file.filename or "").replace("\\", "/"))
    if not filename:
        return jsonify({'error': 'No filename'}), 400

    try:
        if filename.lower().endswith('.csv'):
            content = file.read().decode('utf-8')
            # 自动检测分隔符：先试 tab，如果只有一列则试逗号
            try:
                df = pd.read_csv(io.StringIO(content), sep='\t', dtype=str)
                if len(df.columns) < 2:
                    df = pd.read_csv(io.StringIO(content), sep=',', dtype=str)
            except Exception:
                df = pd.read_csv(io.StringIO(content), sep=',', dtype=str)
        elif filename.lower().endswith('.xlsx'):
            df = pd.read_excel(io.BytesIO(file.read()))
        else:
            return jsonify({'error': 'Unsupported file type. Please upload .csv or .xlsx'}), 400

        # Save as CSV (tab-separated)
        csv_name = os.path.splitext(filename)[0] + '.csv'
        csv_path = os.path.join(DATA_DIR, csv_name)
        df.to_csv(csv_path, index=False, sep='\t')

        return jsonify({
            'name': csv_name,
            'columns': df.columns.tolist(),
            'rows': df.fillna('').to_dict(orient='records')
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tables', methods=['GET'])
def tables():
    tables = []
    if os.path.exists(DATA_DIR):
        for f in os.listdir(DATA_DIR):
            if f.lower().endswith('.csv'):
                tables.append(f)
    tables.sort()
    return jsonify(tables)


@app.route('/api/form-options', methods=['GET'])
def form_options():
    """表单下拉参照数据：产品→步骤、Lot→产品，供前端按 product_name 联动下拉。

    与排程共用 data_loader 解析，保证下拉选项与排程实际接受的步骤/产品一致。
    """
    try:
        from data_loader import load_flow, load_lot_list, get_product_flow_map
        flows = load_flow(os.path.join(DATA_DIR, "flow.csv"))
        flow_map = get_product_flow_map(flows)
        product_steps = {
            product: [s.step_name for s in steps]
            for product, steps in sorted(flow_map.items())
        }
        products = sorted(product_steps.keys())
        lots = []
        if os.path.exists(os.path.join(DATA_DIR, "lot_list.csv")):
            lots = load_lot_list(
                os.path.join(DATA_DIR, "lot_list.csv"),
                constraints_filepath=os.path.join(DATA_DIR, "lot_constraints.csv"))
        lot_names = sorted({l.lot_name for l in lots})
        lot_product = {l.lot_name: l.product_name for l in lots}
        return jsonify({
            "success": True,
            "products": products,
            "product_steps": product_steps,
            "lot_names": lot_names,
            "lot_product": lot_product,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/table/<name>', methods=['GET'])
def get_table(name):
    if not _safe_table_name(name):
        return jsonify({'error': 'Invalid table name'}), 400
    csv_path = os.path.join(DATA_DIR, name)
    if not os.path.exists(csv_path):
        return jsonify({'error': 'Table not found'}), 404

    try:
        # 空文件直接返回空表
        if os.path.getsize(csv_path) == 0:
            return jsonify({'name': name, 'columns': [], 'rows': []})

        df = pd.read_csv(csv_path, sep='\t', dtype=str)
        return jsonify({
            'name': name,
            'columns': df.columns.tolist(),
            'rows': df.fillna('').to_dict(orient='records')
        })
    except pd.errors.EmptyDataError:
        return jsonify({'name': name, 'columns': [], 'rows': []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/save/<name>', methods=['POST'])
def save_table(name):
    if not _safe_table_name(name):
        return jsonify({'error': 'Invalid table name'}), 400
    csv_path = os.path.join(DATA_DIR, name)
    data = request.get_json()
    if not data or 'rows' not in data:
        return jsonify({'error': 'Invalid data format'}), 400

    try:
        # 使用前端传来的列顺序，确保保存后列顺序不变
        columns_order = data.get('columns') if 'columns' in data else None
        if not data['rows'] and columns_order:
            df = pd.DataFrame(columns=columns_order)
        else:
            df = pd.DataFrame(data['rows'], columns=columns_order)
        df.to_csv(csv_path, index=False, sep='\t')
        return jsonify({'success': True, 'name': name})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/download/<name>', methods=['GET'])
def download_table(name):
    if not _safe_table_name(name):
        return jsonify({'error': 'Invalid table name'}), 400
    csv_path = os.path.join(DATA_DIR, name)
    if not os.path.exists(csv_path):
        return jsonify({'error': 'Table not found'}), 404
    # 读取 tab 分隔的 CSV，转换为逗号分隔的标准 CSV 供下载（Excel 打开显示为单元格）
    import io as io_module
    try:
        df = pd.read_csv(csv_path, sep='\t', dtype=str)
    except pd.errors.EmptyDataError:
        # 空文件返回空内容
        buf = io_module.BytesIO()
        buf.seek(0)
        return send_file(buf, as_attachment=True, download_name=name, mimetype='text/csv')
    buf = io_module.BytesIO()
    df.to_csv(buf, index=False, encoding='utf-8-sig')
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=name, mimetype='text/csv')


# ── 报表路由 ──

@app.route('/reports')
def reports():
    """可视化报表页面"""
    return render_template('reports.html')


def _run_schedule(seed: int, req_body: dict, export_excel: bool = True):
    """用给定 seed 运行排程并构建完整响应（不保存快照）。

    返回 (response_dict, lot_entries, eqp_entries, qtime_alerts, shift_times)。
    """
    try:
        from data_loader import (
            load_lot_list, load_flow, load_step_ct,
            load_qtime, build_ct_lookup, auto_repair_step_ct,
            load_ftf_qty_change,
            load_special_lot_step, load_priority_wait, load_lot_constraints,
            load_eqp_constraints, load_shift_change_times,
            load_step_time_windows, load_shift_config, load_manual_adjusts,
            load_special_eqp,
        )
        from scheduler import schedule
        from optimizer import schedule_optimized
        from validation import validate_schedule, compute_objective
        from visualization import plot_lot_gantt, plot_eqp_gantt, export_to_excel

        # 加载所有数据
        lots = load_lot_list(f"{DATA_DIR}/lot_list.csv",
                            constraints_filepath=f"{DATA_DIR}/lot_constraints.csv")
        flows = load_flow(f"{DATA_DIR}/flow.csv")
        step_cts = load_step_ct(f"{DATA_DIR}/step_ct.csv")
        step_ct_path = f"{DATA_DIR}/step_ct.csv"
        step_cts = auto_repair_step_ct(flows, step_cts, step_ct_filepath=step_ct_path)
        qtimes = load_qtime(f"{DATA_DIR}/qtime.csv")
        ct_lookup = build_ct_lookup(step_cts)

        # 可选配置文件（文件不存在或为空时返回空）
        ftf_qty_change = load_ftf_qty_change(f"{DATA_DIR}/ftf_qty_change.csv") if os.path.exists(f"{DATA_DIR}/ftf_qty_change.csv") else {}
        special_lot_step_lookup = load_special_lot_step(f"{DATA_DIR}/special_lot_step.csv") if os.path.exists(f"{DATA_DIR}/special_lot_step.csv") else {}
        priority_wait_map = load_priority_wait(f"{DATA_DIR}/priority_wait.csv") if os.path.exists(f"{DATA_DIR}/priority_wait.csv") else {}
        eqp_constraints = load_eqp_constraints(f"{DATA_DIR}/eqp_constraint.csv") if os.path.exists(f"{DATA_DIR}/eqp_constraint.csv") else []
        step_time_window_constraints = load_step_time_windows(f"{DATA_DIR}/step_time_window.csv") if os.path.exists(f"{DATA_DIR}/step_time_window.csv") else []
        shift_configs = load_shift_config(f"{DATA_DIR}/shift_config.csv") if os.path.exists(f"{DATA_DIR}/shift_config.csv") else []
        shift_change_times = load_shift_change_times(f"{DATA_DIR}/shift_change_time.csv") if os.path.exists(f"{DATA_DIR}/shift_change_time.csv") else []
        manual_adjusts = load_manual_adjusts(f"{DATA_DIR}/manual_adjust.csv") if os.path.exists(f"{DATA_DIR}/manual_adjust.csv") else []
        special_eqp_map = load_special_eqp(f"{DATA_DIR}/special_eqp.csv") if os.path.exists(f"{DATA_DIR}/special_eqp.csv") else {}
        lot_constraints = load_lot_constraints(f"{DATA_DIR}/lot_constraints.csv") if os.path.exists(f"{DATA_DIR}/lot_constraints.csv") else []

        # 合并内存中的手动调整（Web 界面动态添加的；缓存优先于文件，
        # 保证"编辑文件里已存在的规则"在界面上能真正生效）
        if _manual_adjusts_cache:
            from models import ManualAdjust
            manual_map = {(ma.lot_name, ma.step_name): ma for ma in manual_adjusts}
            for ma_dict in _manual_adjusts_cache:
                delay_to = datetime.strptime(ma_dict["delay_to"], "%Y/%m/%d %H:%M")
                mode = ma_dict.get("mode", "delay")
                manual_map[(ma_dict["lot_name"], ma_dict["step_name"] or None)] = ManualAdjust(
                    lot_name=ma_dict["lot_name"],
                    step_name=ma_dict["step_name"] or None,
                    delay_to=delay_to,
                    mode=mode if mode in ("delay", "pin") else "delay",
                )
            manual_adjusts = list(manual_map.values())

        # 从 shift_config 解析班次时间
        shift_times = []
        for sc in shift_configs:
            try:
                h, m = map(int, sc.start_time_str.split(":"))
                shift_times.append((h, m))
            except (ValueError, AttributeError):
                pass
        shift_times.sort()

        # 迭代优化参数（前端可传；否则用已保存配置，均来自 optimizer_config.py）
        _cfg = _load_config_dict()
        max_iterations = int(req_body.get("max_iterations", _cfg.get("max_iterations", 40)) or 40)
        weight_by_priority = bool(req_body.get("weight_by_priority", _cfg.get("weight_by_priority", True)))
        resolve_max_iterations = int(req_body.get("resolve_max_iterations", _cfg.get("resolve_max_iterations", 10)) or 10)
        early_stop_patience = int(req_body.get("early_stop_patience", _cfg.get("early_stop_patience", 0)) or 0)
        tight_chain_threshold = _cfg.get("tight_chain_threshold")
        qtight_safety_margin = _cfg.get("qtight_safety_margin")
        chain_wait_safety = _cfg.get("chain_wait_safety")
        cross_shift_avoid = _cfg.get("cross_shift_avoid")
        if cross_shift_avoid is None:
            cross_shift_avoid = True

        # ---- SA+Tabu 细调参数（由配置读取，前端可覆盖） ----
        refine_enabled = bool(req_body.get("refine_enabled", _cfg.get("refine_enabled", True)))
        refine_max_iterations = int(req_body.get("refine_max_iterations", _cfg.get("refine_max_iterations", 60)) or 60)
        tabu_tenure = int(req_body.get("tabu_tenure", _cfg.get("tabu_tenure", 8)) or 8)
        sa_temperature_start = float(req_body.get("sa_temperature_start", _cfg.get("sa_temperature_start", 200.0)))
        sa_temperature_end = float(req_body.get("sa_temperature_end", _cfg.get("sa_temperature_end", 2.0)))
        target_accept_rate = float(req_body.get("target_accept_rate", _cfg.get("target_accept_rate", 0.3)))
        adapt_window = int(req_body.get("adapt_window", _cfg.get("adapt_window", 20)) or 20)

        lot_entries, eqp_entries, qtime_alerts, meta = schedule_optimized(
            lots, flows, ct_lookup, qtimes,
            shift_times,
            ftf_qty_change=ftf_qty_change,
            special_lot_step_lookup=special_lot_step_lookup,
            priority_wait_map=priority_wait_map,
            eqp_constraints=eqp_constraints,
            step_time_window_constraints=step_time_window_constraints,
            shift_change_times=shift_change_times,
            manual_adjusts=manual_adjusts,
            special_eqp_map=special_eqp_map,
            lot_constraints=lot_constraints,
            resolve_max_iterations=resolve_max_iterations,
            max_iterations=max_iterations,
            seed=seed,
            weight_by_priority=weight_by_priority,
            early_stop_patience=early_stop_patience,
            tight_chain_threshold=tight_chain_threshold,
            qtight_safety_margin=qtight_safety_margin,
            chain_wait_safety=chain_wait_safety,
            cross_shift_avoid=cross_shift_avoid,
            refine_enabled=refine_enabled,
            refine_max_iterations=refine_max_iterations,
            tabu_tenure=tabu_tenure,
            sa_temperature_start=sa_temperature_start,
            sa_temperature_end=sa_temperature_end,
            target_accept_rate=target_accept_rate,
            adapt_window=adapt_window,
        )

        # 全量校验（含 Q-time/reference），供前端展示是否有遗留违规
        validation_errors = validate_schedule(
            lot_entries, eqp_entries, qtime_alerts, lots, flows, qtimes,
            lot_constraints=lot_constraints, shift_times=shift_times,
            special_eqp_map=special_eqp_map)

        # 尝试加载 GA 配置并优化（GA 已废弃，改为直接使用启发式结果）
        # 保留 schedule() 的结果即为最终结果

        # 生成甘特图（仅在首次生成时可选，后续按需生成）
        # 甘特图不再自动生成，由 /api/generate-gantt 端点按需生成

        # 生成 Excel（批量对比时跳过，仅导出最优方案）
        lot_order = [lot.lot_name for lot in lots]
        if export_excel:
            excel_path = os.path.join(OUTPUT_DIR, 'schedule_result.xlsx')
            export_to_excel(lot_entries, eqp_entries, qtime_alerts, excel_path,
                            lots=lots, shift_configs=shift_configs, lot_order=lot_order)

        # ── 构建分析摘要 ──
        from outputs import to_lot_dataframe
        lot_df = to_lot_dataframe(lot_entries)

        # 各Lot汇总（含班次切换详情）

        # 从 shift_configs 获取班次时间列表
        shift_starts = []
        for sc in shift_configs:
            try:
                h, m = map(int, sc.start_time_str.split(":"))
                shift_starts.append((sc.shift_name, h, m))
            except (ValueError, AttributeError):
                pass
        shift_starts.sort(key=lambda x: (x[1], x[2]))  # 按时间排序

        shift_type_map = {"白班": "D", "夜班": "N"}

        def _get_shift_label(dt):
            """返回给定 datetime 的班次标签，如 "8/25D" 或 "8/25N" """
            if not shift_starts:
                return ""
            n_shifts = len(shift_starts)
            # 尝试今天和昨天
            for day_offset in [0, -1]:
                check_date = dt.date() + td(days=day_offset)
                for i, (sname, sh, sm) in enumerate(shift_starts):
                    shift_start = datetime.combine(check_date, time(sh, sm))
                    next_idx = (i + 1) % n_shifts
                    next_name, next_h, next_m = shift_starts[next_idx]
                    if next_idx <= i:
                        shift_end = datetime.combine(check_date + td(days=1), time(next_h, next_m))
                    else:
                        shift_end = datetime.combine(check_date, time(next_h, next_m))
                    if shift_start <= dt < shift_end:
                        return shift_start.strftime("%m/%d") + shift_type_map.get(sname, sname)
            return ""

        # 预计算每个班次的结束时间（下一个班次开始，末班次为次日首班次）
        n_shifts = len(shift_starts)
        shift_end_map = {}
        for i, (sname, sh, sm) in enumerate(shift_starts):
            next_idx = (i + 1) % n_shifts
            next_name, next_h, next_m = shift_starts[next_idx]
            wraps = next_idx <= i
            shift_end_map[(sname, sh, sm)] = (next_h, next_m, wraps)

        # 第一遍：收集所有班次列
        all_shift_columns = []  # 保持顺序
        lot_steps_map = {}  # lot_name -> {shift_key -> step}
        lot_entries_map = {}  # lot_name -> sorted lot_entries
        for lot_name in lot_order:
            sub = lot_df[lot_df["lot_name"] == lot_name]
            if sub.empty:
                continue
            lot_entries_sorted = sorted(
                [e for e in lot_entries if e.lot_name == lot_name],
                key=lambda e: e.start_time
            )
            if not lot_entries_sorted:
                continue
            lot_entries_map[lot_name] = lot_entries_sorted

            lot_start_dt = lot_entries_sorted[0].start_time
            lot_end_dt = lot_entries_sorted[-1].end_time
            current_date = lot_start_dt.date()
            end_date = lot_end_dt.date()

            lot_steps_map[lot_name] = {}
            while current_date <= end_date:
                for shift_name, h, m in shift_starts:
                    # 班次结束时间
                    end_h, end_m, wraps = shift_end_map[(shift_name, h, m)]
                    shift_end_dt = datetime.combine(
                        current_date + td(days=1) if wraps else current_date,
                        time(end_h, end_m)
                    )
                    # 列头用班次开始时间标识，值用班次结束时的 step
                    shift_start_dt = datetime.combine(current_date, time(h, m))

                    if shift_end_dt <= lot_start_dt or shift_start_dt >= lot_end_dt:
                        continue
                    shift_key = shift_start_dt.strftime("%m/%d") + " " + shift_type_map.get(shift_name, shift_name)

                    # 找到在班次结束时刻正在执行的步骤（或刚完成的）
                    active_step = "-"
                    for entry in lot_entries_sorted:
                        if entry.start_time <= shift_end_dt <= entry.end_time:
                            active_step = entry.step_name
                            break
                        elif entry.end_time <= shift_end_dt:
                            active_step = entry.step_name

                    lot_steps_map[lot_name][shift_key] = active_step
                    if shift_key not in all_shift_columns:
                        all_shift_columns.append(shift_key)
                current_date += td(days=1)

        # 构建透视表行
        lot_by_name = {l.lot_name: l for l in lots}
        completion_map = {}
        for e in lot_entries:
            cur = completion_map.get(e.lot_name)
            if cur is None or e.end_time > cur:
                completion_map[e.lot_name] = e.end_time

        # 交期 KPI（仅展示，不计入目标函数）：统计延期批次与总延期分钟
        tardy_lots = 0
        total_tardiness = 0.0
        for lot_name, end in completion_map.items():
            lot = lot_by_name.get(lot_name)
            if lot and lot.planned_end and end > lot.planned_end:
                tardy_lots += 1
                total_tardiness += (end - lot.planned_end).total_seconds() / 60.0
        lot_shift_details = {
            "shift_columns": all_shift_columns,
            "rows": [
                {
                    "lot_name": lot_name,
                    "start_shift": _get_shift_label(lot_entries_sorted[0].start_time) if lot_name in lot_entries_map else "",
                    "finish_shift": _get_shift_label(lot_entries_sorted[-1].end_time) if lot_name in lot_entries_map else "",
                    "finish_time": completion_map.get(lot_name).strftime("%m/%d %H:%M") if lot_name in completion_map else "",
                    "planned_end": (lot_by_name[lot_name].planned_end.strftime("%m/%d %H:%M")
                                    if lot_by_name.get(lot_name) and lot_by_name[lot_name].planned_end else ""),
                    "tardiness": round(
                        (completion_map[lot_name] - lot_by_name[lot_name].planned_end).total_seconds() / 60.0, 1)
                        if (lot_name in completion_map and lot_by_name.get(lot_name)
                            and lot_by_name[lot_name].planned_end) else None,
                    "steps": {col: lot_steps_map.get(lot_name, {}).get(col, "-") for col in all_shift_columns}
                }
                for lot_name in lot_order if lot_name in lot_steps_map
            ]
        }

        # Q-time告警（只返回超时的）
        qtime_alerts_data = []
        qtime_over_count = 0
        for a in qtime_alerts:
            if a.status == "超时":
                qtime_alerts_data.append({
                    "lot_name": a.lot_name,
                    "qtime_rule": a.qtime_rule,
                    "start_time": a.start_time.strftime("%m/%d %H:%M"),
                    "deadline": a.deadline.strftime("%m/%d %H:%M"),
                    "actual_end": a.actual_end.strftime("%m/%d %H:%M"),
                    "over_minutes": a.over_minutes,
                    "status": a.status,
                })
                qtime_over_count += 1

        # 收集可用设备 ID 和 Lot 名称
        all_eqp_ids = sorted(set(e.eqp_id for e in eqp_entries if e.eqp_id != "-"))
        all_lot_names = sorted(set(e.lot_name for e in lot_entries))

        # 缓存排程结果供甘特图按需生成
        global _last_schedule_result
        _last_schedule_result = {
            "lot_entries": lot_entries,
            "eqp_entries": eqp_entries,
            "shift_times": shift_times,
        }

        resp = {
            "success": True,
            "stats": {
                "lot_entries": len(lot_entries),
                "eqp_entries": len(eqp_entries),
                "qtime_alerts": qtime_over_count,
                "validation_errors": len(validation_errors),
                "valid_iterations": meta.get("valid_iterations", 0),
                "total_iterations": meta.get("total_iterations", 0),
                "best_score": round(meta["best_score"], 2) if meta.get("best_score") is not None else None,
                "min_qtime_margin": round(meta["min_qtime_margin"], 1) if meta.get("min_qtime_margin") is not None else None,
                "tardy_lots": tardy_lots,
                "total_tardiness": round(total_tardiness, 1),
                "seed": meta.get("seed", 0),
            },
            "validation_errors": validation_errors[:50],
            "lot_summaries": lot_shift_details,
            "qtime_alerts": qtime_alerts_data,
            "lot_entries_data": [
                {
                    "lot_name": e.lot_name,
                    "priority": e.priority,
                    "product_name": e.product_name,
                    "stage_name": e.stage_name,
                    "step_number": e.step_number,
                    "step_name": e.step_name,
                    "eqp_id": e.eqp_id,
                    "start_time": e.start_time.strftime("%Y/%m/%d %H:%M"),
                    "end_time": e.end_time.strftime("%Y/%m/%d %H:%M"),
                    "ct": e.ct,
                    "qtime_risk": e.qtime_risk,
                    "shift": _get_shift_label(e.start_time),
                }
                for e in lot_entries
            ],
            "manual_adjusts": [
                {
                    "lot_name": ma.lot_name,
                    "step_name": ma.step_name or "",
                    "delay_to": ma.delay_to.strftime("%Y/%m/%d %H:%M") if ma.delay_to else "",
                }
                for ma in manual_adjusts
            ],
            "available_eqp_ids": all_eqp_ids,
            "available_lot_names": all_lot_names,
            "images": {
                "lot_gantt": "",
                "eqp_gantt": "",
            },
            "excel_url": "/api/report-excel",
            "warning": meta.get("warning"),
        }
        return resp, lot_entries, eqp_entries, qtime_alerts, shift_times
    except ImportError as e:
        import traceback
        traceback.print_exc()
        mod = str(e).split("'")[1] if "'" in str(e) else str(e)
        raise RuntimeError(f"缺少依赖: {mod}。请在 web 目录下运行: py -m pip install -r requirements.txt") from e
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise


def _load_export_ctx():
    """加载 Excel 导出所需的批次/班次/顺序信息（快照导出与最优方案导出用）"""
    from data_loader import load_lot_list, load_shift_config
    lots = load_lot_list(f"{DATA_DIR}/lot_list.csv",
                         constraints_filepath=f"{DATA_DIR}/lot_constraints.csv")
    shift_configs = load_shift_config(f"{DATA_DIR}/shift_config.csv") if os.path.exists(f"{DATA_DIR}/shift_config.csv") else []
    return lots, shift_configs, [l.lot_name for l in lots]


@app.route('/api/generate-report', methods=['POST'])
def generate_report():
    """运行单次排程并保存为快照"""
    try:
        req_body = request.get_json(silent=True) or {}
        _cfg = _load_config_dict()
        seed = int(req_body.get("seed", _cfg.get("seed", 0)) or 0)
        resp, lot_entries, eqp_entries, qtime_alerts, shift_times = _run_schedule(
            seed, req_body, export_excel=True)
        try:
            save_snapshot(resp, lot_entries, eqp_entries, qtime_alerts, shift_times, seed)
        except Exception as e:
            print("[generate-report] 保存快照失败:", e)
        return jsonify(resp)
    except ImportError as e:
        import traceback
        traceback.print_exc()
        mod = str(e).split("'")[1] if "'" in str(e) else str(e)
        return jsonify({"error": f"缺少依赖: {mod}。请在 web 目录下运行: py -m pip install -r requirements.txt"}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/snapshots/list', methods=['GET'])
def snapshots_list():
    """列出全部已保存快照摘要"""
    try:
        return jsonify({"success": True, "snapshots": list_snapshots()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _snap_worker_init(cache):
    """多 seed 并行 worker 初始化：把内存中的手动调整缓存传入子进程"""
    global _manual_adjusts_cache
    _manual_adjusts_cache = cache


def _snap_worker(args):
    """多 seed 并行 worker：跑单个 seed 的排程（不导出 Excel）"""
    seed, req_body = args
    return _run_schedule(seed, req_body, export_excel=False)


@app.route('/api/snapshots/generate', methods=['POST'])
def snapshots_generate():
    """用多个 seed 各跑一次排程，保存为快照并返回对比列表（最优方案自动导出 Excel）。

    多 seed 并行执行（多进程），利用多核加速批量生成。
    """
    try:
        req_body = request.get_json(silent=True) or {}
        num = int(req_body.get("num_snapshots", 3) or 3)
        num = max(1, min(num, 8))
        seeds_param = req_body.get("seeds")
        if seeds_param:
            seed_list = [int(s) for s in seeds_param]
        else:
            base_seed = int(req_body.get("base_seed", 0) or 0)
            seed_list = [base_seed + i for i in range(num)]

        # 顺序执行各 seed（不再用 ProcessPoolExecutor 起子进程）：
        # PyInstaller 打包后，多进程 worker 会以新进程重跑入口脚本，即使加了
        # freeze_support()，在部分 Windows 环境下仍可能反复打开浏览器/报端口冲突，
        # 表现为"生成多方案弹出多个网页"。顺序执行从根本上消除该问题；
        # 3 个方案约 15s、8 个约 40s，对多方案对比场景完全可接受。
        worker_args = [(s, req_body) for s in seed_list]
        results = []
        for a in worker_args:
            try:
                results.append(_snap_worker(a))
            except Exception:
                import traceback
                traceback.print_exc()
                results.append(None)

        best_id, best_score = None, None
        saved = 0
        for s, res in zip(seed_list, results):
            if res is None:
                continue
            resp, lot_entries, eqp_entries, qtime_alerts, shift_times = res
            snap_id = save_snapshot(resp, lot_entries, eqp_entries, qtime_alerts, shift_times, s)
            saved += 1
            score = resp.get("stats", {}).get("best_score")
            if score is not None and (best_score is None or score < best_score):
                best_id, best_score = snap_id, score

        # 为最优方案导出 Excel，保证"下载 Excel"与最优方案一致
        if best_id is not None:
            try:
                snap = load_snapshot(best_id)
                lots, shift_configs, lot_order = _load_export_ctx()
                from visualization import export_to_excel
                excel_path = os.path.join(OUTPUT_DIR, 'schedule_result.xlsx')
                export_to_excel(snap["lot_entries"], snap["eqp_entries"], snap["qtime_alerts"],
                                excel_path, lots=lots, shift_configs=shift_configs, lot_order=lot_order)
            except Exception as e:
                print("[snapshots/generate] 最优方案Excel导出失败:", e)

        return jsonify({
            "success": True,
            "count": saved,
            "best_snapshot_id": best_id,
            "snapshots": list_snapshots(),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/snapshots/get/<snap_id>', methods=['GET'])
def snapshots_get(snap_id):
    """加载指定快照，使其成为当前展示与甘特图的数据源"""
    try:
        snap = load_snapshot(snap_id)
        if snap is None:
            return jsonify({"error": "快照不存在"}), 404
        global _last_schedule_result
        _last_schedule_result = {
            "lot_entries": snap["lot_entries"],
            "eqp_entries": snap["eqp_entries"],
            "shift_times": snap["shift_times"],
        }
        resp = dict(snap["response"])
        resp["snapshot_id"] = snap_id
        resp["seed"] = snap["seed"]
        # 修复：查看快照后"下载 Excel"必须导出当前所看方案，
        # 而不是固定指向只含最优方案的 /api/report-excel
        resp["excel_url"] = f"/api/snapshots/export/{snap_id}"
        return jsonify(resp)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/snapshots/export/<snap_id>', methods=['GET'])
def snapshots_export(snap_id):
    """导出指定快照方案为 Excel"""
    try:
        snap = load_snapshot(snap_id)
        if snap is None:
            return jsonify({"error": "快照不存在"}), 404
        lots, shift_configs, lot_order = _load_export_ctx()
        from visualization import export_to_excel
        excel_path = os.path.join(OUTPUT_DIR, f"snapshot_{snap_id}.xlsx")
        export_to_excel(snap["lot_entries"], snap["eqp_entries"], snap["qtime_alerts"],
                        excel_path, lots=lots, shift_configs=shift_configs, lot_order=lot_order)
        return send_file(excel_path, as_attachment=True, download_name=f"snapshot_{snap_id}.xlsx")
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/snapshots/delete', methods=['POST'])
def snapshots_delete():
    """删除指定快照"""
    data = request.get_json(silent=True) or {}
    snap_id = (data.get("snapshot_id") or "").strip()
    if not snap_id:
        return jsonify({"error": "snapshot_id 不能为空"}), 400
    try:
        ok = delete_snapshot(snap_id)
        if not ok:
            return jsonify({"error": "快照不存在"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/health-check', methods=['GET', 'POST'])
def health_check():
    """排程前数据体检：检查数据一致性问题"""
    try:
        from data_loader import (
            load_lot_list, load_flow, load_step_ct, load_qtime,
            load_special_lot_step, load_priority_wait, load_lot_constraints,
            load_manual_adjusts, load_special_eqp, load_ftf_qty_change,
            load_step_time_windows,
        )
        from health_check import check_data

        lots = load_lot_list(f"{DATA_DIR}/lot_list.csv",
                             constraints_filepath=f"{DATA_DIR}/lot_constraints.csv")
        flows = load_flow(f"{DATA_DIR}/flow.csv")
        step_cts = load_step_ct(f"{DATA_DIR}/step_ct.csv")
        qtimes = load_qtime(f"{DATA_DIR}/qtime.csv")
        priority_wait_map = load_priority_wait(f"{DATA_DIR}/priority_wait.csv") if os.path.exists(f"{DATA_DIR}/priority_wait.csv") else None
        special_lot_step_lookup = load_special_lot_step(f"{DATA_DIR}/special_lot_step.csv") if os.path.exists(f"{DATA_DIR}/special_lot_step.csv") else None
        lot_constraints = load_lot_constraints(f"{DATA_DIR}/lot_constraints.csv") if os.path.exists(f"{DATA_DIR}/lot_constraints.csv") else []
        manual_adjusts = load_manual_adjusts(f"{DATA_DIR}/manual_adjust.csv") if os.path.exists(f"{DATA_DIR}/manual_adjust.csv") else []
        special_eqp_map = load_special_eqp(f"{DATA_DIR}/special_eqp.csv") if os.path.exists(f"{DATA_DIR}/special_eqp.csv") else {}
        ftf_qty_change = load_ftf_qty_change(f"{DATA_DIR}/ftf_qty_change.csv") if os.path.exists(f"{DATA_DIR}/ftf_qty_change.csv") else {}
        step_time_windows = load_step_time_windows(f"{DATA_DIR}/step_time_window.csv") if os.path.exists(f"{DATA_DIR}/step_time_window.csv") else []

        result = check_data(
            lots, flows, step_cts, qtimes,
            priority_wait_map=priority_wait_map,
            special_lot_step_lookup=special_lot_step_lookup,
            lot_constraints=lot_constraints,
            manual_adjusts=manual_adjusts,
            special_eqp_map=special_eqp_map,
            ftf_qty_change=ftf_qty_change,
            step_time_window_constraints=step_time_windows,
        )
        return jsonify({
            "success": True,
            "error_count": len(result["errors"]),
            "warning_count": len(result["warnings"]),
            "errors": result["errors"],
            "warnings": result["warnings"],
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/report-excel', methods=['GET'])
def download_report_excel():
    """下载排程结果 Excel"""
    excel_path = os.path.join(OUTPUT_DIR, 'schedule_result.xlsx')
    if not os.path.exists(excel_path):
        return jsonify({'error': 'Report not generated yet. Please generate the report first.'}), 404
    return send_file(excel_path, as_attachment=True, download_name='schedule_result.xlsx')


@app.route('/static/<path:filename>')
def serve_static(filename):
    """提供静态文件（甘特图等）"""
    return send_from_directory(STATIC_DIR, filename)


# ── 手动调整 API ──

# 内存中的手动调整缓存（用于 Web 界面动态添加）
_manual_adjusts_cache: list[dict] = []

# 缓存最后一次排程结果，供甘特图按需生成使用
_last_schedule_result: dict = {}


def _load_manual_adjust_dicts() -> list[dict]:
    """从文件加载手动调整规则（dict 列表），文件不存在/读失败返回空列表"""
    from data_loader import load_manual_adjusts
    filepath = os.path.join(DATA_DIR, "manual_adjust.csv")
    if not os.path.exists(filepath):
        return []
    try:
        adjusts = load_manual_adjusts(filepath)
    except Exception:
        return []
    return [
        {
            "lot_name": ma.lot_name,
            "step_name": ma.step_name or "",
            "delay_to": ma.delay_to.strftime("%Y/%m/%d %H:%M") if ma.delay_to else "",
            "mode": ma.mode,
        }
        for ma in adjusts
    ]


def _merge_manual_adjusts() -> list[dict]:
    """合并 文件规则 ∪ 内存缓存（缓存优先），保证界面/保存/排程三方数据一致。

    修复历史 bug：文件与内存双数据源导致"编辑已存在规则被静默忽略、
    保存时文件规则被覆盖丢失"。
    """
    merged = _load_manual_adjust_dicts()
    keyed = {(m["lot_name"], m["step_name"] or None): m for m in merged}
    for ma in _manual_adjusts_cache:
        keyed[(ma["lot_name"], ma["step_name"] or None)] = ma
    return list(keyed.values())


@app.route('/api/manual-adjusts', methods=['GET'])
def get_manual_adjusts():
    """获取所有手动调整（文件 ∪ 内存缓存合并结果，缓存优先）"""
    try:
        return jsonify(_merge_manual_adjusts())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/manual-adjust', methods=['POST'])
def add_manual_adjust():
    """添加或更新手动调整（内存操作）"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid data"}), 400

    lot_name = data.get("lot_name", "").strip()
    step_name = data.get("step_name", "").strip() or None
    delay_to_str = data.get("delay_to", "").strip()
    mode = data.get("mode", "delay")
    if mode not in ("delay", "pin"):
        mode = "delay"

    if not lot_name or not delay_to_str:
        return jsonify({"error": "lot_name and delay_to are required"}), 400

    try:
        delay_to = datetime.strptime(delay_to_str, "%Y/%m/%d %H:%M")
    except ValueError:
        return jsonify({"error": "delay_to must be yyyy/mm/dd HH:MM format"}), 400

    global _manual_adjusts_cache
    # 查找是否已存在相同的 lot_name + step_name
    found = False
    for ma in _manual_adjusts_cache:
        if ma["lot_name"] == lot_name and ma.get("step_name", "") == (step_name or ""):
            ma["delay_to"] = delay_to_str
            ma["mode"] = mode
            found = True
            break
    if not found:
        _manual_adjusts_cache.append({
            "lot_name": lot_name,
            "step_name": step_name or "",
            "delay_to": delay_to_str,
            "mode": mode,
        })

    return jsonify({"success": True, "manual_adjusts": _merge_manual_adjusts()})


@app.route('/api/manual-adjust', methods=['DELETE'])
def delete_manual_adjust():
    """删除手动调整（内存操作）。

    精确匹配 lot_name + step_name + delay_to + mode，避免 step 为空时误删整批规则。
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid data"}), 400

    lot_name = data.get("lot_name", "").strip()
    step_name = data.get("step_name", "").strip() or None
    delay_to = data.get("delay_to", "").strip() or None
    mode = data.get("mode", "").strip() or None

    if not lot_name:
        return jsonify({"error": "lot_name is required"}), 400

    # 规范化 delay_to 为 yyyy/mm/dd HH:MM，避免内存添加与文件加载格式不一致导致匹配失败
    norm_delay = None
    if delay_to:
        try:
            norm_delay = datetime.strptime(delay_to, "%Y/%m/%d %H:%M").strftime("%Y/%m/%d %H:%M")
        except ValueError:
            try:
                norm_delay = datetime.strptime(delay_to, "%Y-%m-%d %H:%M").strftime("%Y/%m/%d %H:%M")
            except ValueError:
                norm_delay = None

    global _manual_adjusts_cache
    before = len(_manual_adjusts_cache)
    _manual_adjusts_cache = [
        ma for ma in _manual_adjusts_cache
        if not (
            ma["lot_name"] == lot_name
            and ma.get("step_name", "") == (step_name or "")
            # 仅当调用方给出了 delay_to/mode 时才参与匹配（向前兼容）
            and (norm_delay is None or ma.get("delay_to", "") == norm_delay)
            and (mode is None or ma.get("mode", "") == mode)
        )
    ]
    removed = before - len(_manual_adjusts_cache)

    return jsonify({
        "success": True,
        "removed": removed,
        "manual_adjusts": _merge_manual_adjusts(),
    })


@app.route('/api/manual-adjust/save', methods=['POST'])
def save_manual_adjusts():
    """将（文件 ∪ 内存）合并后的手动调整保存到文件，随后清空内存缓存"""
    global _manual_adjusts_cache
    try:
        filepath = os.path.join(DATA_DIR, "manual_adjust.csv")
        merged = _merge_manual_adjusts()
        if merged:
            df = pd.DataFrame(merged)
            if "mode" in df.columns:
                df = df[["lot_name", "step_name", "delay_to", "mode"]]
            else:
                df = df[["lot_name", "step_name", "delay_to"]]
            df.to_csv(filepath, index=False, sep="\t")
        else:
            # 空时删除文件（无规则）
            if os.path.exists(filepath):
                os.remove(filepath)
        _manual_adjusts_cache = []
        return jsonify({"success": True, "count": len(merged), "manual_adjusts": merged})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/manual-adjust-reload', methods=['POST'])
def reload_manual_adjusts():
    """从文件重新加载手动调整到内存"""
    global _manual_adjusts_cache
    try:
        from data_loader import load_manual_adjusts
        filepath = os.path.join(DATA_DIR, "manual_adjust.csv")
        if os.path.exists(filepath):
            adjusts = load_manual_adjusts(filepath)
            _manual_adjusts_cache = [
                {
                    "lot_name": ma.lot_name,
                    "step_name": ma.step_name or "",
                    "delay_to": ma.delay_to.strftime("%Y/%m/%d %H:%M") if ma.delay_to else "",
                    "mode": ma.mode,
                }
                for ma in adjusts
            ]
        else:
            _manual_adjusts_cache = []
        return jsonify({"success": True, "manual_adjusts": _manual_adjusts_cache})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _safe_str_pd(row, col):
    """从 pandas 行安全取值（兼容 NaN / None）"""
    try:
        v = row.get(col)
        if v is None:
            return ""
        return "" if str(v).lower() in ("nan", "none") else str(v)
    except Exception:
        return ""


@app.route('/api/due-reverse', methods=['POST'])
def api_due_reverse():
    """交期反推：给定 Lot 与期望完工时间，反推流程内各步最晚开始/结束时间。

    基于"无设备竞争"的粗略估算（CT + 步间等待），供用户判断交期是否可行，
    并可一键把目标步骤设为"精确锁定"（pin）使排程尽量贴近该时间。
    """
    try:
        data = request.get_json(silent=True) or {}
        lot_name = (data.get("lot_name") or "").strip()
        due_str = (data.get("due_time") or "").strip()
        target_step = (data.get("target_step") or "").strip() or None
        if not lot_name or not due_str:
            return jsonify({"error": "lot_name 和 due_time 必填"}), 400
        try:
            due = datetime.strptime(due_str, "%Y/%m/%d %H:%M")
        except ValueError:
            return jsonify({"error": "due_time 格式应为 yyyy/mm/dd HH:MM"}), 400

        from data_loader import (
            load_lot_list, load_flow, load_step_ct, build_ct_lookup,
            get_step_index_in_flow, get_product_flow_map, get_step_ct,
            load_special_lot_step, load_priority_wait,
        )
        lots = load_lot_list(f"{DATA_DIR}/lot_list.csv",
                             constraints_filepath=f"{DATA_DIR}/lot_constraints.csv")
        flows = load_flow(f"{DATA_DIR}/flow.csv")
        ct_lookup = build_ct_lookup(load_step_ct(f"{DATA_DIR}/step_ct.csv"))
        special_lot_step_lookup = load_special_lot_step(f"{DATA_DIR}/special_lot_step.csv") if os.path.exists(f"{DATA_DIR}/special_lot_step.csv") else {}
        priority_wait_map = load_priority_wait(f"{DATA_DIR}/priority_wait.csv") if os.path.exists(f"{DATA_DIR}/priority_wait.csv") else {}

        lot = next((l for l in lots if l.lot_name == lot_name), None)
        if lot is None:
            return jsonify({"error": f"Lot [{lot_name}] 不存在"}), 404
        flow_map = get_product_flow_map(flows)
        product_flow = flow_map.get(lot.product_name)
        if not product_flow:
            return jsonify({"error": f"产品 [{lot.product_name}] 无流程"}), 400
        try:
            cur_idx = get_step_index_in_flow(product_flow, lot.current_step_name)
        except ValueError:
            return jsonify({"error": "当前步骤不在其产品流程中"}), 400
        if target_step:
            try:
                end_idx = get_step_index_in_flow(product_flow, target_step)
            except ValueError:
                return jsonify({"error": f"目标步骤 [{target_step}] 不在流程中"}), 400
        else:
            end_idx = len(product_flow) - 1
        if end_idx < cur_idx:
            return jsonify({"error": "目标步骤早于当前步骤"}), 400

        steps = product_flow[cur_idx:end_idx + 1]

        def _step_ct(s):
            ct = get_step_ct(ct_lookup, s.product_name, s.step_number, lot.qty)
            sls_key = (lot.lot_name, s.step_name)
            sls = special_lot_step_lookup.get(sls_key)
            if sls and sls.special_ct is not None:
                ct = sls.special_ct
            return ct

        cts = [_step_ct(s) for s in steps]
        waits = []
        total_min = sum(cts)
        for i in range(len(steps) - 1):
            w = 0
            if priority_wait_map:
                from scheduler import get_step_wait_time
                w = get_step_wait_time(lot.priority[0], lot.priority[1], priority_wait_map)
            waits.append(w)
            total_min += w

        # ── 该 Lot 的就绪时刻（与排程一致）：有 start_time 用之，否则从现在开始；
        #    并考虑 hold 时段。running 状态时第一步剩余 CT 扣减 running_time。──
        now = datetime.now()
        ready_time = lot.start_time if lot.start_time is not None else now
        if lot.hold_periods:
            for hs, he in lot.hold_periods:
                if he is None:
                    continue
                if ready_time < he:
                    ready_time = max(ready_time, he)
        # running：第一步剩余时间 = CT - running_time（>=CT 按 0 处理，不扣负）
        first_ct_adj = None
        if lot.lot_state == "running" and lot.running_time:
            first_ct_adj = max(0.0, cts[0] - lot.running_time) if cts else 0.0

        # ── 正推表（粗略最早计划，无设备竞争）：从就绪时刻顺序排 ──
        f_start = ready_time
        forward = []
        for i, s in enumerate(steps):
            ct_i = first_ct_adj if (i == 0 and first_ct_adj is not None) else cts[i]
            f_end = f_start + td(minutes=ct_i)
            forward.append({
                "step_name": s.step_name,
                "step_number": s.step_number,
                "ct": round(ct_i, 1),
                "earliest_start": f_start.strftime("%Y/%m/%d %H:%M"),
                "earliest_end": f_end.strftime("%Y/%m/%d %H:%M"),
            })
            if i < len(steps) - 1:
                f_start = f_end + td(minutes=waits[i])
        earliest_completion = forward[-1]["earliest_end"] if forward else due
        # 正推实际总时长（含第一步 running 扣减，保证与正推表一致）
        if forward:
            _ec_dt = datetime.strptime(earliest_completion, "%Y/%m/%d %H:%M")
            total_min_actual = max(0.0, (_ec_dt - ready_time).total_seconds() / 60.0)
        else:
            total_min_actual = total_min

        # ── 反推表：从期望完工时间往前推各步最晚开始/结束 ──
        latest_end = due
        plan = []
        for i in range(len(steps) - 1, -1, -1):
            latest_start = latest_end - td(minutes=cts[i])
            plan.append({
                "step_name": steps[i].step_name,
                "step_number": steps[i].step_number,
                "ct": cts[i],
                "latest_start": latest_start.strftime("%Y/%m/%d %H:%M"),
                "latest_end": latest_end.strftime("%Y/%m/%d %H:%M"),
            })
            if i > 0:
                latest_end = latest_start - td(minutes=waits[i - 1])
        plan.reverse()

        # 交期可行 = 从就绪时刻无竞争排完的最早完工 <= 期望完工时间
        feasible = datetime.strptime(earliest_completion, "%Y/%m/%d %H:%M") <= due

        return jsonify({
            "success": True,
            "lot_name": lot_name,
            "due_time": due_str,
            "target_step": steps[-1].step_name,
            "ready_time": ready_time.strftime("%Y/%m/%d %H:%M"),
            "total_minutes": round(total_min_actual, 1),
            "earliest_completion": earliest_completion,
            "feasible": feasible,
            "steps": plan,
            "forward_steps": forward,
            "note": "正推为从该Lot就绪时刻无设备竞争的粗略最早计划；反推为满足交期的最晚开始时间。均未考虑设备竞争、班次与Q-time链内压缩。",
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/generate-gantt', methods=['POST'])
def generate_gantt():
    """按需生成甘特图（支持筛选）"""
    global _last_schedule_result
    try:
        from visualization import plot_lot_gantt, plot_eqp_gantt
        import matplotlib
        matplotlib.use('Agg')

        data = request.get_json() or {}
        gantt_type = data.get('type', 'eqp')  # 'lot' or 'eqp'
        time_start_str = data.get('time_start', '')
        time_end_str = data.get('time_end', '')
        eqp_ids = data.get('eqp_ids', [])
        lot_names = data.get('lot_names', [])

        # 解析时间
        time_start = None
        time_end = None
        if time_start_str:
            try:
                time_start = datetime.strptime(time_start_str.replace('T', ' '), "%Y-%m-%d %H:%M")
            except ValueError:
                try:
                    time_start = datetime.strptime(time_start_str, "%Y/%m/%d %H:%M")
                except ValueError:
                    try:
                        time_start = datetime.strptime(time_start_str, "%Y-%m-%d")
                    except ValueError:
                        try:
                            time_start = datetime.strptime(time_start_str, "%Y/%m/%d")
                        except ValueError:
                            pass
        if time_end_str:
            try:
                time_end = datetime.strptime(time_end_str.replace('T', ' '), "%Y-%m-%d %H:%M")
            except ValueError:
                try:
                    time_end = datetime.strptime(time_end_str, "%Y/%m/%d %H:%M")
                except ValueError:
                    try:
                        time_end = datetime.strptime(time_end_str, "%Y-%m-%d")
                        time_end = time_end.replace(hour=23, minute=59, second=59)
                    except ValueError:
                        try:
                            time_end = datetime.strptime(time_end_str, "%Y/%m/%d")
                            time_end = time_end.replace(hour=23, minute=59, second=59)
                        except ValueError:
                            pass

        if gantt_type == 'lot':
            lot_entries = _last_schedule_result.get('lot_entries', [])
            if not lot_entries:
                return jsonify({"error": "请先生成排程报表"}), 400
            output_path = os.path.join(STATIC_DIR, 'lot_gantt.png')
            plot_lot_gantt(lot_entries, output_path,
                          time_start=time_start, time_end=time_end,
                          lot_filter=lot_names if lot_names else None,
                          shift_times=_last_schedule_result.get('shift_times'))
            return jsonify({"success": True, "image": "/static/lot_gantt.png?t=" + str(int(datetime.now().timestamp()))})
        else:
            eqp_entries = _last_schedule_result.get('eqp_entries', [])
            if not eqp_entries:
                return jsonify({"error": "请先生成排程报表"}), 400
            output_path = os.path.join(STATIC_DIR, 'eqp_gantt.png')
            plot_eqp_gantt(eqp_entries, output_path,
                          time_start=time_start, time_end=time_end,
                          eqp_filter=eqp_ids if eqp_ids else None,
                          shift_times=_last_schedule_result.get('shift_times'))
            return jsonify({"success": True, "image": "/static/eqp_gantt.png?t=" + str(int(datetime.now().timestamp()))})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/generate-gantt-html', methods=['POST'])
def generate_gantt_html():
    """生成交互式 HTML 甘特图（滚轮缩放/拖拽/hover）"""
    global _last_schedule_result
    try:
        from visualization import plot_gantt_html
        data = request.get_json() or {}
        gantt_type = data.get('type', 'eqp')
        if gantt_type == 'lot':
            entries = _last_schedule_result.get('lot_entries', [])
            filter_ids = data.get('lot_names', [])
        else:
            entries = _last_schedule_result.get('eqp_entries', [])
            filter_ids = data.get('eqp_ids', [])
        if not entries:
            return jsonify({"error": "请先生成排程报表"}), 400
        html = plot_gantt_html(
            gantt_type, entries,
            filter_ids=filter_ids or None,
            shift_times=_last_schedule_result.get('shift_times'))
        return jsonify({"success": True, "html": html})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Flow 原始数据导入 ──

FLOW_IMPORT_DIR = os.path.join(DATA_DIR, "flow_import")
os.makedirs(FLOW_IMPORT_DIR, exist_ok=True)


@app.route('/api/flow-import/files', methods=['GET'])
def flow_import_files():
    """列出已上传的原始 flow 文件"""
    files = []
    if os.path.exists(FLOW_IMPORT_DIR):
        for f in sorted(os.listdir(FLOW_IMPORT_DIR)):
            if f.lower().endswith(('.csv', '.xlsx', '.xls')):
                filepath = os.path.join(FLOW_IMPORT_DIR, f)
                files.append({
                    "name": f,
                    "product_name": os.path.splitext(f)[0],
                    "size": os.path.getsize(filepath),
                })
    return jsonify({"files": files})


@app.route('/api/flow-import/upload', methods=['POST'])
def flow_import_upload():
    """上传原始 flow 文件到导入目录"""
    uploaded_files = request.files.getlist('files')
    if not uploaded_files:
        return jsonify({"error": "请选择文件"}), 400

    uploaded = []
    skipped = []
    for file in uploaded_files:
        if not file.filename:
            continue
        # 净化文件名，防止路径穿越
        filename = os.path.basename(file.filename.replace("\\", "/"))
        if not filename or not filename.lower().endswith(('.csv', '.xlsx', '.xls')):
            skipped.append(f"{filename} (不支持的文件类型)")
            continue
        filepath = os.path.join(FLOW_IMPORT_DIR, filename)
        file.save(filepath)
        uploaded.append(filename)

    return jsonify({
        "success": True,
        "uploaded": uploaded,
        "skipped": skipped,
        "count": len(uploaded),
    })


@app.route('/api/flow-import/convert', methods=['POST'])
def flow_import_convert():
    """转换所有已上传的原始 flow 文件到系统数据"""
    try:
        from flow_importer import import_flow_files

        result = import_flow_files(FLOW_IMPORT_DIR, DATA_DIR)

        return jsonify({
            "success": True,
            "total_files": result.total_files,
            "success_files": result.success_files,
            "error_files": result.error_files,
            "products_updated": result.products_updated,
            "products_added": result.products_added,
            "flow_rows_added": result.flow_rows_added,
            "flow_rows_removed": result.flow_rows_removed,
            "step_ct_rows_added": result.step_ct_rows_added,
            "step_ct_rows_removed": result.step_ct_rows_removed,
            "warnings": result.warnings,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/flow-import/clear', methods=['POST'])
def flow_import_clear():
    """清空导入目录"""
    try:
        if os.path.exists(FLOW_IMPORT_DIR):
            for f in os.listdir(FLOW_IMPORT_DIR):
                if f.lower().endswith(('.csv', '.xlsx', '.xls')):
                    os.remove(os.path.join(FLOW_IMPORT_DIR, f))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/flow-import/delete', methods=['POST'])
def flow_import_delete():
    """删除导入目录中的单个文件"""
    data = request.get_json() or {}
    filename = os.path.basename((data.get("filename", "") or "").replace("\\", "/"))
    if not filename:
        return jsonify({"error": "filename is required"}), 400
    filepath = os.path.join(FLOW_IMPORT_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404
    try:
        os.remove(filepath)
        return jsonify({"success": True, "filename": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 算法参数配置 API ──

CONFIG_FILE = os.path.join(DATA_DIR, "optimizer_config.json")


def _load_config_dict() -> dict:
    """从文件加载配置字典"""
    from optimizer_config import OptimizerConfig
    cfg = OptimizerConfig().to_dict()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return saved
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def _save_config_dict(d: dict) -> None:
    """保存配置字典到文件"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


@app.route('/api/config', methods=['GET'])
def api_get_config():
    """获取当前配置、默认值、参数元信息"""
    from optimizer_config import OptimizerConfig
    current = _load_config_dict()
    defaults = OptimizerConfig().to_dict()
    return jsonify({
        "config": current,
        "defaults": defaults,
        "meta": OptimizerConfig.parameter_meta(),
    })


@app.route('/api/config', methods=['POST'])
def api_save_config():
    """保存配置（仅接受 optimizer_config.py 中定义的白名单参数）"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid data"}), 400
    try:
        from optimizer_config import OptimizerConfig
        whitelist = set(OptimizerConfig().to_dict().keys())
        filtered = {k: v for k, v in data.items() if k in whitelist}
        _save_config_dict(filtered)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    """关闭本地 Web 服务并退出进程。

    供网页“退出程序”按钮调用：关闭浏览器标签页不会结束后台进程，
    必须通过此接口（或任务管理器结束 schedule_app）才能真正退出。
    """
    try:
        def _exit_soon():
            _time.sleep(1)  # 留 1 秒让“退出中…”响应先返回给浏览器
            os._exit(0)
        threading.Thread(target=_exit_soon, daemon=True).start()
        return jsonify({"success": True, "message": "正在退出..."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 浏览器关闭自动退出（避免后台残留进程）──
# 说明：打包后程序以无窗口方式后台运行，用户关闭浏览器标签页/整个浏览器就是
# 常见的“关闭程序”动作。这里用 Web Worker 心跳检测浏览器是否仍存活：
#   · 只要有页面打开，前端就每隔几秒 POST /api/heartbeat；
#   · 关闭所有标签页/整个浏览器后心跳停止，后台看门狗超时后自动退出整个程序。
#   （用 Web Worker 发心跳可避免后台标签页的定时器被浏览器节流导致误判。）
_last_heartbeat_time = None
_heartbeat_lock = threading.Lock()
HEARTBEAT_TIMEOUT = 15  # 秒：超过该时长没收到心跳即视为浏览器已关闭


@app.route('/api/heartbeat', methods=['POST'])
def api_heartbeat():
    """前端心跳：刷新最后存活时间。"""
    global _last_heartbeat_time
    with _heartbeat_lock:
        _last_heartbeat_time = _time.time()
    return jsonify({"success": True})


def _heartbeat_watchdog():
    """后台看门狗：长时间收不到心跳则自动退出，避免后台残留进程。"""
    while True:
        _time.sleep(2)
        with _heartbeat_lock:
            last = _last_heartbeat_time
        if last is not None and _time.time() - last > HEARTBEAT_TIMEOUT:
            print("[schedule_app] 浏览器已关闭，程序自动退出。")
            os._exit(0)




if __name__ == '__main__':
    # PyInstaller 打包后，多进程（ProcessPoolExecutor，用于"生成多方案"）会以新进程
    # 重新执行本主块；不加 freeze_support() 时每个 worker 都会再开一次浏览器、
    # 再起一次 Flask（端口冲突），表现为"生成多方案弹出多个网页"。freeze_support()
    # 让 worker 进程在进入主块后立即退出，只保留真正的父进程逻辑。
    try:
        from multiprocessing import freeze_support
        freeze_support()
    except Exception:
        pass

    # 打包运行时：matplotlib 缓存落在可写目录（避免只读目录报错）
    try:
        os.environ.setdefault("MPLCONFIGDIR", os.path.join(OUTPUT_DIR, '.mplcache'))
        os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)
    except Exception:
        pass

    port = int(os.environ.get("PORT", "5000"))

    # 延迟 1.2s 自动打开浏览器，等 HTTP 服务先就绪
    import threading

    def _open_browser():
        import webbrowser
        try:
            url = f"http://127.0.0.1:{port}/"
            webbrowser.open(url)
        except Exception:
            pass

    threading.Timer(1.2, _open_browser).start()

    # 启动心跳看门狗：浏览器全部关闭后自动退出，避免后台残留进程
    threading.Thread(target=_heartbeat_watchdog, daemon=True).start()

    print(f"\n  排程调度系统已启动，请用浏览器访问: http://127.0.0.1:{port}/\n")
    app.run(host='127.0.0.1', port=port, debug=False)
    # 服务停止（如收到 /api/shutdown）后强制退出，避免后台残留进程
    os._exit(0)