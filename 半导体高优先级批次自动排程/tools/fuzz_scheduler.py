#!/usr/bin/env python3
"""调度器模糊测试：对真实算例做大量小参数扰动，批量跑 schedule() + 全量校验，
找逻辑漏洞（崩溃 / 丢步骤 / 设备重叠 / 顺序错 / 引用违背 / 重复步骤 / 早于就绪）。

扰动维度（每个算例随机组合，见 mutate()）：
  - 优先级 / 起始时间偏移 / 数量 / 起跑点 current_step / running_time / target_step
  - 引用关系：增删 / 改 start_mod / 改 start_step / 改 reference_step
  - Q-time：缩放预算 / 换 track in-out
  - 流程设备：随机重指 eqp_ids / 换同池设备
  - 特殊设备 special_eqp：together / max_lots / max_qty / 删除
  - 手动调整：随机 delay_to / 随机步骤
  - hold_periods

用法: python fuzz_scheduler.py [--cases N] [--seed S] [--quiet]
"""
import sys, os, random, copy, time, logging
from datetime import datetime, timedelta
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SCHED_TRACE", "0")

from data_loader import (
    load_flow, load_step_ct, load_qtime, load_priority_wait, load_lot_list,
    load_lot_constraints, load_eqp_constraints, load_shift_change_times,
    load_step_time_windows, load_special_lot_step, load_ftf_qty_change,
    load_shift_config, load_special_eqp, build_ct_lookup, auto_repair_step_ct,
    get_product_flow_map, get_step_index_in_flow,
)
from models import (Lot, ManualAdjust, SpecialEqp, LotConstraint)
from scheduler import schedule
from validation import validate_schedule

logging.getLogger("scheduler").setLevel(logging.CRITICAL)
logging.disable(logging.CRITICAL)  # 全局抑制，覆盖 schedule() 内部的 setLevel(WARNING)

BASE = os.path.join(os.path.dirname(__file__), "data")
SCHEDULE_START = datetime(2026, 8, 17, 8, 0)


def load_base():
    flows = load_flow(os.path.join(BASE, "flow.csv"))
    fm = get_product_flow_map(flows)
    qtimes = load_qtime(os.path.join(BASE, "qtime.csv"))
    pw = load_priority_wait(os.path.join(BASE, "priority_wait.csv"))
    cts = auto_repair_step_ct(flows, load_step_ct(os.path.join(BASE, "step_ct.csv")), None)
    ct = build_ct_lookup(cts)
    ec = load_eqp_constraints(os.path.join(BASE, "eqp_constraint.csv"))
    sc = load_shift_change_times(os.path.join(BASE, "shift_change_time.csv"))
    sw = load_step_time_windows(os.path.join(BASE, "step_time_window.csv"))
    sls = load_special_lot_step(os.path.join(BASE, "special_lot_step.csv"))
    ftf = load_ftf_qty_change(os.path.join(BASE, "ftf_qty_change.csv"))
    scfg = load_shift_config(os.path.join(BASE, "shift_config.csv"))
    st = sorted(tuple(map(int, x.start_time_str.split(":"))) for x in scfg if getattr(x, "start_time_str", None))
    se = load_special_eqp(os.path.join(BASE, "special_eqp.csv"))
    real_lots = load_lot_list(os.path.join(BASE, "lot_list.csv"),
                              constraints_filepath=os.path.join(BASE, "lot_constraints.csv"))
    all_eqp = sorted({e for f in flows for e in f.eqp_ids if e != "-"})
    return dict(flows=flows, fm=fm, qtimes=qtimes, pw=pw, ct=ct, ec=ec, sc=sc,
                sw=sw, sls=sls, ftf=ftf, st=st, se=se, real_lots=real_lots, all_eqp=all_eqp)


