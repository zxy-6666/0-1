"""运行带约束的排程，输出完整结果 + 甘特图 + Excel"""
import argparse
import os
import sys
from datetime import datetime, timedelta

# 脚本位于 tools/ 下，项目根在其上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_loader import (
    load_lot_list, load_flow, load_step_ct, load_qtime,
    build_ct_lookup, auto_repair_step_ct,
    load_ftf_qty_change, load_special_lot_step, load_priority_wait,
    load_eqp_constraints, load_step_time_windows, load_shift_config, load_shift_change_times,
    load_special_eqp, load_lot_constraints,
)
from scheduler import schedule
from optimizer import schedule_optimized
from outputs import to_lot_dataframe
from visualization import plot_lot_gantt, plot_eqp_gantt, export_to_excel

parser = argparse.ArgumentParser(description="运行带约束的排程（启发式 + 种子随机局部搜索）")
parser.add_argument("--verbose", action="store_true", help="输出调试日志")
parser.add_argument("--iterations", type=int, default=40, help="迭代轮数（默认 40）")
parser.add_argument("--seed", type=int, default=0, help="随机种子（默认 0）")
parser.add_argument("--no-weighted-priority", action="store_true", help="不按优先级加权（等权重）")
args = parser.parse_args()

DATA_DIR = "/workspace/data"
OUTPUT_DIR = "/workspace/output"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# 加载必要数据
lots = load_lot_list(f"{DATA_DIR}/lot_list.csv",
                    constraints_filepath=f"{DATA_DIR}/lot_constraints.csv")
flows = load_flow(f"{DATA_DIR}/flow.csv")
step_cts = load_step_ct(f"{DATA_DIR}/step_ct.csv")
step_ct_path = f"{DATA_DIR}/step_ct.csv"
step_cts = auto_repair_step_ct(flows, step_cts, step_ct_filepath=step_ct_path)
qtimes = load_qtime(f"{DATA_DIR}/qtime.csv")
ct_lookup = build_ct_lookup(step_cts)

# 加载可选配置文件
ftf_qty_change = load_ftf_qty_change(f"{DATA_DIR}/ftf_qty_change.csv") if os.path.exists(f"{DATA_DIR}/ftf_qty_change.csv") else {}
special_lot_step_lookup = load_special_lot_step(f"{DATA_DIR}/special_lot_step.csv") if os.path.exists(f"{DATA_DIR}/special_lot_step.csv") else {}
priority_wait_map = load_priority_wait(f"{DATA_DIR}/priority_wait.csv") if os.path.exists(f"{DATA_DIR}/priority_wait.csv") else {}
eqp_constraints = load_eqp_constraints(f"{DATA_DIR}/eqp_constraint.csv") if os.path.exists(f"{DATA_DIR}/eqp_constraint.csv") else []
step_time_window_constraints = load_step_time_windows(f"{DATA_DIR}/step_time_window.csv") if os.path.exists(f"{DATA_DIR}/step_time_window.csv") else []
shift_configs = load_shift_config(f"{DATA_DIR}/shift_config.csv") if os.path.exists(f"{DATA_DIR}/shift_config.csv") else []
shift_change_times = load_shift_change_times(f"{DATA_DIR}/shift_change_time.csv") if os.path.exists(f"{DATA_DIR}/shift_change_time.csv") else []
special_eqp_map = load_special_eqp(f"{DATA_DIR}/special_eqp.csv") if os.path.exists(f"{DATA_DIR}/special_eqp.csv") else {}
lot_constraints = load_lot_constraints(f"{DATA_DIR}/lot_constraints.csv") if os.path.exists(f"{DATA_DIR}/lot_constraints.csv") else []

# 从 shift_config 解析班次时间
shift_times = []
for sc in shift_configs:
    try:
        h, m = map(int, sc.start_time_str.split(":"))
        shift_times.append((h, m))
    except (ValueError, AttributeError):
        pass
shift_times.sort()

print("=" * 100)
print("运行排程（启发式构造解 + 种子随机局部搜索优化）...")
print(f"  班次: {shift_times}")
print(f"  步骤时间窗口约束: {len(step_time_window_constraints)} 条")
print(f"  设备约束: {len(eqp_constraints)} 条")
if special_eqp_map:
    print(f"  特殊设备批处理: {len(special_eqp_map)} 台")
print(f"  迭代轮数: {args.iterations}")
print(f"  随机种子: {args.seed}")
print("=" * 100)

