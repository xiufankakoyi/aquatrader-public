"""BayesianWeightCalibrator：贝叶斯逻辑回归权重校准。

核心思想：
- 使用贝叶斯逻辑回归（Laplace近似）从历史outcome数据学习权重
- 以当前硬编码权重为先验均值，先验强度由prior_strength控制
- 输出权重的后验分布（均值+方差），而非点估计
- 方差大的维度表示"不确定"，在评分时自动收缩到先验
- walk-forward验证防止过拟合

这不是直接最大化历史收益（那是过拟合），而是：
1. 以领域知识（硬编码权重）为先验
2. 数据量少时信任先验
3. 数据量充足时让数据说话
4. 始终给出不确定性估计
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.research.perilla.alpha.outcome_tracker import OutcomeTracker
from core.research.perilla.alpha.weight_registry import WeightRegistry

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Return a finite float without allowing malformed model data through."""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _feature_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(
        str(item).strip() for item in value if isinstance(item, str) and item.strip()
    ))


@dataclass
class WeightPosterior:
    """单个权重的后验分布。"""
    name: str
    prior_mean: float
    posterior_mean: float
    posterior_std: float
    effective_samples: float
    direction: str = "stable"  # increased / decreased / stable

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "prior_mean": self.prior_mean,
            "posterior_mean": self.posterior_mean, "posterior_std": self.posterior_std,
            "effective_samples": self.effective_samples, "direction": self.direction,
        }


@dataclass
class CalibrationResult:
    """校准结果。"""
    feature_names: list[str] = field(default_factory=list)
    posteriors: list[WeightPosterior] = field(default_factory=list)
    log_likelihood: float = 0.0
    log_prior: float = 0.0
    n_samples: int = 0
    n_positive: int = 0
    n_negative: int = 0
    walk_forward_score: float | None = None
    calibrated_at: str = ""
    calibrator_version: str = "1.0"

    def get_weight(self, name: str) -> float:
        """获取校准后的权重（后验均值）。"""
        for p in self.posteriors:
            if p.name == name:
                return p.posterior_mean
        return 0.0

    def get_weight_uncertainty(self, name: str) -> float:
        """获取权重不确定性（后验标准差）。"""
        for p in self.posteriors:
            if p.name == name:
                return p.posterior_std
        return 1.0

    def get_shrunken_weight(self, name: str, shrinkage_strength: float = 1.0) -> float:
        """获取收缩后的权重。

        当有效样本少时，权重向先验收缩：
            w_shrunken = (n_eff * w_post + k * w_prior) / (n_eff + k)
        """
        for p in self.posteriors:
            if p.name == name:
                n_eff = max(0.0, _finite_float(p.effective_samples))
                k = max(0.0, _finite_float(shrinkage_strength)) * 5.0
                denominator = n_eff + k
                if denominator == 0.0:
                    return _finite_float(p.prior_mean)
                return (n_eff * _finite_float(p.posterior_mean) + k * _finite_float(p.prior_mean)) / denominator
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_names": self.feature_names,
            "posteriors": [p.to_dict() for p in self.posteriors],
            "log_likelihood": self.log_likelihood,
            "log_prior": self.log_prior,
            "n_samples": self.n_samples,
            "n_positive": self.n_positive,
            "n_negative": self.n_negative,
            "walk_forward_score": self.walk_forward_score,
            "calibrated_at": self.calibrated_at,
            "calibrator_version": self.calibrator_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationResult":
        if not isinstance(data, Mapping):
            return cls()
        result = cls()
        result.feature_names = _feature_names(data.get("feature_names"))
        raw_posteriors = data.get("posteriors", [])
        if isinstance(raw_posteriors, (list, tuple)):
            for raw in raw_posteriors:
                if not isinstance(raw, Mapping):
                    continue
                name = raw.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                result.posteriors.append(WeightPosterior(
                    name=name.strip(),
                    prior_mean=_finite_float(raw.get("prior_mean")),
                    posterior_mean=_finite_float(raw.get("posterior_mean")),
                    posterior_std=max(0.0, _finite_float(raw.get("posterior_std"), 1.0)),
                    effective_samples=max(0.0, _finite_float(raw.get("effective_samples"))),
                    direction=str(raw.get("direction", "stable")),
                ))
        result.log_likelihood = _finite_float(data.get("log_likelihood"))
        result.log_prior = _finite_float(data.get("log_prior"))
        result.n_samples = max(0, int(_finite_float(data.get("n_samples"))))
        result.n_positive = max(0, int(_finite_float(data.get("n_positive"))))
        result.n_negative = data.get("n_negative", 0)
        result.n_negative = max(0, int(_finite_float(result.n_negative)))
        raw_score = data.get("walk_forward_score")
        result.walk_forward_score = (
            _finite_float(raw_score) if raw_score is not None else None
        )
        result.calibrated_at = str(data.get("calibrated_at", "") or "")
        return result


