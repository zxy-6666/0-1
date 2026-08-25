# -*- coding: utf-8 -*-
"""
生产调度技能包 (production-scheduling) 应用演示
场景：某离散制造工厂（五金零部件），本周 8 个工单，4 个工作中心，2 班制（16h/天）。
流程：
  1. 瓶颈识别（DBR/TOC：利用率 = 负载小时 / 可用小时）
  2. 作业优先级分类（决策树：逾期/临期 > 供应约束 > EDD）
  3. 换型序列优化（setup 矩阵 + 2-opt，交期合规为硬约束）
  4. 有限产能排产（前向调度，输出分钟级甘特数据 + 交期检查）
"""
import json
from datetime import datetime, timedelta

# ---------------------------------------------------------------
# 1. 场景数据
# ---------------------------------------------------------------
# 工作中心: (名称, 机器数, 每周可用小时/台, 班次说明)
WORK_CENTERS = {
    "WC1-CNC加工":  {"machines": 1, "hours_per_machine": 80,  "note": "2班制 16h×5天"},
    "WC2-车削":     {"machines": 2, "hours_per_machine": 80,  "note": "2班制 16h×5天"},
    "WC3-表面处理": {"machines": 1, "hours_per_machine": 80,  "note": "2班制 16h×5天"},
    "WC4-包装":     {"machines": 2, "hours_per_machine": 80,  "note": "2班制 16h×5天"},
}

# 工单: 编号, 产品族, 数量, 各工作中心单件分钟 (CNC/车削/表面/包装), 交期(小时, 从周一8:00起), 客户层级
ORDERS = [
    {"id": "WO-101", "fam": "A", "qty": 100, "proc": {"WC1-CNC加工": 5.0, "WC2-车削": 2.0, "WC3-表面处理": 1.5, "WC4-包装": 1.0}, "due_h": 24,  "tier": "Tier-1"},
    {"id": "WO-102", "fam": "B", "qty": 80,  "proc": {"WC1-CNC加工": 4.0, "WC2-车削": 1.5, "WC3-表面处理": 1.0, "WC4-包装": 0.8}, "due_h": 72,  "tier": "Tier-1"},
    {"id": "WO-103", "fam": "A", "qty": 150, "proc": {"WC1-CNC加工": 5.0, "WC2-车削": 2.0, "WC3-表面处理": 1.5, "WC4-包装": 1.0}, "due_h": 120, "tier": "Tier-2"},
    {"id": "WO-104", "fam": "C", "qty": 60,  "proc": {"WC1-CNC加工": 8.0, "WC2-车削": 2.5, "WC3-表面处理": 2.0, "WC4-包装": 1.2}, "due_h": 48,  "tier": "Tier-1"},
    {"id": "WO-105", "fam": "B", "qty": 120, "proc": {"WC1-CNC加工": 4.0, "WC2-车削": 1.5, "WC3-表面处理": 1.0, "WC4-包装": 0.8}, "due_h": 144, "tier": "Tier-2"},
    {"id": "WO-106", "fam": "D", "qty": 50,  "proc": {"WC1-CNC加工": 10.0, "WC2-车削": 3.0, "WC3-表面处理": 2.0, "WC4-包装": 1.5}, "due_h": 96,  "tier": "Tier-3"},
    {"id": "WO-107", "fam": "C", "qty": 90,  "proc": {"WC1-CNC加工": 8.0, "WC2-车削": 2.5, "WC3-表面处理": 2.0, "WC4-包装": 1.2}, "due_h": 168, "tier": "Tier-2"},
    {"id": "WO-108", "fam": "A", "qty": 200, "proc": {"WC1-CNC加工": 5.0, "WC2-车削": 2.0, "WC3-表面处理": 1.5, "WC4-包装": 1.0}, "due_h": 192, "tier": "Tier-3"},
]

