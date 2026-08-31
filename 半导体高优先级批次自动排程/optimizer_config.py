"""优化器参数配置模块

提供 OptimizerConfig 数据类，集中管理种子随机局部搜索优化器的所有可调参数。
每个参数包含：中文说明、推荐范围、变大/变小的效果描述。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OptimizerConfig:
    """种子随机局部搜索优化器参数配置

    可通过 Web 界面 /config 页面可视化调整，也可通过 CLI 传入。
    对应 optimizer.schedule_optimized() 的参数。
    """

    # ════════════════════════════════════════════════════════
    # 搜索控制
    # ════════════════════════════════════════════════════════
    max_iterations: int = 40
    """最大迭代轮数。每轮生成不同（批次顺序/设备偏好/链放置策略）构造一个解并校验。
    推荐范围: 20-200
    变大: 搜索更充分，更可能找到合法且更优的解，但总耗时增加
    变小: 更快返回结果，但可能找不到合法解（返回违规最少的解）"""

    seed: int = 0
    """随机种子。相同 seed + 相同 max_iterations = 可复现结果。
    换种子可做多次独立重排，避免每次得到相同解。
    推荐范围: 任意整数
    变大/变小: 仅改变随机序列，用于重排产生不同解"""

    weight_by_priority: bool = True
    """是否按优先级加权目标。启用后高优先级批次的完工时间权重更大，
    优化器优先压缩高优先级批次的完工时间。"""

    resolve_max_iterations: int = 10
    """单步约束解析最大迭代。解析设备不可用/换班/时间窗时向前跳转的上限。
    推荐范围: 5-20
    变大: 更可能找到满足约束的槽位，但单步耗时增加
    变小: 更快，但可能因约束解析失败跳过步骤"""

    early_stop_patience: int = 0
    """自适应早停耐心（轮）。连续这么多轮最优得分无改进则提前终止迭代。
    推荐范围: 0-30（0=关闭，跑满 max_iterations）
    变大: 更倾向跑满，结果更稳定但耗时固定
    变小: 更快收敛，适合时间紧张时的快速排程"""

    # ════════════════════════════════════════════════════════
    # Q-time 链 / 紧凑性
    # ════════════════════════════════════════════════════════
    tight_chain_threshold: int = 240
    """紧链判定阈值（分钟）。Q-time 上限 ≤ 该值视为紧链，启用起点延迟/整链锚点后移。
    推荐范围: 60-480
    变大: 更多链被当作紧链，起点延迟/紧凑化更激进，端步骤更少超 Q，但可能出现大间隔
    变小: 更少链被当作紧链，链放置更靠近自然最早时间，间隔更小，但端步骤更易超 Q"""

    qtight_safety_margin: int = 90
    """紧 Q-time 起点延迟的安全余量（分钟）。预留端步骤因设备竞争/换班被推后的缓冲。
    推荐范围: 30-180
    变大: 起点更保守地延迟，端步骤更不易超 Q，但整体完工时间更晚
    变小: 起点更贴近最早可行，整体更紧凑，但端步骤受设备挤兑时更易超 Q"""

    chain_wait_safety: int = 20
    """链内步间等待安全余量（分钟）。分摊 Q-time 预算时保留，避免刚好卡在临界。
    推荐范围: 0-60
    变大: 链内更紧凑，Q-time 余量更大，但中间步骤可能更早排
    变小: 更充分利用 Q-time 预算，链更长，但可能卡在临界"""

    cross_shift_avoid: bool = True
    """紧 Q 链不跨班次（用户规则）。紧链相邻步骤（如 PLASMA→DISPENSE）的 Q 窗口
    若跨过班次切换时刻，把链首起点推后到班次之后，使整段链落在同一班次内
    （best-effort：推后会撑破上游紧链或不可行时保留原排程并输出跨班次告警）。
    推荐范围: 开/关
    开启: 紧 Q 链不跨班次（更贴近现实工艺习惯），链首可能被推迟到班次边界之后
    关闭: 保持原行为，紧 Q 链可能跨班次（与旧版本一致）"""

    # ════════════════════════════════════════════════════════
    # SA+Tabu 细调搜索（借鉴 meta_heuristic_before）
    # ════════════════════════════════════════════════════════
    refine_enabled: bool = True
    """是否启用 SA+Tabu 细调层。启用后在多 seed 构造择优之上再叠加
    禁忌表 + 模拟退火接受准则的领域细调，进一步压低目标函数。
    推荐范围: 开/关
    变大: 开启后更可能在已构造解附近进一步优化，但总耗时略增
    变小: 关闭后仅保留多 seed 构造择优，结果与旧版一致"""

    refine_max_iterations: int = 60
    """SA+Tabu 细调最大轮数（0 = 关闭细调）。每轮生成一个领域解并基于
    SA 接受准则 / 禁忌表决定是否采纳。
    推荐范围: 20-200
    变大: 细调搜索更充分，更可能逼近局部最优，但耗时增加
    变小: 更快返回，但领域细化程度有限"""

    tabu_tenure: int = 8
    """禁忌期限（轮）。被采纳的移动在接下来若干轮内禁止重复，
    防止搜索在同一个局部区域反复打转；邻居显著优于历史最优时破禁。
    推荐范围: 1-30
    变大: 更不易回退刚做过的移动，探索更广，但可能错过短链重复增益
    变小: 允许更快重用移动，收敛更快，但更易陷入局部最优"""

    sa_temperature_start: float = 200.0
    """模拟退火初始温度。温度越高，恶化解被接受的概率越大，早期探索越充分。
    推荐范围: 50-1000
    变大: 早期更可能接受劣解，探索更广，但收敛慢
    变小: 更早进入爬山模式，收敛快，但易陷局部最优"""

    sa_temperature_end: float = 2.0
    """模拟退火终止温度。随迭代从 sa_temperature_start 指数冷却至此，接近终止时几乎
    只接受改进解。
    推荐范围: 0.1-20
    变大: 末期仍可能接受劣解，探索略多
    变小: 末期严格爬山，收敛稳但易卡局部最优"""

    target_accept_rate: float = 0.3
    """目标接受率（温度自适应）。若近 adapt_window 轮实际接受率偏离该值，自动微调
    温度，使探索程度稳定在预定阈值附近。
    推荐范围: 0.1-0.6
    变大: 更愿意接受劣解，探索更多
    变小: 更追求当前最好，收敛更快"""

    adapt_window: int = 20
    """温度/算子权重自适应评估窗口（轮）。统计最近该轮数的接受率与各算子贡献。
    推荐范围: 5-50
    变大: 自适应更平滑稳定，但响应慢
    变小: 响应灵敏，但可能抖动"""

    # ── 内部状态（不对外暴露）──
    _internal: dict[str, Any] = field(default_factory=dict, repr=False)

    # ── 从扁平 kwargs 构造 ──
    @classmethod
    def from_dict(cls, d: dict) -> "OptimizerConfig":
        """从字典构造（忽略未知字段）"""
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})

    def to_dict(self) -> dict:
        """转为字典（去除 _internal）"""
        return {f.name: getattr(self, f.name)
                for f in dataclasses.fields(self)
                if not f.name.startswith("_")}

    # ── 参数元信息（供 Web UI 渲染）──
    @staticmethod
    def parameter_meta() -> list[dict]:
        """返回所有参数的元信息列表，供 Web 界面渲染配置表单"""
        return [
            {"group": "搜索控制", "key": "max_iterations", "type": "int",
             "label": "最大迭代轮数", "default": 40, "min": 10, "max": 500, "step": 10,
             "desc": "每轮生成不同（批次顺序/设备偏好/链策略）构造解并全量校验，取最优合法解。",
             "up": "搜索更充分，更可能找到合法且更优的解，但总耗时增加",
             "down": "更快返回结果，但可能找不到合法解"},
            {"group": "搜索控制", "key": "seed", "type": "int",
             "label": "随机种子", "default": 0, "min": 0, "max": 99999999, "step": 1,
             "desc": "相同 seed + 相同迭代轮数 = 可复现结果。换种子重排可得到不同解，避免每次相同。",
             "up": "仅改变随机序列，用于重排产生不同解",
             "down": "仅改变随机序列，用于重排产生不同解"},
            {"group": "搜索控制", "key": "weight_by_priority", "type": "bool",
             "label": "按优先级加权目标", "default": True,
             "desc": "启用后高优先级批次完工时间权重更大，优化器优先压缩高优先级批次完工时间。"},
            {"group": "搜索控制", "key": "resolve_max_iterations", "type": "int",
             "label": "约束解析最大迭代", "default": 10, "min": 5, "max": 30, "step": 1,
             "desc": "单步解析设备不可用/换班/时间窗时向前跳转的上限。",
             "up": "更可能找到满足约束的槽位，但单步耗时增加",
             "down": "更快，但可能因约束解析失败跳过步骤"},
            {"group": "搜索控制", "key": "early_stop_patience", "type": "int",
             "label": "自适应早停耐心（轮）", "default": 0, "min": 0, "max": 30, "step": 1,
             "desc": "连续 N 轮最优得分无改进即提前终止迭代（0=关闭，跑满 max_iterations）。",
             "up": "更倾向跑满，结果更稳定但耗时固定",
             "down": "更快收敛，适合时间紧张时的快速排程"},
            {"group": "Q-time 链 / 紧凑性", "key": "tight_chain_threshold", "type": "int",
             "label": "紧链判定阈值（分钟）", "default": 240, "min": 60, "max": 480, "step": 10,
             "desc": "Q-time 上限 ≤ 该值视为紧链，启用起点延迟/整链锚点后移，避免端步骤超 Q。",
             "up": "更多链按紧链紧凑化，端步骤更少超 Q，但可能出现大间隔",
             "down": "链靠自然最早放置，间隔更小，但端步骤更易超 Q"},
            {"group": "Q-time 链 / 紧凑性", "key": "qtight_safety_margin", "type": "int",
             "label": "Q-time 安全余量（分钟）", "default": 90, "min": 30, "max": 180, "step": 5,
             "desc": "紧 Q-time 起点延迟预留的缓冲，缓冲端步骤被设备/换班挤兑。",
             "up": "起点更保守延迟，端步骤更不易超 Q，但完工时间更晚",
             "down": "起点更早，整体紧凑，但端步骤受挤兑时更易超 Q"},
            {"group": "Q-time 链 / 紧凑性", "key": "chain_wait_safety", "type": "int",
             "label": "链内等待安全余量（分钟）", "default": 20, "min": 0, "max": 60, "step": 5,
             "desc": "分摊 Q-time 预算时保留的余量，避免链内步间等待刚好卡在临界。",
             "up": "链内更紧凑，Q-time 余量更大",
             "down": "更充分利用 Q-time 预算，但可能卡在临界"},
            {"group": "Q-time 链 / 紧凑性", "key": "cross_shift_avoid", "type": "bool",
             "label": "紧 Q 链不跨班次", "default": True,
             "desc": "紧链相邻步骤（如 PLASMA→DISPENSE）的 Q 窗口若跨过班次切换时刻，把链首起点推后到班次之后，使整段链落在同一班次内（best-effort，不可行时保留原排程并告警）。",
             "up": "紧 Q 链不跨班次（更贴近现实工艺习惯），链首可能被推迟到班次边界之后",
             "down": "保持原行为，紧 Q 链可能跨班次（与旧版本一致）"},
            {"group": "SA+Tabu 搜索", "key": "refine_enabled", "type": "bool",
             "label": "启用 SA+Tabu 细调", "default": True,
             "desc": "在多 seed 构造择优之上叠加禁忌表 + 模拟退火细调层，进一步压低目标函数。"},
            {"group": "SA+Tabu 搜索", "key": "refine_max_iterations", "type": "int",
             "label": "细调最大轮数", "default": 60, "min": 0, "max": 300, "step": 10,
             "desc": "SA+Tabu 细调轮数（0=关闭）。每轮生成一个邻域解并按接受准则决定是否采纳。",
             "up": "细调更充分，更逼近局部最优，但耗时增加",
             "down": "更快返回，但领域细化有限"},
            {"group": "SA+Tabu 搜索", "key": "tabu_tenure", "type": "int",
             "label": "禁忌期限（轮）", "default": 8, "min": 1, "max": 30, "step": 1,
             "desc": "被采纳移动的禁忌时长，防止在同一局部反复打转；显著优于历史最优时破禁。",
             "up": "探索更广，但可能错过短链重复增益",
             "down": "收敛更快，但更易陷入局部最优"},
            {"group": "SA+Tabu 搜索", "key": "sa_temperature_start", "type": "float",
             "label": "初始温度", "default": 200.0, "min": 20, "max": 1000, "step": 20,
             "desc": "模拟退火初始温度。越高早期越易接受劣解、探索越充分。",
             "up": "早期探索更广，但收敛慢",
             "down": "更早进入爬山，但易陷局部最优"},
            {"group": "SA+Tabu 搜索", "key": "sa_temperature_end", "type": "float",
             "label": "终止温度", "default": 2.0, "min": 0.1, "max": 20, "step": 0.5,
             "desc": "随迭代指数冷却至此，接近终止时几乎只接受改进解。",
             "up": "末期仍可能接受劣解",
             "down": "末期严格爬山，收敛稳但易卡局部最优"},
            {"group": "SA+Tabu 搜索", "key": "target_accept_rate", "type": "float",
             "label": "目标接受率", "default": 0.3, "min": 0.05, "max": 0.8, "step": 0.05,
             "desc": "温度自适应目标接受率。实际接受率偏离时自动微调温度。",
             "up": "更愿接受劣解、探索更多",
             "down": "更追求当前最好、收敛更快"},
            {"group": "SA+Tabu 搜索", "key": "adapt_window", "type": "int",
             "label": "自适应窗口（轮）", "default": 20, "min": 5, "max": 50, "step": 5,
             "desc": "统计最近 N 轮接受率与各算子贡献，用于温度/算子权重自适应。",
             "up": "自适应更平滑稳定，但响应慢",
             "down": "响应灵敏，但可能抖动"},
        ]

    @staticmethod
    def groups() -> list[str]:
        """返回参数分组列表"""
        return ["搜索控制", "Q-time 链 / 紧凑性", "SA+Tabu 搜索"]
