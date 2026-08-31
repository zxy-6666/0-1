# 功能设计：同步门 / 跟停（pairwise sync gate）

> 目标：在**少数关键 step** 上让先导批次 lot1 "跟停"——lot1 停在某个 step（gate_step Y）
> 等 lot2 跟上挨着对应 step（mate_step X），两者到齐才一起继续，杜绝 lot1 做太快、越跑越前。
> 一条声明表达一个同步门，覆盖现有"手工互引用"建模，且不产生引用环误报。

---

## 1. 需求还原

用户绝大多数场景只需**几个关键 step**：
lot1 做完 `step Y` 后必须**停住**（不做 `step Y+1`），等 lot2 到达 `step X`；
同时 lot2 的 `step X` 不得早于 lot1 `step Y` 完成。到齐后 lot1 越过 Y 继续。
这样 lot1 在该点"恰好在 lot2 前面挨着作业对应 step"。

**"停不住 → lot1 做太快"** 是当前（仅 lot1→lot2 单向引用）的缺陷：那只保证 lot2 不超 lot1，
并不能拦 lot1 一路往前冲。同步门补上反向闸（lot1 的下一步等 lot2）。

---

## 2. 设计目标 / 非目标

- 目标
  - 一条声明表达一个"跟停门"（gate），只在关键 step 生效。
  - lot1 完成 `gate_step` 即被门拦住，等 lot2 到 `mate_step` 才放行。
  - 消除引用环误报与死锁回退对门的影响。
  - 复用现有 per-step 阻塞 + 两遍排程；lead 被拖慢时 lot2 自动同步（强同步）。
- 非目标
  - 连续全流程跟跑（用户明确不需要）。
  - 设备资源互斥；lot2 自身 Q-time/手动约束不受影响。
  - 流程异构的 step 映射（限定同构，见 §8）。

---

## 3. 数据模型（lot_constraints.csv 扩展）

| 列名        | 含义                                                                 |
| --------- | ------------------------------------------------------------------ |
| `mode`    | `gate`=同步门；空=现有引用语义                                           |
| `gate_step`  | lot1（`lot_name`）停车的 step Y                                         |
| `mate_step`  | lot2（`reference_lot`）对应 step X；与 Y 同号即"挨着领先一步"                |

**示例**：

```
lot_name  reference_lot  mode  gate_step                mate_step
real1     PC1            gate  A005-R1-UF-DISPENSE      A005-R1-UF-DISPENSE
```

说明：`lot_name`=要跟停的 lot1（先导），`reference_lot`=lot2。`gate_step`/`mate_step` 也可以显式给 step（同构下同号常见）。

### 3.1 校验规则（数据体检）
- lot1、lot2 存在；两者流程同构（`gate_step` 在 lot1 流程、`mate_step` 在 lot2 流程）。
- `mode=gate` 行必须是"成对"：除非另一侧由手工引用存在，否则门自动补全对偶边。
- 门之间、门与普通引用之间**不得成环**（DAG 检查，逐门 DFS）。
- lot1 已完成排在门后（当前 step 已在门后）则对 lot1 侧无效，结果标注（边界见 §8）。

---

## 4. 语义定义（精确）

设门 (lot1.gate_step=Y, lot2.mate_step=X)，`Y+1` 为 lot1 紧随 Y 的下一个 step：

- **闸 A（lot2 不超 lot1）**：`lot2.step_X.start >= lot1.step_Y.end_track_out`
- **闸 B（lot1 停在 Y）**：`lot1.step_{Y+1}.start >= lot2.step_X.end_track_out`

两者构成安全握手：lot1 完成 Y 后下一次前进必须等 lot2 到 X；而 lot2 到 X 又必须等 lot1 完成 Y。
step 级依赖链 `lot1.step_Y → lot2.step_X → lot1.step_{Y+1}`，是 DAG，无死锁。
当 X=Y（同号对齐）时，lot1 在该点恒领先 lot2 恰好一步。

---

## 5. 内部实现设计（scheduler 集成）

