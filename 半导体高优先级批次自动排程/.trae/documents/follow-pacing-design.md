# 功能设计：lead（领导批次衔接 / back-to-back）与 Q-time 回拉

> 目标：在**少数关键 step** 上，让一个批次（lot1，领导批）的某个 step 与其配套批次（lot2）
> 对应 step **背靠背衔接**（lot1 先做完、lot2 紧接着做），且全程**不超任何区间 Q-time、
> 不在运行中的 Q 窗口内空闲等待**。一条声明表达，复用现有 `lot_constraints` 字段，
> 不新增字段、不产生引用环误报。

---

## 1. 需求还原

用户绝大多数场景只需**几个关键 step**：lot2（领导批 / leading lot）先做完 `step2`，
lot1（主点 / 跟随批）的 `step1` **紧接着**做（背靠背尾随），lot2 恒领先。
难点在于：`step2` 前后常有**多个连续 Q-time**（如
`UF-BAKE → UF-PLASMA → UF-DISPENSE → UF-CURE`），若 naive 地让 lot2 提前做完并"停在 step2 等"，
就会停在**紧 Q 计时窗口内空等 → 超 Q**。因此 lead 必须把领导批 lot2 上游链按 Q-time **回拉对齐**。

---

## 2. 设计目标 / 非目标

- 目标
  - 一条声明表达"lot2.step2 尾随 lot1.step1"（背靠背），只在关键 step 生效。
  - **不超任何区间 Q-time**；等待只发生在 Q 计时未启动/宽松的位置（紧 Q 链入口之前）。
  - 复用现有 `lot_constraints` 字段，**不新增列**；消除引用环误报与死锁回退干扰。
  - 复用现有 per-step 阻塞 + 两遍排程 + Q-time 目标求解（`_tight_qtime_target_start`/倒排）。
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
| lot1 | `lot_name` | **主点/跟随批**（用户主要关注的批次，背靠背尾随侧） |
| step1 | `start_step` | lot1 的衔接 step |
| lot2 | `reference_lot` | **领导批次 / leading lot**（在前跑，衔接点回拉侧） |
| step2 | `reference_step` | lot2 的对应衔接 step |
| mod | `start_mod` | 关系修饰（见 3.1） |

> 原 `hold_period_*` 列**删除**（确认无用）。
>
> **角色说明（重要）**：用户声明中 **lot2 是领导批（leading lot，在前面跑）**，lot1 是主点（跟随批，尾随）。
> 内部实现（LeadPair）统一为"lot1=领导批、lot2=配套批"，加载时自动交换角色（见 §5.1），
> 因此 §4 及以后的"内部实现"描述仍以"lot1=领导批"为准。

### 3.1 `mod` 含义（本页明确标注，web 端同步提示）

| mod | 语义 |
| ---- | ---- |
| （空） | 普通引用：lot1.step1 **在 lot2.step2 的完成时刻之后才能释放**（开始） |
| N（如 0.5 / 2） | 普通引用 + 偏移 N 小时 |
| shift | 普通引用，lot2.step2 完成后等到**下一班次**释放 |
| shift_day | 普通引用，等到**下一白班**释放 |
| lead | 领导衔接：**lot2（领导批）在前跑**，lot1.step1 **背靠背尾随** lot2.step2，并把领导批上游链按 Q-time 回拉对齐（§4） |

**示例（一行一个 lead）**：

```
lot1   step1                   lot2   step2                   mod
real1  A005-R1-UF-DISPENSE     PC1    A005-R1-UF-DISPENSE     lead
```

含义：`PC1`（lot2，领导批）先跑，做完 `A005-R1-UF-DISPENSE` 后 `real1`（lot1，主点）紧接着背靠背开始该步。