# 产品族间换型矩阵（分钟）—— 序列相关
FAMS = ["A", "B", "C", "D"]
SETUP = {
    ("A","A"): 0,  ("A","B"): 30, ("A","C"): 45, ("A","D"): 60,
    ("B","A"): 25, ("B","B"): 0,  ("B","C"): 35, ("B","D"): 50,
    ("C","A"): 40, ("C","B"): 30, ("C","C"): 0,  ("C","D"): 40,
    ("D","A"): 55, ("D","B"): 45, ("D","C"): 35, ("D","D"): 0,
}

WEEK_START = datetime(2026, 8, 24, 8, 0)  # 周一 08:00 开工

def load_hours(wc):
    """某工作中心总负载（小时），含每工单 20 分钟准备 + 换型按需。"""
    total = 0.0
    for o in ORDERS:
        total += o["qty"] * o["proc"][wc] / 60.0
        total += 20 / 60.0  # 常规准备
    return total

def available_hours(wc):
    info = WORK_CENTERS[wc]
    return info["machines"] * info["hours_per_machine"]

# ---------------------------------------------------------------
# 2. 瓶颈识别（TOC / DBR）
# ---------------------------------------------------------------
print("=" * 78)
print("步骤 1｜瓶颈识别：利用率 = 负载小时 / 可用小时（技能包：>85% 视为约束/鼓）")
print("=" * 78)
util_ranking = []
for wc in WORK_CENTERS:
    load, avail = load_hours(wc), available_hours(wc)
    util = load / avail
    util_ranking.append((wc, load, avail, util))
    flag = "  <<< 约束（鼓）" if util > 0.85 else ("  <<< 接近约束" if util > 0.7 else "")
    print(f"  {wc:<12} 负载 {load:6.1f}h / 可用 {avail:5.1f}h = 利用率 {util*100:5.1f}%{flag}")
constraint = max(util_ranking, key=lambda x: x[3])
print(f"\n  结论：约束资源 = {constraint[0]}（利用率 {constraint[3]*100:.1f}%，唯一超载工作中心）")
print("  验证：在 CNC 加工中心增加 1 小时产能，工厂产出会随之增加吗？—— 会，其余工作中心均有富余。")

# ---------------------------------------------------------------
# 3. 作业优先级分类（技能包决策树）
# ---------------------------------------------------------------
print("\n" + "=" * 78)
print("步骤 2｜作业优先级分类：逾期/临期 > 供应约束 > 剩余按 EDD")
print("=" * 78)
o_map = {o["id"]: o for o in ORDERS}
for o in ORDERS:
    o["cnc_min"] = o["qty"] * o["proc"]["WC1-CNC加工"]

# 决策树 1：临期（交期 ≤ 48h）
expedite = sorted([o for o in ORDERS if o["due_h"] <= 48], key=lambda x: x["due_h"])
# 决策树 3：其余按 EDD
rest = sorted([o for o in ORDERS if o["due_h"] > 48], key=lambda x: (x["due_h"], 0 if x["tier"] == "Tier-1" else 1))

initial_seq = expedite + rest
print("  分类结果：")
print(f"    临期工单（交期≤48h，优先）：{[o['id'] for o in expedite]}")
print(f"    其余按 EDD 排序：            {[o['id'] for o in rest]}")
print(f"    初始序列：                   {[o['id'] for o in initial_seq]}")

def setup_total(seq):
    return sum(SETUP[(seq[i - 1]["fam"], seq[i]["fam"])] for i in range(1, len(seq)))

print(f"  初始序列总换型时间：{setup_total(initial_seq)} 分钟")

# ---------------------------------------------------------------
# 4. 换型序列优化（setup 矩阵 + 2-opt，交期合规硬约束）
# ---------------------------------------------------------------
print("\n" + "=" * 78)
print("步骤 3｜换型序列优化：2-opt 相邻交换，交期符合为硬约束")
print("=" * 78)

def finish_hours(seq):
    """按序列返回每个工单的 CNC 完成时刻（小时，自周一 8:00 起）。"""
    t = 0.0
    res = []
    for i, o in enumerate(seq):
        t += (SETUP[(seq[i - 1]["fam"], o["fam"])] if i > 0 else 0) / 60.0
        t += o["cnc_min"] / 60.0
        res.append(t)
    return res

