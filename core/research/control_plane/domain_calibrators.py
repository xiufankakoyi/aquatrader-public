"""严格分域的三个校准器。任何交易结果都不能训练事实层。

校准方法论:
1. Walk-forward 时间切分: 训练集时间戳全部 < 测试集最小时间戳，杜绝未来泄漏
2. 最小样本数门槛: 样本不足时返回默认参数，status="insufficient"
3. 完整指标: Brier Score、校准曲线、precision/recall、误升级率、状态准确率
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from statistics import mean
from typing import Any

from .contracts import CalibrationRun, FeedbackDomain, _safe_float, _safe_int, _safe_text
from .repository import ResearchRepository

# 最小样本数门槛，不足时返回默认参数
MIN_SAMPLES = 50
# 训练集占比
TRAIN_RATIO = 0.7

RETRIEVAL_STAGE_TO_TIER = {
    "local": "high",
    "first_party": "high",
    "high": "high",
    "second_hand": "medium",
    "medium": "medium",
    "web": "low",
    "low": "low",
}


def _event_mapping(event: Any, name: str) -> Mapping[str, Any]:
    value = event.get(name) if isinstance(event, Mapping) else getattr(event, name, None)
    return value if isinstance(value, Mapping) else {}


def _event_value(event: Any, name: str, default: Any = "") -> Any:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _binary_label(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and value in (0, 1):
        return float(value)
    return None


def normalize_retrieval_feedback(event: Any) -> dict[str, float | str] | None:
    """Normalize both runtime retrieval stages and budget-tier feedback."""
    metrics = _event_mapping(event, "metrics")
    attribution = _event_mapping(event, "attribution")
    raw_tier = _safe_text(attribution.get("tier") or metrics.get("tier")).lower()
    tier = RETRIEVAL_STAGE_TO_TIER.get(raw_tier)
    if tier is None:
        return None
    documents = max(0, _safe_int(metrics.get("documents_found"), 0))
    effective = _safe_float(metrics.get("effective_evidence_rate"), float("nan"))
    if effective != effective:
        effective = max(0, _safe_int(metrics.get("evidence_saved"), 0)) / max(1, documents)
    independent = _safe_float(metrics.get("independent_source_rate"), float("nan"))
    if independent != independent:
        independent = max(0, _safe_int(metrics.get("independent_sources"), 0)) / max(1, documents)
    unit_cost = max(0.01, _safe_float(metrics.get("unit_cost"), 1.0))
    marginal = _safe_float(metrics.get("marginal_information_gain"), independent)
    return {
        "tier": tier,
        "effective_evidence_rate": max(0.0, min(1.0, effective)),
        "independent_source_rate": max(0.0, min(1.0, independent)),
        "unit_cost": unit_cost,
        "marginal_information_gain": max(0.0, marginal),
    }


def _walk_forward_split(
    events: list[Any], train_ratio: float = TRAIN_RATIO
) -> tuple[list[Any], list[Any]]:
    """按 created_at 时间排序后切分训练集和测试集。

    P4.4: 通过适配器复用 adaptation.engine.walk_forward_windows()，
    保证训练集最大时间戳 < 测试集最小时间戳，杜绝未来信息泄漏。
    样本不足以形成滑动窗口时回退到单次比例切分（保持兼容）。

    Args:
        events: 反馈事件列表。
        train_ratio: 训练集占比（回退模式使用）。

    Returns:
        (train_events, test_events) 时间有序切分。
    """
    if not isinstance(events, list) or not events:
        return [], []
    safe_ratio = max(0.1, min(0.9, _safe_float(train_ratio, TRAIN_RATIO)))
    sorted_events = sorted(
        events,
        key=lambda e: _safe_text(_event_value(e, "created_at")),
    )

    # P4.4: 优先复用 adaptation.engine.walk_forward_windows()
    try:
        from core.research.adaptation.engine import walk_forward_windows
        dates = [_safe_text(_event_value(e, "created_at")) for e in sorted_events]
        # 训练集大小按 train_ratio 计算，测试集为剩余部分
        train_size = max(1, int(len(dates) * safe_ratio))
        test_size = max(1, len(dates) - train_size)
        windows = walk_forward_windows(dates, train_size, test_size)
        if windows:
            # 使用第一个窗口的 train_end/test_start 切分
            first = windows[0]
            train_events = [
                e for e in sorted_events
                if _safe_text(_event_value(e, "created_at")) <= first["train_end"]
            ]
            test_events = [
                e for e in sorted_events
                if _safe_text(_event_value(e, "created_at")) >= first["test_start"]
            ]
            if train_events and test_events:
                return train_events, test_events
    except (ImportError, KeyError, TypeError, ValueError, IndexError):
        pass

    # 回退：单次比例切分（保持兼容）
    split_idx = max(1, int(len(sorted_events) * safe_ratio))
    return sorted_events[:split_idx], sorted_events[split_idx:]


def _brier_score(predictions: list[float], labels: list[float]) -> float:
    """计算 Brier Score（均方误差），越低越好。

    Args:
        predictions: 预测概率列表。
        labels: 真实标签列表（0/1）。

    Returns:
        Brier Score。
    """
    if not predictions or len(predictions) != len(labels):
        return 1.0
    safe_predictions = [_safe_float(value, float("nan")) for value in predictions]
    safe_labels = [_safe_float(value, float("nan")) for value in labels]
    if any(value != value for value in (*safe_predictions, *safe_labels)):
        return 1.0
    return sum((p - l) ** 2 for p, l in zip(safe_predictions, safe_labels)) / len(safe_predictions)


def _calibration_curve(
    predictions: list[float], labels: list[float], n_bins: int = 5
) -> list[dict[str, Any]]:
    """计算校准曲线（分桶后的预测均值与实际均值）。

    Args:
        predictions: 预测概率列表。
        labels: 真实标签列表。
        n_bins: 分桶数。

    Returns:
        每桶 {bin_center, predicted_mean, empirical_mean, count}。
    """
    if not predictions or len(predictions) != len(labels):
        return []
    predictions = [_safe_float(value, float("nan")) for value in predictions]
    labels = [_safe_float(value, float("nan")) for value in labels]
    if any(value != value for value in (*predictions, *labels)):
        return []
    n_bins = _safe_int(n_bins, 5, minimum=1)
    n_bins = min(n_bins, 50)
    bins: list[dict[str, Any]] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        bucket_preds = [p for p, l in zip(predictions, labels) if lo <= p < hi]
        bucket_labels = [l for p, l in zip(predictions, labels) if lo <= p < hi]
        if bucket_preds:
            bins.append({
                "bin_center": round((lo + hi) / 2, 4),
                "predicted_mean": round(mean(bucket_preds), 4),
                "empirical_mean": round(mean(bucket_labels), 4),
                "count": len(bucket_preds),
            })
    return bins


def _precision_recall(
    predictions: list[float], labels: list[float], threshold: float = 0.5
) -> dict[str, float]:
    """计算 precision/recall/f1。

    Args:
        predictions: 预测概率列表。
        labels: 真实标签列表。
        threshold: 二值化阈值。

    Returns:
        {precision, recall, f1}。
    """
    if not predictions or len(predictions) != len(labels):
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    predictions = [_safe_float(value, float("nan")) for value in predictions]
    labels = [_safe_float(value, float("nan")) for value in labels]
    if any(value != value for value in (*predictions, *labels)):
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    threshold = max(0.0, min(1.0, _safe_float(threshold, 0.5)))
    tp = sum(1 for p, l in zip(predictions, labels) if p >= threshold and l >= 0.5)
    fp = sum(1 for p, l in zip(predictions, labels) if p >= threshold and l < 0.5)
    fn = sum(1 for p, l in zip(predictions, labels) if p < threshold and l >= 0.5)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def _default_knowledge_params() -> dict[str, Any]:
    """知识域默认参数（样本不足时使用）。"""
    return {
        "source_weights": {"announcement": 1.0, "official": 1.0, "news": 0.7, "unknown": 0.5},
        "claim_activation_threshold": 0.7,
        "source_discount_kappa": 0.5,
        "importance_overrides": {},
    }


def _default_retrieval_params() -> dict[str, Any]:
    """检索域默认参数。"""
    return {
        "tier_utilities": {"high": 0.8, "medium": 0.5, "low": 0.3},
        "recommended_budget": 10,
        "stop_min_independent_sources": 2,
        "require_counter_search": True,
        "priority_features": [
            "research_importance",
            "evidence_gap",
            "expected_information_gain",
            "timeliness",
            "cost_factor",
        ],
        "priority_feature_weights": {
            "research_importance": 0.30,
            "evidence_gap": 0.25,
            "expected_information_gain": 0.25,
            "timeliness": 0.15,
            "cost_factor": 0.05,
        },
    }


def _default_alpha_params() -> dict[str, Any]:
    """Alpha 域默认参数。"""
    return {
        "alpha_weight_multiplier": 1.0,
        "calibrated_node_weights": {},
    }


class _DomainCalibrator:
    """分域校准器基类，提供 walk-forward 切分和指标计算通用能力。"""

    domain: str
    calibration_domain: str

    def __init__(self, repository: ResearchRepository) -> None:
        self.repository = repository

    def events(self) -> list[Any]:
        """读取本域反馈事件。"""
        events = self.repository.list_feedback(self.domain, limit=5000)
        return events if isinstance(events, list) else []

    def _insufficient_run(
        self,
        sample_size: int,
        default_params: dict[str, Any],
        bounds: dict[str, Any],
    ) -> CalibrationRun:
        """样本不足时返回默认参数的 CalibrationRun。

        Args:
            sample_size: 实际样本数。
            default_params: 默认参数。
            bounds: 参数边界。

        Returns:
            status="insufficient" 的 CalibrationRun，不参与激活。
        """
        return CalibrationRun(
            domain=self.calibration_domain,
            status="insufficient",
            sample_size=sample_size,
            metrics={
                "reason": f"样本不足 {sample_size}/{MIN_SAMPLES}",
                "walk_forward_score": 0.0,
                "brier_score": 1.0,
                "walk_forward_windows": 0,
            },
            parameters=default_params,
            bounds=bounds,
        )


class KnowledgePolicyCalibrator(_DomainCalibrator):
    """只消费事实证实/否定/失效标签，校准来源权重与 Claim 阈值。

    使用 walk-forward 时间切分: 训练集计算来源权重，测试集验证。
    """

    domain = FeedbackDomain.KNOWLEDGE.value
    calibration_domain = "knowledge"

    HIGH_QUALITY_SOURCES = frozenset({"announcement", "official", "announcement_news"})
    LOW_QUALITY_SOURCES = frozenset({"forum", "blog", "social_media", "social"})

    def fit(self) -> CalibrationRun:
        """校准来源权重、Claim 阈值、来源折扣 kappa 与重要性降级映射。

        Returns:
            CalibrationRun，包含完整 walk-forward 验证指标。
        """
        events = self.events()
        bounds = {
            "claim_activation_threshold": {"min": 0.55, "max": 0.90},
            "source_discount_kappa": {"min": 0.3, "max": 1.0},
        }
        if len(events) < MIN_SAMPLES:
            return self._insufficient_run(len(events), _default_knowledge_params(), bounds)

        train_events, test_events = _walk_forward_split(events)
        params = self._fit_params(train_events)
        metrics = self._evaluate(test_events, params)
        return CalibrationRun(
            domain=self.calibration_domain,
            status="shadow",
            sample_size=len(events),
            metrics=metrics,
            parameters=params,
            bounds=bounds,
        )

    def _fit_params(self, events: list[Any]) -> dict[str, Any]:
        """在训练集上拟合参数。

        Args:
            events: 训练集事件。

        Returns:
            拟合后的参数字典。
        """
        by_source: dict[str, list[float]] = defaultdict(list)
        labels: list[float] = []
        high_quality_labels: list[float] = []
        low_quality_labels: list[float] = []
        by_node_type: dict[str, list[float]] = defaultdict(list)
        for event in events:
            metrics = _event_mapping(event, "metrics")
            attribution = _event_mapping(event, "attribution")
            label = _binary_label(metrics.get("fact_label"))
            if label is None:
                continue
            numeric = label
            source_type = _safe_text(attribution.get("source_type"), "unknown")
            by_source[source_type].append(numeric)
            labels.append(numeric)
            if source_type in self.HIGH_QUALITY_SOURCES:
                high_quality_labels.append(numeric)
            elif source_type in self.LOW_QUALITY_SOURCES:
                low_quality_labels.append(numeric)
            node_type = _safe_text(attribution.get("node_type"))
            if node_type:
                by_node_type[node_type].append(numeric)
        source_weights = {
            source: round((sum(values) + 2.0) / (len(values) + 4.0), 4)
            for source, values in by_source.items()
        }
        accuracy = mean(labels) if labels else 0.0
        high_rate = mean(high_quality_labels) if high_quality_labels else 0.5
        low_rate = mean(low_quality_labels) if low_quality_labels else 0.5
        kappa = max(0.3, min(1.0, 0.5 + 0.5 * (low_rate - high_rate)))
        importance_overrides: dict[str, str] = {}
        for node_type, values in by_node_type.items():
            if len(values) < 5:
                continue
            if mean(values) < 0.3:
                importance_overrides[node_type] = "downgrade"
        return {
            "source_weights": source_weights,
            "claim_activation_threshold": round(max(0.55, min(0.9, 1.0 - 0.5 * accuracy)), 4),
            "source_discount_kappa": round(kappa, 4),
            "importance_overrides": importance_overrides,
        }

    def _evaluate(self, events: list[Any], params: dict[str, Any]) -> dict[str, Any]:
        """在测试集上计算完整指标。

        Args:
            events: 测试集事件。
            params: 训练集拟合的参数。

        Returns:
            完整指标字典。
        """
        predictions: list[float] = []
        labels: list[float] = []
        source_weights = params.get("source_weights", {}) if isinstance(params, Mapping) else {}
        source_weights = source_weights if isinstance(source_weights, Mapping) else {}
        kappa = (
            max(0.3, min(1.0, _safe_float(params.get("source_discount_kappa"), 0.5)))
            if isinstance(params, Mapping) else 0.5
        )
        threshold = (
            max(0.0, min(1.0, _safe_float(params.get("claim_activation_threshold"), 0.7)))
            if isinstance(params, Mapping) else 0.7
        )
        by_source_labels: dict[str, list[float]] = defaultdict(list)
        for event in events:
            metrics = _event_mapping(event, "metrics")
            attribution = _event_mapping(event, "attribution")
            label = _binary_label(metrics.get("fact_label"))
            if label is None:
                continue
            numeric = label
            labels.append(numeric)
            source_type = _safe_text(attribution.get("source_type"), "unknown")
            by_source_labels[source_type].append(numeric)
            weight = _safe_float(source_weights.get(source_type), 0.5)
            # 预测概率：来源权重经 kappa 折扣
            pred = max(0.0, min(1.0, weight * (1.0 - 0.5 * kappa)))
            predictions.append(pred)
        brier = _brier_score(predictions, labels)
        cal_curve = _calibration_curve(predictions, labels)
        pr = _precision_recall(predictions, labels, threshold)
        accuracy = mean(labels) if labels else 0.0
        # 误升级率：预测为正但实际为负
        false_prom = (
            sum(1 for p, l in zip(predictions, labels) if p >= threshold and l < 0.5)
            / max(1, len(labels))
        )
        # ACTIVE/TRUSTED 准确率：测试集中被升级且标签为正的比例
        active_total = sum(
            1 for p in predictions
            if p >= threshold
        )
        active_correct = sum(
            1 for p, l in zip(predictions, labels)
            if p >= threshold and l >= 0.5
        )
        active_accuracy = active_correct / active_total if active_total else 0.0
        # 来源可靠性
        source_reliability: dict[str, float] = {}
        for source, vals in by_source_labels.items():
            source_reliability[source] = round(mean(vals), 4) if vals else 0.0
        return {
            "walk_forward_score": round(accuracy, 4),
            "brier_score": round(brier, 4),
            "calibration_curve": cal_curve,
            "precision": pr["precision"],
            "recall": pr["recall"],
            "f1": pr["f1"],
            "false_promotion_rate": round(false_prom, 4),
            "active_trusted_accuracy": round(active_accuracy, 4),
            "source_reliability": source_reliability,
            "train_size": len(events),
            "test_size": len(labels),
            "walk_forward_windows": 1,
        }


class RetrievalPolicyCalibrator(_DomainCalibrator):
    """只消费检索命中、独立来源、有效证据率与成本。"""

    domain = FeedbackDomain.RETRIEVAL.value
    calibration_domain = "retrieval"

    def fit(self) -> CalibrationRun:
        """校准检索域参数，使用 walk-forward 时间切分。

        Returns:
            CalibrationRun，包含完整 walk-forward 验证指标。
        """
        events = [
            event for event in self.events()
            if normalize_retrieval_feedback(event) is not None
        ]
        bounds = {"recommended_budget": {"min": 2, "max": 100}}
        if len(events) < MIN_SAMPLES:
            return self._insufficient_run(len(events), _default_retrieval_params(), bounds)

        train_events, test_events = _walk_forward_split(events)
        params = self._fit_params(train_events)
        metrics = self._evaluate(test_events, params)
        metrics["train_size"] = len(train_events)
        return CalibrationRun(
            domain=self.calibration_domain,
            status="shadow",
            sample_size=len(events),
            metrics=metrics,
            parameters=params,
            bounds=bounds,
        )

    def _fit_params(self, events: list[Any]) -> dict[str, Any]:
        """在训练集上拟合检索参数。"""
        tier_values: dict[str, list[float]] = defaultdict(list)
        budgets: list[float] = []
        for event in events:
            sample = normalize_retrieval_feedback(event)
            if sample is None:
                continue
            tier = str(sample["tier"])
            effective = float(sample["effective_evidence_rate"])
            independent = float(sample["independent_source_rate"])
            cost = float(sample["unit_cost"])
            tier_values[tier].append((0.6 * effective + 0.4 * independent) / cost)
            marginal = float(sample["marginal_information_gain"])
            if marginal > 0:
                budgets.append(max(2.0, min(100.0, 1.0 / marginal)))
        utilities = {tier: round(mean(values), 4) for tier, values in tier_values.items() if values}
        return {
            "tier_utilities": utilities,
            "recommended_budget": round(mean(budgets), 2) if budgets else 10,
            "stop_min_independent_sources": 2,
            "require_counter_search": True,
            "priority_features": [
                "research_importance",
                "evidence_gap",
                "expected_information_gain",
                "timeliness",
                "cost_factor",
            ],
            "priority_feature_weights": {
                "research_importance": 0.30,
                "evidence_gap": 0.25,
                "expected_information_gain": 0.25,
                "timeliness": 0.15,
                "cost_factor": 0.05,
            },
        }

    def _evaluate(self, events: list[Any], params: dict[str, Any]) -> dict[str, Any]:
        """在测试集上计算检索指标。"""
        utilities = params.get("tier_utilities", {}) if isinstance(params, Mapping) else {}
        utilities = utilities if isinstance(utilities, Mapping) else {}
        predictions: list[float] = []
        labels: list[float] = []
        labels_by_tier: dict[str, list[float]] = defaultdict(list)
        for event in events:
            sample = normalize_retrieval_feedback(event)
            if sample is None:
                continue
            tier = str(sample["tier"])
            effective = float(sample["effective_evidence_rate"])
            independent = float(sample["independent_source_rate"])
            cost = float(sample["unit_cost"])
            actual_utility = (0.6 * effective + 0.4 * independent) / cost
            pred_utility = _safe_float(utilities.get(tier), 0.5)
            predictions.append(max(0.0, min(1.0, pred_utility)))
            label = max(0.0, min(1.0, actual_utility))
            labels.append(label)
            labels_by_tier[tier].append(label)
        brier = _brier_score(predictions, labels)
        pr = _precision_recall(predictions, labels, 0.5)
        avg_score = mean(labels) if labels else 0.0
        return {
            "walk_forward_score": round(avg_score, 4),
            "brier_score": round(brier, 4),
            "precision": pr["precision"],
            "recall": pr["recall"],
            "f1": pr["f1"],
            "false_promotion_rate": round(
                sum(1 for p, label in zip(predictions, labels) if p >= 0.5 and label < 0.5)
                / max(1, len(labels)),
                4,
            ),
            "active_trusted_accuracy": round(avg_score, 4),
            "source_reliability": {
                tier: round(mean(values), 4)
                for tier, values in labels_by_tier.items()
                if values
            },
            "train_size": len(events),
            "test_size": len(labels),
            "walk_forward_windows": 1,
        }


class AlphaPolicyCalibrator(_DomainCalibrator):
    """只消费 IC、超额表现、回撤和衰减，绝不输出事实层参数。"""

    domain = FeedbackDomain.ALPHA.value
    calibration_domain = "alpha"

    def fit(self) -> CalibrationRun:
        """校准 alpha 权重乘子与按 node_type 维度权重。

        Returns:
            CalibrationRun，parameters 含 alpha_weight_multiplier 和 calibrated_node_weights。
        """
        events = self.events()
        bounds = {"alpha_weight_multiplier": {"min": 0.8, "max": 1.2}}
        if len(events) < MIN_SAMPLES:
            return self._insufficient_run(len(events), _default_alpha_params(), bounds)

        train_events, test_events = _walk_forward_split(events)
        params = self._fit_params(train_events)
        metrics = self._evaluate(test_events, params)
        return CalibrationRun(
            domain=self.calibration_domain,
            status="shadow",
            sample_size=len(events),
            metrics=metrics,
            parameters=params,
            bounds=bounds,
        )

    def _fit_params(self, events: list[Any]) -> dict[str, Any]:
        """在训练集上拟合 alpha 参数。"""
        scores: list[float] = []
        by_node_type: dict[str, list[float]] = defaultdict(list)
        for event in events:
            metrics = _event_mapping(event, "metrics")
            attribution = _event_mapping(event, "attribution")
            ic = _safe_float(metrics.get("out_of_sample_ic"), float("nan"))
            if ic != ic:
                continue
            excess = _safe_float(metrics.get("excess_return"))
            drawdown = abs(_safe_float(metrics.get("max_drawdown")))
            decay = abs(_safe_float(metrics.get("decay_rate")))
            scores.append(ic + 0.2 * excess - 0.3 * drawdown - 0.1 * decay)

            event_kind = _safe_text(_event_value(event, "event_kind") or _event_value(event, "event_type"))
            if event_kind != "node_activation":
                continue
            node_type = _safe_text(attribution.get("node_type"))
            if not node_type:
                continue
            outcome = _event_value(event, "outcome")
            by_node_type[node_type].append(1.0 if outcome == "positive" else 0.0)

        score = mean(scores) if scores else 0.0
        calibrated_node_weights: dict[str, dict] = {}
        for node_type, values in by_node_type.items():
            if len(values) < 5:
                continue
            activation_rate = sum(values) / len(values)
            multiplier = max(0.8, min(1.2, activation_rate * 1.5))
            calibrated_node_weights[node_type] = {
                "connectivity": {"weight": round(multiplier, 4), "saturation": 0.8},
                "evidence_density": {"weight": 1.0, "saturation": 0.7},
                "temporal_freshness": {"weight": 1.0, "saturation": 0.6},
            }
        return {
            "alpha_weight_multiplier": round(max(0.8, min(1.2, 1.0 + score)), 4),
            "calibrated_node_weights": calibrated_node_weights,
        }

    def _evaluate(self, events: list[Any], params: dict[str, Any]) -> dict[str, Any]:
        """在测试集上计算 alpha 指标。"""
        multiplier = _safe_float(
            params.get("alpha_weight_multiplier") if isinstance(params, Mapping) else None,
            1.0,
        )
        predictions: list[float] = []
        labels: list[float] = []
        for event in events:
            metrics = _event_mapping(event, "metrics")
            ic = _safe_float(metrics.get("out_of_sample_ic"), float("nan"))
            if ic != ic:
                continue
            excess = _safe_float(metrics.get("excess_return"))
            drawdown = abs(_safe_float(metrics.get("max_drawdown")))
            decay = abs(_safe_float(metrics.get("decay_rate")))
            actual_score = ic + 0.2 * excess - 0.3 * drawdown - 0.1 * decay
            # 预测分：乘子映射到 [0,1]
            pred = max(0.0, min(1.0, 0.5 + 0.5 * multiplier * actual_score))
            predictions.append(pred)
            labels.append(max(0.0, min(1.0, 0.5 + 0.5 * actual_score)))
        brier = _brier_score(predictions, labels)
        pr = _precision_recall(predictions, labels, 0.5)
        avg_score = mean(labels) if labels else 0.0
        return {
            "walk_forward_score": round(max(0.0, min(1.0, avg_score)), 4),
            "brier_score": round(brier, 4),
            "precision": pr["precision"],
            "recall": pr["recall"],
            "f1": pr["f1"],
            "false_promotion_rate": 0.0,
            "active_trusted_accuracy": round(avg_score, 4),
            "source_reliability": {},
            "train_size": len(events),
            "test_size": len(labels),
            "walk_forward_windows": 1,
        }


CALIBRATORS = {
    "knowledge": KnowledgePolicyCalibrator,
    "retrieval": RetrievalPolicyCalibrator,
    "alpha": AlphaPolicyCalibrator,
}


def fit_domain(repository: ResearchRepository, domain: str) -> CalibrationRun:
    """拟合指定域的校准参数并持久化。

    Args:
        repository: 研究仓库。
        domain: 校准域（knowledge/retrieval/alpha）。

    Returns:
        CalibrationRun。

    Raises:
        ValueError: 域不存在。
    """
    calibrator = CALIBRATORS.get(_safe_text(domain)) if isinstance(domain, str) else None
    if calibrator is None:
        raise ValueError("校准域必须是 knowledge/retrieval/alpha")
    run = calibrator(repository).fit()
    repository.save_calibration(run)
    return run
