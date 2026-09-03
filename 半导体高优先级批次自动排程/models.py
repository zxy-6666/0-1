"""数据模型定义"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class LotConstraint:
    """Lot 启动约束（从 lot_constraints.csv 加载，一个 Lot 可有多条）

    五列声明格式：lot_name | start_step | reference_lot | reference_step | start_mod
    （语义映射见 lead 设计文档 §3.0：lot1→lot_name, step1→start_step,
    lot2→reference_lot, step2→reference_step, mod→start_mod）
    """
    lot_name: str
    reference_lot: Optional[str] = None
    reference_step: Optional[str] = None
    start_mod: Optional[str] = None       # n(小时), shift, shift_day, 空(立即), lead(领导衔接)
    start_step: Optional[str] = None      # 满足 reference 条件后，该 step 从计算时间开始；之前的 step 从 start_time 开始
    lead_id: str = ""                     # 非空=由 lead 行自动生成的内部引用边（铅级配套），
                                          # 环检测/死锁判定应跳过；其余参与正常引用逻辑
    hold_periods: list[tuple[Optional[datetime], Optional[datetime]]] = field(default_factory=list)


@dataclass
class LeadPair:
    """lead（领导批次衔接 / back-to-back）关系。

    语义：lot2 的 step2 **尾随** lot1 的 step1（背靠背），且 lot1 上游链按 Q-time
    回拉对齐，确保不超任何区间 Q-time、不在紧 Q 窗口内空等。
    - 闸A：lot2.step2.start >= lot1.step1.end（配套不超前）——由挂在 lot2 上、
      带 lead_id 的内部引用边承载。
    - 回拉：以 lot2.step2.start 为锚，把 lot1 的 step1 及其入向 Q 段倒排对齐。
    """
    lot1: str                 # 领导批（lot_name）
    step1: str                # lot1 的衔接 step（start_step）
    lot2: str                 # 配套批（reference_lot）
    step2: str                # lot2 的对应衔接 step（reference_step）
    lead_id: str = ""


@dataclass
class Lot:
    """批次"""
    lot_name: str
    priority: tuple[int, int]  # (外部优先级, 内部优先级)
    qty: int
    current_step_name: str     # 当前所在的 step_name
    product_name: str
    target_step: Optional[str] = None  # None 表示排到底
    lot_state: str = ""        # running / wait / hold / "" (空闲)
    running_time: int = 0      # 当前步骤已运行时间(分钟)
    carrier_id: str = ""       # 载具 ID（历史遗留列，已在 lot_list.csv 移除，保留字段兼容旧数据/工具脚本）
    # 约束字段（从 lot_constraints.csv 合并而来）
    references: list[LotConstraint] = field(default_factory=list)  # 多 reference 依赖
    lead_pairs: list[LeadPair] = field(default_factory=list)       # 本 lot 作为领导批(lead 侧重)的衔接关系
    start_time: Optional[datetime] = None  # 绝对开始时间（取最早）
    start_step: Optional[str] = None       # 满足 reference 条件后，该 step 从计算时间开始
    hold_periods: list[tuple[Optional[datetime], Optional[datetime]]] = field(default_factory=list)  # 多段Hold（合并所有）
    planned_end: Optional[datetime] = None  # 计划完成时间（交期，可选）
    pioneer: bool = False                 # 先导批：跑在前面起"扫雷"作用；其完工时间不计入目标得分（delay 无谓），但 Q-time 等硬约束仍生效


@dataclass
class FlowStep:
    """流程步骤"""
    product_name: str
    step_number: str           # "10.002"
    step_name: str
    stage_name: str = ""
    eqp_ids: list[str] = field(default_factory=list)  # 空列表 = 不需要设备


@dataclass
class StepCT:
    """步骤CT"""
    product_name: str
    step_number: str
    step_name: str
    qty: int
    step_ct: float             # 分钟


@dataclass
class QTimeConstraint:
    """Q-time 约束"""
    product_name: str
    start_step: str            # step_name
    end_step: str              # step_name
    start_mod: str             # "track in" | "track out"
    end_mod: str               # "track in" | "track out"
    max_duration: int          # 分钟


@dataclass
class ScheduleEntry:
    """Lot 维度排程条目"""
    lot_name: str
    priority: str              # "2-1"
    product_name: str
    step_number: str
    step_name: str
    eqp_id: str                # "-" 表示无需设备
    start_time: datetime
    end_time: datetime
    ct: float
    qtime_risk: str            # "OK" / "RISK: 超时XXmin" / "-"
    stage_name: str = ""


@dataclass
class EqpScheduleEntry:
    """设备维度排程条目"""
    eqp_id: str
    start_time: datetime
    end_time: datetime
    lot_name: str
    step_name: str
    qty: int


@dataclass
class QTimeAlert:
    """Q-time 风险告警"""
    lot_name: str
    qtime_rule: str            # "10.002→10.003"
    start_time: datetime
    deadline: datetime
    actual_end: datetime
    over_minutes: int
    status: str                # "OK" / "超时"


@dataclass
class SpecialLotStep:
    """Lot级特殊CT/设备覆盖"""
    lot_name: str
    step_name: str
    special_ct: Optional[float] = None     # 分钟，None 表示使用原 CT
    special_eqp: list[str] = field(default_factory=list)  # 限定可用设备，空表示不限制


@dataclass
class EqpConstraint:
    """设备不可用约束（从 eqp_constraint.csv 加载）
    字段: eqp_name, no_used_start_time, no_used_end_time, date, week
    - no_used_start_time / no_used_end_time: hh:mm 格式
    - date: 空=无效, -1=每天, yyyy/mm/dd=指定日期
    - week: 1-7(周一到周日), 空=无约束, date 和 week 都填以 date 为准
    """
    eqp_name: str
    start_time_str: Optional[str] = None      # hh:mm
    end_time_str: Optional[str] = None        # hh:mm
    date_str: Optional[str] = None            # 空 / -1 / yyyy/mm/dd
    week: Optional[int] = None                # 1-7


@dataclass
class StepTimeWindow:
    """步骤可作业时间窗口（从 step_time_window.csv 加载）
    字段: step_name, start_time, end_time, day, end_start_time, end_end_time, end_day
    - start_time / end_time: hh:mm 格式，步骤开始时间窗（步骤只能在此时间窗内开始）
    - day: 空=无效, -1=每天, yyyy/mm/dd=指定日期, 1-7=周几
    - end_start_time / end_end_time: hh:mm 格式，步骤结束时间窗（步骤只能在此时间窗内结束）
    - end_day: 结束时间窗的日期约束，格式同 day
    """
    step_name: str
    start_time_str: str                           # hh:mm
    end_time_str: str                             # hh:mm
    date_str: Optional[str] = None                # 空 / -1 / yyyy/mm/dd
    week: Optional[int] = None                    # 1-7
    end_start_time_str: Optional[str] = None      # hh:mm，结束时间窗开始
    end_end_time_str: Optional[str] = None        # hh:mm，结束时间窗结束
    end_date_str: Optional[str] = None            # 结束时间窗的日期约束
    end_week: Optional[int] = None                # 结束时间窗的周几约束
    product_name: Optional[str] = None            # 所属产品（前端按产品过滤下拉用，可缺省）


@dataclass
class ShiftChangeTime:
    """换班时间窗口（从 shift_change_time.csv 加载）
    这段时间内 step 不能开始作业
    字段: start_time, end_time, day
    - start_time / end_time: hh:mm 格式
    - day: 空=无效, -1=每天, yyyy/mm/dd=指定日期, 1-7=周几
    """
    start_time_str: str                     # hh:mm
    end_time_str: str                       # hh:mm
    date_str: Optional[str] = None          # 空 / -1 / yyyy/mm/dd
    week: Optional[int] = None              # 1-7


@dataclass
class ShiftConfig:
    """班次配置（从 shift_config.csv 加载）"""
    shift_name: str
    start_time_str: str                     # hh:mm


@dataclass
class ManualAdjust:
    """手动调整约束（从 manual_adjust.csv 加载 / Web 界面动态添加）
    字段: lot_name, step_name, delay_to, mode
    - step_name 为空 = 该 Lot 所有未排步骤均不早于 delay_to
    - delay_to: yyyy/mm/dd HH:MM 格式
    - mode: "delay"=不早于（最早），"pin"=精确锁定在该时刻（整链以它为支点压实）
    """
    lot_name: str
    step_name: Optional[str] = None   # 空=整个Lot
    delay_to: Optional[datetime] = None
    mode: str = "delay"


@dataclass
class SpecialEqp:
    """特殊设备批处理配置（从 special_eqp.csv 加载）
    字段: eqp_name, max_lots, max_qty, together
    - max_lots: 该设备同时可处理的最大 Lot 数量
    - max_qty: 该设备同时可处理的最大总片数
    - together: 是否需要同时开始（true=设备运行中锁定不允许新Lot加入; false=运行中可加入但需检查限制）
    """
    eqp_name: str
    max_lots: int
    max_qty: int
    together: bool = False