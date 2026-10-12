"""体制检测器(Regime Detector)— 报告层 2 缺口补齐。

报告要求:HMM / Markov-Switching 模型,带体制转换概率和持续性推断。
现状:QuantFlowPipeline.market_regime_detect 是纯规则阈值,无统计模型。

本模块用 hmmlearn GaussianHMM 学习 2-3 状态体制,
输出 regime_labels / regime_probs / transition_matrix / regime_names。
hmmlearn 缺失时降级为规则阈值(20 日收益 ±3%),保持接口一致。

不强制引入新依赖;requirements.txt 仅注释说明可选。
"""
from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _try_import_hmmlearn() -> Any | None:
    """懒加载 hmmlearn.GaussianHMM,缺失返回 None。"""
    try:
        from hmmlearn.hmm import GaussianHMM

        return GaussianHMM
    except Exception as exc:
        logger.debug("hmmlearn unavailable, RegimeDetector degrades to rule_threshold: %s", exc)
        return None


# 默认 regime 名称映射(按状态 ID 升序对应 mean 降序):
#   n_states=2: 状态 0(bull,高均值)→ "bull"; 状态 1(bear,低均值)→ "bear"
#   n_states=3: 加 "neutral"(中均值)
_DEFAULT_REGIME_NAMES: dict[int, str] = {
    0: "bull",
    1: "bear",
    2: "neutral",
}


@dataclass
class RegimeReport:
    """体制检测结果。

    Attributes:
        regime_labels: 每个样本的 regime 名称(如 "bull"/"bear"/"neutral"),长度 = 输入序列长度。
        regime_probs: 每个样本属于各状态的概率 [n_samples, n_states],每行和=1.0。
        transition_matrix: 状态转移矩阵 [n_states, n_states],每行和=1.0。
        regime_names: 状态 ID → regime 名称的映射。
        method: 检测方法,"hmm" 或 "rule_threshold"。
    """

    regime_labels: list[str] = field(default_factory=list)
    regime_probs: list[list[float]] = field(default_factory=list)
    transition_matrix: list[list[float]] = field(default_factory=list)
    regime_names: dict[int, str] = field(default_factory=dict)
    method: str = "rule_threshold"


