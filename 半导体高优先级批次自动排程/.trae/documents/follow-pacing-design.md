# 功能设计：随动/跟跑批次（follow / lead-follow pacing）

> 目标：让一个批次（follower）沿另一个批次（lead/先导）的工艺主时间线**保持固定间距**推进，
> 恒落后 lead 一步（或固定时间）。用一条声明代替手工`lot_constraints`互引用，
> 消除：引用环误报、死锁→自然锚点回退导致的乱序、2N 行手工维护。

---

## 1. 需求还原

lot1 = 先导批次，lot2 = 常规批次。lot1 不能比 lot2 快太多，必须**恒领先 lot2".
（恰好一步或固定时间差）才有意义。本质是"沿同一主时间线保持间距"，而非两个独立批次的互等。

手工建模（现状）等价于对每个 step n 写两条互引用：

```
lot2.step_n   必须在 lot1.step_n    完成之后做
lot1.step_n   必须在 lot2.step_{n-1}完成之后做
```

这在 step 级是一条**阶梯 DAG**（不是真环），但 lot 级引用图被判成环。

---

## 2. 设计目标 / 非目标

- **目标**
  - 一条声明表达"整段流水间距"。
  - 消除引用环误报与死锁回退对这类需求的干扰。
  - 复用现有调度通道（`lower_bounds`、`_resolve_constraints`、Q-time 前瞻、两遍排程），不引入新死锁面。
  - lead 被设备/班次/手动约束拖慢时，follower **自动同步顺延**（强同步语义）。
- **非目标**
  - lead 与 follower 设备资源自动互斥（仍按各自设备插槽竞争）。
  - follower 自身 Q-time / 手动 pin-delay / 引用 等既有约束不受影响。
  - 流程异构（step 需一一映射）暂不做，本期限定"同构流程"。

---

## 3. 数据模型（lot_constraints.csv 扩展）

为 `lot_constraints.csv` 增加语义列（默认留空时行为与现有完全一致，向后兼容）：

| 列名               | 含义（follow 语义）                                                        |
| ----------------- | --------------------------------------------------------------------- |
| `mode`            | `follow`=跟随；空=现有引用语义                                        |
| `follow_step`     | 步间距：follower 的 step k 在 lead 的 step (k−follow_step) 完成后才做，默认 1 |
| `follow_minutes`  | 时间间距：follower 的 step k 在 lead 的 step k 完成后再晚 N 分钟才做      |
| `follow_start_step` | 跟随作用区间起点 step（可选，默认从首个 step 起）                       |
| `follow_end_step` | 跟随作用区间终点 step（可选，默认到目标/末尾）                          |

> `follow_minutes` 与 `follow_step` 二选一，同时填时以 `follow_minutes` 为准（时间间距）。

**示例（跟跑先导一步）**：

```
lot_name  reference_lot  mode    follow_step  follow_minutes  follow_start_step  follow_end_step
real1     PC1            follow  1
```

**说明**：`lot_name` 即 follower，`reference_lot` 即 lead。单项声明 → 运行时生成整条阶梯。

### 3.1 校验规则（数据体检）
- lead 必须在 lot_list 中存在，且与 follower **流程同构**（step 序号一一对应）。
- `follow_step >= 1`；`follow_minutes >= 0`。
- follow 关系须**无环**：lead 不能（直接或间接）跟随它的 follower（构造成 DAG）。
- lead 与 follower 不能互相跟随；follower 不得再被其它跟随时作为 lead（简化校验，可选放开）。

---

## 4. 语义定义（精确）

设 follower=F，lead=L，同构流程 step 1..N：

- **步间距（follow_step=s，默认 1）**
  `F.step_k.start >= L.step_{max(k−s, 当前已满足)}.end_track_out`
  即：F 的 step k 的最早日，是 lead 的 step (k−s) 的**机台离开**时刻。
- **时间间距（follow_minutes=D）**
  `F.step_k.start >= L.step_k.end_track_out + D`
  即：F 沿 L 的时间线平移 D 分钟。

由此天然成立：
- F 绝不会跑到 L 前面（下界由 lead 完成后释放）。
- 步间距 s=1 时，**step 级 gap 恒 ≤ 1** → 精确实现"lot1 恒领先 lot2 一步（最多一步）"。

---

## 5. 内部实现设计（scheduler 集成）

### 5.1 解析与数据结构
- 新增 `FollowPacing(follower, lead, step_offset, minutes, start_idx, end_idx)`。
- 建立 `follow_map: dict[str, FollowPacing]`（key = follower）。

