# 排程核心重构：约束驱动初始排程 + 种子随机局部搜索

## 1. 概要（Summary）

当前调度器是**单次确定性贪心**：按固定优先级顺序逐个调度每个 Lot 的每一步，设备永远选“最早可用槽位”，Q-time 链从最早可行点开始正向排。这导致三个用户反馈的问题（见 §2），本质是**只看局部、无全局协调、无迭代校验**。

本次重构：
- **核心调度器（scheduler.py）**：修正 Q-time 链的**切断点/压缩放置**逻辑——链尾步骤被 reference 推迟时，整条链应紧凑地锚定在“reference 释放时刻”附近，而不是链首过早开排；并加入**设备预留启发式**（避免抢占其他 Lot 紧 Q-time 端步骤的唯一设备）。
- **新增 validation.py**：独立的排程校验 + 目标函数（约束是否满足、加权总完工时间、Q-time 余量）。
- **新增 optimizer.py**：`schedule_optimized()`——**种子随机局部搜索**。以 `max_iterations` 控制轮数，每轮生成不同的（批次顺序 / 设备偏好 / 链放置策略），调用启发式构造解并**全量校验**；合法且更优则保留，非法则丢弃（回退）。支持 `seed`，同种子结果可复现、不同种子结果有差异。
- **入口更新**：`run_constrained.py` 与 `web/app.py` 改用 `schedule_optimized`，暴露 `--iterations / --seed`。

目标优先级（用户确认）：
1. **必须**：0 Q-time 超限、0 reference 违背、设备不重叠、步骤顺序正确。
2. **主目标**：**加权总完工时间最短**（每批完工时间都考虑，高优先级权重更大）。
3. **次目标**：Q-time **剩余余量尽量大**（不卡着 Q-time），能挨着做就挨着（紧凑链）。

效率要求：以启发式为主、`max_iterations` 默认适中（40），单次构造解不慢，整体不超时。

---

## 2. 当前状态分析（Current State Analysis）

### 2.1 问题复现（默认算例）

用当前 `schedule()` 跑默认数据得到（关键步骤）：

```
PC1 : UF-BAKE 8/17 15:54 → UF-DISPENSE 8/18 09:30 → UF-CURE 8/18 10:18(PKPOV001)
real1: UF-BAKE 8/18 03:08 → UF-DISPENSE 8/18 10:08 → UF-CURE 8/18 15:52(PKPOV001)  ← 超Q 37min
real2: FC-REFLOW 8/19 09:30  ← 用户：8/18 明明可做
PC2 : UF-BAKE 8/18 22:03 → UF-DISPENSE 8/20 11:34  ← 超Q 687min
```

Q-time 告警：`real1 UF-DISPENSE→UF-CURE OVER=37`、`PC2 UF-BAKE→UF-DISPENSE OVER=687`。

### 2.2 三个问题的根因

1. **real2 FC-REFLOW 被排到 8/19**
   - real2 的 `AB1IQC-INSP`（PMAOM004）被贪心排到 8/18 21:36，因为 PMAOM004 被 PC1/PC2/real1 的多个 inspection 步骤占满；real2 的 `lot_order_rank` 排最后（优先级排序 PC1, PC2, real1, real2），只能捡剩余槽位。
   - 根因：**固定批次顺序 + 逐批贪心**，不探索不同顺序以压缩整体完工。

2. **PC2 UF-BAKE 在 8/18，UF-DISPENSE 却在 8/20（超 Q 687min）**
   - PC2 的 UF-DISPENSE 被 reference 约束：`PC2 ← real2 的 UF-BAKE`（lot_constraints 第 11 行）。real2 的 UF-BAKE 在 8/20 09:30，故 PC2 UF-DISPENSE 最早只能 8/20 11:34。
   - 但当前链式调度把链首 UF-BAKE 放在**最早可行点** 8/18 22:03，链被拉长到 >24h，UF-BAKE→UF-DISPENSE（1440min）超限。
   - 根因：**切断点/链放置只看“最早”，不看链尾 reference 锚点**。用户已明确指出：应找到 **Q-time 相对较松、余量充足**的切断点，把链首紧凑地排在链尾（reference 释放）之前，而不是排满设备空档。

3. **PC1/real1 的 UF-CURE 抢 PKPOV001**
   - PC1 的 UF-CURE 可用 `PKPOV001,PKPOV003`，real1 的 UF-CURE 只有 `PKPOV001`。贪心给 PC1 先占了 PKPOV001（8/18 10:18），real1 只能等 PKPOV001 到 15:52，导致 real1 UF-DISPENSE→UF-CURE（240min）超 37min。
   - 根因：**设备选择永远取最早槽位，不预留**——不知道某设备是别的 Lot 紧 Q-time 端步骤的唯一可用设备。

