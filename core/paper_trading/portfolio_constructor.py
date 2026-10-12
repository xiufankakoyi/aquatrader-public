"""组合权重构造器。

将目标权重构造逻辑从 Portfolio.rebalance 中解耦，
支持等权、评分加权等不同策略。

构造器为纯函数式抽象：输入选股结果 + 总资金，输出 {code: target_value}。
不执行任何交易，不读取持仓状态，便于单元测试与策略替换。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


def _get_code(item: Any) -> str:
    """从选股结果中提取标准化 6 位代码。

    兼容 dict（含 'code' 键）与 dataclass/对象（含 .code 属性）两种形态，
    便于构造器同时服务 orchestrator 的 dict 流和 selector 的 SelectedStock 流。

    Args:
        item: 单个选股结果，dict 或对象。

    Returns:
        标准化后的 6 位代码字符串。
    """
    if isinstance(item, dict):
        return str(item.get("code", "")).strip()[-6:]
    return str(getattr(item, "code", "")).strip()[-6:]


def _get_score(item: Any) -> float:
    """从选股结果中提取评分，缺失时回退为 0。

    兼容 dict 与对象两种形态，与 _get_code 配套使用。

    Args:
        item: 单个选股结果，dict 或对象。

    Returns:
        评分浮点数；缺失或非法时返回 0.0。
    """
    if isinstance(item, dict):
        try:
            return float(item.get("score", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(getattr(item, "score", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


class PortfolioConstructor(ABC):
    """目标权重构造器抽象基类。

    负责将选股结果转换为目标持仓价值字典 {code: target_value}，
    不涉及任何交易执行或持仓读取。
    """

    @abstractmethod
    def construct(
        self,
        selected_stocks: list[Any],
        total_capital: float,
    ) -> dict[str, float]:
        """构造目标持仓。

        Args:
            selected_stocks: 选中的股票列表，元素可为 dict（含 'code'/'score'
                键）或对象（含 .code/.score 属性）。
            total_capital: 总可用资金（含现金与持仓市值）。

        Returns:
            {code: target_value} 目标持仓价值字典，code 已标准化为 6 位。
        """
        ...


class EqualWeightConstructor(PortfolioConstructor):
    """等权构造器。

    所有选中股票分配相同权重的资金，与原 Portfolio.rebalance 默认行为一致。
    """

    def construct(
        self,
        selected_stocks: list[Any],
        total_capital: float,
    ) -> dict[str, float]:
        """等权分配资金。

        Args:
            selected_stocks: 选中的股票列表。
            total_capital: 总可用资金。

        Returns:
            {code: target_value} 等权目标持仓；空列表返回空字典。
        """
        if not selected_stocks:
            return {}
        weight = 1.0 / len(selected_stocks)
        target_value = total_capital * weight
        return {_get_code(s): target_value for s in selected_stocks}


class ScoreWeightConstructor(PortfolioConstructor):
    """评分加权构造器。

    按选股评分占比分配资金，评分越高权重越大。评分缺失或全零时
    自动回退为等权，避免除零。
    """

    def construct(
        self,
        selected_stocks: list[Any],
        total_capital: float,
    ) -> dict[str, float]:
        """按评分比例分配资金。

        Args:
            selected_stocks: 选中的股票列表（需含 score 字段）。
            total_capital: 总可用资金。

        Returns:
            {code: target_value} 评分加权目标持仓；评分全零时回退等权。
        """
        if not selected_stocks:
            return {}
        # 评分下限 0.01，避免零评分股票拿到 0 权重而无法建仓。
        scores = {_get_code(s): max(_get_score(s), 0.01) for s in selected_stocks}
        total_score = sum(scores.values())
        if total_score <= 0:
            # 防御性回退：评分异常时退化为等权，保证可执行。
            weight = 1.0 / len(selected_stocks)
            return {code: total_capital * weight for code in scores}
        return {
            code: total_capital * (score / total_score)
            for code, score in scores.items()
        }