### 5.2 环检测 / 死锁规避
- `_detect_schedule_anomalies` 与 `all_blocked` 死锁判定**不把 follow 计入引用图**：
  - 不参与 lot 级引用环 DFS。
  - 不写入 `pending_refs` / `reference_deps`，避免进入"自然锚点回退"分支。
- 新增独立校验：follow 关系 DAG 检查（见 3.1）。

### 5.3 跟随基线（follow baseline）的生成与消费
- **两遍获得基线**：第一遍先给每个 follow 的 **lead** 排出（或复用 coarse anchors），收集
  `lead_step_end[k] = lead 的 step k 的 track-out 时刻`，写入 `lead_step_end_map[lead][k]`。
- **消费为 per-step lower_bound**：给 follower 的每个 step k 计算
  - 步间距：`lb_k = lead_step_end[k − step_offset]`
  - 时间间距：`lb_k = lead_step_end[k] + minutes`
  写入 follower 的 per-step `lower_bounds[k]`。
- 该 `lower_bounds` 走现有通道（与 `_precompute_whole_chain_block`、`_tight_qtime_target_start`
  已支持的 `lower_bounds` 一致），被 `_resolve_constraints`、设备插槽、班次、手动 pin/delay、
  Q-time 人门口前瞻**统一消费**，与既有的"安全站点/倒排保底"逻辑不打架。

### 5.4 同排程调度顺序
- 当 lead 与 follower 在**同一轮**排程时，须保证 lead 先于 follower：
  - 在 lot 处理排序中，给 lead 更高的调度优先级（拓扑序：lead 在依赖它的 follower 之前）。
  - 若 lead 未完成（如跨调度边界），回退用基线库/粗排锚点，并在终检标注"使用预测基线"。

### 5.5 同步语义（强同步，默认）
- 因为 lb 取自 lead 的**最终排程结束时刻**，lead 被设备/班次拖慢 → follower 下界自动顺延。
- 可选弱化（后续迭代）：`follow_max_gap_minutes`，当 gap 超阈值允许 follower 适度提前；
  本期默认不做，保持强同步。

---

## 6. 与现有功能交互

| 既有功能          | 关系                                                                 |
| -------------- | ---------------------------------------------------------------- |
| Q-time（含链块）   | follow 只提供"最早"下界；Q-time 压缩/倒排保底照常，取两者更严格者，不冲突              |
| 手动 pin / delay | 对 follower 仍生效（作为更晚的下界）                                        |
| 设备/班次竞争       | 正常排插槽；lead 慢 → follower 同步顺延                                  |
| 其它普通引用        | follower 仍可带普通引用；引用只影响其相关 step，follow 只影响间距，互不干扰          |
| 多 seed 快照       | 跟随基线在两遍排程内确定，随 seed 稳定                                       |

---

## 7. 校验与回归

- **终检（validation）**：对每条 follow，逐 step 校验
  `F.step_k.start >= lead_step_end[k−offset]`（步间距）或 `>= lead_step_end[k]+D`（时间间距）；
  违规计为错误并输出到告警。
- **数据体检**：执行 3.1 全部规则。
- **回归样例（tools/）**：
  1. 同构先导/常规双 lot，`follow_step=1`：验证 F 恒落后 L 一步、无引用环告警。
  2. lead 被 `eqp_constraint`/班次/手动 delay 刻意拖慢：验证 F 自动同步顺延。
  3. F 带 Q-time：验证 follow 下界 + Q-time 同时成立。
  4. 多 follower 跟随同一 lead：验证 DAG 与各自间距。
  5. 恶意配置（follow 成环 / 异构流程）：验证数据体检正确报错。

---

## 8. 边界与限制

- 本期限定同构流程；异构需显式 step 映射，留待后续。
- 时间间距 / 步间距二选一。
- lead 跨调度边界未完成时用预测基线并在结果标注。
- follow 不改变 lead 自身排程；lead 仍按原约束排。

---

## 9. 落点（后续改动清单，仅供评审参考）

1. `models.py`：新增 `FollowPacing` 与 lot_constraints 解析（读入新列）。
2. `data_loader.py`：`load_lot_constraints` 解析 `mode=follow` 与间距字段；补体检规则。
3. `scheduler.py`：
   - 解析 `follow_map`；两遍基线收集 `lead_step_end_map`；
   - 填 per-step `lower_bounds`；剔除 follow 于引用环/死锁判定；
   - lot 排序保证 lead 先于 follower。
4. `validation.py`：follow 间距终检。
5. `web/` 索引相关列、报表/体检展示 follow 命中。
6. `tools/` 回归样例（见 §7）。
7. 主目录与 `Windows打包` 同步、SOP 增补一节。