### 3.2 校验规则（数据体检）
- lot1、lot2 存在；两者流程同构（step1 在 lot1 流程、step2 在 lot2 流程）。
- `mod=lead`：系统自动按 lead 语义补全两条内部阻塞（§4.1），不需要额外行。
- lead 之间、lead 与普通引用之间**不得成环**（DAG 检查，逐 lead DFS，含 Q 链内的隐式约束）。
- lot2（领导批）当前已排在 step2 之后（热启动太靠后）：lead 对领导批侧已失效，结果标注（边界见 §8）。

---

## 4. 语义定义（lead + Q-time 回拉）

设 lead `(lot1, step1, lot2, step2, mod=lead)`，`step1-1` 为 lot1 紧邻 step1 的上一步。

### 4.1 硬闸（保持跟随不超前）
- **闸 A**：`lot1.step1.start >= lot2.step2.end_track_out`（lot1 不得早于 lot2 完成 step2）。
- 这一步是下界保证；但**不能只靠它**——它并不能拦住 lot2 自己提前做完后在 step2 等待超 Q。

### 4.2 核心：尾随目标 + 上游回拉（back-shift alignment）
lead 不做"在 step1 硬停"，而是**以 `lot2.step2.start` 为锚，把 lot1 上游整段链按 Q-time 回拉**，让
`lot1.step1.end ≈ lot2.step2.start`（背靠背）：

1. **目标**：`T* = lot2.step2.start`（衔接点）。希望 lot1.step1 的完成时刻 = `T*`。
2. **回拉**：以 `T*` 为**目标时刻**，把 lot1 的 `step1` 及其上游每一步倒排，使：
   `lot1.step1.end 命中 T*`，且链条上**每个中间 Q-time 都不超**。
   复用现有 Q-time 目标求解/倒排机制（`_tight_qtime_target_start` 所在那套）。
3. **Q 安全等待不变量（关键）**：lot1 的"空闲等待"只能放在
   **Q 计时未启动（紧 Q 链入口之前）或 Q 预算宽松**的位置；绝不让 lot1 停在
   紧 Q 计时窗口内（如 PLASMA→DISPENSE 之间）空等。

### 4.3 你一定会在意的连锁：Q 链把两 lot 拉齐
当 PLASMA→DISPENSE 的 Q 很紧、且 DISPENSE 必须相邻时：
- lot1 与 lot2 的 DISPENSE 相邻 ⇒ 它们的 **PLASMA 也必须在 Q 预算内近乎对齐**。
- 回拉自动把 lot1 的 PLASMA 对齐到 lot2 的 PLASMA（Q 入口），二者同速流过 Q 链，DISPENSE 恰好相连。
- 因此等价于：**lot1 的"就绪/释放"实际发生在 lot2 进入 Q 区间（甚至更前）之时**——正是你描述的期望行为。
- 若某 Q 段宽松，则无需强对齐，两 lot 可按各自设备自然排，lead 只保证不超前。

### 4.4 示例（读法）
`UF-BAKE → UF-PLASMA → UF-DISPENSE → UF-CURE`
- 你声明 step1=step2=UF-DISPENSE、mod=lead，期望 DISPENSE 背靠背。
- scheduler 行为：目标 `T*=lot2.DISPENSE.start`；把 lot1 上游按 Q 回拉，
  lot1 从 **PLASMA 之前**（UF-BAKE 处）开始与 lot2 步调一致地进入 Q 链；
  二者 PLASMA 对齐、同速流至 DISPENSE，DISPENSE 相邻，PLASMA→DISPENSE 与 DISPENSE→CURE 均不超。
- 反例（不做回拉）会做的事：lot1 提前完成并停在 DISPENSE 前 → 停在 PLASMA→DISPENSE 紧 Q 内 → 超 Q。

### 4.5 多 Q 区间环绕 step1 的处理（你特别提醒的重点）

step1（如 DISPENSE）前后常有**多个 Q 区间**，必须分清每一段谁是"对齐对象"、谁只是"承诺不超"：