class RegimeDetector:
    """体制检测器。

    Args:
        n_states: 隐状态数,2(bull/bear)或 3(bull/bear/neutral)。
        n_iter: HMM EM 算法最大迭代次数。
        random_state: 随机种子,保证可复现。
        window: 规则阈值降级路径的滚动窗口大小(默认 20 日)。
        threshold: 规则阈值路径的累积收益阈值(默认 0.03 = ±3%)。
    """

    def __init__(
        self,
        n_states: int = 2,
        n_iter: int = 100,
        random_state: int = 42,
        *,
        window: int = 20,
        threshold: float = 0.03,
    ) -> None:
        if n_states not in (2, 3):
            raise ValueError("n_states must be 2 or 3")
        self.n_states = n_states
        self.n_iter = n_iter
        self.random_state = random_state
        self.window = window
        self.threshold = threshold
        self._gaussian_hmm_cls = _try_import_hmmlearn()
        self._model: Any = None
        # HMM 状态 ID → regime 名称的映射(拟合后根据 means_ 排序确定)
        self._state_to_name: dict[int, str] = dict(_DEFAULT_REGIME_NAMES)

    def is_hmm_available(self) -> bool:
        """返回 hmmlearn 是否可用。"""
        return self._gaussian_hmm_cls is not None

    def fit(self, returns: list[float]) -> "RegimeDetector":
        """拟合模型。

        HMM 路径:用 GaussianHMM 拟合收益率序列,根据 means_ 排序确定状态名映射。
        规则阈值路径:无需拟合,直接记录参数。

        Args:
            returns: 日收益率序列(如 [0.01, -0.02, ...])。

        Returns:
            self,支持链式调用。
        """
        if not returns:
            raise ValueError("returns must not be empty")

        if self._gaussian_hmm_cls is None:
            # 规则阈值路径无需拟合
            return self

        try:
            import numpy as np

            X = np.array(returns, dtype=float).reshape(-1, 1)
            model = self._gaussian_hmm_cls(
                n_components=self.n_states,
                n_iter=self.n_iter,
                random_state=self.random_state,
                covariance_type="diag",
            )
            model.fit(X)
            self._model = model
            # 根据 means_ 降序排序:最高均值 → bull,最低 → bear,中间 → neutral
            means = np.array(model.means_).flatten()
            sorted_idx = np.argsort(-means)  # 降序
            self._state_to_name = {
                int(state_id): _DEFAULT_REGIME_NAMES.get(rank, "neutral")
                for rank, state_id in enumerate(sorted_idx)
            }
        except Exception as exc:
            logger.warning("HMM fit failed, falling back to rule_threshold: %s", exc)
            self._model = None
            self._gaussian_hmm_cls = None  # 触发后续 detect 走降级路径
        return self

    def detect(self, returns: list[float]) -> RegimeReport:
        """对收益率序列做体制检测。

        HMM 路径:predict 返回状态 ID,再映射到 regime 名称。
        规则阈值路径:用滚动窗口累积收益 ±threshold 判断 bull/bear/neutral。

        Args:
            returns: 日收益率序列。

        Returns:
            RegimeReport。
        """
        if not returns:
            return RegimeReport(
                regime_names=dict(self._state_to_name),
                method="rule_threshold",
            )

        if self._gaussian_hmm_cls is not None and self._model is not None:
            return self._detect_hmm(returns)
        return self._detect_rule_threshold(returns)

    def save(self, path: str) -> None:
        """保存拟合后的模型到 pickle 文件。

        Args:
            path: 目标文件路径。
        """
        payload = {
            "n_states": self.n_states,
            "n_iter": self.n_iter,
            "random_state": self.random_state,
            "window": self.window,
            "threshold": self.threshold,
            "model": self._model,
            "state_to_name": self._state_to_name,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(payload, f)

    def load(self, path: str) -> "RegimeDetector":
        """从 pickle 文件加载模型。

        Args:
            path: 模型文件路径。

        Returns:
            self。
        """
        with open(path, "rb") as f:
            payload = pickle.load(f)
        self.n_states = payload["n_states"]
        self.n_iter = payload["n_iter"]
        self.random_state = payload["random_state"]
        self.window = payload["window"]
        self.threshold = payload["threshold"]
        self._model = payload["model"]
        self._state_to_name = payload["state_to_name"]
        # 模型存在时,需要重新绑定 GaussianHMM 类供 detect 路径判断
        if self._model is not None and self._gaussian_hmm_cls is None:
            self._gaussian_hmm_cls = _try_import_hmmlearn()
        return self

    # ---------- 内部方法 ----------

    def _detect_hmm(self, returns: list[float]) -> RegimeReport:
        """HMM 路径:用拟合的 GaussianHMM 做状态预测。"""
        import numpy as np

        X = np.array(returns, dtype=float).reshape(-1, 1)
        model = self._model
        state_ids = model.predict(X)
        probs = model.predict_proba(X) if hasattr(model, "predict_proba") else None

        regime_labels: list[str] = []
        for sid in state_ids:
            name = self._state_to_name.get(int(sid), "neutral")
            regime_labels.append(name)

        regime_probs: list[list[float]] = []
        if probs is not None:
            for row in probs:
                regime_probs.append([float(p) for p in row])
        else:
            # 退化:用 one-hot 表示
            for sid in state_ids:
                one_hot = [0.0] * self.n_states
                one_hot[int(sid)] = 1.0
                regime_probs.append(one_hot)

        transmat = getattr(model, "transmat_", None)
        if transmat is not None:
            transition_matrix = [[float(p) for p in row] for row in transmat]
        else:
            transition_matrix = self._identity_transition_matrix()

        return RegimeReport(
            regime_labels=regime_labels,
            regime_probs=regime_probs,
            transition_matrix=transition_matrix,
            regime_names=dict(self._state_to_name),
            method="hmm",
        )

    def _detect_rule_threshold(self, returns: list[float]) -> RegimeReport:
        """规则阈值路径:用滚动窗口累积收益判断 regime。

        - 累积收益 > +threshold → "bull"
        - 累积收益 < -threshold → "bear"
        - 其他 → "neutral"(n_states=3)或基于符号二分(n_states=2)

        Args:
            returns: 日收益率序列。

        Returns:
            RegimeReport,method="rule_threshold"。
        """
        n = len(returns)
        regime_labels: list[str] = []
        regime_probs: list[list[float]] = []
        window = self.window
        threshold = self.threshold

        for i in range(n):
            start = max(0, i - window + 1)
            window_returns = returns[start : i + 1]
            cum_return = sum(window_returns)
            label = self._classify_by_threshold(cum_return)
            regime_labels.append(label)
            # 简单概率:命中状态 0.85,其他 0.15/(n_states-1)
            probs = [0.15 / (self.n_states - 1)] * self.n_states if self.n_states > 1 else [1.0]
            probs[self._name_to_state_id(label)] = 0.85
            regime_probs.append(probs)

        return RegimeReport(
            regime_labels=regime_labels,
            regime_probs=regime_probs,
            transition_matrix=self._identity_transition_matrix(),
            regime_names=dict(self._state_to_name),
            method="rule_threshold",
        )

    def _classify_by_threshold(self, cum_return: float) -> str:
        """根据累积收益与阈值判定 regime。"""
        if cum_return > self.threshold:
            return "bull"
        if cum_return < -self.threshold:
            return "bear"
        if self.n_states == 3:
            return "neutral"
        # n_states=2 时,中性区间按符号二分
        return "bull" if cum_return >= 0 else "bear"

    def _name_to_state_id(self, name: str) -> int:
        """regime 名称 → 状态 ID(逆映射)。"""
        for sid, nm in self._state_to_name.items():
            if nm == name:
                return sid
        return 0

    def _identity_transition_matrix(self) -> list[list[float]]:
        """生成近似单位矩阵(对角 0.9,非对角 0.1/(n-1))。"""
        if self.n_states == 1:
            return [[1.0]]
        diag = 0.9
        off = (1.0 - diag) / (self.n_states - 1)
        return [
            [diag if i == j else off for j in range(self.n_states)]
            for i in range(self.n_states)
        ]