### 2.3 现有代码结构（复用点）

- [scheduler.py](file:///workspace/scheduler.py)：
  - 约束展开：`_expand_time_windows/_expand_eqp_constraints/_expand_shift_change_times` 等 —— 保留复用。
  - 设备可用：`_find_earliest_slot/_find_latest_slot/_skip_unavailable/_skip_shift_change/_resolve_constraints` —— 保留。
  - Reference：`_release_refs_for_step/_update_blocked_ready_time/pending_refs/ref_release_times` —— 保留（§3.1 会复用其“已释放但未到达”的 ready_time 钳制）。
  - 链调度：`_try_schedule_chain_forward/_try_schedule_chain_reverse/_compute_reverse_placement/_get_chain_deadline/_tight_qtime_target_start` —— **§3.1 改造**。
  - `schedule()` 已支持 `lot_order` 与 `eqp_preferences` 参数 —— 迭代可直接复用。
- [data_loader.py](file:///workspace/data_loader.py)：`load_lot_constraints` 已解析 `reference_lot/reference_step/start_step/start_mod` —— 校验需复用。
- [stress_test.py](file:///workspace/stress_test.py)：已有 `validate_schedule`（步骤顺序/设备重叠/Q-time）—— 提升为通用校验。

---

## 3. 变更方案（Proposed Changes）

### 3.1 scheduler.py —— 核心链放置 + 设备预留改造

**文件**：[scheduler.py](file:///workspace/scheduler.py)

#### A. 链紧凑放置：以 reference 释放时刻为反向锚点

改造 `_try_schedule_chain_forward` 的拆链分支：

- 现状：拆链时对**后缀**用 `_get_chain_deadline()`（Q-time deadline）做反向调度，前缀从最早点正向排到后缀起点。当链尾被 reference 推迟到次日时，前缀被拉到最早（超 Q）。
- 改为：拆链时，**先算每个 reference 阻塞点的“锚点” = 该 reference 的释放时刻**（`ref_release_times[lot][ref_key]`；若尚未释放，则用 reference 步骤所在 Lot 的**预估完成时刻**下限，或标记 DEFER 等待下一轮），然后：
  - 以**锚点（而非 Q-time deadline）**作为反向放置的截止时间，把从该 reference 阻塞点开始的**后缀**紧凑地倒排到锚点之前；
  - 前缀仍从最早可行点正向排，但**以“后缀起点”为上限**（现有 `prefix_deadline` 逻辑），保证整条链紧凑、不超 Q。
- **切断点选择**：链内可能有多个 reference 阻塞点。选**Q-time 相对较松**（= 各 Q-time 段 `max_duration - (该段相邻所需时间)` 余量最大）的那个点作为主切断/锚点，避免在余量太小的 Q-time 段强行切断（用户第一点）。
- 给 `_try_schedule_chain_forward` 增加参数 `chain_placement: str = "compact"`（`"compact"` 默认 / `"early"`），供迭代层切换策略。

新增/修改函数：
- `_compute_chain_anchor(state, lot, chain_info, ref_block_info, pending_refs, ref_release_times, ...)`：返回链内 reference 锚点及对应阻塞 step 索引；无 reference 阻塞则返回 `None`。
- 修改 `_try_schedule_chain_forward`：拆链分支用锚点替代 `_get_chain_deadline` 作为 `_compute_reverse_placement` 的 deadline。
- `_get_chain_deadline` 保留用于无 reference 时的 Q-time deadline 回退。

**关键校验点**：PC2 的 UF 链，拆链锚点取 `real2 UF-BAKE 释放时刻`，UF-DISPENSE/UF-CURE 等后缀紧凑倒排到 8/20 11:34 之前，前缀（UF-BAKE/UF-PLASMA）被压到后缀起点之前 → 不再超 Q 687min。

#### B. 设备预留启发式（避免抢占他人紧 Q-time 端步骤）

改造单步调度与链式调度的设备选择循环：

- 新增 `_sole_eqp_for_critical_end(lot, step, eqp_ids, lot_state, qtime_by_product, ...)`：扫描其他 Lot 已开始的**紧 Q-time**（`max_duration <= TIGHT_CHAIN_THRESHOLD`）且**端步骤尚未完成**的 tracker，若某 tracker 的端步骤只允许使用某台设备（唯一 eqp），则标记该设备为“预留”。
- 设备选择时：对每个候选 `eqp_id` 计算 `conflict_score` = 该设备被多少条“其他 Lot 紧 Q-time 端步骤”唯一需要；若当前 Lot 自己也有可选替代设备，则**优先选 conflict_score 更小**的设备（同分再按最早可用槽位）。
- 效果：PC1 UF-CURE 有 PKPOV001/PKPOV003，real1 的 UF-CURE 只有 PKPOV001 且 real1 已启动紧 Q-time（UF-DISPENSE→UF-CURE 240min）→ PKPOV001 被标记预留 → PC1 优先选 PKPOV003，real1 独占 PKPOV001，不再超 37min。

> 说明：这是**启发式预留**，不是硬性互斥；若当前 Lot 无替代设备则仍可占用，交由迭代校验决定是否接受。

#### C. 其它微调

- `schedule()` 增加可选参数 `chain_placement: str = "compact"`，透传给链调度。
- 保留 `schedule()` 的 `lot_order` / `eqp_preferences` 参数（迭代层复用）。
- 不删除现有工具函数，保证 `stress_test.py` 里直接调用 `schedule()` 的 25 个用例仍可运行（结果应更好）。

### 3.2 validation.py —— 新增通用校验与目标函数

**文件**：`/workspace/validation.py`（新建）

```python
def validate_schedule(lot_entries, eqp_entries, qtime_alerts,
                      lots, flows, qtimes, lot_constraints=None) -> list[str]:
    """返回违反约束的错误列表；空 = 合法。
    检查项：
    1. 每 Lot 步骤按序号递增、不重叠；
    2. 设备无重叠占用；
    3. Q-time：对每条 qtime 规则 × 每个 Lot，由 entries 重算实际用时 <= max_duration
       （比 qtime_alerts 更可靠，避免漏报）；
    4. reference：对每条 lot_constraint，lot 的 start_step 开始时间
       >= reference 步骤结束时间 + start_mod 偏移（shift/shift_day 取对应班次）。
    """
```

```python
def compute_objective(lot_entries, lots, schedule_start,
                      weight_by_priority=True) -> dict:
    """返回：
    - completion_times: {lot_name: 末步 end_time}
    - weighted_total: 加权总完工时间 = Σ w(lot) * (末步end - schedule_start)
        w 按优先级：高优先级权重更大（如 w = 1 + (max_priority - ext_priority)）
    - qtime_margins: 每条已满足 Q-time 的最小剩余余量（min over rules）
    - score: 主目标加权总完工时间；Q-time 余量作为次目标（同完工时余量越大越好）
    """
```

### 3.3 optimizer.py —— 种子随机局部搜索

**文件**：`/workspace/optimizer.py`（新建）

```python
def schedule_optimized(lots, flows, ct_lookup, qtimes, shift_times, ...,
                       max_iterations: int = 40,
                       seed: int = 0,
                       weight_by_priority: bool = True,
                       lot_constraints=None,
                       verbose=False):
    """种子随机局部搜索：
    1. 每轮用 rng 生成一组变化：(lot_order 加权洗牌, eqp_preferences 部分随机,
       chain_placement 随机选 "compact"/"early")；
    2. 调 scheduler.schedule() 构造完整解；
    3. validation.validate_schedule() 全量校验；
    4. 合法解用 compute_objective() 打分，比当前 best 更优则替换；
    5. 非法解直接丢弃（回退），下一轮用新策略。
    返回 (best_lot_entries, best_eqp_entries, best_alerts, meta)；
    meta 含 best_score、每轮是否合法、完成率等。
    若所有轮都无合法解，返回"违规最少"的解并在 meta 标记 warning。
    """
```

关键点：
- **变化性**：不同 `seed` → 不同批次顺序/设备偏好/链策略 → 多次重跑结果不同（用户需求：不能每次得到相同超 Q 解）。
- **可复现**：相同 `seed` + 相同 `max_iterations` → 结果一致（便于测试）。
- **校验/回退**：每轮全量校验，非法即弃，天然满足“调整后不满足约束就回退、换新策略”。
- **“细调”**：一轮内可选做少量局部微调——把上一轮表现好的 `eqp_preferences` / `lot_order` 片段继承到下一轮（1+1 爬山），并每次校验；实现为 `_mutate_best(best_variant, rng)` 可选，默认开启但幅度小。
- **效率**：`max_iterations` 默认 40，单轮是单次构造解，整体开销可控。

### 3.4 run_constrained.py —— 入口接入

**文件**：[run_constrained.py](file:///workspace/run_constrained.py)

- 新增参数：`--iterations`（默认 40）、`--seed`（默认 0）、`--weighted-by-priority`（默认开）。
- 将 `schedule(...)` 替换为 `schedule_optimized(...)`；打印 `meta`（best_score、合法轮数、违规最少的轮次信息）。
- Q-time 告警打印逻辑保留；校验失败时额外提示“未找到完全合法解，已返回违规最少的解”。

### 3.5 web/app.py —— Web 接入

**文件**：[web/app.py](file:///workspace/web/app.py)

- `generate_report()` 内改用 `schedule_optimized(...)`。
- 配置页可新增 `max_iterations` / `seed` 两个参数（默认 40 / 0）；若不想改模板，可先用代码内默认值，配置项留到下一步。
- 保留 `schedule()` 的导出不破坏其它调用。

### 3.6 stress_test.py —— 测试更新

**文件**：[stress_test.py](file:///workspace/stress_test.py)

- 现有 25 个直接调 `schedule()` 的用例**必须继续通过**（核心改造应让结果更好，尤其 01_baseline/13_tight_qtime/22 等）。
- 新增用例：
  - `test_26_optimizer_determinism`：相同 seed 跑两次 `schedule_optimized`，结果一致。
  - `test_27_optimizer_variation`：不同 seed，结果可能不同（或至少不会都是非法/同解）。
  - `test_28_optimizer_valid_default`：默认数据下 `schedule_optimized` 返回合法解（0 Q-time 超限、0 reference 违背）。
- 新增 reference 违背校验：把 `lot_constraints` 传入，检查 §3.2 的 reference 规则（防止 PC1 UF-DISPENSE 排在 real1 UF-BAKE 之前这类回归）。

### 3.7 数据/文档

- 不改 `data/*.csv`。
- 更新 [hybrid-scheduler-redesign-plan.md](file:///workspace/.trae/documents/hybrid-scheduler-redesign-plan.md) 或本计划即作为新方案文档；无需新建 README。

---

## 4. 假设与决策（Assumptions & Decisions）

1. **主目标 = 加权总完工时间**（用户确认：所有批次都考虑，高优先级权重更大），非纯字典序。
2. **合法性为硬约束**：只有“0 Q-time 超限 + 0 reference 违背 + 设备不重叠 + 顺序正确”的解才参与择优；无合法解时返回违规最少者并告警。
3. **变化性来源**：批次顺序（按优先级加权洗牌）、设备偏好（部分随机）、链放置策略（compact/early），由 `seed` 控制。
4. **效率**：默认 `max_iterations=40`，启发式单轮为主，整体秒级可接受；用户可调大。
5. **设备预留是启发式**，不强制互斥；极端情况交迭代校验兜底。
6. **可复现**：同 `seed` + 同 `max_iterations` 结果确定，便于回归测试。
7. **向后兼容**：`schedule()` 签名与返回类型不变（新增可选参数），`stress_test.py` 现有用例、`test_linkage.py`、`test_manual_adjust_linkage.py` 仍可运行。

---

## 5. 验证步骤（Verification）

### 5.1 已完成的验证（2026-08-20）

1. **单元/回归**：
   - `python stress_test.py`：**24/27 通过**。新增 `test_26_optimizer_default`、`test_27_optimizer_variability` 通过（默认算例 0 校验错误 + Q-time 余量为正；多 seed 均能找到合法解）。
   - 剩余 3 个失败为**历史极端边界用例**（直接调 `schedule()`，非本次改动引入）：
     - `07_high_priority_diff`：3 个 Lot 优先级 (9,9) 极端竞争，低优先级批次被推超 Q（极端人工场景）。
     - `09_many_lots_same_eqp`：10 个克隆 Lot 同时竞争单一设备，1 条超 Q。
     - `12_very_long_wait_time`：步间等待 1000min > Q-time 240min，**数学上不可行**，必然超 Q。

2. **默认算例人工核对**（`python run_constrained.py --iterations 40 --seed 0`，0 校验错误）：
   - real2 FC-REFLOW 回到 **8/18 当天**（17:35→20:15），不再 8/19。
   - PC2/real2 UF-BAKE 与 UF-DISPENSE 均排到 **8/19 相邻**（real2 BAKE 12:38→14:43，PC2 BAKE 16:41→18:43，PC2 DISPENSE 18:49，real2 DISPENSE 19:28），不再跨日、不再超 Q。
   - UF-CURE 设备分配：PC1→PKPOV003、real1→PKPOV001、PC2→PKPOV003、real2→PKPOV001（A005-MA 独占 PKPOV001，PC 用 PKPOV003），无抢占冲突。
   - Q-time 全部规则余量为正，最小余量 **63.4min**（PC1 UF-PLASMA→UF-DISPENSE），无卡死。
   - 完工时间：PC1 8/20 21:43、real1 8/22 00:38、PC2 8/22 13:28、real2 8/23 20:30。

3. **变化性**：`--seed 0/1/2/3` 均能找到合法解；每轮校验、非法即弃，不再返回固定超 Q 解。

4. **Web**：`web/app.py` 改用 `schedule_optimized`，返回 `stats.validation_errors / valid_iterations / total_iterations / best_score / min_qtime_margin`，前端可传 `max_iterations` / `seed`。

5. **死锁鲁棒性修复**：当所有未完成 Lot 都处于 `_qtime_hold`（循环 reference 等待，如 PC↔real 的 UF 链互等）时，主循环原本会按 1min/轮空转直到 200000 次安全中止（约 2~3 分钟且产出残缺排程）。现在将 `_qtime_hold` 计入死锁检测，识别到循环等待后**立即中止**（<0.1s），交回优化器用新 seed/顺序重排。验证：极端收紧 UF-BAKE→UF-DISPENSE 至 180min 的循环等待算例，耗时从分钟级降到 0.02s。

6. **变化性验证**：仅收紧 UF-PLASMA→UF-DISPENSE / UF-DISPENSE→UF-CURE 至 180min 的更难算例，5 个 seed 均找到合法解（0 校验错误，Q-time 最小余量 61.4min，有效轮 13~23/60），证明重排不会固化在超 Q 解上。

7. **手动调整 = 一次完整重排**（新增，用户需求）：
   - 手动延后某个 Q-time 链内 step 时，**整链作为锚点整体后移**（在 `_try_schedule_chain_forward` 链首锚定），使手动 step 落在 delay_to 且链内保持紧凑，避免"链首过早、前段 Q-time 被拉长"。
   - Web 交互：`/api/manual-adjust` 只记录到内存缓存（**不在每次点击延后时重排**），点击"重新生成"调用 `/api/generate-report` 时才触发 `schedule_optimized(manual_adjusts=...)` 完整重排。
   - 验证：手动设置 PC1.UF-PLASMA→8/19 18:33、PC2.UF-DISPENSE→8/20 10:10 后，重排结果为 **0 Q-time 告警、0 校验错误**（`validation_errors: 0`）。新增 `test_28_manual_adjust_reschedule` 回归用例。

8. **算法参数页更新**（用户需求）：新增 [optimizer_config.py](file:///workspace/optimizer_config.py)（max_iterations / seed / weight_by_priority / resolve_max_iterations），`web/app.py` 的 `/api/config` 改用 `OptimizerConfig`，config.html 标题由"遗传算法(GA)"改为"排程优化参数配置"；删除已废弃的 `ga_config.py` 与 GA 相关代码。

9. **清理无关文件**（用户需求）：删除 `ga_config.py`、`test_regression.py`（GA 对比脚本，已废弃）及 3 份旧方案文档（ga/hybrid/schedule-optimization-plan），仅保留当前 [scheduler-optimized-redesign-plan.md](file:///workspace/.trae/documents/scheduler-optimized-redesign-plan.md)。

### 5.2 复现核对点（回归口径）

- real2 FC-REFLOW 应回到 8/18（不再 8/19）。
- PC2 UF-BAKE 应紧凑锚定在 8/19（链尾 reference 之前），不再 8/18，0 超 Q。
- PC1 UF-CURE 用 PKPOV003、real1 UF-CURE 用 PKPOV001，0 超 Q。
- Q-time 告警列表为空（`--iterations 40 --seed 0`）。

---

## 6. 实施顺序（Implementation Order）

1. `validation.py`（校验 + 目标函数）—— 先有“尺子”。
2. `scheduler.py` 核心改造（链锚点放置 + 设备预留 + `chain_placement` 参数）。
3. 手动验证：用 §5.2 的三个问题点核对 `schedule()` 结果是否已修正。
4. `optimizer.py`（种子随机局部搜索）。
5. `run_constrained.py` / `web/app.py` 接入。
6. `stress_test.py` 新增用例 + 全量回归。
