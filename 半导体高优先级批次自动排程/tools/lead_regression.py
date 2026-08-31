"""lead（领导批次衔接 / back-to-back）回归测试。

合成产品链：BAKE → PLASMA → DISPENSE → CURE（连续 Q-time），两个同产品批：
- LEAD（领导批）与 FOLLOW（配套批）；
- lead 声明：LEAD.DISPENSE 领导，FOLLOW.DISPENSE 尾随（背靠背）。

Phase 1 验证（闸A 不变量 + 不超 Q，通过复用普通引用机制承载）：
- 全部步骤排完、无超 Q、validation 0 错误；
- 闸A：FOLLOW.DISPENSE.start >= LEAD.DISPENSE.end（配套不超前）。
- 引用环检测不把 lead 内部边计入（无"引用环"误告警）。

运行：python tools/lead_regression.py
"""
import sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import Lot, FlowStep, QTimeConstraint, LotConstraint, LeadPair
from scheduler import schedule
from validation import validate_schedule

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}  {detail}")


def make_ctx():
    flow = [
        FlowStep(product_name="PROD", step_number="10", step_name="BAKE", eqp_ids=["EQP-BAKE"]),
        FlowStep(product_name="PROD", step_number="20", step_name="PLASMA", eqp_ids=["EQP-PLASMA"]),
        FlowStep(product_name="PROD", step_number="30", step_name="DISPENSE", eqp_ids=["EQP-DISP"]),
        FlowStep(product_name="PROD", step_number="40", step_name="CURE", eqp_ids=["EQP-CURE"]),
    ]
    ct = {
        ("PROD", "10", 1): 60.0, ("PROD", "20", 1): 120.0,
        ("PROD", "30", 1): 90.0, ("PROD", "40", 1): 300.0,
    }
    qtimes = [
        # Phase 1 用宽松预算，隔离验证"闸A 不变量 + 机制接线（loader→scheduler→validation）"
        # 紧 Q 下的背靠背对齐由 Phase 2 倒排回拉实现（见设计文档 §4.5/§4.6）
        QTimeConstraint("PROD", "BAKE", "PLASMA", "track in", "track out", 100000),
        QTimeConstraint("PROD", "PLASMA", "DISPENSE", "track in", "track out", 100000),
        QTimeConstraint("PROD", "DISPENSE", "CURE", "track in", "track out", 100000),
    ]
    return flow, ct, qtimes


def make_lot(name, start):
    return Lot(
        lot_name=name, carrier_id=f"C{name}", product_name="PROD", qty=1,
        priority=(1, 1), current_step_name="BAKE", target_step=None,
        lot_state="wait", running_time=0, start_time=start, references=[],
    )


def run(flow, ct, qtimes, lots):
    st = [(9, 0), (17, 0)]
    return schedule(
        lots=lots, flows=flow, ct_lookup=ct, qtimes=qtimes, shift_times=st,
        ftf_qty_change=None, special_lot_step_lookup=None,
        priority_wait_map={}, eqp_constraints=[], step_time_window_constraints=[],
        shift_change_times=[], manual_adjusts=[], special_eqp_map={},
        resolve_max_iterations=10)


