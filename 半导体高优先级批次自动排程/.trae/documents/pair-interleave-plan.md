# 彻底成对重排（pair-interleave）：末步共享设备的背靠背收敛

> **For agentic workers:** 本计划针对 P_molding_multi_pair 场景的结构性 >120min 间隙。
> 由主会话按任务逐项实现，每一步用 pressure-test 验证，不引入回归。

**Goal:** 把"末步共享设备"上多对 lead 的槽位序列从"先全部 leader、再全部 follower"
交错成每对背靠背（L0,F0,L1,F1,…），并反推 follower 的上游链前移，使间隙收敛到冗余带(≤30min)。
**Architecture:** 在 `_lead_back_shift` 之后新增独立后置 pass `_pair_interleave`：
不动多数派槽位，只对"成对碎裂"的同类共享设备做交错重排 + 反推重定时，硬校验所有 Q / 闸A /
停机窗，任一槽位不可行即整体回滚该组，保证"绝不再引入 Q 超时"。
**Tech Stack:** 复用现有 `_max_forward_shift` / `_partial_later_ok` / `_down_ok` /
Q 起终点语义等原语；仅新增一个 pass 与两个纯函数。

---

## 背景与根因（已确认）

压力测试 P 场景（`FLOW_MOLD`: DAF-BAKE→WARP-MEAS→MD-PLASMA→MD-MOLDING，
Q: DAF-BAKE→MD-MOLDING 1440、MD-PLASMA→MD-MOLDING 240，EQP-MOLD 停机 22:00-08:30）
的一个典型 case，EQP-MOLD 占用序列为：

```
L0 08:30-10:10 | L1 10:15-11:55 | L2 11:58-13:38 | F0 13:38-15:18 | F1 15:18-16:58 | F2 16:58-18:38
```

- F0 与 L0 间隙 208min（累计了它前面所有 leader 的 CT）。
- 根因：分发把同一步骤按 "leader 先、follower 后" 排，follower 的整条上游链也
  （DAF-BAKE 11:03、WARP 12:53、PLASMA 13:23）全排在 leader 之后。
- 结论：仅调换 MOLDING 槽位不够，F0 的 MOLDING 若提前到 10:10 会早于其自身 PLASMA(13:23)，
  必须**反推 F0 整条链**（复用 EQP-BAKE 07:40-09:20 空闲槽 → WARP 09:30 → PLASMA 10:00 → MOLDING 10:10）。
  Q: DAF-BAKE→MOLDING≈150min、PLASMA→MOLDING≈5min，均满足。

---

## 算法（`_pair_interleave` 后置 pass）

对每组"同末步 S + 同设备 + 成对碎裂"的 lead 组：

1. **检测**：收集设备 D 上步骤 S 的条目 `slots`。对每个 lead 对
   (`lot1=leader, step1=S`, `lot2=follower, step2=S`)，若 `follower.S.start − leader.S.end > TOL`
   记为碎裂。组内若有碎裂 → 进入重排。
2. **目标序**：pair 按各自 leader.S.start 排序，发射序列 `[L0,F0,L1,F1,…]`。
3. **槽位重定时**：以组内**最早 slot 的原 start 为锚**（不提前首槽），按 CT 顺推每个 slot
   `start/end`，跨过停机窗（重叠则跳到下个白班起点）。得到每个 lot 的 S 目标 `(t_s, t_e)`。
4. **反推 follower 整链**：对每个目标提前的 follower，从 S 反推到首步，逐上游步在其设备上
   取"≤ 目标时刻的最晚空闲 + 本步 CT"的可行时间（backward restack），硬约束：
   - 停在首步不得早于 `lot.start_time`（释放时刻）；
   - 任意相邻步 Q（含 DAF-BAKE→MD-MOLDING 1440 / MD-PLASMA→MD-MOLDING 240）不得超；
   - 不得落入停机窗。
   任一不满足 → 该组**整体回滚**（恢复原 le/ee），不消化。
5. **静态槽位前移/后移处理**：
   - follower 必须前移（反推 fail 即回滚）；
   - leader 若目标后移：校验其**入向 Q**（`前一步→S` 按 Q 起终点语义）不超；且其后移不破坏
     与自身上游的先后；否则回滚。
6. **提交**：仅当整组全部槽位与反推均可行，一次性写入 le/ee 并 `continue`。

**安全兜底**：任何 Q 超时 / 闸A 倒序 / 停机窗 / 优先级违背 → 整组回滚。绝不引入新 Q 错误
（即使 user 已授权"可能击穿"，实现仍以"不新增 Q"为硬边界，收敛到 Q 允许的最大程度）。

---

## 数据结构与依赖

- 现有原语（已存在，直接复用）：
  - `_max_forward_shift(entries, max_shift, other_int, lower, step_min, down_check)`
  - `_partial_later_ok(shift_entries, shift_d, other_int, wend)`
  - `_down_ok(entry, ns, ne)` / `_down_map`、`_other_intervals(lot)`
  - Q 起终点语义（与 validation 一致）：`q_start = start步.end if start_mod=="track out" else .start`，
    `q_end = end步.end if end_mod=="track out" else .start`。
- 新增：
  - `_pair_interleave(lots, lot_entries, eqp_entries, flows, window_end, eqp_constraints, schedule_start, qtimes)`
  - `_rollback_free_slots_devices(...)`（纯函数，见 Task 1）——不新增字段。

---

## 验证指标（每个 Task 后运行）

```
cd /workspace/半导体高优先级批次自动排程
python tools/lead_stress.py   # P 的 >120min gaps 应显著下降；H/B/J 等已通过场景不得回归
```

- P_molding_multi_pair：`>120min` 从 193 明显下降；err_total 下降。
- 回归守卫：A/B/C/D/G/H/I/K/L/M/O 必须仍 `[OK]`（0 error）；J/N 结构指标（闸A=0、crash=0、
  Qerr=0）不得变差。