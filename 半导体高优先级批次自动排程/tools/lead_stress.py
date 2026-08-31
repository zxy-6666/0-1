#!/usr/bin/env python3
"""lead（领导批次衔接）压力测试：多场景多算例，压测 lead 稳定性。

场景：
  A 单 lead、宽松设备（每步 2 台）→ 衔接应背靠背（gap 小）
  B lead 落在紧 Q 链内（UF-BAKE→PLASMA→DISPENSE→CURE，Q=240min）、设备 1 台竞争
  C 多个 lead（3 对）、共享设备竞争 → 无死锁/无缺步
  D lead + 手动 pin/delay → 尊重手动约束
  E 恶意配置（lead 成环 / 异构 step / 热启动靠后）→ 数据体检告警、不崩溃
  F 随机扰动（start_time/优先级/qty/设备池随机）→ 不崩溃、0 校验错误

检查：崩溃 / 校验错误 / 闸A 违背 / 缺步 / 衔接 gap 分布。
用法: python lead_stress.py [--cases N] [--seed S] [--quiet]
"""
import sys, os, random, time
from datetime import datetime, timedelta
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SCHED_TRACE", "0")

import logging
logging.getLogger("scheduler").setLevel(logging.CRITICAL)
logging.disable(logging.CRITICAL)

from models import Lot, FlowStep, QTimeConstraint, LotConstraint, LeadPair, ManualAdjust
from scheduler import schedule
from validation import validate_schedule
from data_loader import get_product_flow_map

BASE = datetime(2026, 9, 1, 9, 0)

FLOW = [
    FlowStep(product_name="PROD", step_number="10", step_name="BAKE", eqp_ids=["EQP-BAKE"]),
    FlowStep(product_name="PROD", step_number="20", step_name="PLASMA", eqp_ids=["EQP-PLASMA"]),
    FlowStep(product_name="PROD", step_number="30", step_name="DISPENSE", eqp_ids=["EQP-DISP"]),
    FlowStep(product_name="PROD", step_number="40", step_name="CURE", eqp_ids=["EQP-CURE"]),
]
# 宽松版：每步 2 台（场景 A）
FLOW_WIDE = [
    FlowStep(product_name="PROD", step_number="10", step_name="BAKE", eqp_ids=["EQP-BAKE1", "EQP-BAKE2"]),
    FlowStep(product_name="PROD", step_number="20", step_name="PLASMA", eqp_ids=["EQP-PLASMA1", "EQP-PLASMA2"]),
    FlowStep(product_name="PROD", step_number="30", step_name="DISPENSE", eqp_ids=["EQP-DISP1", "EQP-DISP2"]),
    FlowStep(product_name="PROD", step_number="40", step_name="CURE", eqp_ids=["EQP-CURE1", "EQP-CURE2"]),
]
CT = {("PROD", "10", 1): 30.0, ("PROD", "20", 1): 30.0, ("PROD", "30", 1): 30.0, ("PROD", "40", 1): 60.0}
Q_LOOSE = [
    QTimeConstraint("PROD", "BAKE", "PLASMA", "track in", "track out", 100000),
    QTimeConstraint("PROD", "PLASMA", "DISPENSE", "track in", "track out", 100000),
    QTimeConstraint("PROD", "DISPENSE", "CURE", "track in", "track out", 100000),
]
Q_TIGHT = [
    QTimeConstraint("PROD", "BAKE", "PLASMA", "track in", "track out", 480),
    QTimeConstraint("PROD", "PLASMA", "DISPENSE", "track in", "track out", 240),
    QTimeConstraint("PROD", "DISPENSE", "CURE", "track in", "track out", 240),
]
ST = [(8, 30), (20, 30)]


def mk_lot(name, start_delta, priority=(1, 1), qty=1, cur="BAKE"):
    return Lot(
        lot_name=name, carrier_id=f"C{name}", product_name="PROD", qty=qty,
        priority=priority, current_step_name=cur, target_step=None,
        lot_state="wait", running_time=0, start_time=BASE + start_delta, references=[],
        lead_pairs=[])


def attach_lead(follow, lead_lot, step="DISPENSE", lead_id="l0"):
    """用户视角：follow 为主点(跟随)，lead_lot 为领导(在前跑)。
    内部 LeadPair：lot1=lead_lot(领导)、step1=step；lot2=follow(跟随)、step2=step。"""
    follow.lead_pairs = []
    lead_lot.lead_pairs = [LeadPair(lead_lot.lot_name, step, follow.lot_name, step, lead_id)]
    follow.references = [LotConstraint(
        lot_name=follow.lot_name, reference_lot=lead_lot.lot_name,
        reference_step=step, start_step=step, start_mod=None, lead_id=lead_id)]


