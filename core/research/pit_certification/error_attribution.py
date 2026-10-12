"""P3.9 预测错误归因器。

按 P3.9 §认证内容「模型、数据、映射、时点和假设错误归因」：
- MODEL: 模型本身缺陷（结构、参数、校准问题）
- DATA: 输入数据错误（数据源错误、缺失、陈旧）
- MAPPING: 概念映射错误（行业归属、产品映射、分部归集）
- TIMING: 时点错误（使用了未来信息或时点对齐错误）
- ASSUMPTION: 假设错误（显式假设与实际偏离）

归因优先级：
1. TIMING: 先检查时点错误（最严重，违反 PIT 原则）
2. DATA: 再检查数据错误
3. MAPPING: 再检查概念映射错误
4. ASSUMPTION: 再检查假设错误
5. MODEL: 以上都排除后归因为模型本身缺陷
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from core.research.pit_certification.contracts import PITErrorAttribution


@dataclass(frozen=True)
class ErrorAttributionResult:
    """错误归因结果。

    Attributes:
        attribution: 错误归因类型
        detail: 详细说明
        confidence: 归因置信度 [0, 1]
        evidence: 支撑归因的证据列表
    """

    attribution: PITErrorAttribution
    detail: str
    confidence: float = 0.5
    evidence: list[str] | None = None


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _messages(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value if item is not None and str(item)]


def attribute_error(
    prediction: float | None,
    actual: float | None,
    model_version: str = "",
    inputs: dict[str, Any] | None = None,
    *,
    leakage_detected: bool = False,
    mapping_errors: list[str] | None = None,
    assumption_errors: list[str] | None = None,
    data_quality_issues: list[str] | None = None,
) -> ErrorAttributionResult:
    """归因预测错误来源。

    按优先级检查：TIMING → DATA → MAPPING → ASSUMPTION → MODEL。
    一旦命中某类错误即返回，不继续检查后续类型。

    Args:
        prediction: 预测值
        actual: 实际值
        model_version: 使用的模型版本
        inputs: 输入数据字典（用于检查数据质量）
        leakage_detected: 是否检测到未来信息泄漏
        mapping_errors: 概念映射错误列表
        assumption_errors: 假设错误列表
        data_quality_issues: 数据质量问题列表

    Returns:
        ErrorAttributionResult: 错误归因结果
    """
    # 计算预测误差（用于判断是否有错误需要归因）
    has_error = _has_prediction_error(prediction, actual)

    # 1. TIMING: 时点错误最严重（违反 PIT 原则）
    if leakage_detected:
        return ErrorAttributionResult(
            attribution=PITErrorAttribution.TIMING,
            detail=(
                f"检测到未来信息泄漏：预测使用了 PIT 时点之后的输入数据，"
                f"模型版本={model_version}，违反 PIT 原则"
            ),
            confidence=0.95,
            evidence=["leakage_detected=True"],
        )

    if not has_error:
        # 无预测误差时不归因
        return ErrorAttributionResult(
            attribution=PITErrorAttribution.MODEL,
            detail="预测误差为零或无法计算，无需归因",
            confidence=0.0,
            evidence=[],
        )

    # 2. DATA: 数据错误
    data_issues = _messages(data_quality_issues)
    if data_issues:
        return ErrorAttributionResult(
            attribution=PITErrorAttribution.DATA,
            detail=(
                f"输入数据质量问题导致预测偏差：{'; '.join(data_issues)}，"
                f"模型版本={model_version}"
            ),
            confidence=0.80,
            evidence=list(data_issues),
        )

    # 3. MAPPING: 概念映射错误
    mapping = _messages(mapping_errors)
    if mapping:
        return ErrorAttributionResult(
            attribution=PITErrorAttribution.MAPPING,
            detail=(
                f"概念映射错误导致预测偏差：{'; '.join(mapping)}，"
                f"模型版本={model_version}"
            ),
            confidence=0.75,
            evidence=list(mapping),
        )

    # 4. ASSUMPTION: 假设错误
    assumptions = _messages(assumption_errors)
    if assumptions:
        return ErrorAttributionResult(
            attribution=PITErrorAttribution.ASSUMPTION,
            detail=(
                f"显式假设与实际偏离：{'; '.join(assumptions)}，"
                f"模型版本={model_version}"
            ),
            confidence=0.70,
            evidence=list(assumptions),
        )

    # 5. MODEL: 排除以上后归因为模型本身缺陷
    error_detail = _format_model_error(prediction, actual, model_version)
    return ErrorAttributionResult(
        attribution=PITErrorAttribution.MODEL,
        detail=error_detail,
        confidence=0.60,
        evidence=[
            f"prediction={prediction}",
            f"actual={actual}",
            f"model_version={model_version}",
            "no_timing_error",
            "no_data_error",
            "no_mapping_error",
            "no_assumption_error",
        ],
    )


def _has_prediction_error(prediction: float | None, actual: float | None) -> bool:
    """判断是否存在预测误差。

    Args:
        prediction: 预测值
        actual: 实际值

    Returns:
        bool: 是否存在非零误差
    """
    if prediction is None or actual is None:
        return False
    prediction_value = _finite_float(prediction)
    actual_value = _finite_float(actual)
    return (
        prediction_value is not None
        and actual_value is not None
        and abs(prediction_value - actual_value) > 1e-9
    )


def _format_model_error(
    prediction: float | None,
    actual: float | None,
    model_version: str,
) -> str:
    """格式化模型错误描述。

    Args:
        prediction: 预测值
        actual: 实际值
        model_version: 模型版本

    Returns:
        str: 错误描述
    """
    if prediction is None or actual is None:
        return f"模型版本={model_version}：预测值或实际值为空，无法归因到其他类型"
    prediction_value = _finite_float(prediction)
    actual_value = _finite_float(actual)
    if prediction_value is not None and actual_value is not None:
        abs_error = abs(prediction_value - actual_value)
        if abs(actual_value) > 1e-9:
            pct_error = abs_error / abs(actual_value)
            return (
                f"模型本身缺陷导致预测偏差：预测={prediction}，实际={actual}，"
                f"绝对误差={abs_error:.4f}，百分比误差={pct_error:.2%}，"
                f"模型版本={model_version}（已排除时点/数据/映射/假设错误）"
            )
    return (
        f"模型本身缺陷导致预测偏差：预测={prediction}，实际={actual}，"
        f"模型版本={model_version}（已排除时点/数据/映射/假设错误）"
    )
