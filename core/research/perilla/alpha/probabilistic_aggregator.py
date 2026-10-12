"""ProbabilisticAggregator：概率聚合替代线性加权。

核心思想：
- Noisy-OR模型：多个独立证据指向同一结论时，P(alpha) = 1 - ∏(1 - p_i * w_i_norm)
  天然处理多证据协同效应和"多个弱证据不等于强证据"的问题
- 乘法惩罚：冲突/过热等惩罚作为折扣因子乘在总分上，而非减法
- 保序回归(Isotonic Regression)校准：将原始分数映射为真实概率
  确保0.7分的路径确实有大约70%的实证胜率
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _probability(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite_float(value, default)))


def noisy_or_aggregate(
    features: dict[str, float],
    weights: dict[str, dict],
    prior: float = 0.05,
) -> float:
    """使用Noisy-OR模型聚合多个正向特征。

    Noisy-OR模型假设各特征独立地"激活"alpha结论：
        P(alpha | f1,...,fn) = 1 - (1 - prior) * ∏(1 - activation_i)
    其中 activation_i = sigmoid(weight_i * (f_i - threshold_i))

    相比线性加权，Noisy-OR有以下优势：
    1. 天然输出概率值，范围[0,1]
    2. 高置信度多证据有协同效应（公告+研报>两者简单相加）
    3. 单个0.3的弱证据几乎不激活，但多个弱证据可以积累
    4. 不会出现权重加总>1导致分数超过1的问题

    Args:
        features: 特征名->分数（0-1）
        weights: 特征名->{weight: float, saturation: float}
        prior: 先验概率（无任何证据时的基础概率）

    Returns:
        聚合概率（0-1）
    """
    prior = min(1.0 - 1e-10, _probability(prior, 0.05))
    if not isinstance(features, Mapping) or not features:
        return prior

    log_one_minus = math.log(max(1e-10, 1.0 - prior))

    for feat_name, feat_value in features.items():
        feat_value = _probability(feat_value)
        w_config = weights.get(feat_name, {}) if isinstance(weights, Mapping) else {}
        if not isinstance(w_config, Mapping):
            w_config = {}
        w = max(0.0, _finite_float(w_config.get("weight", 1.0), 1.0))
        sat = max(0.1, _finite_float(w_config.get("saturation", 0.8), 0.8))

        # Sigmoid激活：分数越高激活越强烈，但有饱和
        # activation = w * feat_value，但使用平滑饱和函数
        # 当feat_value接近saturation时激活接近w
        # 当feat_value接近0时激活接近0
        if feat_value <= 0:
            continue
        # 使用压缩的线性激活：p_i = tanh(feat_value * w / sat) * sat
        # 这样保证单特征最大激活为sat（由权重配置控制）
        p_i = math.tanh(feat_value * w / max(0.1, sat)) * sat
        p_i = max(0.0, min(1.0 - 1e-10, p_i))
        log_one_minus += math.log(1.0 - p_i)

    prob = 1.0 - math.exp(log_one_minus)
    return _probability(prob)


def apply_multiplicative_penalties(
    base_score: float,
    penalties: dict[str, float],
    penalty_configs: dict[str, dict],
) -> float:
    """应用乘法惩罚。

    每个惩罚是一个[0, max_decay]的值，作为折扣因子乘在基础分数上：
        score = base * ∏(1 - penalty_i * scale_i)

    相比减法，乘法惩罚的优势：
    1. 惩罚是比例性的，高base分数惩罚比例相同
    2. 多个惩罚叠加时自然衰减，不会减到负数
    3. 符合"折扣"语义：冲突证据打8折，过热打7折等

    Args:
        base_score: 基础分数（0-1）
        penalties: 惩罚名->惩罚值（0-1，0表示无惩罚，1表示最大惩罚）
        penalty_configs: 惩罚名->{max_decay: float}

    Returns:
        惩罚后分数
    """
    score = _probability(base_score)
    if not isinstance(penalties, Mapping):
        return score
    for pen_name, pen_value in penalties.items():
        pen_value = _probability(pen_value)
        config = penalty_configs.get(pen_name, {}) if isinstance(penalty_configs, Mapping) else {}
        if not isinstance(config, Mapping):
            config = {}
        max_decay = _probability(config.get("max_decay", 0.3), 0.3)
        decay = pen_value * max_decay
        score *= (1.0 - decay)
    return _probability(score)


def apply_vetoes(
    score: float,
    vetoes: dict[str, bool],
) -> float:
    """应用一票否决。

    Args:
        score: 当前分数
        vetoes: 否决项名->是否触发

    Returns:
        如任何veto触发则返回0.0，否则返回原分数
    """
    score = _probability(score)
    if not isinstance(vetoes, Mapping):
        return score
    for triggered in vetoes.values():
        if isinstance(triggered, bool) and triggered:
            return 0.0
    return score


# ---------------------------------------------------------------------------
# 保序回归校准（Isotonic Regression）
# ---------------------------------------------------------------------------

class IsotonicCalibrator:
    """保序回归校准器，将原始分数映射为真实经验概率。

    使用PAV (Pool Adjacent Violators) 算法实现。
    不需要测试时scikit-learn依赖，纯numpy实现。

    原理：给定 (score_i, label_i) 对，学习一个单调非减映射 f，
    使得 f(score_i) 是该分数段的真实胜率。
    这保证了"分数越高胜率越高"的单调性。
    """

    def __init__(self) -> None:
        self._thresholds: np.ndarray = np.array([])
        self._calibrated: np.ndarray = np.array([])
        self._fitted = False
        self._pseudo_count = 5  # 贝叶斯平滑的伪计数

    def fit(self, scores: np.ndarray, labels: np.ndarray) -> "IsotonicCalibrator":
        """使用PAV算法拟合校准映射。

        Args:
            scores: 原始分数数组（未排序）
            labels: 二值标签数组（0/1）

        Returns:
            self
        """
        try:
            scores = np.asarray(scores, dtype=np.float64).reshape(-1)
            labels = np.asarray(labels, dtype=np.float64).reshape(-1)
        except (TypeError, ValueError, OverflowError):
            logger.warning("Invalid input for isotonic calibration")
            return self

        if scores.size != labels.size:
            logger.warning("Invalid input for isotonic calibration")
            return self

        valid = np.isfinite(scores) & np.isfinite(labels)
        scores = scores[valid]
        labels = np.clip(labels[valid], 0.0, 1.0)

        if len(scores) != len(labels) or len(scores) == 0:
            logger.warning("Invalid input for isotonic calibration")
            return self

        # 按分数排序
        order = np.argsort(scores)
        sorted_scores = scores[order]
        sorted_labels = labels[order]

        # PAV算法
        # 每个块: (sum_labels, sum_weights, score_start, score_end)
        blocks: list[list[float]] = []
        for i in range(len(sorted_scores)):
            # 添加新块，加入贝叶斯平滑（2个伪样本：1正1负）
            blocks.append([
                sorted_labels[i] * 1.0 + self._pseudo_count / 2 * 0.5,  # 加权正例
                1.0 + self._pseudo_count / 2,  # 权重
                sorted_scores[i],
                sorted_scores[i],
            ])

            # 合并违反单调性的相邻块
            while len(blocks) >= 2:
                prev = blocks[-2]
                curr = blocks[-1]
                prev_rate = prev[0] / prev[1]
                curr_rate = curr[0] / curr[1]
                if prev_rate > curr_rate + 1e-10:  # 违反单调性
                    # 合并
                    merged = [
                        prev[0] + curr[0],
                        prev[1] + curr[1],
                        prev[2],
                        curr[3],
                    ]
                    blocks.pop()
                    blocks.pop()
                    blocks.append(merged)
                else:
                    break

        # 提取校准映射
        thresholds = []
        calibrated = []
        for block in blocks:
            rate = block[0] / block[1]
            thresholds.append((block[2] + block[3]) / 2)
            calibrated.append(rate)

        self._thresholds = np.array(thresholds)
        self._calibrated = np.array(calibrated)
        self._fitted = True
        return self

    def transform(self, scores: np.ndarray | list[float] | float) -> np.ndarray | float:
        """将原始分数转换为校准概率。

        Args:
            scores: 原始分数

        Returns:
            校准后的概率
        """
        if not self._fitted or len(self._thresholds) == 0:
            # 未校准时使用恒等映射
            is_scalar = np.isscalar(scores)
            try:
                result = np.asarray(scores, dtype=np.float64)
            except (TypeError, ValueError, OverflowError):
                result = np.asarray(0.0 if is_scalar else [], dtype=np.float64)
            result = np.nan_to_num(result, nan=0.0, posinf=1.0, neginf=0.0)
            return float(result.reshape(-1)[0]) if is_scalar else result

        is_scalar = np.isscalar(scores)
        try:
            s = np.atleast_1d(np.asarray(scores, dtype=np.float64))
        except (TypeError, ValueError, OverflowError):
            s = np.zeros(1 if is_scalar else 0, dtype=np.float64)
        s = np.nan_to_num(s, nan=0.0, posinf=1.0, neginf=0.0)

        # 插值
        # 低于最小阈值使用最小值，高于最大阈值使用最大值
        result = np.interp(s, self._thresholds, self._calibrated,
                          left=self._calibrated[0], right=self._calibrated[-1])
        result = np.clip(result, 0.0, 1.0)

        if is_scalar:
            return float(result[0])
        return result

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "thresholds": self._thresholds.tolist(),
            "calibrated": self._calibrated.tolist(),
            "fitted": self._fitted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IsotonicCalibrator":
        """从字典反序列化。"""
        cal = cls()
        if isinstance(data, Mapping) and data.get("fitted"):
            try:
                thresholds = np.asarray(data.get("thresholds", []), dtype=np.float64).reshape(-1)
                calibrated = np.asarray(data.get("calibrated", []), dtype=np.float64).reshape(-1)
            except (TypeError, ValueError, OverflowError):
                return cal
            if (
                len(thresholds) > 0
                and len(thresholds) == len(calibrated)
                and np.isfinite(thresholds).all()
                and np.isfinite(calibrated).all()
                and np.all(np.diff(thresholds) >= 0)
            ):
                cal._thresholds = thresholds
                cal._calibrated = np.clip(calibrated, 0.0, 1.0)
                cal._fitted = True
        return cal


# ---------------------------------------------------------------------------
# 组合评分函数
# ---------------------------------------------------------------------------

def score_node_probabilistic(
    positive_features: dict[str, float],
    penalty_features: dict[str, float],
    veto_features: dict[str, bool],
    pos_weights: dict[str, dict],
    pen_configs: dict[str, dict],
    prior: float = 0.05,
    calibrator: IsotonicCalibrator | None = None,
) -> dict[str, Any]:
    """概率方式计算节点分数。

    Args:
        positive_features: 正向特征名->分数
        penalty_features: 惩罚特征名->惩罚值
        veto_features: 否决特征名->是否触发
        pos_weights: 正向特征权重配置
        pen_configs: 惩罚配置
        prior: 先验概率
        calibrator: 可选的保序回归校准器

    Returns:
        {"raw_score": 原始Noisy-OR分数, "final_score": 校准后分数,
         "positive_prob": 正向聚合概率, "penalty_factor": 乘积惩罚因子}
    """
    # 1. Noisy-OR聚合正向证据
    pos_prob = noisy_or_aggregate(positive_features, pos_weights, prior=prior)

    # 2. 乘法惩罚
    penalized = apply_multiplicative_penalties(pos_prob, penalty_features, pen_configs)

    # 3. 一票否决
    after_veto = apply_vetoes(penalized, veto_features)

    # 4. 保序校准
    if calibrator is not None and getattr(calibrator, "_fitted", False):
        try:
            final = _probability(calibrator.transform(after_veto))
        except (AttributeError, TypeError, ValueError, RuntimeError):
            final = after_veto
    else:
        final = after_veto

    # 计算惩罚因子（用于解释）
    penalty_factor = after_veto / max(pos_prob, 1e-10) if pos_prob > 0 else 1.0

    return {
        "raw_score": after_veto,
        "final_score": final,
        "positive_prob": pos_prob,
        "penalty_factor": penalty_factor,
        "calibrated": calibrator is not None and getattr(calibrator, "_fitted", False),
    }


def compute_temporal_weight(
    event_age_days: float,
    half_life_days: float,
    kernel_type: str = "exponential",
) -> float:
    """计算时间衰减权重（指数核）。

    指数核：w = exp(-ln(2) * age / half_life)
    - age=0（当天）: w=1.0
    - age=half_life: w=0.5
    - age→∞: w→0

    替代硬编码分段函数，实现平滑衰减。

    Args:
        event_age_days: 事件年龄（天）
        half_life_days: 半衰期（天）
        kernel_type: 核类型（目前支持exponential）

    Returns:
        时间权重（0-1）
    """
    event_age_days = _finite_float(event_age_days, 0.0)
    half_life_days = _finite_float(half_life_days, 0.0)
    if event_age_days <= 0:
        return 1.0
    if half_life_days <= 0:
        return 1.0
    if kernel_type == "exponential":
        return math.exp(-math.log(2) * event_age_days / half_life_days)
    return math.exp(-math.log(2) * event_age_days / half_life_days)


def hawkes_excitation(
    event_times: list[float],
    current_time: float,
    alpha: float = 0.1,
    beta: float = 0.05,
    max_excitation: float = 0.3,
) -> float:
    """计算Hawkes过程自激效应。

    事件到达会暂时提高相关事件的强度，按指数衰减：
        excitation = sum over past events: alpha * exp(-beta * (t - t_i))

    这模拟了：
    - 利好密集出现时，市场情绪叠加（催化叠加）
    - 事件影响随时间衰减

    Args:
        event_times: 过去事件的时间（天，相对值）
        current_time: 当前时间（天）
        alpha: 每次事件的激发强度
        beta: 激发衰减速率（天^-1）
        max_excitation: 最大激发加成

    Returns:
        激发值（0到max_excitation）
    """
    current_time = _finite_float(current_time, 0.0)
    alpha = max(0.0, _finite_float(alpha, 0.1))
    beta = max(0.0, _finite_float(beta, 0.05))
    max_excitation = max(0.0, _finite_float(max_excitation, 0.3))
    total = 0.0
    for t_i in event_times if isinstance(event_times, (list, tuple)) else ():
        t_i = _finite_float(t_i, float("nan"))
        if not math.isfinite(t_i):
            continue
        delta = current_time - t_i
        if delta < 0:
            continue
        total += alpha * math.exp(-beta * delta)
    return min(max_excitation, total)