def mutate(b, rng, benign=False):
    """生成一个扰动算例，返回 (lots, flows, qtimes, special_eqp_map, manual_adjusts, constraints_for_validate)
    benign=True 时关闭"制造不可行数据"的扰动（起跑点后移/目标截断/Q-time 收紧），
    仅做优先级/时间/数量/设备/引用模式等良性扰动——用于探测真实逻辑 bug（丢步骤/崩溃/引用违背）。
    """
    fm = b["fm"]
    # ---- 1. 流程：随机重指部分步骤设备 ----
    flows = []
    for fl in fm.values():
        for s in copy.deepcopy(fl):
            if rng.random() < 0.05:
                if rng.random() < 0.5:
                    s.eqp_ids = ["-"]  # 改为无设备步骤
                else:
                    s.eqp_ids = [rng.choice(b["all_eqp"])]
            elif rng.random() < 0.10:
                # 同池换设备（PKCON<->PKFCV 之类）
                if s.eqp_ids:
                    pool = [e for e in b["all_eqp"] if e not in s.eqp_ids]
                    if pool and rng.random() < 0.5:
                        s.eqp_ids = [rng.choice(pool)]
            flows.append(s)

    # ---- 2. lots：子集 + 参数扰动 ----
    # 子集选择：先随机取基批次，再补全其 reference 目标 lot（避免悬空引用——
    # 引用目标不在子集里会导致"缺失步骤"假阳性，掩盖真实丢步骤 bug）。
    base_lots = b["real_lots"]
    n_lots = rng.choice([1, 2, 2, 3, 3, 4, 4, 4])
    _chosen_names = set(l.lot_name for l in rng.sample(base_lots, min(n_lots, len(base_lots))))
    _grow = True
    while _grow:
        _grow = False
        for lot in base_lots:
            if lot.lot_name not in _chosen_names:
                continue
            for ref in lot.references or []:
                if ref.reference_lot and ref.reference_lot not in _chosen_names:
                    _chosen_names.add(ref.reference_lot)
                    _grow = True
    chosen = [l for l in base_lots if l.lot_name in _chosen_names]
    lots = []
    for lot in sorted(chosen, key=lambda x: x.lot_name):
        l = copy.deepcopy(lot)
        l.references = []
        if rng.random() < 0.30:
            l.priority = (rng.choice([1, 1, 2, 3]), rng.choice([1, 2, 3]))
        if rng.random() < 0.40:
            l.start_time = lot.start_time + timedelta(hours=rng.uniform(-8, 8))
        if rng.random() < 0.40:
            l.qty = rng.choice([1, 2, 4, 8, 13, 20, 25])
        # 起跑点：随机选一个靠前步骤（30%）（benign 关闭——后移会制造"引用源步骤在过去"的不可行数据）
        fl = fm[l.product_name]
        if not benign and rng.random() < 0.30:
            idx = rng.randint(0, min(len(fl) - 1, 30))
            l.current_step_name = fl[idx].step_name
            l.running_time = 0
        if rng.random() < 0.15:
            l.running_time = rng.randint(1, 600)
        if not benign and rng.random() < 0.20:
            i = get_step_index_in_flow(fl, l.current_step_name)
            j = rng.randint(i + 1, min(len(fl) - 1, i + 40))
            l.target_step = fl[j].step_name
        if rng.random() < 0.10:
            h0 = l.start_time + timedelta(hours=rng.uniform(2, 60))
            h1 = h0 + timedelta(hours=rng.uniform(1, 12))
            l.hold_periods = [(h0, h1)]
        # ---- 引用关系扰动 ----
        for ref in lot.references or []:
            if rng.random() < 0.30:
                continue  # 删掉该引用
            r = copy.deepcopy(ref)
            if rng.random() < 0.25:
                r.start_mod = rng.choice(["0", "shift", "shift_day", "1", "2", "-1", "0.5"])
            if not benign:
                # 改 start_step / reference_step（限制在各自流程内且落在剩余段，保持引用可解析）
                tgt_fl = fm.get(r.reference_lot)
                if tgt_fl and rng.random() < 0.15:
                    _tgt_lot = next((x for x in base_lots if x.lot_name == r.reference_lot), None)
                    _base = get_step_index_in_flow(tgt_fl, _tgt_lot.current_step_name) if _tgt_lot else 0
                    _pool = tgt_fl[_base:]
                    if _pool:
                        r.reference_step = rng.choice(_pool).step_name
                dep_fl = fm.get(l.product_name)
                if dep_fl and rng.random() < 0.15:
                    _base = get_step_index_in_flow(dep_fl, l.current_step_name)
                    _pool = dep_fl[_base:]
                    if _pool:
                        r.start_step = rng.choice(_pool).step_name
            l.references.append(r)
        lots.append(l)

    # 校验用的 constraints（从 lots.references 重建，保证一致）
    cons = []
    for l in lots:
        for ref in l.references or []:
            cons.append(LotConstraint(
                lot_name=l.lot_name, reference_lot=ref.reference_lot,
                reference_step=ref.reference_step, start_mod=ref.start_mod,
                start_step=ref.start_step, hold_periods=ref.hold_periods))

    # ---- 3. Q-time 扰动 ----
    qtimes = []
    for q in b["qtimes"]:
        qq = copy.deepcopy(q)
        if qq.product_name not in fm:
            continue
        if rng.random() < 0.45:
            # benign 模式不收紧预算（收紧会制造不可行链）
            _mul = rng.choice([1.0, 1.3, 1.6, 2.0]) if benign else rng.choice([0.5, 0.7, 1.0, 1.3, 1.6, 2.0])
            qq.max_duration = max(5, int(qq.max_duration * _mul))
        if rng.random() < 0.12:
            qq.start_mod, qq.end_mod = "track out", "track in"
        qtimes.append(qq)

    # ---- 4. 特殊设备扰动 ----
    se = {}
    for name, spec in b["se"].items():
        if rng.random() < 0.10:
            continue
        se[name] = SpecialEqp(name,
                              max_lots=rng.choice([2, 3, 4, 4, 5, 6]),
                              max_qty=rng.choice([15, 20, 25, 25, 40]),
                              together=rng.choice([True, False]) if rng.random() < 0.4 else spec.together)
    if rng.random() < 0.05:
        se = {}

    # ---- 5. 手动调整扰动 ----
    ma = []
    if rng.random() < 0.25:
        for _ in range(rng.randint(1, 2)):
            l = rng.choice(lots)
            fl = fm[l.product_name]
            step = rng.choice(fl).step_name
            ma.append(ManualAdjust(l.lot_name, step,
                                   delay_to=SCHEDULE_START + timedelta(hours=rng.uniform(24, 120)),
                                   mode=rng.choice(["delay", "delay", "pin"])))

    allf = flows
    return lots, allf, qtimes, se, ma, cons