def run_sched(lots, flows, qtimes, manual_adjusts=None):
    return schedule(
        lots=lots, flows=flows, ct_lookup=CT, qtimes=qtimes, shift_times=ST,
        ftf_qty_change=None, special_lot_step_lookup=None, priority_wait_map={},
        eqp_constraints=[], step_time_window_constraints=[],
        shift_change_times=[], manual_adjusts=manual_adjusts or [],
        special_eqp_map={}, resolve_max_iterations=10)


def compute_gaps(le, lots):
    """返回每个 lead 的衔接 gap 分钟列表（内部：跟随 step2.start - 领导 step1.end）。"""
    by = {(e.lot_name, e.step_name): e for e in le}
    gaps = []
    for lot in lots:
        for lp in lot.lead_pairs or []:
            e1 = by.get((lp.lot1, lp.step1))
            e2 = by.get((lp.lot2, lp.step2))
            if e1 and e2:
                gaps.append((e2.start_time - e1.end_time).total_seconds() / 60.0)
    return gaps


def gen_scenario_A(rng):
    """单 lead、宽松设备、跟随批晚 0.5~4h。"""
    d = rng.uniform(0.5, 4.0)
    lead_lot = mk_lot("LEAD", timedelta(0), priority=(1, 1))
    follow = mk_lot("FOLLOW", timedelta(hours=d), priority=(3, 1))
    attach_lead(follow, lead_lot)
    return [lead_lot, follow], FLOW_WIDE, Q_LOOSE, None


def gen_scenario_B(rng):
    """紧 Q 链、设备 1 台竞争、跟随批晚 0~3h。"""
    d = rng.uniform(0.0, 3.0)
    lead_lot = mk_lot("LEAD", timedelta(0), priority=(1, 1))
    follow = mk_lot("FOLLOW", timedelta(hours=d), priority=(3, 1))
    attach_lead(follow, lead_lot)
    return [lead_lot, follow], FLOW, Q_TIGHT, None


def gen_scenario_C(rng):
    """3 对 lead、共享设备竞争。"""
    lots = []
    for i in range(3):
        d = rng.uniform(0.0, 2.0)
        ld = mk_lot(f"LEAD{i}", timedelta(hours=i * 6), priority=(1, 1))
        fl = mk_lot(f"FOLLOW{i}", timedelta(hours=i * 6 + d), priority=(3, 1))
        attach_lead(fl, ld, lead_id=f"l{i}")
        lots += [ld, fl]
    return lots, FLOW, Q_TIGHT, None


def gen_scenario_D(rng):
    """lead + 手动 pin/delay。"""
    lead_lot = mk_lot("LEAD", timedelta(0), priority=(1, 1))
    follow = mk_lot("FOLLOW", timedelta(hours=rng.uniform(0.5, 2.0)), priority=(3, 1))
    attach_lead(follow, lead_lot)
    ma = []
    if rng.random() < 0.5:
        ma.append(ManualAdjust(lead_lot.lot_name, "DISPENSE",
                               delay_to=BASE + timedelta(hours=4), mode="delay"))
    else:
        ma.append(ManualAdjust(follow.lot_name, "DISPENSE",
                               delay_to=BASE + timedelta(hours=5), mode="pin"))
    return [lead_lot, follow], FLOW_WIDE, Q_LOOSE, ma


def gen_scenario_E(rng):
    """恶意配置：随机选一种（成环 lead / 异构 step / 热启动靠后）。"""
    kind = rng.choice(["cycle", "hetero", "hotstart"])
    if kind == "cycle":
        a = mk_lot("A", timedelta(0), priority=(1, 1))
        b = mk_lot("B", timedelta(1), priority=(1, 1))
        a.lead_pairs = [LeadPair("A", "DISPENSE", "B", "DISPENSE", "c1")]
        b.lead_pairs = [LeadPair("B", "DISPENSE", "A", "DISPENSE", "c2")]
        a.references = [LotConstraint("A", "B", "DISPENSE", "DISPENSE", None, lead_id="c1")]
        b.references = [LotConstraint("B", "A", "DISPENSE", "DISPENSE", None, lead_id="c2")]
        return [a, b], FLOW_WIDE, Q_LOOSE, None
    if kind == "hetero":
        lead_lot = mk_lot("LEAD", timedelta(0))
        follow = mk_lot("FOLLOW", timedelta(1), priority=(3, 1))
        follow.lead_pairs = [LeadPair("LEAD", "NO-SUCH-STEP", "FOLLOW", "DISPENSE", "h1")]
        return [lead_lot, follow], FLOW_WIDE, Q_LOOSE, None
    # hotstart：跟随批已越过衔接步
    lead_lot = mk_lot("LEAD", timedelta(0))
    follow = mk_lot("FOLLOW", timedelta(1), priority=(3, 1), cur="CURE")
    attach_lead(follow, lead_lot)
    return [lead_lot, follow], FLOW_WIDE, Q_LOOSE, None


