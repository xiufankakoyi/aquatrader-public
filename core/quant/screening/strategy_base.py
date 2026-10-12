"""日线筛选策略插件契约。

外部策略大师只需继承 ScreenStrategyBase 并实现 check() 方法，
即可将自己的策略接入 QUANT 平台，无需修改核心代码。

接入步骤：
1. 实现 ScreenStrategyBase 子类
2. 通过 @register_screen_strategy 装饰器或 ScreenStrategyRegistry.register() 注册
3. DailyScreener 会自动调用所有已注册策略

设计原则：
- 策略隔离：每个策略独立失败，不阻塞其他策略
- PIT 安全：策略只接收 as_of 之前的数据
- 降级友好：策略异常时标记 MISSING_DATA 而非崩溃
- 可组合：多策略结果聚合为最终 ScreeningResult
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.quant.contracts import (
    QuantCandidate,
    RejectReason,
    ScreeningResult,
    SectorAssessment,
    SectorPhase,
)

logger = logging.getLogger(__name__)


@dataclass
class StrategyContext:
    """策略执行上下文。

    提供策略所需的只读环境，避免策略直接访问全局状态。
    所有字段在单次 screen() 调用内保持不变。

    Attributes:
        current_date: 当前日期（ISO 格式）。
        candidate: 当前候选对象。
        sector_assessment: 板块评估结果（可 None）。
        daily_data: 标准化后的日线数据（已截断到 as_of 之前）。
        data_query: 数据查询对象（可 None）。
        params: 策略参数字典（来自注册时配置）。
    """

    current_date: str = ""
    candidate: QuantCandidate | None = None
    sector_assessment: SectorAssessment | None = None
    daily_data: pd.DataFrame | None = None
    data_query: Any = None
    params: dict[str, Any] = field(default_factory=dict)


class ScreenStrategyBase(ABC):
    """日线筛选策略抽象基类。

    外部策略大师继承此类并实现 check() 方法即可接入平台。
    每个策略负责一项独立检查（趋势/位置/流动性等），
    返回的部分结果会被 DailyScreener 聚合为最终 ScreeningResult。

    Class attributes:
        strategy_id: 策略唯一标识（用于注册和日志）。
        strategy_name: 策略中文名（用于展示）。
        description: 策略描述。
        priority: 执行优先级（数字越小越先执行，默认 100）。
        required: 是否必需（True 时失败则整体拒绝，False 时仅记录）。
    """

    strategy_id: str = ""
    strategy_name: str = ""
    description: str = ""
    priority: int = 100
    required: bool = True

    @abstractmethod
    def check(self, ctx: StrategyContext) -> tuple[bool, float, dict[str, Any], list[RejectReason]]:
        """执行单项筛选检查。

        Args:
            ctx: 策略执行上下文。

        Returns:
            tuple(passed, score, details, reject_reasons):
            - passed: 是否通过本项检查
            - score: 本项分数 [0, 1]
            - details: 详情字典（写入 ScreeningResult.details）
            - reject_reasons: 拒绝原因列表（未通过时填充）
        """
        raise NotImplementedError

    def safe_check(self, ctx: StrategyContext) -> tuple[bool, float, dict[str, Any], list[RejectReason]]:
        """安全包装的 check()：异常时返回 MISSING_DATA 降级。

        Args:
            ctx: 策略执行上下文。

        Returns:
            同 check() 返回值；异常时返回 (False, 0.0, {}, [MISSING_DATA])。
        """
        try:
            return self.check(ctx)
        except Exception as exc:
            logger.warning(
                "策略 %s(%s) 执行异常: %s",
                self.strategy_id, self.strategy_name, exc,
            )
            return False, 0.0, {"error": str(exc)}, [RejectReason.MISSING_DATA]


class ScreenStrategyRegistry:
    """日线筛选策略注册中心。

    策略通过 register() 或 @register_screen_strategy 装饰器注册。
    DailyScreener 通过 list_strategies() 获取所有已注册策略。

    设计：
    - 全局单例 _DEFAULT_REGISTRY
    - 支持自定义 registry 实例（用于测试隔离）
    - 按 priority 排序执行
    """

    def __init__(self) -> None:
        """初始化注册中心。"""
        self._strategies: dict[str, ScreenStrategyBase] = {}

    def register(self, strategy: ScreenStrategyBase) -> None:
        """注册策略。

        Args:
            strategy: 策略实例。

        Raises:
            ValueError: strategy_id 为空或重复注册。
        """
        if not strategy.strategy_id:
            raise ValueError("strategy_id 不能为空")
        if strategy.strategy_id in self._strategies:
            logger.warning("策略 %s 已注册，覆盖旧实例", strategy.strategy_id)
        self._strategies[strategy.strategy_id] = strategy
        logger.info("注册日线策略: %s(%s)", strategy.strategy_id, strategy.strategy_name)

    def unregister(self, strategy_id: str) -> bool:
        """注销策略。

        Args:
            strategy_id: 策略 ID。

        Returns:
            True 表示注销成功。
        """
        return self._strategies.pop(strategy_id, None) is not None

    def list_strategies(self, *, sorted_by_priority: bool = True) -> list[ScreenStrategyBase]:
        """列出所有已注册策略。

        Args:
            sorted_by_priority: 是否按 priority 升序排序。

        Returns:
            策略实例列表。
        """
        strategies = list(self._strategies.values())
        if sorted_by_priority:
            strategies.sort(key=lambda s: s.priority)
        return strategies

    def get(self, strategy_id: str) -> ScreenStrategyBase | None:
        """按 ID 获取策略。

        Args:
            strategy_id: 策略 ID。

        Returns:
            策略实例或 None。
        """
        return self._strategies.get(strategy_id)

    def clear(self) -> None:
        """清空所有注册策略。"""
        self._strategies.clear()

    def __len__(self) -> int:
        return len(self._strategies)


# 全局默认注册中心
_DEFAULT_REGISTRY = ScreenStrategyRegistry()


def get_default_registry() -> ScreenStrategyRegistry:
    """获取全局默认注册中心。

    Returns:
        全局 ScreenStrategyRegistry 实例。
    """
    return _DEFAULT_REGISTRY


def register_screen_strategy(
    registry: ScreenStrategyRegistry | None = None,
) -> Any:
    """装饰器：注册日线筛选策略。

    用法：
        @register_screen_strategy()
        class MyStrategy(ScreenStrategyBase):
            strategy_id = "my_trend"
            strategy_name = "我的趋势策略"
            def check(self, ctx):
                ...
                return True, 0.8, {"ma5": 10.5}, []

    Args:
        registry: 目标注册中心；None 时使用全局默认。

    Returns:
        类装饰器。
    """
    # 注意：不能用 `registry or _DEFAULT_REGISTRY`，
    # 因为 ScreenStrategyRegistry 实现了 __len__，空 registry 是 falsy。
    target = registry if registry is not None else _DEFAULT_REGISTRY

    def decorator(cls: type[ScreenStrategyBase]) -> type[ScreenStrategyBase]:
        instance = cls()
        target.register(instance)
        return cls

    return decorator