def analyze(le, ee, qa, lots, flows, qtimes, cons, st, se, seed, params):
    """返回 (类别列表 [(category, detail)], 校验错误数)"""
    cats = []
    try:
        errors = validate_schedule(le, ee, qa, lots, flows, qtimes,
                                   lot_constraints=cons, shift_times=st, special_eqp_map=se)
    except Exception as e:
        return [("VALIDATE_EXC", f"{type(e).__name__}: {e}")], 0

    # 结构级增强检查
    by_lot = {}
    for e in le:
        by_lot.setdefault(e.lot_name, []).append(e)
    fm = get_product_flow_map(flows)
    for l in lots:
        es = by_lot.get(l.lot_name, [])
        if not es:
            continue
        # 重复步骤
        seen = {}
        for e in es:
            if e.step_name in seen:
                cats.append(("DUP_STEP", f"{l.lot_name} {e.step_name} 出现2次 "
                            f"({seen[e.step_name]:%m/%d %H:%M} & {e.start_time:%m/%d %H:%M})"))
            seen[e.step_name] = e.start_time
        # 早于就绪
        for e in es:
            if l.start_time and e.start_time < l.start_time - timedelta(minutes=1):
                cats.append(("BEFORE_READY", f"{l.lot_name} {e.step_name} 开始 {e.start_time:%m/%d %H:%M} 早于就绪 {l.start_time:%m/%d %H:%M}"))
        # 流程顺序错
        fl = fm.get(l.product_name)
        if fl:
            idx = {s.step_name: i for i, s in enumerate(fl)}
            order = [idx[e.step_name] for e in es if e.step_name in idx]
            if any(order[i] > order[i + 1] for i in range(len(order) - 1)):
                cats.append(("ORDER_VIOLATION", f"{l.lot_name} 步骤顺序错乱: "
                             f"{[es[k].step_name for k in range(len(es)) if k < len(es)-1 and order[k] > order[k+1]]}"))
    return cats, len(errors)