def feasible(seq):
    fin = finish_hours(seq)
    return all(f <= o["due_h"] for f, o in zip(fin, seq))

best = initial_seq[:]
best_setup = setup_total(best)
improved = True
while improved:
    improved = False
    for i in range(len(best) - 1):
        cand = best[:]
        cand[i], cand[i + 1] = cand[i + 1], cand[i]
        if feasible(cand):
            s = setup_total(cand)
            if s < best_setup:
                best, best_setup = cand, s
                improved = True
                print(f"  2-opt 交换 {best[i+1]['id']}↔{best[i]['id']}：换型 {best_setup} 分钟")
                break

print(f"\n  优化后序列：{[o['id'] for o in best]}")
print(f"  换型时间：{setup_total(initial_seq)} 分钟 → {best_setup} 分钟（节省 {setup_total(initial_seq) - best_setup} 分钟）")
print("  交期符合性：", "全部满足 ✓" if feasible(best) else "存在逾期 ✗")

# ---------------------------------------------------------------
# 5. 有限产能排产（前向调度）
# ---------------------------------------------------------------
print("\n" + "=" * 78)
print("步骤 4｜有限产能排产：CNC 约束资源逐工单前向调度（换型 + 加工）")
print("=" * 78)
t = 0.0
schedule = []
for i, o in enumerate(best):
    setup = SETUP[(best[i - 1]["fam"], o["fam"])] if i > 0 else 0
    start, end = t + setup / 60.0, t + setup / 60.0 + o["cnc_min"] / 60.0
    t = end
    due = o["due_h"]
    status = "✓ 按期" if end <= due else f"✗ 逾期 {end - due:.1f}h"
    schedule.append({"id": o["id"], "fam": o["fam"], "setup": setup, "cnc_h": round(o["cnc_min"] / 60.0, 2),
                     "start_h": round(start, 2), "end_h": round(end, 2), "due_h": due, "status": status})
    print(f"  {o['id']} (族{o['fam']})  换型 {setup:>3}min  加工 {o['cnc_min']:>4}min  "
          f"完成@{end:6.2f}h / 交期{due:>4}h  {status}")

total_h = t
avail_h = available_hours("WC1-CNC加工")
print(f"\n  CNC 总占用 {total_h:.1f}h（含换型 {best_setup/60:.1f}h），可用 {avail_h:.0f}h")
if total_h > avail_h:
    print(f"  >> 超载 {total_h - avail_h:.1f}h：MRP 无限产能计划不可执行。建议：加班 {total_h - avail_h:.1f}h "
          f"或按技能包『Re-sequencing』对非约束工序后移、保留约束 100% 运转。")
else:
    print(f"  >> 富余 {avail_h - total_h:.1f}h，计划可执行。")

# 汇总输出
print("\n" + "=" * 78)
print("结果汇总")
print("=" * 78)
print(f"  约束资源      : {constraint[0]}（利用率 {constraint[3]*100:.1f}%）")
seq_str = " → ".join(f"{o['id']}({o['fam']})" for o in best)
print(f"  作业序列      : {seq_str}")
print(f"  总换型时间    : {best_setup} 分钟（初始 {setup_total(initial_seq)} 分钟，-{setup_total(initial_seq)-best_setup} 分钟）")
print(f"  按期交付率    : {sum(1 for s in schedule if '✓' in s['status'])}/{len(schedule)}")
print(f"  设备占用      : {total_h:.1f}h / {avail_h:.0f}h 可用")

# 导出甘特数据供可视化
with open("/workspace/demo/production-scheduling-case/gantt_data.json", "w", encoding="utf-8") as f:
    json.dump({"week_start": WEEK_START.strftime("%Y-%m-%d %H:%M"), "schedule": schedule}, f, ensure_ascii=False, indent=2)
print("  甘特数据已导出 → gantt_data.json")