def _sigmoid(x: np.ndarray) -> np.ndarray:
    """数值稳定的sigmoid函数。"""
    return np.where(
        x >= 0,
        1.0 / (1.0 + np.exp(-np.clip(x, -50, 50))),
        np.exp(np.clip(x, -50, 50)) / (1.0 + np.exp(np.clip(x, -50, 50))),
    )


class BayesianWeightCalibrator:
    """贝叶斯逻辑回归权重校准器。

    使用Laplace近似（高斯近似后验）：
    - 先验：w ~ N(w_prior, (1/lambda) * I)，其中lambda = prior_strength
    - 后验通过IRLS（迭代重加权最小二乘）找到MAP估计
    - Hessian矩阵的逆作为后验协方差
    """

    def __init__(
        self,
        outcome_tracker: OutcomeTracker | None = None,
        weight_registry: WeightRegistry | None = None,
        model_path: str | Path | None = None,
    ) -> None:
        """初始化校准器。

        Args:
            outcome_tracker: 结果追踪器
            weight_registry: 权重注册表
            model_path: 模型保存路径
        """
        self.outcome_tracker = outcome_tracker
        self.registry = weight_registry or WeightRegistry.get_instance()
        self._model_path = Path(model_path) if model_path else (
            Path(__file__).resolve().parents[4] / "data" / "calibration_model.json"
        )
        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        self._result: CalibrationResult | None = None
        self._load_model()

    def _load_model(self) -> None:
        """加载已保存的校准模型。"""
        if self._model_path.exists():
            try:
                data = json.loads(self._model_path.read_text(encoding="utf-8"))
                self._result = CalibrationResult.from_dict(data)
                logger.info("Loaded calibration model from %s (n=%d)",
                           self._model_path, self._result.n_samples)
            except Exception as e:
                logger.warning("Failed to load calibration model: %s", e)

    def save_model(self) -> None:
        """保存校准模型到磁盘。"""
        if self._result:
            temp_path: Path | None = None
            try:
                fd, raw_temp_path = tempfile.mkstemp(
                    prefix=f".{self._model_path.name}.",
                    dir=self._model_path.parent,
                )
                temp_path = Path(raw_temp_path)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._result.to_dict(), handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self._model_path)
            except (OSError, TypeError, ValueError, OverflowError) as exc:
                logger.warning("Failed to save calibration model: %s", exc)
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

    def get_calibrated_weights(self) -> dict[str, float]:
        """获取校准后的权重（供评分使用）。

        Returns:
            特征名->收缩后权重
        """
        if self._result is None:
            return {}
        bayes_config = self.registry.get_bayesian_config()
        if not isinstance(bayes_config, Mapping):
            bayes_config = {}
        shrinkage = bayes_config.get("shrinkage", {})
        if not isinstance(shrinkage, Mapping):
            shrinkage = {}
        shrinkage_max = max(0.0, _finite_float(shrinkage.get("max_weight_deviation"), 0.15))
        weights = {}
        for p in self._result.posteriors:
            w = self._result.get_shrunken_weight(p.name)
            # 限制偏移幅度
            prior_w = self._get_prior_weight(p.name)
            max_dev = shrinkage_max
            w = max(prior_w - max_dev, min(prior_w + max_dev, w))
            weights[p.name] = w
        return weights

    def _get_prior_weight(self, feature_name: str) -> float:
        """从WeightRegistry获取先验权重。"""
        node_weights = self.registry.get_node_weights()
        node_weights = node_weights if isinstance(node_weights, Mapping) else {}
        config = node_weights.get(feature_name)
        if isinstance(config, Mapping):
            return _finite_float(config.get("weight"), 1.0)
        return 1.0

    def _get_feature_vector(self, features: dict[str, float], feature_names: list[str]) -> np.ndarray:
        """从features字典提取特征向量。"""
        features = features if isinstance(features, Mapping) else {}
        vec = np.zeros(len(feature_names))
        for i, name in enumerate(feature_names):
            vec[i] = _finite_float(features.get(name, 0.0))
        return vec

    def _prepare_training_data(
        self,
        feature_names: list[str],
        positive_label: str = "success",
        negative_label: str = "failure",
    ) -> tuple[np.ndarray, np.ndarray]:
        """从OutcomeTracker准备训练数据。

        Returns:
            (X, y) 特征矩阵和标签向量
        """
        if self.outcome_tracker is None:
            return np.array([]).reshape(0, len(feature_names)), np.array([])

        outcomes = self.outcome_tracker.get_labeled_outcomes(limit=5000)
        outcomes = outcomes if isinstance(outcomes, (list, tuple)) else []
        X_list = []
        y_list = []

        for outcome in outcomes:
            label_value = (
                outcome.get("outcome_label")
                if isinstance(outcome, Mapping)
                else getattr(outcome, "outcome_label", "")
            )
            if label_value == positive_label:
                label = 1
            elif label_value == negative_label:
                label = 0
            else:
                continue  # neutral跳过
            raw_features = (
                outcome.get("features", {})
                if isinstance(outcome, Mapping)
                else getattr(outcome, "features", {})
            )
            features = raw_features if isinstance(raw_features, Mapping) else {}
            x = self._get_feature_vector(features, feature_names)
            X_list.append(x)
            y_list.append(label)

        if not X_list:
            return np.array([]).reshape(0, len(feature_names)), np.array([])
        return np.array(X_list), np.array(y_list)

    def calibrate(
        self,
        feature_names: list[str] | None = None,
        prior_strength: float | None = None,
        max_iter: int = 50,
    ) -> CalibrationResult:
        """执行贝叶斯逻辑回归校准。

        使用IRLS找到MAP估计，用Hessian构造后验协方差。

        Args:
            feature_names: 要校准的特征名列表，None则使用所有node_scoring维度
            prior_strength: 先验强度，None则从配置读取
            max_iter: IRLS最大迭代次数

        Returns:
            CalibrationResult
        """
        if feature_names is None:
            node_weights = self.registry.get_node_weights()
            feature_names = _feature_names(list(node_weights.keys())) if isinstance(node_weights, Mapping) else []
        else:
            feature_names = _feature_names(feature_names)

        if prior_strength is None:
            bayes_config = self.registry.get_bayesian_config()
            if not isinstance(bayes_config, Mapping):
                bayes_config = {}
            prior_strength = _finite_float(
                bayes_config.get("prior_strength"), 10.0
            )
        prior_strength = max(1e-6, _finite_float(prior_strength, 10.0))
        max_iter = max(1, min(1000, int(_finite_float(max_iter, 50))))

        X, y = self._prepare_training_data(feature_names)

        result = CalibrationResult()
        result.feature_names = feature_names
        result.calibrated_at = datetime.now().isoformat()
        result.n_samples = len(y)
        result.n_positive = int(y.sum()) if len(y) > 0 else 0
        result.n_negative = result.n_samples - result.n_positive

        if len(y) < 10:
            # 数据太少，直接使用先验
            logger.info("Not enough data for calibration (n=%d), using priors", len(y))
            for name in feature_names:
                prior_w = self._get_prior_weight(name)
                result.posteriors.append(WeightPosterior(
                    name=name, prior_mean=prior_w, posterior_mean=prior_w,
                    posterior_std=1.0 / math.sqrt(prior_strength),
                    effective_samples=0.0, direction="stable",
                ))
            self._result = result
            return result

        # 添加bias项
        X_bias = np.hstack([X, np.ones((X.shape[0], 1))])
        n_features = X_bias.shape[1]
        n_actual = n_features - 1  # 不含bias

        # 先验均值（前n_actual维来自配置，bias先验=0）
        prior_means = np.zeros(n_features)
        for i, name in enumerate(feature_names):
            prior_means[i] = self._get_prior_weight(name)
        # bias的先验为0
        prior_means[-1] = -2.0  # 先验胜率低（约12%）

        # 先验精度矩阵（Lambda = prior_strength * I）
        prior_precision = prior_strength * np.eye(n_features)
        # bias的先验更弱
        prior_precision[-1, -1] = prior_strength * 0.1

        # IRLS求解MAP
        w = prior_means.copy()
        for _ in range(max_iter):
            # 预测概率
            z = X_bias @ w
            p = _sigmoid(z)
            # 权重矩阵 R = diag(p*(1-p))
            R = p * (1 - p)
            # Hessian = X^T R X + Lambda
            XTR = X_bias * R[:, np.newaxis]
            H = XTR.T @ X_bias + prior_precision
            # 梯度 = X^T(y-p) - Lambda*(w - w_prior)
            grad = X_bias.T @ (y - p) - prior_precision @ (w - prior_means)
            # Newton更新
            try:
                delta = np.linalg.solve(H, grad)
            except np.linalg.LinAlgError:
                delta = np.linalg.lstsq(H, grad, rcond=None)[0]
            w_new = w + delta
            if np.max(np.abs(delta)) < 1e-6:
                w = w_new
                break
            w = w_new

        # 后验协方差 = H^{-1}
        try:
            post_cov = np.linalg.inv(H)
        except np.linalg.LinAlgError:
            post_cov = np.linalg.pinv(H)
        post_std = np.sqrt(np.maximum(np.diag(post_cov), 1e-10))

        # 计算log-likelihood和log-prior
        z = X_bias @ w
        p = _sigmoid(z)
        ll = np.sum(y * np.log(p + 1e-10) + (1 - y) * np.log(1 - p + 1e-10))
        lp = -0.5 * (w - prior_means) @ prior_precision @ (w - prior_means)
        result.log_likelihood = float(ll)
        result.log_prior = float(lp)

        # 有效样本估计
        # 每个参数的有效样本 ≈ prior_strength / (post_std^2 * prior_strength)
        for i, name in enumerate(feature_names):
            prior_w = prior_means[i]
            post_w = w[i]
            post_s = post_std[i]
            # 有效样本：先验是n=prior_strength个观测，后验精度 = prior_precision + data_info
            # n_eff ≈ (1/post_var - prior_precision_i) / scale
            prior_prec_i = prior_strength
            post_prec_i = 1.0 / (post_s ** 2)
            n_eff = max(0.0, (post_prec_i - prior_prec_i) / max(0.1, prior_w ** 2) if abs(prior_w) > 0.01 else post_prec_i - prior_prec_i)
            n_eff = max(0.0, min(float(result.n_samples), n_eff))

            direction = "stable"
            if post_w > prior_w + 0.05:
                direction = "increased"
            elif post_w < prior_w - 0.05:
                direction = "decreased"

            result.posteriors.append(WeightPosterior(
                name=name,
                prior_mean=float(prior_w),
                posterior_mean=float(post_w),
                posterior_std=float(post_s),
                effective_samples=float(n_eff),
                direction=direction,
            ))

        # Walk-forward验证
        result.walk_forward_score = self._walk_forward_validate(
            feature_names, w, X_bias, y
        )

        self._result = result
        self.save_model()
        logger.info("Calibration complete: n=%d, pos=%d, neg=%d, ll=%.2f, wf_auc=%.3f",
                    result.n_samples, result.n_positive, result.n_negative,
                    result.log_likelihood, result.walk_forward_score or 0.0)
        return result

    def _walk_forward_validate(
        self,
        feature_names: list[str],
        w: np.ndarray,
        X: np.ndarray,
        y: np.ndarray,
    ) -> float | None:
        """简单的时间序列walk-forward验证（按顺序切分）。

        Returns:
            AUC分数，样本不足时返回None
        """
        if len(y) < 30:
            return None

        # 简单的80/20分割（前80%训练，后20%测试，模拟时间顺序）
        split = int(len(y) * 0.8)
        X_test = X[split:]
        y_test = y[split:]

        if len(np.unique(y_test)) < 2:
            return None

        # 用训练集预测测试集
        z_test = X_test @ w
        p_test = _sigmoid(z_test)

        # 计算AUC
        try:
            from sklearn.metrics import roc_auc_score
            auc = roc_auc_score(y_test, p_test)
            return float(auc)
        except ImportError:
            # 无sklearn时用简单的Mann-Whitney AUC
            return self._compute_auc_simple(y_test, p_test)
        except Exception:
            return None

    @staticmethod
    def _compute_auc_simple(y_true: np.ndarray, y_score: np.ndarray) -> float:
        """简单AUC计算（无sklearn依赖）。"""
        pos_scores = y_score[y_true == 1]
        neg_scores = y_score[y_true == 0]
        if len(pos_scores) == 0 or len(neg_scores) == 0:
            return 0.5
        scores = np.asarray(y_score, dtype=float)
        labels = np.asarray(y_true, dtype=int)
        order = np.argsort(scores, kind="mergesort")
        sorted_scores = scores[order]
        ranks = np.empty(len(scores), dtype=float)
        start = 0
        while start < len(scores):
            end = start + 1
            while end < len(scores) and sorted_scores[end] == sorted_scores[start]:
                end += 1
            ranks[order[start:end]] = (start + 1 + end) / 2.0
            start = end
        n_pos = int(np.sum(labels == 1))
        n_neg = int(np.sum(labels == 0))
        rank_sum = float(np.sum(ranks[labels == 1]))
        return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)

    def predict_success_probability(self, features: dict[str, float]) -> float:
        """预测一条路径的成功概率。

        Args:
            features: 特征字典

        Returns:
            成功概率（0-1）
        """
        if self._result is None:
            return 0.5
        features = features if isinstance(features, Mapping) else {}
        weights = self.get_calibrated_weights()
        feature_names = self._result.feature_names

        logit = -2.0  # bias
        for name in feature_names:
            w = weights.get(name, self._get_prior_weight(name))
            logit += _finite_float(w) * _finite_float(features.get(name, 0.0))

        return float(_sigmoid(np.array([logit]))[0])