| 段 | 相对 step1 | 角色 | 对齐要求 |
| ---- | ---- | ---- | ---- |
| `BAKE→PLASMA` | step1 的上游 | **入向 Q 段**，随回拉一起 | 不超即可，最好贴紧（gap 可小） |
| `PLASMA→DISPENSE` | step1 的紧邻入向 | **入向紧 Q 段**，回拉的锚链 | 不超（预算最紧，最容易踩雷） |
| `DISPENSE→CURE` | step1 的下游 | **出向 Q 段** | 不超；lot1 做完 DISPENSE 顺流进 CURE，不在其内空等 |

**核心机制 = 面向锚点 `T* = lot2.step2.start` 的"倒排回拉"**，不是整段刚性平移：

1. **锚点**：令 `lot1.step1.end ≈ T*`（背靠背，lot2 不得早于它开始）。
2. **倒排入向紧 Q 段**（`PLASMA→DISPENSE` 及更上游的 Q 链）：从 `DISPENSE.end = T*` 起，**逐段从后往前**求每步的**最晚可行开始**，并把相邻 Q 步**贴紧（gap→0）**排布，从而：进入该段后顺流不做任何等待 → 天然满足"不在紧 Q 窗口内空等"。
3. **出向段**（`DISPENSE→CURE`）：lot1 在 `step1.end` 后自己顺流即可，与 lot2 无关；只校验 `CURE.start − step1.end ≤ Q_CURE`。
4. **每条入向 Q 校验**：贴紧排布下相邻步 gap≈0，显然 ≤ 预算；因此**超 Q 的唯一来源是设备强制拉开的 gap**——这正是要显式处理的下界问题。

**关键：设备/约束强制拉开的 gap 怎么既不超、又满足"step2 紧跟 step1"**

- 倒排时，每一步的 `start ≥ LOWER`，其中 `LOWER = max(上一步可到达时刻, 该步设备最早可用, ready_time, pin/delay, 引用释放)`。
- **若某个入向 Q 段内，设备迫使 gap 仍 ≤ 预算** → 直接采用（如 `PLASMA` 机台满，`PLASMA.end` 被迫早于 `DISPENSE.start` 但差值 < 120min）：此时两者仍贴 Q 预算流动，**不倒排架不住、倒排可容纳**，`lot1.DISPENSE.end` 依旧命中 `T*`，`lot2.DISPENSE` 紧跟，各段 Q 不超。
- **若 gap 超过预算**（如 `PLASMA→DISPENSE` 预算 120，设备迫使 gap 210）→ 倒排无法让 `lot1.DISPENSE.end` 命中 `T*` 且不超入向紧 Q：走退化（§5.3-3），**把锚点整体后移 / 接受最小间隙 + 软告警**，绝不强推让 lot1 空等超 Q，也不允许 lot2 早于 lot1 开始。

**这就是"什么时候真正释放 lot1"的答案**

紧 Q（PLASMA→DISPENSE）下，两条批要想 DISPENSE 相邻：
- 倒排会把 lot1 的 `PLASMA`（入向紧 Q 段的入口）**对齐到 lot2 的 PLASMA 附近**；
- 因此 lot1 的"就绪/可继续"**实际发生在其 PLASMA（甚至更前）之时**，而不是等 lot2 物理到达 DISPENSE——与你判断一致；
- **不变量**：lot1 入向段一旦进入就顺流，出向段完成 `step1.end` 后也顺流；所有"等待/空闲"都落在**紧 Q 计时尚未启动（段入口之前）**处。

### 4.6 数值示例（证明"怎么才不会超 Q"）

设 `BAKE ct=60 · PLASMA ct=120 · DISPENSE ct=90 · CURE ct=300`，
`BAKE→PLASMA ≤ 480min · PLASMA→DISPENSE ≤ 120min（紧）· DISPENSE→CURE ≤ 480min`，全部 `track-out`（倒数到下一步开始）。

lot2 的 `DISPENSE.start = T*`，倒排后 lot1 的理想排布：