def test_lead_invariant():
    """闸A + 不超 Q：FOLLOW.DISPENSE 不早于 LEAD.DISPENSE 完成；各段 Q 不超。"""
    print("\n=== test_lead_invariant ===")
    flow, ct, qtimes = make_ctx()
    BASE = datetime(2026, 9, 1, 9, 0)
    # 让 FOLLOW 设备空闲、早排；le这样若不住闸，FOLLOW 会超前于 LEAD，验证闸A 生效
    lead_lot = make_lot("LEAD", BASE)
    follow_lot = make_lot("FOLLOW", BASE)
    lead_lot.start_time = BASE
    # 有意让 FOLLOW 的"当前步骤/BASE 相同"，若不施加 lead，FOLLOW 会与 LEAD 同时甚至更早 DISPENSE
    lead_lot.lead_pairs = [LeadPair("LEAD", "DISPENSE", "FOLLOW", "DISPENSE", "lead0")]
    follow_lot.references = [
        LotConstraint(lot_name="FOLLOW", reference_lot="LEAD",
                      reference_step="DISPENSE", start_step="DISPENSE",
                      start_mod=None, lead_id="lead0"),
    ]
    le, ee, qa = run(flow, ct, qtimes, [lead_lot, follow_lot])
    errs = validate_schedule(le, ee, qa, [lead_lot, follow_lot], flow, qtimes)

    def find(lot, step):
        for e in le:
            if e.lot_name == lot and e.step_name == step:
                return e
        return None

    l_disp = find("LEAD", "DISPENSE")
    f_disp = find("FOLLOW", "DISPENSE")
    _needed = {k: [s.step_name for s in flow] for k in ["LEAD", "FOLLOW"]}
    check("lead: 两批 4 步齐全",
          all(all(find(k, s) is not None for s in steps)
              for k, steps in _needed.items()),
          f"le={[(e.lot_name, e.step_name) for e in le]}")
    check("lead: 全量校验 0 错误", len(errs) == 0, str(errs[:3]))
    if l_disp and f_disp:
        check("lead: 闸A FOLLOW.DISPENSE.start >= LEAD.DISPENSE.end",
              f_disp.start_time >= l_disp.end_time,
              f"FOLLOW.start={f_disp.start_time} LEAD.end={l_disp.end_time}")
    else:
        check("lead: DISPENSE 均已排", False, f"LEAD={l_disp} FOLLOW={f_disp}")


def test_lead_no_ring_warning():
    """引用环检测不把 lead 内部边计入：LEAD↔FOLLOW 的 lead 不触发"引用环"告警。"""
    print("\n=== test_lead_no_ring_warning ===")
    from scheduler import _detect_schedule_anomalies
    flow, ct, qtimes = make_ctx()
    BASE = datetime(2026, 9, 1, 9, 0)
    lead_lot = make_lot("LEAD", BASE)
    follow_lot = make_lot("FOLLOW", BASE)
    lead_lot.lead_pairs = [LeadPair("LEAD", "DISPENSE", "FOLLOW", "DISPENSE", "lead0")]
    follow_lot.references = [
        LotConstraint(lot_name="FOLLOW", reference_lot="LEAD",
                      reference_step="DISPENSE", start_step="DISPENSE",
                      start_mod=None, lead_id="lead0"),
    ]
    warnings = _detect_schedule_anomalies([], [lead_lot, follow_lot], {}, {}, {})
    ring = [w for w in warnings if "引用环" in w]
    check("lead: 无'引用环'误告警", not ring, str(ring))


def _make_backshift_ctx():
    """清洁背靠背场景：每步 2 台设备（两批各占一台、无跨批争用）、CT 小、Q 预算宽松。
    FOLLOW 延后 2h 开始 → 制造可被回拉闭合的空带。"""
    import copy as _c
    from datetime import timedelta as _td
    if False:
        _c, _td  # noqa: F841  # 保留导入锚点（与调用方一致）
    flow = [
        FlowStep(product_name="PROD", step_number="10", step_name="BAKE",
                 eqp_ids=["EQP-BAKE1", "EQP-BAKE2"]),
        FlowStep(product_name="PROD", step_number="20", step_name="PLASMA",
                 eqp_ids=["EQP-PLASMA1", "EQP-PLASMA2"]),
        FlowStep(product_name="PROD", step_number="30", step_name="DISPENSE",
                 eqp_ids=["EQP-DISP1", "EQP-DISP2"]),
        FlowStep(product_name="PROD", step_number="40", step_name="CURE",
                 eqp_ids=["EQP-CURE1", "EQP-CURE2"]),
    ]
    ct = {("PROD", "10", 1): 30.0, ("PROD", "20", 1): 30.0,
          ("PROD", "30", 1): 30.0, ("PROD", "40", 1): 60.0}
    qtimes = [
        QTimeConstraint("PROD", "BAKE", "PLASMA", "track in", "track out", 480),
        QTimeConstraint("PROD", "PLASMA", "DISPENSE", "track in", "track out", 480),
        QTimeConstraint("PROD", "DISPENSE", "CURE", "track in", "track out", 480),
    ]
    BASE = datetime(2026, 9, 1, 9, 0)

    def mk(name, extra):
        st = BASE + extra
        return Lot(lot_name=name, carrier_id=f"C{name}", product_name="PROD", qty=1,
                   priority=(1, 1), current_step_name="BAKE", target_step=None,
                   lot_state="wait", running_time=0, start_time=st, references=[])

    lead = mk("LEAD", _td(minutes=0))
    follow = mk("FOLLOW", _td(hours=2))
    lead.lead_pairs = [LeadPair("LEAD", "DISPENSE", "FOLLOW", "DISPENSE", "lead0")]
    follow.references = [LotConstraint(
        lot_name="FOLLOW", reference_lot="LEAD", reference_step="DISPENSE",
        start_step="DISPENSE", start_mod=None, lead_id="lead0")]
    return _c.deepcopy, flow, ct, qtimes, lead, follow


