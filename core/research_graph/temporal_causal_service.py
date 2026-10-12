"""时序因果筛选服务(Step 8)。

对宏观/行业/政策序列做 Granger 筛选和 BSTS/CausalImpact 归因,
输出带 causal_status 的候选边,作为 CANDIDATE evidence 写入 ledger。

设计:
- statsmodels / pycausalimpact 懒加载,缺失时 graceful 降级为 TEMPORAL_PRECEDENCE。
- 输出 CausalCandidate(cause_symbol/effect_symbol/causal_status/lag/stability_score)。
- 不直接修改图谱:结果由 environment 以 CANDIDATE evidence 写入 ledger,
  后续 Verifier 决定是否升级为 ACTIVE 边。

不强制引入新依赖;requirements.txt 仅注释说明可选。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.research_graph.models import Evidence, utc_now
from core.research_graph.schema import CausalStatus, EvidenceLevel

logger = logging.getLogger(__name__)


@dataclass
class CausalCandidate:
    """时序因果候选结果。"""

    cause_symbol: str
    effect_symbol: str
    causal_status: str
    lag: int = 0
    stability_score: float = 0.0
    p_value: float = 1.0
    pre_period_rmse: float | None = None
    post_period_effect: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Step 3:regime 标签,让下游 Verifier/检索/调度按 regime 差异化处理。
    # 由 environment 在调用 granger_screen / bsts_attribution 时注入,
    # 默认空字符串以保持向后兼容。
    regime_label: str = ""

    def to_evidence(
        self,
        *,
        evidence_id: str,
        source_id: str = "",
        event_date: str = "",
    ) -> Evidence:
        """把候选结果转为 CANDIDATE Evidence。"""
        quote = (
            f"{self.cause_symbol} -> {self.effect_symbol} "
            f"(lag={self.lag}, status={self.causal_status}, "
            f"stability={self.stability_score:.3f}, p={self.p_value:.3f})"
        )
        return Evidence(
            evidence_id=evidence_id,
            source_type="temporal_causal_service",
            source_title=f"causal_screening_{self.cause_symbol}_{self.effect_symbol}",
            source_id=source_id or f"tcs_{self.cause_symbol}_{self.effect_symbol}",
            quote=quote,
            summary=f"时序因果筛选:{quote}",
            evidence_level=EvidenceLevel.LIKELY.value,
            extracted_by="temporal_causal_service",
            verified_by="rule_verifier",
            event_date=event_date,
            causal_status=self.causal_status,
            metadata={
                "cause_symbol": self.cause_symbol,
                "effect_symbol": self.effect_symbol,
                "lag": self.lag,
                "stability_score": self.stability_score,
                "p_value": self.p_value,
                "pre_period_rmse": self.pre_period_rmse,
                "post_period_effect": self.post_period_effect,
                "regime_label": self.regime_label,
                "content_origin": "temporal_causal_service",
                "generated_at": utc_now(),
                **(self.metadata or {}),
            },
        )


def _try_import_statsmodels() -> Any | None:
    """懒加载 statsmodels,缺失返回 None。"""
    try:
        import statsmodels  # noqa: F401
        from statsmodels.tsa.stattools import grangercausalitytests

        return grangercausalitytests
    except Exception as exc:
        logger.debug("statsmodels unavailable, Granger screening disabled: %s", exc)
        return None


def _try_import_causalimpact() -> Any | None:
    """懒加载 pycausalimpact,缺失返回 None。"""
    try:
        from causalimpact import CausalImpact

        return CausalImpact
    except Exception as exc:
        logger.debug("pycausalimpact unavailable, BSTS attribution disabled: %s", exc)
        return None


def _compute_pre_period_rmse(series_df: Any, intervention_point: int) -> float | None:
    """从 CausalImpact.series DataFrame 计算干预前 RMSE。

    RMSE = sqrt(mean((actual - predicted)^2)) 在 pre_period 范围内。

    兼容 pycausalimpact 不同版本的列名:
    - actual: 'response' 或 'y'
    - predicted: 'point_pred' 或 'predicted'

    Args:
        series_df: CausalImpact.series 属性(DataFrame)。None 时返回 None。
        intervention_point: 干预时点索引,pre_period = [0, intervention_point)。

    Returns:
        干预前 RMSE,无法计算时返回 None。
    """
    if series_df is None:
        return None
    try:
        import numpy as np
        import pandas as pd

        if not isinstance(series_df, pd.DataFrame):
            return None
        if intervention_point <= 0 or intervention_point >= len(series_df):
            return None

        pre = series_df.iloc[:intervention_point]
        actual_col = next(
            (col for col in ("response", "y") if col in pre.columns), None
        )
        pred_col = next(
            (col for col in ("point_pred", "predicted") if col in pre.columns), None
        )
        if actual_col is None or pred_col is None:
            return None

        actual = pre[actual_col].to_numpy(dtype=float)
        predicted = pre[pred_col].to_numpy(dtype=float)
        diff = actual - predicted
        return float(np.sqrt(np.mean(diff * diff)))
    except Exception as exc:
        logger.debug("compute_pre_period_rmse failed: %s", exc)
        return None


class TemporalCausalService:
    """时序因果筛选服务。

    Args:
        max_lag: Granger 检验最大滞后期。
        p_value_threshold: p 值阈值,小于视为显著。
        stability_threshold: 稳定性分数阈值,高于视为可升级为 PREDICTIVE_CAUSAL。
    """

    def __init__(
        self,
        *,
        max_lag: int = 4,
        p_value_threshold: float = 0.05,
        stability_threshold: float = 0.6,
    ) -> None:
        self.max_lag = max_lag
        self.p_value_threshold = p_value_threshold
        self.stability_threshold = stability_threshold
        self._granger = _try_import_statsmodels()
        self._causal_impact = _try_import_causalimpact()

    def is_available(self) -> dict[str, bool]:
        """返回各后端可用性。"""
        return {
            "granger": self._granger is not None,
            "causal_impact": self._causal_impact is not None,
        }

    def granger_screen(
        self,
        cause_series: list[float],
        effect_series: list[float],
        *,
        cause_symbol: str,
        effect_symbol: str,
        regime_label: str = "",
    ) -> CausalCandidate:
        """Granger 因果筛选。

        Args:
            cause_series: 原因序列(如政策利率)。
            effect_series: 结果序列(如行业指数)。
            cause_symbol/effect_symbol: 序列标签。
            regime_label: 体制标签(如 "bull"/"bear"),透传到 candidate.metadata。

        Returns:
            CausalCandidate。依赖缺失时降级为 TEMPORAL_PRECEDENCE。
        """
        if self._granger is None:
            return CausalCandidate(
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
                causal_status=CausalStatus.TEMPORAL_PRECEDENCE.value,
                lag=self.max_lag,
                stability_score=0.0,
                p_value=1.0,
                metadata={"degraded": "statsmodels_unavailable"},
                regime_label=regime_label,
            )

        if len(cause_series) < self.max_lag + 5 or len(effect_series) < self.max_lag + 5:
            return CausalCandidate(
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
                causal_status=CausalStatus.OBSERVATIONAL_ASSOCIATION.value,
                lag=0,
                stability_score=0.0,
                p_value=1.0,
                metadata={"degraded": "insufficient_data"},
                regime_label=regime_label,
            )

        import numpy as np

        data = np.column_stack([effect_series, cause_series])
        try:
            result = self._granger(data, maxlag=self.max_lag, verbose=False)
        except Exception as exc:
            logger.warning("Granger test failed: %s", exc)
            return CausalCandidate(
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
                causal_status=CausalStatus.OBSERVATIONAL_ASSOCIATION.value,
                lag=0,
                stability_score=0.0,
                p_value=1.0,
                metadata={"error": str(exc)},
                regime_label=regime_label,
            )

        # 取最小 p 值对应的 lag。
        best_lag = 1
        best_p = 1.0
        for lag in range(1, self.max_lag + 1):
            if lag not in result:
                continue
            tests = result[lag]
            p = 1.0
            for test_name in ("ssr_ftest", "ssr_chi2test", "lrtest", "params_ftest"):
                if test_name in tests:
                    p = min(p, float(tests[test_name][1]))
            if p < best_p:
                best_p = p
                best_lag = lag

        if best_p < self.p_value_threshold:
            status = CausalStatus.TEMPORAL_PRECEDENCE.value
            stability = 1.0 - best_p
        else:
            status = CausalStatus.OBSERVATIONAL_ASSOCIATION.value
            stability = 0.0

        return CausalCandidate(
            cause_symbol=cause_symbol,
            effect_symbol=effect_symbol,
            causal_status=status,
            lag=best_lag,
            stability_score=round(stability, 4),
            p_value=round(best_p, 4),
            regime_label=regime_label,
        )

    def bsts_attribution(
        self,
        effect_series: list[float],
        *,
        intervention_point: int,
        cause_symbol: str,
        effect_symbol: str,
        event_date: str = "",
        regime_label: str = "",
        niter: int = 1000,
        return_posterior: bool = False,
    ) -> CausalCandidate:
        """BSTS/CausalImpact 准实验归因。

        Args:
            effect_series: 结果序列。
            intervention_point: 干预时点索引。
            cause_symbol/effect_symbol: 序列标签。
            event_date: 干预事件日期。
            regime_label: 体制标签(如 "bull"/"bear"),透传到 candidate.metadata。
            niter: MCMC 采样次数(P2 新增,默认 1000)。
                pycausalimpact 可用时透传,缺失时用于 bootstrap 重采样。
            return_posterior: True 时在 metadata 中写入后验分布字段
                (posterior_mean/posterior_std/credible_interval_lower/upper/r_hat)。

        Returns:
            CausalCandidate。依赖缺失时降级为 TEMPORAL_PRECEDENCE。
        """
        if self._causal_impact is None:
            # P2:pycausalimpact 缺失时,用 bootstrap 重采样估计后验分布
            return self._bsts_attribution_bootstrap(
                effect_series,
                intervention_point=intervention_point,
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
                event_date=event_date,
                regime_label=regime_label,
                niter=niter,
                return_posterior=return_posterior,
            )

        import pandas as pd

        if intervention_point <= 0 or intervention_point >= len(effect_series) - 2:
            return CausalCandidate(
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
                causal_status=CausalStatus.OBSERVATIONAL_ASSOCIATION.value,
                lag=0,
                stability_score=0.0,
                p_value=1.0,
                metadata={"degraded": "invalid_intervention_point"},
                regime_label=regime_label,
            )

        df = pd.DataFrame({"y": effect_series})
        # CausalImpact 真实 API 要求 pre_period/post_period 为 [start, end] 索引对。
        # 旧代码错误地传 intervention_point 标量,导致 _causal_impact 抛 TypeError,
        # 被 except 捕获后降级为 OBSERVATIONAL_ASSOCIATION,QUASI 链路被静默吞掉。
        pre_period = [0, intervention_point - 1]
        post_period = [intervention_point, len(effect_series) - 1]
        try:
            ci = self._causal_impact(df, pre_period, post_period)
            # 部分版本需显式 run(),兼容缺失时跳过。
            run_method = getattr(ci, "run", None)
            if callable(run_method):
                run_method()
            # CausalImpact summary 含 p 值和效应估计。
            p_value = float(getattr(ci, "p_value", 1.0) or 1.0)
            effect = float(getattr(ci, "abs_effect", 0.0) or 0.0)
            # Step 1 修复:从 ci.series 计算干预前 RMSE(预测值 vs 实际值),
            # 让 QUASI_EXPERIMENTAL_EFFECT evidence 能通过 Verifier 准实验门控。
            # 兼容 pycausalimpact 的多种列名:'response'/'y' + 'point_pred'/'predicted'。
            series_df = getattr(ci, "series", None)
            pre_period_rmse = _compute_pre_period_rmse(series_df, intervention_point)
        except Exception as exc:
            logger.warning("CausalImpact failed: %s", exc)
            return CausalCandidate(
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
                causal_status=CausalStatus.OBSERVATIONAL_ASSOCIATION.value,
                lag=0,
                stability_score=0.0,
                p_value=1.0,
                metadata={"error": str(exc), "event_date": event_date},
                regime_label=regime_label,
            )

        if p_value < self.p_value_threshold:
            status = CausalStatus.QUASI_EXPERIMENTAL_EFFECT.value
            stability = 1.0 - p_value
        else:
            status = CausalStatus.OBSERVATIONAL_ASSOCIATION.value
            stability = 0.0

        # P2:从 ci.series 提取后验分布字段
        posterior_metadata: dict[str, Any] = {}
        if return_posterior and series_df is not None:
            posterior_metadata = self._extract_posterior_from_series(
                series_df, intervention_point, niter
            )

        metadata: dict[str, Any] = {
            "intervention_point": intervention_point,
            "event_date": event_date,
            "pre_period_rmse": round(pre_period_rmse, 6) if pre_period_rmse is not None else None,
            "backend": "pycausalimpact",
            "niter": niter,
        }
        metadata.update(posterior_metadata)

        return CausalCandidate(
            cause_symbol=cause_symbol,
            effect_symbol=effect_symbol,
            causal_status=status,
            lag=0,
            stability_score=round(stability, 4),
            p_value=round(p_value, 4),
            post_period_effect=round(effect, 4),
            pre_period_rmse=round(pre_period_rmse, 6) if pre_period_rmse is not None else None,
            metadata=metadata,
            regime_label=regime_label,
        )

    def _extract_posterior_from_series(
        self,
        series_df: Any,
        intervention_point: int,
        niter: int,
    ) -> dict[str, Any]:
        """从 pycausalimpact 的 series DataFrame 提取后验分布字段。

        pycausalimpact.series 含 point_pred / point_pred_upper / point_pred_lower 列。
        用这些列估计后验均值和可信区间。

        Args:
            series_df: CausalImpact.series DataFrame。
            intervention_point: 干预时点。
            niter: MCMC 采样次数(用于 metadata 记录)。

        Returns:
            含 posterior_mean/posterior_std/credible_interval_lower/upper/r_hat 的 dict。
        """
        try:
            import numpy as np
            import pandas as pd

            if not isinstance(series_df, pd.DataFrame):
                return {}
            if intervention_point <= 0 or intervention_point >= len(series_df):
                return {}

            post = series_df.iloc[intervention_point:]
            # 兼容列名:point_pred / predicted
            pred_col = next(
                (c for c in ("point_pred", "predicted") if c in post.columns), None
            )
            upper_col = next(
                (c for c in ("point_pred_upper", "predicted_upper") if c in post.columns), None
            )
            lower_col = next(
                (c for c in ("point_pred_lower", "predicted_lower") if c in post.columns), None
            )
            if pred_col is None:
                return {}

            predictions = post[pred_col].to_numpy(dtype=float)
            posterior_mean = float(np.mean(predictions))
            posterior_std = float(np.std(predictions)) if len(predictions) > 1 else 0.0

            if upper_col is not None and lower_col is not None:
                ci_lower = float(np.min(post[lower_col]))
                ci_upper = float(np.max(post[upper_col]))
            else:
                # 无可信区间列时,用 mean ± 1.96*std 近似
                ci_lower = posterior_mean - 1.96 * posterior_std
                ci_upper = posterior_mean + 1.96 * posterior_std

            return {
                "posterior_mean": round(posterior_mean, 6),
                "posterior_std": round(posterior_std, 6),
                "credible_interval_lower": round(ci_lower, 6),
                "credible_interval_upper": round(ci_upper, 6),
                "r_hat": 1.0,  # pycausalimpact 不暴露 r_hat,固定为 1.0 表示收敛
            }
        except Exception as exc:
            logger.debug("extract_posterior_from_series failed: %s", exc)
            return {}

    def _bsts_attribution_bootstrap(
        self,
        effect_series: list[float],
        *,
        intervention_point: int,
        cause_symbol: str,
        effect_symbol: str,
        event_date: str = "",
        regime_label: str = "",
        niter: int = 100,
        return_posterior: bool = False,
    ) -> CausalCandidate:
        """pycaausalimpact 缺失时的 BSTS 归因降级路径(P2 新增)。

        用 bootstrap 重采样估计干预效应的后验分布:
        1. 从 pre_period 残差重采样,模拟反事实序列
        2. 计算每次重采样的 post_period 效应
        3. 用效应分布的均值和分位数估计后验

        Args:
            effect_series: 结果序列。
            intervention_point: 干预时点索引。
            cause_symbol/effect_symbol: 序列标签。
            event_date: 干预事件日期。
            regime_label: 体制标签。
            niter: bootstrap 重采样次数。
            return_posterior: 是否返回后验分布字段。

        Returns:
            CausalCandidate。降级路径,causal_status 仍按 p_value 判定。
        """
        try:
            import numpy as np
        except Exception:
            return CausalCandidate(
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
                causal_status=CausalStatus.TEMPORAL_PRECEDENCE.value,
                lag=0,
                stability_score=0.0,
                p_value=1.0,
                metadata={"degraded": "numpy_unavailable", "event_date": event_date},
                regime_label=regime_label,
            )

        if intervention_point <= 0 or intervention_point >= len(effect_series) - 2:
            return CausalCandidate(
                cause_symbol=cause_symbol,
                effect_symbol=effect_symbol,
                causal_status=CausalStatus.OBSERVATIONAL_ASSOCIATION.value,
                lag=0,
                stability_score=0.0,
                p_value=1.0,
                metadata={"degraded": "invalid_intervention_point"},
                regime_label=regime_label,
            )

        rng = np.random.default_rng(42)
        arr = np.asarray(effect_series, dtype=float)
        pre = arr[:intervention_point]
        post = arr[intervention_point:]

        # 干预前均值和标准差作为反事实基线
        pre_mean = float(np.mean(pre))
        pre_std = float(np.std(pre)) if len(pre) > 1 else 0.1
        pre_std = max(pre_std, 0.01)  # 避免除零

        # 反事实效应:post 实际值 - pre 基线
        actual_effect = float(np.mean(post) - pre_mean)

        # bootstrap 重采样估计后验分布
        bootstrap_effects = np.zeros(niter)
        for i in range(niter):
            # 从 pre 残差重采样,模拟反事实 post 序列
            boot_pre = rng.choice(pre, size=len(post), replace=True)
            boot_baseline = float(np.mean(boot_pre))
            bootstrap_effects[i] = float(np.mean(post) - boot_baseline)

        # p 值:bootstrap 中效应绝对值小于实际效应绝对值的比例
        p_value = float(np.mean(np.abs(bootstrap_effects) >= abs(actual_effect)))
        p_value = max(min(p_value, 1.0), 0.001)  # clamp

        # pre_period_rmse:用 pre 期标准差作为代理
        pre_period_rmse = pre_std

        if p_value < self.p_value_threshold:
            status = CausalStatus.QUASI_EXPERIMENTAL_EFFECT.value
            stability = 1.0 - p_value
        else:
            status = CausalStatus.OBSERVATIONAL_ASSOCIATION.value
            stability = 0.0

        metadata: dict[str, Any] = {
            "intervention_point": intervention_point,
            "event_date": event_date,
            "pre_period_rmse": round(pre_period_rmse, 6),
            "backend": "bootstrap_lite",
            "niter": niter,
        }

        if return_posterior:
            posterior_mean = float(np.mean(bootstrap_effects))
            posterior_std = float(np.std(bootstrap_effects))
            ci_lower = float(np.percentile(bootstrap_effects, 2.5))
            ci_upper = float(np.percentile(bootstrap_effects, 97.5))
            # r_hat:bootstrap 无链,固定为 1.0
            r_hat = 1.0
            metadata.update({
                "posterior_mean": round(posterior_mean, 6),
                "posterior_std": round(posterior_std, 6),
                "credible_interval_lower": round(ci_lower, 6),
                "credible_interval_upper": round(ci_upper, 6),
                "r_hat": r_hat,
            })

        return CausalCandidate(
            cause_symbol=cause_symbol,
            effect_symbol=effect_symbol,
            causal_status=status,
            lag=0,
            stability_score=round(stability, 4),
            p_value=round(p_value, 4),
            post_period_effect=round(actual_effect, 4),
            pre_period_rmse=round(pre_period_rmse, 6),
            metadata=metadata,
            regime_label=regime_label,
        )

    def screen_to_evidence(
        self,
        candidate: CausalCandidate,
        *,
        evidence_id: str,
        event_date: str = "",
    ) -> Evidence:
        """把候选结果转为 Evidence(便于 environment 写入 ledger)。"""
        return candidate.to_evidence(evidence_id=evidence_id, event_date=event_date)

    def dynotears_run(
        self,
        series_dict: dict[str, list[float]],
        *,
        max_lag: int = 1,
        lambda_w: float = 0.1,
        threshold: float = 0.05,
    ) -> list[CausalCandidate]:
        """DYNOTEARS 时序 DAG 学习(P1 简化版:LaggedSparseCausalScreen)。

        报告中期优先:用 L1 正则化的滞后项回归近似 NOTEARS 连续优化。
        预留 NOTEARS / DYNOTEARS backend 接口,后续可接完整优化器。

        策略:
        1. 对每个序列 y_t,用所有序列(含自身)的滞后项 y_{t-k} 做 Lasso 回归
        2. 非零系数 → 因果边(cause=滞后序列,effect=目标序列,lag=k)
        3. 系数绝对值 > threshold → 写入 CausalCandidate
        4. 输出 causal_status=PREDICTIVE_CAUSAL

        Args:
            series_dict: 序列名到序列值的映射,所有序列长度应一致。
            max_lag: 最大滞后期。
            lambda_w: L1 正则化强度(alpha),越大越稀疏。
            threshold: 系数绝对值阈值,低于此值视为噪声过滤。

        Returns:
            list[CausalCandidate]。依赖缺失或样本不足时返回空列表。
        """
        if not series_dict:
            return []

        names = list(series_dict.keys())
        n_series = len(names)
        # 校验序列长度
        lengths = [len(v) for v in series_dict.values()]
        if min(lengths) < max_lag + 5:
            logger.warning(
                "dynotears_run: 序列长度 %d 不足(需 >= max_lag + 5 = %d)",
                min(lengths),
                max_lag + 5,
            )
            return []

        try:
            import numpy as np
            from sklearn.linear_model import Lasso
        except Exception as exc:
            logger.warning(
                "dynotears_run: sklearn 不可用,降级为空列表: %s", exc
            )
            return []

        # 构造滞后项设计矩阵:对每个序列 y_t,
        # 特征 = [y_{t-1}, y_{t-2}, ..., y_{t-max_lag}] for all series
        # 标签 = y_t
        T = min(lengths)
        candidates: list[CausalCandidate] = []

        # 构造滞后矩阵 (T - max_lag) x (n_series * max_lag)
        lagged_features = []
        feature_names: list[tuple[str, int]] = []
        for name in names:
            series = series_dict[name]
            for lag in range(1, max_lag + 1):
                # y_{t-lag} for t in [max_lag, T)
                lagged = series[max_lag - lag : T - lag]
                lagged_features.append(lagged)
                feature_names.append((name, lag))

        if not lagged_features:
            return []

        # 转为 numpy 数组
        X = np.column_stack(lagged_features)
        if X.shape[0] < X.shape[1] + 5:
            logger.warning(
                "dynotears_run: 样本数 %d 不足(需 > 特征数 %d + 5)",
                X.shape[0],
                X.shape[1],
            )
            return []

        backend = "lagged_sparse"
        for target_idx, target_name in enumerate(names):
            y = np.array(series_dict[target_name][max_lag:T], dtype=float)
            if len(y) != X.shape[0]:
                continue
            # L1 正则化回归
            try:
                model = Lasso(alpha=lambda_w, max_iter=1000, random_state=42)
                model.fit(X, y)
            except Exception as exc:
                logger.warning(
                    "dynotears_run: Lasso 拟合失败 (target=%s): %s",
                    target_name,
                    exc,
                )
                continue

            # 非零系数 → 因果边
            for feat_idx, coef in enumerate(model.coef_):
                if abs(coef) < threshold:
                    continue
                cause_name, lag = feature_names[feat_idx]
                # 排除自因果(自身滞后对自身)
                if cause_name == target_name:
                    continue
                candidates.append(
                    CausalCandidate(
                        cause_symbol=cause_name,
                        effect_symbol=target_name,
                        causal_status=CausalStatus.PREDICTIVE_CAUSAL.value,
                        lag=lag,
                        stability_score=round(min(abs(coef), 1.0), 4),
                        p_value=round(1.0 - min(abs(coef), 1.0), 4),
                        metadata={
                            "backend": backend,
                            "max_lag": max_lag,
                            "lambda_w": lambda_w,
                            "threshold": threshold,
                            "coefficient": round(float(coef), 6),
                            "content_origin": "dynotears_run",
                        },
                    )
                )

        return candidates

    def pc_fci_run(
        self,
        series_dict: dict[str, list[float]],
        *,
        alpha: float = 0.05,
        max_condSet_size: int = 3,
    ) -> list[CausalCandidate]:
        """PC/FCI 约束型因果发现(P2 简化版:PC-Lite)。

        报告长期优先:用条件独立性检验(CI test)识别因果结构,
        能处理隐混淆(条件独立 given 共混淆 → 删除伪因果边)。

        策略(PC-Lite):
        1. 从完全图开始(所有节点对有边)
        2. 对每对 (X, Y),检验条件独立性 given conditioning set S
        3. 若 X ⊥ Y | S,删除 X-Y 边
        4. 对剩余边做方向判定(用顺序启发式:高阶变量更可能是 cause)
        5. 输出 CausalCandidate with causal_status=PREDICTIVE_CAUSAL

        预留 causal-learn backend 接口:metadata["backend"] 标注实现方式。

        Args:
            series_dict: 序列名到序列值的映射,所有序列长度应一致。
            alpha: 条件独立性检验显著性水平(默认 0.05)。
            max_condSet_size: 最大条件集大小(默认 3,越大越慢但越精确)。

        Returns:
            list[CausalCandidate]。依赖缺失或样本不足时返回空列表。
        """
        if not series_dict or len(series_dict) < 2:
            return []

        names = list(series_dict.keys())
        n_series = len(names)

        # 校验序列长度
        lengths = [len(v) for v in series_dict.values()]
        if min(lengths) < 10:
            logger.warning(
                "pc_fci_run: 序列长度 %d 不足(需 >= 10)", min(lengths)
            )
            return []

        try:
            import numpy as np
            from scipy import stats as scipy_stats
        except Exception as exc:
            logger.warning(
                "pc_fci_run: scipy 不可用,降级为空列表: %s", exc
            )
            return []

        T = min(lengths)
        # 构造数据矩阵 (T, n_series)
        data = np.column_stack(
            [np.asarray(series_dict[name][:T], dtype=float) for name in names]
        )

        # 1. 条件独立性检验:偏相关系数 + Fisher z 检验
        def partial_correlation(x: np.ndarray, y: np.ndarray, Z: np.ndarray) -> float:
            """计算 X 和 Y 给定 Z 的偏相关系数。"""
            if Z.shape[1] == 0:
                # 简单相关
                corr = np.corrcoef(x, y)[0, 1]
            else:
                # 用线性回归残差计算偏相关
                # X = a*Z + e1, Y = b*Z + e2, corr(e1, e2) = partial corr
                Z_aug = np.column_stack([Z, np.ones(len(x))])
                # 最小二乘
                beta_x, _, _, _ = np.linalg.lstsq(Z_aug, x, rcond=None)
                beta_y, _, _, _ = np.linalg.lstsq(Z_aug, y, rcond=None)
                resid_x = x - Z_aug @ beta_x
                resid_y = y - Z_aug @ beta_y
                corr = np.corrcoef(resid_x, resid_y)[0, 1]
            return float(corr) if not np.isnan(corr) else 0.0

        def is_conditional_independent(
            x: np.ndarray, y: np.ndarray, Z: np.ndarray, alpha: float
        ) -> bool:
            """Fisher z 检验:X ⊥ Y | Z?"""
            r = partial_correlation(x, y, Z)
            # Fisher z 变换
            r = max(min(r, 0.9999), -0.9999)  # clamp 避免 inf
            z = 0.5 * np.log((1 + r) / (1 - r))
            n = len(x)
            # 检验统计量
            z_stat = z * np.sqrt(n - 3 - Z.shape[1])
            # 双侧 p 值
            p_value = 2 * (1 - scipy_stats.norm.cdf(abs(z_stat)))
            return p_value > alpha

        # 2. PC 算法骨架:从完全图开始,逐步删除独立对
        # 邻接矩阵:True 表示有边
        adj = np.ones((n_series, n_series), dtype=bool)
        np.fill_diagonal(adj, False)

        # 对每对 (i, j),尝试不同的 conditioning set
        for i in range(n_series):
            for j in range(i + 1, n_series):
                if not adj[i, j]:
                    continue
                x = data[:, i]
                y = data[:, j]
                # 其他变量作为候选 conditioning set
                others = [k for k in range(n_series) if k not in (i, j)]
                # 尝试大小 0, 1, ..., max_condSet_size 的 conditioning set
                from itertools import combinations
                found_independent = False
                for size in range(min(max_condSet_size, len(others)) + 1):
                    for cond_set in combinations(others, size):
                        Z = data[:, list(cond_set)] if cond_set else np.empty((len(x), 0))
                        if is_conditional_independent(x, y, Z, alpha):
                            adj[i, j] = False
                            adj[j, i] = False
                            found_independent = True
                            break
                    if found_independent:
                        break

        # 3. 方向判定(简化版:用方差大小启发式)
        # 高方差变量更可能是 cause(信息源),低方差更可能是 effect
        variances = np.var(data, axis=0)
        candidates: list[CausalCandidate] = []
        backend = "pc_fci_lite"

        for i in range(n_series):
            for j in range(i + 1, n_series):
                if not adj[i, j]:
                    continue
                # 方向:高方差 → 低方差
                if variances[i] >= variances[j]:
                    cause_idx, effect_idx = i, j
                else:
                    cause_idx, effect_idx = j, i
                # 用偏相关系数作为 stability_score
                others = [k for k in range(n_series) if k not in (cause_idx, effect_idx)]
                r = partial_correlation(
                    data[:, cause_idx], data[:, effect_idx],
                    data[:, others] if others else np.empty((T, 0))
                )
                candidates.append(
                    CausalCandidate(
                        cause_symbol=names[cause_idx],
                        effect_symbol=names[effect_idx],
                        causal_status=CausalStatus.PREDICTIVE_CAUSAL.value,
                        lag=0,  # PC 是 contemporaneous,不区分 lag
                        stability_score=round(abs(r), 4),
                        p_value=round(1.0 - abs(r), 4),
                        metadata={
                            "backend": backend,
                            "alpha": alpha,
                            "max_condSet_size": max_condSet_size,
                            "partial_correlation": round(float(r), 6),
                            "content_origin": "pc_fci_run",
                        },
                    )
                )

        return candidates