| lot1 步 | 开始 | 结束 | 入向 gap | 是否超 |
| ---- | ---- | ---- | ---- | ---- |
| BAKE | `T*−270` | `T*−210` | — | — |
| PLASMA | `T*−210` | `T*−90` | 0（≤480 ✓） | 否 |
| DISPENSE | `T*−90` | `T*` | 0（≤120 ✓） | 否 |
| CURE | `T*`+ | … | 0（≤480 ✓） | 否 |

- lot1.DISPENSE.end = `T*`，lot2.DISPENSE.start = `T*` → **背靠背**。
- 入向/出向 all gap=0 → **任何段都不超 Q**，且**不在任何运行中 Q 窗口内空等**。
- 若 PLASMA 设备迫使 `PLASMA.end = T*−180`：入向 gap = 90（≤120），依旧合法，`step1.end` 仍命中 `T*`。
- 若 PLASMA 设备迫使 `PLASMA.end = T*−300`：gap = 210 > 120 → 单靠倒排无法既命中 `T*` 又不超紧 Q → **退化**（锚点后移 / 最小间隙 + 软告警）。

---

## 5. 内部实现设计（scheduler 集成）

### 5.1 解析与数据结构
- 沿用 `load_lot_constraints`；行字段 `lot_name/lot1, start_step/step1, reference_lot/lot2, reference_step/step2, start_mod→mod`。
- `mod=="lead"` 时生成 `LeadPair(lot1, step1, lot2, step2)`，`lead_map: dict[str, list[LeadPair]]`（key=lot1）。
- 加载时展开两条内部阻塞，带 **`lead_id`** 标记（§4.1 闸 A 及回拉所需锚点占位）。

### 5.2 环检测 / 死锁规避
- `_detect_schedule_anomalies` 与 `all_blocked` 死锁判定**跳过**带 `lead_id` 的边：
  - 不把 lead 计入 lot 级引用环 DFS。
  - 不走"自然锚点回退"分支（回拉由 Q-time 目标求解显式完成，不靠锚点回退）。
- lead + 其 Q 链隐式约束的成环校验走独立 DFS（§3.2）。

### 5.3 两遍解析（解决 lot1/lot2 相互依赖）
1. **Pass A（领先一遍）**：先给 **lot2** 排（lot1 以软锚/最坏情形参与闸 A），读 `lot2.step2.start`。
2. **Pass B（回拉）**：令 `T* = lot2.step2.start`；对 **lot1**，以 `T*` 为目标，
   调用现有 Q-time 目标求解/倒排，把 `step1` 及其上游回拉到位，命中 `T*` 且不超任何中间 Q-time。
3. 若 `T*` 对 lot1 不可达（设备/ready 拖不住导致必须更早），则：
   以 lot1 实际最早可达的 `step1.end` 为锚反向微调 lot2，或接受一个小的间隙并**软告警"衔接带超阈"**。

### 5.4 Q 回拉的落点（倒排回拉，见 §4.5）
- **实施为面向 `T*` 的倒排回拉**，覆盖 lot1 从 `step1` 起**逐段向前的整条入向 Q 链段**（含 `step1` 自己）：
  以 `step1.end = T*` 为终点，相邻 Q 步贴紧（gap→0）后向回求每步**最晚可行开始**。
- 每步入向下界 `LOWER = max(上一步可持续推进的最晚开始所需, 该步设备最早可用, ready_time, pin/delay, 引用释放)`；
  若某入向 Q 段设备迫使 gap>0 但 `≤ 预算`，容纳之（`step1.end` 仍命中 `T*`）。
- 出向段（`step1→…`）不参与对齐，只校验每段 `gap ≤ 预算`（lot1 顺流，不在内空等）。
- 逐段显式校验：`区间内一步完成 → 下一步开始 ≤ Q 上限`（含 PLASMA→DISPENSE、DISPENSE→CURE、BAKE→PLASMA）。
- 入向紧 Q 段无法同时命中 `T*` 且不超时 → 退化（§5.3-3）。