lot_entries, eqp_entries, qtime_alerts, meta = schedule_optimized(
    lots=lots, flows=flows, ct_lookup=ct_lookup, qtimes=qtimes,
    shift_times=shift_times,
    ftf_qty_change=ftf_qty_change,
    special_lot_step_lookup=special_lot_step_lookup,
    priority_wait_map=priority_wait_map,
    eqp_constraints=eqp_constraints,
    step_time_window_constraints=step_time_window_constraints,
    shift_change_times=shift_change_times,
    special_eqp_map=special_eqp_map,
    lot_constraints=lot_constraints,
    resolve_max_iterations=10,
    max_iterations=args.iterations,
    seed=args.seed,
    weight_by_priority=not args.no_weighted_priority,
    verbose=args.verbose,
)

print(f"\n排程结果: {len(lot_entries)} 条步骤记录, {len(eqp_entries)} 条设备记录, {len(qtime_alerts)} 条Q-time告警")
print(f"优化结果: 有效解 {meta['valid_iterations']}/{meta['total_iterations']} 轮, 最佳得分 {meta['best_score']:.2f}")
if meta.get("min_qtime_margin") is not None:
    print(f"Q-time 最小余量: {meta['min_qtime_margin']:.1f} 分钟")
print(f"最佳批次顺序: {meta.get('lot_order', [])}")
if meta.get("warning"):
    print(f"⚠️ {meta['warning']}")

# 按 Lot 分组输出
lot_df = to_lot_dataframe(lot_entries)

# 批次输出顺序
lot_order = [lot.lot_name for lot in lots]

for lot_name in lot_order:
    sub = lot_df[lot_df["lot_name"] == lot_name].copy()
    if sub.empty:
        continue
    sub = sub.sort_values("start_time")

    first_start = sub.iloc[0]["start_time"]
    last_end = sub.iloc[-1]["end_time"]

    print(f"\n{'='*100}")
    print(f"  {lot_name}  ({len(sub)} 步)  首步: {first_start}  →  末步: {last_end}")
    print(f"{'='*100}")
    print(f"  {'序号':<5} {'步骤名称':<40} {'设备':<15} {'开始时间':<17} {'结束时间':<17} {'CT(min)':<10} {'Q-time'}")
    print(f"  {'-'*5} {'-'*40} {'-'*15} {'-'*17} {'-'*17} {'-'*10} {'-'*8}")

    for i, (_, row) in enumerate(sub.iterrows(), 1):
        print(f"  {i:<5} {row['step_name']:<40} {row['eqp_id']:<15} {row['start_time']:<17} {row['end_time']:<17} {row['ct(min)']:<10} {row['qtime_risk']}")

# Q-time 告警
if qtime_alerts:
    print(f"\n{'='*100}")
    print("  Q-time 告警:")
    print(f"{'='*100}")
    for a in qtime_alerts:
        flag = "⚠️" if a.status == "超时" else "✅"
        print(f"  {flag} {a.lot_name}: {a.qtime_rule} | {a.status}" + (f" | 超时{a.over_minutes}min" if a.over_minutes > 0 else ""))

print(f"\n{'='*100}")
print("  排程完成")
print(f"{'='*100}")

# 生成甘特图和 Excel
print(f"\n{'='*100}")
print("  生成甘特图和Excel...")
print(f"{'='*100}")

_outputs = [
    ("lot_gantt.png", lambda: plot_lot_gantt(lot_entries, f"{OUTPUT_DIR}/lot_gantt.png", shift_times=shift_times)),
    ("eqp_gantt.png", lambda: plot_eqp_gantt(eqp_entries, f"{OUTPUT_DIR}/eqp_gantt.png", shift_times=shift_times)),
    ("schedule_result.xlsx", lambda: export_to_excel(
        lot_entries, eqp_entries, qtime_alerts,
        f"{OUTPUT_DIR}/schedule_result.xlsx",
        lots=lots, shift_configs=shift_configs, lot_order=lot_order)),
]
for _name, _fn in _outputs:
    try:
        _fn()
    except Exception as e:
        print(f"  ⚠ 输出 {_name} 失败: {e}（排程结果本身已生成，可检查依赖/文件占用）")

print(f"\n所有输出文件已保存到 {OUTPUT_DIR}/")
print(f"  - lot_gantt.png  (Lot维度甘特图)")
print(f"  - eqp_gantt.png  (设备维度甘特图)")
print(f"  - schedule_result.xlsx  (Excel完整排程表)")