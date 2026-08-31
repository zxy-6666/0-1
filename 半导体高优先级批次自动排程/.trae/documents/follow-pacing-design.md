# 功能设计：同步门 / 跟停（pairwise sync gate）

> 目标：在**少数关键 step** 上让先导批次 "跟停"——一个批次（lot1）做完某 step 后**停住**，
> 等配套批次（lot2）跟上挨着对应 step，两者到齐才一起继续，杜绝先导批次做太快、越跑越前。
> 一条声明表达一个同步门，复用现有 `lot_constraints` 字段，不新增字段、不产生引用环误报。

---

## 1. 需求还原

用户绝大多数场景只需**几个关键 step**：
lot1 做完 `step1` 后必须**停住**，等 lot2 到达 `step2`；
同时 lot2 的 `step2` 不得早于 lot1 `step1` 完成。到齐后 lot1 越过继续。
因此 lot1 在该点"恰好在 lot2 前面挨着作业对应 step"。

**"停不住 → 先导批次做太快"** 是当前（仅单侧引用）的缺陷：那只保证被约束方不超前，
并不能拦住先导批次一路往前冲。同步门补上反向闸（先导批次的下一步等配套批次）。

---

## 2. 设计目标 / 非目标

- 目标
  - 一条声明表达一个"跟停门"（gate），只在关键 step 生效。
  - 复用现有 `lot_constraints` 字段，**不新增列**。
  - 消除引用环误报与死锁回退对门的影响。
  - 复用现有 per-step 阻塞 + 两遍排程；配套批次被拖慢时先导批次同步等待（强同步）。
- 非目标
  - 连续全流程跟跑（用户明确不需要）。
  - 设备资源互斥；lot2 自身 Q-time/手动约束不受影响。
  - 流程异构的 step 映射（限定同构，见 §8）。

---

## 3. 数据模型（lot_constraints.csv）

### 3.0 统一关系声明

`lot_constraints.csv` 收敛为**五列关系声明**：

```
lot1 | step1 | lot2 | step2 | mod
```

含义：lot1 的 step1 与 lot2 的 step2 构成关系，具体语义由 `mod` 决定。

**与现有字段映射（不新增字段）**：

| 声明列 | 现有字段 | 说明 |
| ---- | ---- | ---- |
| lot1 | `lot_name` | 持有被约束 step 的批次 |
| step1 | `start_step` | lot1 的被约束 step（gate 中即"停车/跟随点"） |
| lot2 | `reference_lot` | 参考批次（关系的另一侧） |
| step2 | `reference_step` | lot2 的参考 step（gate 中即"mate_step"） |
| mod | `start_mod` | 关系修饰（见 3.1） |

> 原 `hold_period_*` 列**删除**（确认无用）。

### 3.1 `mod` 含义（本页明确标注，web 端同步提示）

| mod | 语义 |
| ---- | ---- |
| （空） | 普通引用：lot1.step1 最早 ≥ lot2.step2 的完成时刻 |
| N（如 0.5 / 2） | 普通引用 + 偏移 N 小时 |
| shift | 普通引用，lot2.step2 完成后等到**下一班次**释放 |
| shift_day | 普通引用，等到**下一白班**释放 |
| gate | 同步门（跟停）：lot1 做完 step1 即停在 step1，等 lot2 到达 step2，两者到齐 lot1 才越过继续 |

**示例（一行一个门，gate 停在下步前面的对应 step）**：

```
lot1     step1                     lot2   step2                     mod
real1    A005-R1-UF-DISPENSE       PC1    A005-R1-UF-DISPENSE       gate
```

### 3.2 校验规则（数据体检）
- lot1、lot2 存在；两者流程同构（step1 在 lot1 流程、step2 在 lot2 流程）。
- `mod=gate`：系统自动按门补全两条内部阻塞（§4），不需要额外行。
- 门之间、门与普通引用之间**不得成环**（DAG 检查，逐门 DFS）。
- lot1 当前已排在 step1 之后（热启动太靠后）：gate 对 lot1 侧已失效，结果标注（边界见 §8）。

---

## 4. 语义定义（精确）

设门 `(lot1, step1, lot2, step2, mod=gate)`，`step1+1` 为 lot1 紧随 step1 的下一个 step：

- **闸 A（配套不超前）**：`lot2.step2.start >= lot1.step1.end_track_out`
- **闸 B（先导停在 step1）**：`lot1.(step1+1).start >= lot2.step2.end_track_out`

两者构成安全握手：lot1 完成 step1 后下一次前进必须等 lot2 到 step2；而 lot2 到 step2 又必须等 lot1 完成 step1。
step 级依赖链 `lot1.step1 → lot2.step2 → lot1.(step1+1)`，是 DAG，无死锁。
当 step1=step2（同号对齐，即本行 step1/step2 相同）时，lot1 在该点恒领先 lot2 恰好一步。

