# 跨 Lot 约束链整体排程 + 等待时间修复 + 配置页对齐 + 清理

## 0. 已验证的当前状态（2026-08 复核）

已实际存在于代码中、**无需再改**：
- **#2 等待时间（反向调度补 wait）**：scheduler.py `_compute_reverse_placement` 已含 `chain_info/priority_wait_map` 形参，循环内 `current_deadline = best_start - timedelta(minutes=eff_wait)`（L1051）；`_try_schedule_chain_forward` 调用已传参（L1207）。**已修复。**
- **`resolve_max_iterations` 透传**：app.py L253 仍 `resolve_max_iterations=10` 硬编码 → **待改**（见 3.4）。
- **`_coarse_earliest_anchors` 已接收 `manual_adjusts`**（L1588）并构造 `manual_delay_map`。

**关键缺陷（必须修，即 #1 根因）**：
- scheduler.py **L1756**：`if manual_delay_map and False:` —— 手动延迟级联传播被**硬性关掉**（`and False`），是 PC↔real 相互引用链未整体平移、real bake 太靠前的直接原因。**必须去掉 `and False` 并复用循环逻辑。**
- scheduler.py **L1819**：紧链压实 `if not has_internal_pin and lst[s_rel] < desired_start:` 对"链中存在 reference 钉住"的链（正是 PC↔real 循环引用 UF 链场景）**整链跳过压实**。

**未完成（#3/#4）**：
- optimizer_config.py：**无任何 SA+Tabu 参数**，`parameter_meta()` 仅 7 项、`groups()` 仅 2 组。
- optimizer.py：**无 SA/Tabu 细调层**（仅多 seed 构造择优）。
- web：`/config` 路由（app.py L39）与 `config.html` 仍存在；`/api/config` POST 无白名单；index.html L392 标题仍为"SA+Tabu 算法参数配置"。
- #4 清理：调试脚本与备份、过时 pyc 仍在。

**关于备份文件的更正（2026-08 复核）**：
- `scheduler_before.py`（2729 行）与 `scheduler.py`（2798 行）**并不像旧文案所述逐字节相同，而是有差异**（当前 scheduler 是在其基础上扩展）。两者都不被任何 `from/import` 引用，均为独立备份/参考文件。
- `scheduler_before1.py`（2088 行，用户本次指明"之前上传错文件、真正要借鉴的版本"）采用**逐步调度 + Q-time 前瞻预防 + 每步重排序**，含两项比当前实现更优的方法（详见 **2.3**）。实现期保留参考，`#4`清理阶段再删除。

---

## 2.3 借鉴 scheduler_before1.py 的更好方法（用户第 5 点，新增）