def gen_scenario_F(rng):
    """随机扰动（温和）：在 A/B/C 基础上随机 start_time/优先级/qty(1-2)/同池设备。"""
    base = rng.choice([gen_scenario_A, gen_scenario_B, gen_scenario_C])(rng)
    lots, flows, qtimes, ma = base
    # 同池设备：部分步骤从原设备池中随机选一台（保持池内合理设备）
    flows = [FlowStep(s.product_name, s.step_number, s.step_name, list(s.eqp_ids))
             for s in flows]
    for s in flows:
        if s.eqp_ids and rng.random() < 0.3:
            s.eqp_ids = [rng.choice(s.eqp_ids)]
    for lot in lots:
        lot.qty = rng.choice([1, 2])
        lot.priority = (rng.choice([1, 2, 3, 4]), 1)
        if lot.start_time:
            lot.start_time = lot.start_time + timedelta(hours=rng.uniform(-6, 6))
    return lots, flows, qtimes, ma


SCENARIOS = {
    "A_single_loose": gen_scenario_A,
    "B_tight_q": gen_scenario_B,
    "C_multi_lead": gen_scenario_C,
    "D_pin_delay": gen_scenario_D,
    "E_malicious": gen_scenario_E,
    "F_random": gen_scenario_F,
}


def main():
    args = sys.argv[1:]
    n_per = 60
    seed0 = 20260901
    quiet = False
    for i, a in enumerate(args):
        if a == "--cases" and i + 1 < len(args):
            n_per = int(args[i + 1])
        elif a == "--seed" and i + 1 < len(args):
            seed0 = int(args[i + 1])
        elif a == "--quiet":
            quiet = True

    summary = {}
    total_gap_buckets = Counter()
    for name, gen in SCENARIOS.items():
        rng = random.Random(seed0 + hash(name) % 100000)
        crash = 0
        err_total = 0
        gate_viol = 0
        missing = 0
        gap_all = []
        err_samples = []
        for k in range(n_per):
            seed = seed0 * 1000 + k
            r = random.Random(seed + hash(name) % 100000)
            try:
                lots, flows, qtimes, ma = gen(r)
            except Exception as e:
                crash += 1
                continue
            try:
                le, ee, qa = run_sched(lots, flows, qtimes, ma)
            except Exception as e:
                crash += 1
                continue
            cons = [c for l in lots for c in (l.references or [])]
            try:
                errors = validate_schedule(le, ee, qa, lots, flows, qtimes,
                                           lot_constraints=cons, shift_times=ST, special_eqp_map={})
            except Exception as e:
                crash += 1
                continue
            err_total += len(errors)
            for e in errors:
                if "闸A" in e:
                    gate_viol += 1
                if len(err_samples) < 3:
                    err_samples.append(e)
            # 缺步
            fm = get_product_flow_map(flows)
            done_steps = Counter(e.lot_name for e in le)
            for lot in lots:
                need = len(fm.get(lot.product_name, []))
                if done_steps.get(lot.lot_name, 0) < need:
                    missing += 1
            gap_all.extend(compute_gaps(le, lots))
        buckets = Counter()
        for g in gap_all:
            if g <= 30: buckets["<=30min"] += 1
            elif g <= 60: buckets["<=60min"] += 1
            elif g <= 120: buckets["<=120min"] += 1
            else: buckets[">120min"] += 1
        # E（恶意配置）预期有校验错误：只要求不崩溃、有体检告警（通过 _detect 输出）
        expect_errors = (name == "E_malicious")
        ok = crash == 0 and (expect_errors or err_total == 0)
        summary[name] = dict(crash=crash, err_total=err_total, gate_viol=gate_viol,
                             missing=missing, gaps=len(gap_all), buckets=dict(buckets),
                             samples=err_samples, ok=ok)
        for kk, vv in buckets.items():
            total_gap_buckets[kk] += vv
        flag = "OK" if ok else "FAIL"
        print(f"[{flag}] [{name}] cases={n_per} crash={crash} err_total={err_total} "
              f"gateA_viol={gate_viol} missing_lots={missing} gaps={len(gap_all)} "
              f"gap_dist={dict(buckets)}")
        for s in err_samples[:2]:
            print(f"    ERR: {s}")

    print("\n===== gap 总分布 =====")
    for k, v in total_gap_buckets.items():
        print(f"  {k}: {v}")
    print(f"通过场景: {sum(1 for n in SCENARIOS if summary[n]['ok'])}/{len(SCENARIOS)}")
    sys.exit(0 if all(summary[n]["ok"] for n in SCENARIOS) else 1)


if __name__ == "__main__":
    main()