def test_lead_back_shift():
    """Pass B 回拉：lot1.step1 完成与 lot2.step2 开始背靠背（gap→0），且不超 Q、0 校验错误。"""
    print("\n=== test_lead_back_shift ===")
    import copy as _copy
    from datetime import timedelta as _td
    _mkctx = _make_backshift_ctx
    _dcopy, flow, ct, qtimes, lead, follow = _make_backshift_ctx()

    def find(ls, lot, step):
        for e in ls:
            if e.lot_name == lot and e.step_name == step:
                return e
        return None

    # 对照组：关闭 back-shift（清空 lead_pairs），仅保留闸A 引用
    lead_nb = _copy.deepcopy(lead); lead_nb.lead_pairs = []
    follow_nb = _copy.deepcopy(follow)
    le0, ee0, qa0 = run(flow, ct, qtimes, [lead_nb, follow_nb])
    gap0 = (find(le0, "FOLLOW", "DISPENSE").start_time
            - find(le0, "LEAD", "DISPENSE").end_time).total_seconds() / 60.0

    # 实验组：带 back-shift
    le, ee, qa = run(flow, ct, qtimes, [lead, follow])
    errs = validate_schedule(le, ee, qa, [lead, follow], flow, qtimes)
    l_disp = find(le, "LEAD", "DISPENSE")
    f_disp = find(le, "FOLLOW", "DISPENSE")
    gap = (f_disp.start_time - l_disp.end_time).total_seconds() / 60.0

    check("backshift: 回拉后背靠背 (gap<=2min)",
          gap <= 2.0, f"gap={gap}min (对照组={gap0}min)")
    check("backshift: 比对照组更贴近 (gap < gap0)",
          gap < gap0, f"{gap} vs {gap0}")
    check("backshift: 闸A FOLLOW.DISPENSE.start >= LEAD.DISPENSE.end",
          f_disp.start_time >= l_disp.end_time,
          f"FOLLOW.start={f_disp.start_time} LEAD.end={l_disp.end_time}")
    check("backshift: 全量校验 0 错误", len(errs) == 0, str(errs[:3]))