已逐行读完 [scheduler_before1.py](file:///workspace/scheduler_before1.py)，其中与当前 `scheduler.py` 相比有两项**明确更优**、且与 #1/#2 直接相关的算法，纳入本计划：

### A. 紧链 MIN_CHAIN_WAIT 动态压缩（gap 预算分配，优于当前固定 wait）
- 位置：L1140-1185。
- 做法：对链内每条 Q-time 规则，用 `max_gap_for_q = (q.max_duration - 链内中间步骤有效 CT) / 间隙数` 算出**该规则允许的最大步间等待**；取所有规则的最小值作为整链 `MIN_CHAIN_WAIT`，再 `max(chain_min_step_gap, MIN_CHAIN_WAIT)` 夹紧。
- 优点：等待时间不是固定的，而是**受链内真实 CT 与 Q-time 预算反推**，紧链自动压紧、保证不超 Q，避免了当前 `_effective_chain_wait` + `chain_wait_safety` 这种"固定值"在部分链上留过头、在部分链上不足的问题。
- 采纳到：`scheduler.py` 的链反向放置/正向调度，把步间 wait 由固定 `_effective_chain_wait` 升级为该"按链动态分配"的逻辑（对拆链前缀与后缀各自衔接的 gap 同样适用），双向强化 #2。

### B. 迭代"向后-向前"整链调度 + Q-time 超时基于链重启（负载均衡 + 整链后移）
- 位置：L1208-1454。
- 做法：链尾设 `earliest_chain_end = chain_start + 全链duration(含gap)`；向后 pass 每步选"并列最早窗口 + 更空闲 eqp"做负载均衡；设备不可用时"该步及之前整体后移"；向前约束/设备校验又产生 `constraint_shift/eqp_shift` 时**整链后移并 `continue` 重迭代**；最后逐条验证 Q-time，超时则反推 `target_chain_start` 再次以更晚起点重试（≤10 轮）。
- 优点：真正"把整条链当一块整体"平移、反复收紧，天然给出**散开范围**（= 链 Q-time 预算），且换班 `snap` 只在无约束后移时做、避免把链甩到下一班次导致超 Q。这与 #1 要求的"不能完全拆散后单步调度、拆后给定散开范围"高度一致。
- 采纳到：当前 `_coarse_earliest_anchors` 的**紧链压实**与 `_try_schedule_chain_forward` 的拆链分支，把"跳过循环引用簇"改为"整簇整体回退 + 用 Q-time 预算反推散开范围"，并沿用"整链后移重试直至 Q-time 达标或无法达成"的迭代。

### D. 已排链内"手动调整累积平移"（对 #1 的 PC1.UF-BAKE→15:54 场景最直接）
- 位置：L1459-1478。
- 做法：整链已按紧凑放置后，对链内某一步应用手动延迟（`_apply_manual_adjust`）时，算出 `additional_shift = new_step_start - step_start`，**累积到 `cumulative_shift` 并把链内其后所有步骤同步后移**，保持"改一步、整段跟着平移"的紧密性。
- 优点：这正是用户手动改 `PC1.UF-BAKE→15:54` 后，整条 UF 链（含后续 step）应当**作为整体后移**、而不是只有该一步裸奔的机制；与 B 的"整链后移"互补（B 处理设备/约束触发的后移，D 处理用户手动延迟触发的后移）。
- 采纳到：`_try_schedule_chain_forward` 与 `_coarse_earliest_anchors` 的手动延迟应用处——当手动延迟落在某链内时，累加偏移并级联到链内后续步骤（跨 lot 再经 reference 传播），保证用户延迟与 related lot 整体联动。

### C. Q-time 前瞻预防 / 全链设备可通行性前瞻（已确认最有价值，最适合 #1 的跨 lot 场景）
- 位置：L837-959。
- 做法：当 lot 即将进入某 Q-time 的 `start_step` 时，**在临时机器可用性副本上模拟整条 start→end 链**；若中间步骤（如 PLASMA/DISPENSE）当前积压、无法在 `max_duration` 内完成，则**推迟打开该 start_step**（加大紧迫度惩罚让其它可通行的 lot 先排）；再加一层：同一条链已有别的 lot 打开但未跑完时，**串行化进入** start_step，避免"一下开 5 个 BAKE，4 个抢不到中间设备而超 Q"。
- 优点：在"打开链"这一决策点上就做**跨 lot 设备竞争感知**，直接把"real bake 排得太靠前、随后被卡在中间设备超 Q"预防掉——正是 #1 抱怨的场景。
- 采纳到：`scheduler.py` 中 lot 选择/链放置决策点，作为**跨 lot 整体锚定的前置判断**：对 PC↔real 相互引用的 UF 链，先做全链可行性与关键中间设备积压检查，决定整链何时打开，再交给整体平移逻辑。

> 综述：把 #5 从"只学 meta_heuristic_before(SA+Tabu)"扩展为**还学 scheduler_before1.py**。SA+Tabu 供 optimizer 细调层（#5 原文）；scheduler_before1 的 A/B/C/D 供 scheduler.py 整体锚定、手动延迟平移与等待分配（#1/#2）。`scheduler_before1.py` 保留至实现完成、方法落地验证后再随 #4 清理删除。

---

# 1. 概要（Summary）

本轮针对用户提出的 5 点，在现有"构造式启发式 + 种子随机局部搜索"架构上做**定向修复与增强**，不推翻架构：

1. **跨 Lot 约束的 Q-time 链**：手动延迟某中间步骤后，不能再把链"完全拆散后单步调度"。要求：涉及相互引用的相关 lot 的**整条 Q-time 区间所有 step 作为一块整体**锚定，拆链后要给两段**明确的散开范围**（= 该链的 Q-time 预算）。根因：`_coarse_earliest_anchors` 未接收 `manual_adjusts`，且对"链内部 reference 钉住"的情况跳过了整链压实，导致拆链后失控。
2. **等待时间（wait time）丢失**：`_compute_reverse_placement` 反向放置后缀时，步间只算 CT、**完全没加 wait time**，导致拆链后缀步骤 back-to-back、无等待间隔。
3. **算法配置页与代码不对应**：Web 端 `/api/generate-report` 把 `resolve_max_iterations` **硬编码为 10**，忽略了 `/config` 保存的配置；且存在独立 `/config` 页与主页滑出弹窗**两套重复 UI**，主页弹窗标题仍是过时的"SA+Tabu 算法参数配置"。
4. **清理无用文件与代码**：删除调试/复现脚本与临时输出、与 `scheduler.py` 逐字节相同的 `scheduler_before.py`、过时 pyc，缩减上下文。
5. **借鉴 `meta_heuristic_before`（SA+Tabu）**：选择性采纳其 禁忌表 / SA 接受准则 / 温度与算子权重自适应，作为 `schedule_optimized` 的**增强细调层**；`scheduler_before.py` 与当前 `scheduler.py` 逐字节相同（无额外逻辑可学），SA+Tabu 学习价值全部来自你给的伪代码。新增少量可配置参数并体现在配置弹窗中。

> 决策说明：用户未对"配置界面形态 / 元启发式采纳深度"给出明确答复。本计划按推荐默认执行——**保留主页滑出弹窗为唯一配置入口、删除独立 /config 页**；元启发式采用**选择性采纳**（不整体重写）。如用户后续想保留 /config 页或整体重写，可再调整。

---

## 2. 当前状态分析（Current State Analysis）

### 2.1 关键函数与行为（来自实际代码阅读）

- [scheduler.py](file:///workspace/scheduler.py) `_coarse_earliest_anchors`（L1566-1755）：计算每 lot 每 step 的最早可行锚点；含 reference 传播（L1631-1693）与紧链压实（L1695-1755）。**问题**：签名不含 `manual_adjusts`（L1573 仅 7 个形参），且紧链压实对"链内部存在 reference 钉住"（`has_internal_pin`，L1739-1750）**直接跳过**（L1751）——这正是 PC↔real 相互引用 UF 链的场景。
- `_compute_reverse_placement`（L972-1047）：反向放置链后缀。**问题**：循环（L1007-1047）只按 `ct` 排，步间无 wait time；`current_deadline = best_start`（L1044）直接回退，无间隔。
- `_try_schedule_chain_forward`（L1050-1407）：遇 reference 阻塞松链拆链。后缀反向（L1197-1201），前缀正向（L1215-1343）。前缀最后一步以 `suffix_start_time` 为 deadline（L1243），但 `best_start + ct <= prefix_deadline` 的判定（L1248-1249、L1268）**没有加 wait 缓冲**，衔接处可能无等待。
- `_get_chain_deadline`（L1880-1926）、`_chain_ref_anchor`（L1798-1836）：现用于取后缀截止点/锚点。
- [optimizer_config.py](file:///workspace/optimizer_config.py)：`parameter_meta()` 现返回 7 个参数（`max_iterations/seed/weight_by_priority/resolve_max_iterations/tight_chain_threshold/qtight_safety_margin/chain_wait_safety`），分组为"搜索控制"与"Q-time 链/紧凑性"。
- [web/app.py](file:///workspace/web/app.py)：`/api/generate-report`（L231-260）从 `/config` 读 `max_iterations/seed/weight_by_priority/tight_chain_threshold/qtight_safety_margin/chain_wait_safety` 并传入，但 **`resolve_max_iterations=10` 写死**（L253）。`/api/config` GET/POST（L835-858）已可用，POST 未做字段白名单。

### 2.2 问题根因

1. **跨 Lot 约束链超 Q / real bake 太前面**：
   - 用户手动延迟 `PC1.UF-BAKE→15:54`、`PC2.UF-CURE→14:18`。PC 与 real 的 UF 链存在循环引用（`data/lot_constraints.csv`：PC1.UF-DISPENSE 等 real1.UF-BAKE；real1.UF-DISPENSE 等 PC1.UF-BAKE；PC2↔real2 同理）。
   - `_coarse_earliest_anchors` 不知道手动延迟，且对循环引用钉住的链跳过压实 → 拆链后前缀/后缀各自贴着设备最早/最晚槽位排，**整条 Q-time 区间（跨 lot）各 step 不作为一个整体平移** → real 的 bake 停在"太前面"，PC 的 delay 又把它推开，Q-time 被拉爆。
   - 用户要求：**相关所有 Q-time 区间 step 整体考虑**，不拆散成单步；确需拆时给两段**明确的散开范围（=链 Q-time 预算）**。
2. **wait time 丢失**：反向 placement 未加等待（L1007-1047）；拆链衔接判定未含 wait（L1248/1268）。
3. **配置页不对应**：`resolve_max_iterations` 硬编码；两套 UI 重复；标题文案过时。
4. **死代码**：调试/复现脚本、临时输出、相同备份 `scheduler_before.py`、过时 pyc。

---

## 3. 变更方案（Proposed Changes）

### 3.1 scheduler.py —— 跨 Lot 链整体锚定 + 等待时间修复

**文件**：[scheduler.py](file:///workspace/scheduler.py)

#### A. `_coarse_earliest_anchors` 感知手动延迟并整链/跨 lot 整体平移
- 新增形参 `manual_adjusts`（可空）。
- 在 reference 传播（L1631-1693）稳定后，**追加一轮"手动延迟锚点传播"**：对每个 `manual_adjust_lookup` 命中的 `(lot, step)`，若该 step 在 chain 内、或在某 chain 的 Q-time 区间内，则以 `delay_to` 为下限修正该 step 及其后所有 step（保持内部紧凑平移）；然后**再次跑 reference 传播**，把手动延迟沿 reference 链传播给相关 lot（real 的 bake 随之整体后移，不再"太前面"）。
- 移除/改写紧链压实的 `has_internal_pin` 全跳过逻辑（L1739-1751）：当链处于**跨 lot 循环引用簇**时，改为"以整簇最新释放/手动锚点为锚整体回退"，而不是直接跳过。

> 校验点：手动设 `PC1.UF-BAKE→15:54` 后，real1.UF-BAKE 等引用相关 step 应整体后移，PC1 整条 UF 链紧凑不超 Q。

#### B. `_compute_reverse_placement` 补回步间等待
- 新增形参 `chain_info` 与 `priority_wait_map`。
- 循环内增加步间等待：`gap = _effective_chain_wait(lot, chain_info, priority_wait_map)`，放置 step 后令 `current_deadline = best_start - timedelta(minutes=gap)`（idx>0 时），使后缀相邻步骤保留真实等待间隔。
- 相应地将 `_try_schedule_chain_forward` 中对该函数的调用（L1198-1201）传入 `chain_info/priority_wait_map`。

#### C. 拆链后给定"散开范围"，并保证前缀→后缀衔接带等待
- 在 `_try_schedule_chain_forward` 拆链分支，把"suffix 从 deadline 反向 + prefix 截止 suffix 起点"改为**受"散开范围"约束**：散开范围 = 该链 `min_qtime`（整体 Q-time 预算）。校验：`suffix_start_time - prefix_last_end ≥ 链段所需最小等待`，若不足则整体压缩/微调，避免两段飞散。
- 前缀最后一步对 `prefix_deadline` 的判定（L1248-1249、L1268）：改为 `avail + ct + wait ≤ prefix_deadline`（含步间等待缓冲），保证衔接带等待。

> 预期：PC↔real/PC2↔real2 的相互引用 UF 链整链整体锚定、拆链受 Q-time 预算约束，不再出现链首过早、中段被 reference 拉开导致超 Q。

### 3.2 optimizer.py —— 选择性采纳 SA+Tabu 细调层

**文件**：[optimizer.py](file:///workspace/optimizer.py)

在 `schedule_optimized` 现有"多 seed 构造 + 全量校验 + 择优"框架内，新增一个**细调阶段（refine phase）**，借鉴你给的 `meta_heuristic_before`：

- 用当前代数定义邻居：决策变量 = `(lot_order, eqp_preferences, chain_placement)`。定义算子：`order_swap`（交换两 lot 顺序）、`order_shuffle`（洗牌局部段）、`eqp_swap`（成对交换设备偏好）、`eqp_shuffle`（洗牌设备偏好）。
- **Tabu 表**：`move_id → 解禁迭代`，`tabu_tenure` 到期移除；当邻居优于历史全局最优时 **aspiration 破禁**（采纳伪代码 13.7/13.8）。
- **SA 接受**：`delta = nb_obj - cur_obj`；`delta <= 0` 接受；否则 `prob = exp(-delta/T)` 概率接受；温度按 `alpha = exp(log(T_end/T_start)/(max_iter-1))` 冷却（伪代码 11/13.6-13.9）。
- **温度自适应**：维护近 `adapt_window` 次接受率，朝 `target_accept_rate` 调整 `T`（伪代码 13.14）。
- **算子权重自适应**：统计各算子窗口内贡献（-delta），按占比加权更新并归一化，限制在 [0.05,0.8]（伪代码 13.10/13.15）。
- 评价函数复用现有 `validate_schedule` + `compute_objective`；非法邻居直接回退（保留现有靠校验兜底的安全机制）。

新增可配置参数到 `OptimizerConfig`（见 3.3），供配置弹窗动态渲染与调整。

### 3.3 optimizer_config.py —— 参数与配置对齐

**文件**：[optimizer_config.py](file:///workspace/optimizer_config.py)

- 保留现有 7 个参数；新增一组"SA+Tabu 搜索"参数（选择性采纳所需的最小集合）：
  - `tabu_tenure: int = 8`（禁忌期，1-30）
  - `sa_temperature_start: float = 200.0`（初始温度，可配）
  - `sa_temperature_end: float = 2.0`（终止温度）
  - `target_accept_rate: float = 0.3`（目标接受率，0.1-0.6）
  - `refine_max_iterations: int = 60`（细调轮数，0=关闭）
- 在 `parameter_meta()` 新增这批参数的元信息（分组"SA+Tabu 搜索"），并在 `groups()` 加入该分组。类型 `bool` 支持 `refine` 开关（`refine_enabled: bool = True`）。
- 保证每个元信息 key 都能被 `OptimizerConfig` 字段承载、且被 `schedule_optimized` 消费（与 3.2 对齐）。

### 3.4 web/app.py —— 配置真正生效

**文件**：[web/app.py](file:///workspace/web/app.py)

- `/api/generate-report`（L253）：将硬编码 `resolve_max_iterations=10` 改为 `_cfg.get("resolve_max_iterations", 10)`。
- 把新增 SA+Tabu 参数从 `_cfg` 读取并传入 `schedule_optimized(...)`（与现有 L234-259 同样的透传方式）。
- `/api/config` POST（L848-858）：增加**字段白名单**校验，只保存 `OptimizerConfig` 已知字段，丢弃未知 key。
- `/config` 独立页：按 3.5 决策删除路由与页面。

### 3.5 web/templates —— 统一配置弹窗 + 修正文案

**文件**：[index.html](file:///workspace/web/templates/index.html)、删除 [config.html](file:///workspace/web/templates/config.html)

- index.html 弹窗标题（L392）由 `"SA+Tabu 算法参数配置"` 改为 `"排程优化参数配置"`。
- 弹窗内容已由 `parameter_meta()` 动态渲染（L1028-1061），新增的 SA+Tabu 参数会自动出现，无需改渲染逻辑（如需分组折叠保持现状）。
- **决策（采纳"弹窗"默认）**：删除独立 `/config` 路由（app.py L39-42）与 [config.html](file:///workspace/web/templates/config.html)，使主页滑出弹窗成为唯一配置入口，减少重复维护。

> 若后续希望保留独立 /config 页，仅需撤销该删除项，两处共享同一 `parameter_meta()`，保持一致。

### 3.6 清理无用文件（缩减上下文）

**文件**：删除以下"确证未被引用"的文件/输出：
- 调试/复现脚本：`_dbg_t09.py`、`_dbg_uf.py`、`_repro_opt.py`、`_repro_pc2.py`
- 临时输出文本：`_clone09.txt`、`_real2_out.txt`、`_trace07.txt`、`trace.txt`、`dbg_stderr.txt`、`dbg_stdout.txt`
- `scheduler_before.py`：与 [scheduler.py](file:///workspace/scheduler.py) **逐字节相同**（已 `diff` 确认无差异），无学习价值，删除。
- 过时 `.pyc`：`ga_config`、`ga_optimizer`、`meta_heuristic`、`meta_heuristic_config`、`scheduler_before`、`_dbg_pc2_uf` 等 `__pycache__/*.pyc`（源文件已不存在或已废弃）。
- **保留**：`models.py`、`data_loader.py`、`outputs.py`、`validation.py`、`optimizer.py`、`visualization.py`、`flow_importer.py` 以及测试文件（均被 `app.py/run_constrained.py` 引用）。
- 删除前逐一用 `Grep` 复核无 `from/import` 引用；`.trae-html-share-packages/` 内旧 zip 不属代码，暂不动。

### 3.7 引用/参数核对（不改数据）

- 不改 `data/*.csv`。
- 若确认存在遗漏的 `meta_heuristic.py` 源文件，一并删除（只在 `__pycache__` 见 `.pyc`，源未见）。

---

## 4. 假设与决策（Assumptions & Decisions）

1. **配置入口收敛**：只保留主页滑出弹窗（用户"放到一个弹窗"），删除独立 /config 页。
2. **元启发式 = 选择性采纳**：不整体重写 optimizer；在现有多 seed 构造+校验框架内叠加 SA+Tabu 细调层与自适应算子权重。
3. `scheduler_before.py` 与 `scheduler.py` 逐字节相同，SA+Tabu 学习仅来自用户提供的伪代码；`scheduler_before.py` 作为冗余副本删除。
4. **跨 Lot 链整体锚定**：手动延迟通过最早的锚点传播到整条链及 reference 关联 lot；拆链受"散开范围 = 链 min_qtime"约束。
5. **等待时间**：反向放置补回步间 wait；拆链衔接判定含 wait 缓冲。
6. **新增 5~6 个 SA+Tabu 参数** 全部由 `parameter_meta()` 暴露、被 `schedule_optimized` 消费、可在弹窗修改，映射一一对应。
7. 合法性仍为硬约束（0 Q-time 超限 + 0 reference 违背 + 设备不重叠 + 顺序正确）；非法解回退，多 seed 兜底。

---

## 5. 验证步骤（Verification）

1. **等待时间（#2）**：构造拆链算例（松链 + reference 阻塞），断言 `_compute_reverse_placement` 输出的相邻后缀步骤 `end[i]` 与 `start[i+1]` 间隔 ≥ `_effective_chain_wait`；现有 `stress_test.py` 及 `test_linkage.py`/`test_manual_adjust_linkage.py` 全量回归通过。
2. **跨 Lot 链（#1）**：复现用户场景——手动 `PC1.UF-BAKE→2026/08/20 15:54`、`PC2.UF-CURE→2026/08/20 14:18` 后整链重排，断言 `validation_errors == 0`、qtime_alerts 为空、real 的 bake 不再"太前面"，且 PC 的 UF 链紧凑。
3. **配置生效（#3）**：在弹窗改 `resolve_max_iterations`，保存后用 `/api/generate-report`，用 `grep`/断点确认传入值 = 保存值（不再是 10）；确认无独立 /config 入口。
4. **SA+Tabu（#5）**：不同 seed 可复现；新增开关 `refine_enabled=false` 时结果与原一致（无细调），`true` 时不劣于原 best（构造式回退兜底）。
5. **清理（#4）**：`git status`/`LS` 确认目标文件已删，且 `python -c "import xml"...` 无残留引用导致的 ImportError；`run_constrained.py --iterations 40 --seed 0` 正常出结果。
6. **压测**：`python stress_test.py` 通过；跑多 seed（0/1/2/3）确认都能在新增逻辑下得到合法解、无回归超 Q。

---

## 6. 实施顺序（Implementation Order）

> #2 等待时间修复已在代码中（见 0.c）；借鉴 scheduler_before1 的方法（见 2.3）并入优先级高的调度步骤。本表从**当前真正缺失**的任务起排。

1. `scheduler.py` **跨 Lot 链整体锚定（3.1-A 结合 2.3-B/C/D）——核心**：
   - 移除 L1756 `and False`（启用手动延迟级联传播）。
   - 改写 L1819 `has_internal_pin` 全跳过：跨 lot 循环引用簇改为"整簇整体回退 + 用 Q-time 预算反推散开范围"（借鉴 2.3-B 的整链后移/重迭代）。
   - 手动延迟落在链内时，用"累积平移"把链内后续步骤整体带移（借鉴 2.3-D），再经 reference 传播到位（3.1-A）。
   - 在链放置决策点加入"全链设备可通行性前瞻 + 关键中间设备积压串行化"（借鉴 2.3-C），作为整链打开/整体锚定的前置判断。
2. `scheduler.py` **链内等待动态分配（2.3-A）**：把链步间固定 wait 升级为"按每条 Q-time 规则反推 max_gap、取最小并夹紧"；拆链前后缀及衔接处的 gap 一并应用（强化 #2）。
3. 手动复现用户场景验证 #1：#2：`PC1.UF-BAKE→15:54`、`PC2.UF-CURE→14:18` 后整链整体重排，real bake 不再靠前、无超 Q。
4. `optimizer_config.py` 新增 SA+Tabu 参数 + `groups()`（3.3）。
5. `optimizer.py` 叠加 SA+Tabu 细调层，消费新参数（3.2）。
6. `web/app.py`：`resolve_max_iterations` 由配置取（修 L253）+ 透传 SA+Tabu 参数 + POST 白名单 + 删除 /config 路由；`index.html` 标题改"排程优化参数配置"；删 `config.html`（3.4/3.5）。
7. 清理死文件（3.6，含删除 `scheduler_before1.py`）+ 全量回归 + 压测（3.7）。