### 5.5 同步语义（强同步，默认）
- 因为 `T*` 取 `lot2.step2` 的最终排程开始，lot2 被拖慢 → lot1 上游回拉同步顺延（这正是"衔接"）。
- 可选弱化（后续）：`mod=lead` 附带衔接带阈时允许小间隙；本期默认强衔接 + 软告警。

---

## 6. 与现有功能交互

| 既有功能                | 关系                                                     |
| ------------------ | ---------------------------------------------------- |
| Q-time（含链块/倒排）    | lead 的回拉**复用**目标求解/倒排；不超任何区间 Q；是本节核心耦合点          |
| 安全站点/正向贪心        | lead 对 lot1 采用"目标回拉"，对 lot2 走正常贪心；二者不冲突               |
| 手动 pin / delay       | 对闸内 step 仍生效（作为更晚下界），回拉尊重 pin/delay            |
| 设备/班次竞争            | 正常插槽；lot2 慢 → lot1 上游回拉（衔接）                       |
| 普通引用                | 与 lead 并存；任一更严格者生效，互不干扰                             |
| 多 seed 快照            | 衔接锚点在两遍排程内确定，随 seed 稳定                               |

---

## 7. 校验与回归

- **终检（validation）**：
  - 逐 lead：`lot2.step2.start >= lot1.step1.end`（闸 A）。
  - **逐链：lot1 上游 + step1 后所有 Q-time 均不超**（Q 窗口内无空等）。
  - 衔接带：`|lot2.step2.start − lot1.step1.end| > 阈` 时软告警。
  - 系统标注"使用预测基线"的 lead。
- **数据体检**：§3.2 规则。
- **回归样例（tools/）**：
  1. 单一 lead（无 Q 包裹的 step）：lot1 完成 step1、lot2 紧接着 step2，相邻、无引用环告警。
  2. **lead 落在紧 Q 链内**（如你例 BAKE→PLASMA→DISPENSE→CURE）：lot1 上游回拉、
     PLASMA 对齐、DISPENSE 相邻、各段 Q 不超。
  3. lot2 被设备/班次拖慢：lot1 上游同步回拉（顺延），不超 Q。
  4. 多个 lead（少数关键 step），门间 lot1 自由前进。
  5. lead + 手动 pin/delay：回拉尊重 pin/delay，取更晚下界。
  6. 恶意配置（lead 成环 / 异构流程 / 热启动靠后）：数据体检正确报错或标注。

---

## 8. 边界与限制

- 本期限定同构流程；异构需显式 step 映射，留后续。
- lot1 当前已排在 step1 之后（热启动太靠后）：回拉失效，结果标注，需数据侧配合。
- 若 lot1 设备/ready 使其无法回拉到 `T*`，退化为"接受最小间隙 + 软告警"，不强推违反设备约束。
- 极度拥挤、两 lot 共享瓶颈设备时，衔接可能被迫放宽为"尽可能相邻"（软目标）。

---

## 9. 落点（改动清单，评审参考）

1. `models.py`：`LotReference` 移除 `hold_periods`；新增 `LeadPair`；`mod` 并入（含 `lead`）。
2. `data_loader.py`：`load_lot_constraints` 解析为 `lot1/step1/lot2/step2/mod`；`mod=lead` 生成链路；
   删除 hold_period 解析；补体检（成环/异构/热启动）。
3. `scheduler.py`：lead_map → 两条带 `lead_id` 阻塞；两遍解析（Pass A 排 lot2 → Pass B 回拉 lot1）；
   环检测/死锁白名单跳过带 `lead_id` 的边；Q 回拉落点与逐 Q 校验。
4. `validation.py`：lead 终检（闸 A + 逐链 Q 不超 + 衔接带）。
5. `web/templates/index.html`：lot_constraints 列定义改为 `lot1/step1/lot2/step2/mod`，MOD 含义标注（§3.1）同步到页面。
6. `tools/`：回归样例（见 §7）。
7. 主目录与 `Windows打包` 同步、SOP 增补一节。