def test_lead_health_check():
    """数据体检（设计文档 §3.2）：存在性 / 流程异构 / lead-引用成环 / 热启动太靠后。"""
    print("\n=== test_lead_health_check ===")
    import copy as _copy
    from data_loader import health_check_lead, get_product_flow_map
    _dcopy, flow, ct, qtimes, lead, follow = _make_backshift_ctx()
    flow_map = get_product_flow_map(flow)

    def warn_texts(lots):
        return health_check_lead(lots, flow_map)

    # —— 1. 干净配置：体检 0 告警 ——
    ok = warn_texts([_copy.deepcopy(lead), _copy.deepcopy(follow)])
    check("health: 干净 lead 0 告警", not ok, str(ok))

    # —— 2. 存在性：配套批 missing ——
    only_lead = [_copy.deepcopy(lead)]            # 缺 follow
    only_lead[0].lead_pairs = [LeadPair("LEAD", "DISPENSE", "FOLLOW2", "DISPENSE", "l1")]
    o = warn_texts(only_lead)
    check("health: 配套批缺失告警", any("在 lot_list 中不存在" in w and "FOLLOW2" in w for w in o), str(o))

    # —— 3. 流程异构：衔接步不在流程 ——
    h = _copy.deepcopy(lead)
    h.lead_pairs = [LeadPair("LEAD", "NO-SUCH-STEP", "FOLLOW", "DISPENSE", "l2")]
    o = warn_texts([h, _copy.deepcopy(follow)])
    check("health: 流程异构告警", any("不存在衔接步" in w and "NO-SUCH-STEP" in w for w in o), str(o))

    # —— 4. lead+引用成环：A lead 领导 B，B 普通引用等 A ——
    a = Lot(lot_name="A", carrier_id="CA", product_name="PROD", qty=1,
            priority=(1, 1), current_step_name="BAKE", target_step=None,
            lot_state="wait", running_time=0, start_time=None, references=[])
    b = Lot(lot_name="B", carrier_id="CB", product_name="PROD", qty=1,
            priority=(1, 1), current_step_name="PLASMA", target_step=None,
            lot_state="wait", running_time=0, start_time=None, references=[])
    # 构成成环：a 普通引用等 b（边 A→B）+ b lead 尾随 a（LeadPair(lot1=A,lot2=B) → 边 B→A）
    a.references = [LotConstraint(lot_name="A", reference_lot="B",
                                  reference_step="DISPENSE", start_step="CURE",
                                  start_mod=None, lead_id="")]
    b.lead_pairs = [LeadPair("A", "DISPENSE", "B", "DISPENSE", "l3")]  # lot2=B 尾随 lot1=A → 边 B→A
    o = warn_texts([a, b])
    check("health: lead+引用成环告警", any("成环" in w for w in o), str(o))

    # —— 5. 热启动太靠后：lot1 当前已越过 step1 ——
    hs = _copy.deepcopy(lead)
    hs.current_step_name = "CURE"              # 已排到 CURE（step1=DISPENSE 之前没排）
    hs.lead_pairs = [LeadPair("LEAD", "DISPENSE", "FOLLOW", "DISPENSE", "l4")]
    o = warn_texts([hs, _copy.deepcopy(follow)])
    check("health: 热启动太靠后告警", any("回拉失效" in w and "LEAD" in w for w in o), str(o))


def test_lead_loader_role_swap():
    """loader 角色反转（用户视角）：五列声明 lot1=主点、lot2=leading lot。
    mod=lead 时：闸A 挂到跟随批 lot1（等 lot2 的 step2 完成）；
    LeadPair 内部交换为 lot1=用户声明的 lot2（领导批）、lot2=用户声明的 lot1（跟随批）。"""
    print("\n=== test_lead_loader_role_swap ===")
    import tempfile, os
    import data_loader as dl
    csv = ("lot1\tstep1\tlot2\tstep2\tmod\n"
           "LEAD\tDISPENSE\tFOLLOW\tDISPENSE\tlead\n")
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    f.write(csv); f.close()
    try:
        cs = dl.load_lot_constraints(f.name)
        lps = list(dl.LEAD_PAIRS)
    finally:
        os.unlink(f.name)

    # 闸A：跟随批 LEAD.DISPENSE 等 领导批 FOLLOW.DISPENSE 完成
    gate = [c for c in cs if c.lot_name == "LEAD" and c.lead_id]
    check("loader: 闸A 挂到跟随批 lot1(LEAD) 等 领导批 FOLLOW.DISPENSE",
          len(gate) == 1 and gate[0].reference_lot == "FOLLOW"
          and gate[0].reference_step == "DISPENSE"
          and gate[0].start_step == "DISPENSE", str(gate))

    # LeadPair 内部交换：lot1=FOLLOW(领导批)、lot2=LEAD(跟随批)
    check("loader: LeadPair 内部交换 (lot1=FOLLOW领导, lot2=LEAD跟随)",
          len(lps) == 1 and lps[0].lot1 == "FOLLOW" and lps[0].step1 == "DISPENSE"
          and lps[0].lot2 == "LEAD" and lps[0].step2 == "DISPENSE", str(lps))

    # 交换后经 scheduler 排程：跟随批 LEAD.DISPENSE 不早于 领导批 FOLLOW.DISPENSE 完成（闸A）
    _dcopy, flow, ct, qtimes, lead, follow = _make_backshift_ctx()
    # 用户视角：LEAD=主点(跟随)，FOLLOW=leading(领导在前跑)；按 loader 内部角色构造
    lead_in = _dcopy(lead)    # LEAD
    follow_in = _dcopy(follow)  # FOLLOW
    lead_in.lead_pairs = []    # LEAD 是跟随批，不挂 lead_pairs
    follow_in.lead_pairs = [LeadPair("FOLLOW", "DISPENSE", "LEAD", "DISPENSE", "r0")]
    lead_in.references = [LotConstraint(lot_name="LEAD", reference_lot="FOLLOW",
                                        reference_step="DISPENSE", start_step="DISPENSE",
                                        start_mod=None, lead_id="r0")]
    le, ee, qa = run(flow, ct, qtimes, [lead_in, follow_in])
    errs = validate_schedule(le, ee, qa, [lead_in, follow_in], flow, qtimes)
    l_disp = next(e for e in le if e.lot_name == "LEAD" and e.step_name == "DISPENSE")
    f_disp = next(e for e in le if e.lot_name == "FOLLOW" and e.step_name == "DISPENSE")
    check("loader: 反转后排程 0 错误", len(errs) == 0, str(errs[:3]))
    check("loader: 反转后闸A LEAD.DISPENSE.start >= FOLLOW.DISPENSE.end",
          l_disp.start_time >= f_disp.end_time,
          f"LEAD.start={l_disp.start_time} FOLLOW.end={f_disp.end_time}")