> 说明：step1/step2 由门行给出；闸 A/B 是系统内部由 `mod=gate` **自动展开**的两条单步阻塞，
> 不需要用户在 CSV 里手写两条引用。

---

## 5. 内部实现设计（scheduler 集成）

### 5.1 解析与数据结构
- 沿用 `load_lot_constraints`；行字段 `lot_name/lot2, start_step/step1, reference_lot/lot2, reference_step/step2, start_mod→mod`。
- 新增 `SyncGate(lot1=lot_name, step1, lot2=reference_lot, step2)`（`mod=="gate"` 时生成）。
- `gate_map: dict[str, list[SyncGate]]`（key=lot1）。
- 加载时把每个门展开为两条**内部单步阻塞**（等价 pending reference），并带 **`gate_id`** 标记：
  - `(lot1, (step1)+1)` blocked by `(lot2, step2)`  —— 闸 B
  - `(lot2, step2)` blocked by `(lot1, step1)`    —— 闸 A

### 5.2 环检测 / 死锁规避
- `_detect_schedule_anomalies` 与 `all_blocked` 死锁判定**跳过**带 `gate_id` 的边：
  - 不把门计入 lot 级引用环 DFS。
  - 不走"自然锚点回退"分支（门 step 级本就是 DAG，逐级释放即可收敛）。
- 门的成环校验走独立 DFS（§3.2）。

### 5.3 运行机制
- 复用现有 per-step 阻塞：调度到被门阻塞的 step 时"未就绪"，等待对侧释放；
  对侧 step 完成 → 事件触发释放 → 该 step 就绪。
- 两遍排程天然可用：闸内依赖从 `lot1.step1`（通常无依赖）起步，首个可排 step 有锚点即可收敛。
- lot 顺序：保证被依赖侧（闸所在 lot1）本轮先排，或可回收基线（跨边界用预测锚点并标注）。

### 5.4 同步语义（强同步，默认）
- 闸 B 取 `lot1.(step1+1)` 等待 **lot2.step2 的最终排程结束**，lot2 被拖慢 → lot1 同步在 step1 等（这正是"跟停"）。
- 可选弱化（后续）：`mod=gate` 附带最大间距阈值时超阈放行；本期默认强同步。

---

## 6. 与现有功能交互

| 既有功能          | 关系                                                     |
| -------------- | ---------------------------------------------------- |
| Q-time（含链块）   | 门只加更严格的下界；Q-time 压缩/倒排保底照常，取两者更严格者，不冲突      |
| 手动 pin / delay | 对闸内 step 仍生效（作为更晚下界）                            |
| 设备/班次竞争       | 正常插槽；lot2 慢 → lot1 在 gate 等（跟停）              |
| 普通引用          | 与门并存；任一更严格者生效，互不干扰                          |
| 多 seed 快照       | 门的锚点在两遍排程内确定，随 seed 稳定                         |

---

## 7. 校验与回归

- **终检（validation）**：逐门校验闸 A、闸 B 成立；系统标注"使用预测基线"的门。
- **数据体检**：§3.2 规则。
- **回归样例（tools/）**：
  1. 单一同步门（step1=step2）：lot1 停、lot2 到后一起走，lot1 恒领先一步、无引用环告警。
  2. lot2 被 `eqp_constraint`/班次拖慢：lot1 在 step1 同步等待（跟停），绝不过早放行。
  3. 多个门（少数关键 step），门间 lot1 自由前进。
  4. 门 + Q-time / 手动 delay 叠加：取更严格者成立。
  5. 恶意配置（门成环 / 异构流程）：数据体检正确报错。

---

## 8. 边界与限制

- 本期限定同构流程；异构需显式 step 映射，留后续。
- lot1 当前已在 step1 之后（热启动太靠后）：闸 B 对 lot1 已失效，结果标注，需数据侧配合。
- 门只在声明处生效，未声明区间不约定。

---

## 9. 落点（改动清单，评审参考）

1. `models.py`：`LotReference` 移除 `hold_periods`；新增 `SyncGate`；`mod` 语义并入。
2. `data_loader.py`：`load_lot_constraints` 解析为 `lot1/step1/lot2/step2/mod`；`mod=gate` 生成门；
   删除 hold_period 解析；补体检（成环/异构/热启动）。
3. `scheduler.py`：gate_map → 两条带 `gate_id` 阻塞；环检测/死锁白名单跳过；lot 顺序保证被依赖侧先排。
4. `validation.py`：门终检（闸 A/B）。
5. `web/templates/index.html`：lot_constraints 列定义改为 `lot1/step1/lot2/step2/mod`，MOD 含义标注（§3.1 表）同步到页面。
6. `tools/`：回归样例（见 §7）。
7. 主目录与 `Windows打包` 同步、SOP 增补一节。