### 5.1 解析与数据结构
- 新增 `SyncGate(lot1, lot2, gate_idx_Y, mate_idx_X)`，`gate_map: dict[str, list[SyncGate]]`（key=lot1）。
- 加载时把每个门展开为两条**内部单步阻塞**（等价 pending reference）：
  - `(lot1, step_{Y+1})` blocked by `lot2.step_X`
  - `(lot2, step_X)` blocked by `lot1.step_Y`
- 这两条带 **`gate_id`** 标记。

### 5.2 环检测 / 死锁规避
- `_detect_schedule_anomalies` 与 `all_blocked` 死锁判定**跳过**带 `gate_id` 的边：
  - 不把门计入 lot 级引用环 DFS。
  - 不走"自然锚点回退"分支（门 step 级本就是 DAG，逐级释放即可收敛）。
- 门的成环校验走独立 DFS（§3.1）。

### 5.3 运行机制
- 复用现有 per-step 阻塞：调度到被门阻塞的 step 时"未就绪"，等待对侧释放；
  对侧 step 完成 → 事件触发释放 -> 该 step 就绪。
- 两遍排程天然可用：闸内依赖从 `lot1.step_Y`（通常无依赖）起步，首个可排 step 有锚点即可收敛。
- lot 顺序：保证被依赖侧（闸所在 lot1）在本轮先排或可回收基线（跨边界用预测锚点并标注）。实际闸内两步中 `lot1.step_Y` 无前置门依赖，故 lot1 先排即可。

### 5.4 同步语义（强同步，默认）
- 闸 B 取 `lot1.step_{Y+1}` 等待 **lot2.step_X 的最终排程结束**，lot2 被拖慢 → lot1 同步在 Y 等（这正是"跟停"）。
- 可选弱化（后续）：`follow_max_gap_minutes` 超阈值允许放行；本期默认强同步。

---

## 6. 与现有功能交互

| 既有功能          | 关系                                                             |
| -------------- | ------------------------------------------------------------ |
| Q-time（含链块）   | 门只加更严格的下界；Q-time 压缩/倒排保底照常，取两者更严格者，不冲突              |
| 手动 pin / delay | 对闸内 step 仍生效（作为更晚下界）                                    |
| 设备/班次竞争       | 正常插槽；lot2 慢 → lot1 在 gate 等（跟停）                     |
| 普通引用          | 与门并存；任一更严格者生效，互不干扰                                  |
| 多 seed 快照       | 门的锚点在两遍排程内确定，随 seed 稳定                               |

---

## 7. 校验与回归

- **终检（validation）**：逐门校验闸 A、闸 B 成立；系统标注"使用预测基线"的门。
- **数据体检**：§3.1 规则。
- **回归样例（tools/）**：
  1. 单一同步门（X=Y）：lot1 在 Y 停、lot2 到 X 后一起走，lot1 恒领先一步、无引用环告警。
  2. lot2 被 `eqp_constraint`/班次拖慢：lot1 在 Y 同步等待（跟停），绝不过早放行。
  3. 多个门（少数关键 step），门间 lot1 自由前进。
  4. 门 + Q-time / 手动 delay 叠加：取更严格者成立。
  5. 恶意配置（门成环 / 异构流程）：数据体检正确报错。

---

## 8. 边界与限制

- 本期限定同构流程；异构需显式 step 映射，留后续。
- lot1 当前已在 gate_step 之后（热启动太靠后）：闸 B 对 lot1 已失效，结果标注，需数据侧配合。
- 门只在声明处生效，未声明区间不约定。

---

## 9. 落点（改动清单，评审参考）

1. `models.py`：新增 `SyncGate` 与 lot_constraints 解析（读 `mode/gate_step/mate_step`）。
2. `data_loader.py`：`load_lot_constraints` 解析 gate、补展开两条内部阻塞、体检成环。
3. `scheduler.py`：gate_map → 两条带 `gate_id` 阻塞；环检测/死锁白名单跳过；lot 顺序保证被依赖侧先排。
4. `validation.py`：门终检（闸 A/B）。
5. `web/`：门命中展示、报表/体检。
6. `tools/`：回归样例（见 §7）。
7. 主目录与 `Windows打包` 同步、SOP 增补一节。