def test_lead_upstream_edge():
    """lead 自动补全两条内部边（等效用户双向引用）：
    闸A（跟随批 step2 等 领导批 step1 完成）+ 上游对齐（领导批 step1 等 跟随批 step2
    的紧邻上一步完成）→ 领导批不跑太快，两批衔接步真正背靠背。"""
    print("\n=== test_lead_upstream_edge ===")
    import copy as _copy
    from scheduler import _inject_lead_upstream_refs
    from data_loader import get_product_flow_map
    _dcopy, flow, ct, qtimes, lead, follow = _make_backshift_ctx()
    # 用户视角：LEAD=主点(跟随)，FOLLOW=leading(领导在前跑)
    # 内部 LeadPair：lot1=FOLLOW(领导)、step1=DISPENSE；lot2=LEAD(跟随)、step2=DISPENSE
    lead.lead_pairs = []        # 清掉 ctx 默认（LEAD 领导）
    lead.references = []        # 清掉 ctx 默认闸A
    follow.lead_pairs = [LeadPair("FOLLOW", "DISPENSE", "LEAD", "DISPENSE", "u0")]
    lead.references = [LotConstraint(lot_name="LEAD", reference_lot="FOLLOW",
                                     reference_step="DISPENSE", start_step="DISPENSE",
                                     start_mod=None, lead_id="u0")]
    flow_map = get_product_flow_map(flow)
    _inject_lead_upstream_refs([follow, lead], flow_map)
    up = [r for r in follow.references or [] if r.lead_id.endswith("-u")]
    check("upstream: 领导批补上游对齐边 (FOLLOW.DISPENSE 等 LEAD.PLASMA)",
          len(up) == 1 and up[0].reference_lot == "LEAD"
          and up[0].reference_step == "PLASMA"
          and up[0].start_step == "DISPENSE", str(up))

    le, ee, qa = run(flow, ct, qtimes, [lead, follow])
    errs = validate_schedule(le, ee, qa, [lead, follow], flow, qtimes)
    f_disp = next(e for e in le if e.lot_name == "FOLLOW" and e.step_name == "DISPENSE")
    l_disp = next(e for e in le if e.lot_name == "LEAD" and e.step_name == "DISPENSE")
    l_pla = next(e for e in le if e.lot_name == "LEAD" and e.step_name == "PLASMA")
    check("upstream: 排程 0 错误", len(errs) == 0, str(errs[:3]))
    check("upstream: 领导批 DISPENSE 不早于跟随批 PLASMA 完成（上游对齐生效）",
          f_disp.start_time >= l_pla.end_time,
          f"FOLLOW.DISPENSE.start={f_disp.start_time} LEAD.PLASMA.end={l_pla.end_time}")
    gap = (f_disp.start_time - l_disp.end_time).total_seconds() / 60.0
    check("upstream: 背靠背 gap<=2min", gap <= 2.0, f"gap={gap}min")


def main():
    test_lead_invariant()
    test_lead_no_ring_warning()
    test_lead_back_shift()
    test_lead_health_check()
    test_lead_loader_role_swap()
    test_lead_upstream_edge()
    print(f"\nSUMMARY: {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()