def main():
    args = [a for a in sys.argv[1:]]
    n_cases = 3000
    seed0 = 20260828
    quiet = False
    benign = False
    for i, a in enumerate(args):
        if a == "--cases" and i + 1 < len(args):
            n_cases = int(args[i + 1])
        elif a == "--seed" and i + 1 < len(args):
            seed0 = int(args[i + 1])
        elif a == "--quiet":
            quiet = True
        elif a == "--benign":
            benign = True

    b = load_base()
    rng = random.Random(seed0)
    t0 = time.time()

    totals = Counter()            # 类别 → 次数
    val_breakdown = Counter()     # 校验错误细分（前缀分类）
    err_samples = {}              # 校验错误类别 → [(seed, error_str)] 去重样本
    qtime_over_total = 0
    qtime_over_cases = 0
    crash_cases = 0
    anomalies = []                # (category, seed, detail, params_desc)
    q_over_details = []           # 超 Q 但结构合法且不崩溃的典型样本

    for k in range(n_cases):
        seed = seed0 * 1000 + k
        case_rng = random.Random(seed)
        try:
            lots, flows, qtimes, se, ma, cons = mutate(b, case_rng, benign=benign)
        except Exception as e:
            totals["MUTATE_EXC"] += 1
            anomalies.append(("MUTATE_EXC", seed, f"{type(e).__name__}: {e}", ""))
            continue
        params = f"seed={seed}"
        t_start = time.time()
        try:
            le, ee, qa = schedule(
                lots=lots, flows=flows, ct_lookup=b["ct"], qtimes=qtimes,
                shift_times=b["st"], ftf_qty_change=b["ftf"],
                special_lot_step_lookup=b["sls"], priority_wait_map=b["pw"],
                eqp_constraints=b["ec"], step_time_window_constraints=b["sw"],
                shift_change_times=b["sc"], manual_adjusts=ma,
                special_eqp_map=se, resolve_max_iterations=10)
        except Exception as e:
            totals["CRASH"] += 1
            crash_cases += 1
            anomalies.append(("CRASH", seed, f"{type(e).__name__}: {e}", params))
            continue
        dt = time.time() - t_start
        if dt > 10:
            totals["SLOW"] += 1
            anomalies.append(("SLOW", seed, f"{dt:.1f}s", params))

        cats, n_err = analyze(le, ee, qa, lots, flows, qtimes, cons, b["st"], se, seed, params)
        n_over = len([a for a in qa if a.status != "OK"])
        qtime_over_total += n_over
        if n_over:
            qtime_over_cases += 1

        if not cats:
            if n_err == 0 and n_over == 0:
                continue
        # 归类
        if cats:
            for c, d in cats:
                totals[c] += 1
                anomalies.append((c, seed, d, params))
        elif n_err:
            totals["VALIDATION"] += 1
            # 细分校验错误
            errs = validate_schedule(le, ee, qa, lots, flows, qtimes,
                                     lot_constraints=cons, shift_times=b["st"],
                                     special_eqp_map=se)
            for _e in errs:
                _cat = None
                if _e.startswith("Q-time"):
                    _cat = "Q-time超时"
                elif "缺失步骤" in _e:
                    _cat = "缺失步骤"
                elif _e.startswith("设备") and "重叠" in _e:
                    _cat = "设备重叠"
                elif _e.startswith("设备") and "并发" in _e:
                    _cat = "并发批次超限"
                elif _e.startswith("reference"):
                    _cat = "引用违背"
                elif "顺序" in _e:
                    _cat = "顺序错"
                else:
                    _cat = "其他"
                val_breakdown[_cat] += 1
                # 采集每类错误的前几个样本（去重）
                if len(err_samples.setdefault(_cat, [])) < 8 and _e not in err_samples[_cat]:
                    err_samples[_cat].append((seed, _e))
        if not cats and n_err == 0 and n_over == 0:
            continue

        if not quiet and (k + 1) % 200 == 0:
            print(f"  [{k+1}/{n_cases}] {time.time()-t0:.0f}s "
                  f"crash={crash_cases} anom={sum(totals.values())} qover_cases={qtime_over_cases}",
                  flush=True)

    print(f"\n===== 模糊测试完成: {n_cases} 个算例, 耗时 {time.time()-t0:.0f}s =====")
    print(f"Q-time 超时算例: {qtime_over_cases}/{n_cases} (总超时条目 {qtime_over_total})")
    print(f"\n[异常分类统计]")
    for cat, n in sorted(totals.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}")

    if val_breakdown:
        print(f"\n[校验错误细分（VALIDATION 内部）]")
        for cat, n in sorted(val_breakdown.items(), key=lambda x: -x[1]):
            print(f"  {cat}: {n}")

    if err_samples:
        print(f"\n[校验错误样本（每类去重前 8 条）]")
        for cat, samples in sorted(err_samples.items(), key=lambda x: -len(x[1])):
            print(f"\n  == {cat} ==")
            for seed, msg in samples:
                print(f"    seed={seed} {msg}")

    print(f"\n[样本明细（前 {min(25, len(anomalies))} 条）]")
    shown = 0
    for cat, seed, detail, params in anomalies:
        if shown >= 25:
            break
        if cat in ("VALIDATION",):
            continue
        print(f"  [{cat}] seed={seed} {detail}")
        shown += 1


if __name__ == "__main__":
    main()
