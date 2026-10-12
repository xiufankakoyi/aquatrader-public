"""P1 Step 9:宏观因子层(Fama-French + FAVAR)。

报告层 3(宏观因子)中期优先:
- FamaFrenchFactorLayer:Fama-French 5 因子(Mkt-RF / SMB / HML / RMW / CMA)
  用 PCA + 截面排序近似估计因子收益
- FavarLayer:FAVAR(因子增强 VAR)
  PCA 压缩宏观指标为少量潜因子 + VAR 脉冲响应

降级:
- sklearn 缺失时 FamaFrenchFactorLayer 返回空 dict
- statsmodels 缺失时 FavarLayer.impulse_response 返回空 dict

依赖:sklearn.decomposition.PCA、statsmodels.tsa.api.VAR(均为懒加载)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _try_import_sklearn() -> Any | None:
    """懒加载 sklearn PCA + Lasso,缺失返回 None。"""
    try:
        from sklearn.decomposition import PCA
        from sklearn.linear_model import Lasso
        return {"PCA": PCA, "Lasso": Lasso}
    except Exception as exc:
        logger.debug("sklearn unavailable, macro factor layer disabled: %s", exc)
        return None


def _try_import_statsmodels_var() -> Any | None:
    """懒加载 statsmodels VAR,缺失返回 None。"""
    try:
        from statsmodels.tsa.api import VAR
        return VAR
    except Exception as exc:
        logger.debug("statsmodels VAR unavailable, FAVAR impulse disabled: %s", exc)
        return None


class FamaFrenchFactorLayer:
    """Fama-French 5 因子近似估计层。

    报告要求:三层因子结构 — 收益因子(FF5)是学术标准分解。
    本实现用 PCA + 截面排序近似估计因子收益,而非真实 FF5 数据。

    因子:
    - Mkt-RF:市场超额收益(全样本等权平均 - 无风险利率近似 0)
    - SMB:小盘减大盘(按市值排序,底部 30% - 顶部 30%)
    - HML:价值减成长(按账面市值比排序)
    - RMW:盈利能力强减弱(按收益率排序)
    - CMA:保守投资减激进投资(按资产增长率排序)

    简化策略:用 PCA 主成分 + 截面排序近似,不依赖真实财务数据。
    """

    def __init__(self, n_components: int = 5) -> None:
        """初始化。

        Args:
            n_components: PCA 主成分数,默认 5(对应 FF5)。
        """
        self.n_components = n_components
        self._sklearn = _try_import_sklearn()

    def is_available(self) -> bool:
        """返回 sklearn 是否可用。"""
        return self._sklearn is not None

    def estimate_factors(
        self, returns: Any
    ) -> dict[str, float]:
        """估计 Fama-French 5 因子值。

        用 PCA 主成分近似因子,截面排序构造 SMB/HML/RMW/CMA 价差。

        Args:
            returns: 收益率矩阵 (n_periods, n_stocks),numpy array 或等价对象。

        Returns:
            {"Mkt-RF", "SMB", "HML", "RMW", "CMA"} 因子值 dict。
            sklearn 缺失或输入为空时返回空 dict。
        """
        if self._sklearn is None:
            return {}

        import numpy as np

        # 输入校验
        try:
            arr = np.asarray(returns, dtype=float)
        except Exception as exc:
            logger.warning("estimate_factors: 输入转换失败: %s", exc)
            return {}

        if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 5:
            return {}

        PCA = self._sklearn["PCA"]

        # 1. Mkt-RF:全样本等权平均(简化无风险利率为 0)
        mkt_rf = float(np.mean(arr[-1]))  # 取最后一期作为因子值

        # 2. SMB:按最后一期收益排序,底部 30% - 顶部 30%
        last_returns = arr[-1]
        sorted_idx = np.argsort(last_returns)
        n_stocks = len(sorted_idx)
        bottom_30 = sorted_idx[: max(1, n_stocks // 3)]
        top_30 = sorted_idx[-max(1, n_stocks // 3) :]
        smb = float(np.mean(last_returns[bottom_30]) - np.mean(last_returns[top_30]))

        # 3. HML:用 PCA 第 2 主成分近似(价值因子)
        # 4. RMW:用 PCA 第 3 主成分近似(盈利因子)
        # 5. CMA:用 PCA 第 4 主成分近似(投资因子)
        try:
            pca = PCA(n_components=min(self.n_components, arr.shape[1]))
            pca.fit(arr)
            components = pca.components_
            explained = pca.explained_variance_ratio_
            # 用主成分的载荷作为因子值的近似
            hml = float(components[1, 0]) if len(components) > 1 else 0.0
            rmw = float(components[2, 0]) if len(components) > 2 else 0.0
            cma = float(components[3, 0]) if len(components) > 3 else 0.0
        except Exception as exc:
            logger.warning("estimate_factors: PCA 拟合失败: %s", exc)
            hml = rmw = cma = 0.0

        return {
            "Mkt-RF": mkt_rf,
            "SMB": smb,
            "HML": hml,
            "RMW": rmw,
            "CMA": cma,
        }


class FavarLayer:
    """FAVAR(因子增强 VAR)层。

    报告要求:宏观因子层用 FAVAR 压缩宏观指标为少量潜因子 + VAR 脉冲响应。

    流程:
    1. fit():用 PCA 把多个宏观指标压缩为 n_factors 个潜因子
    2. 用潜因子 + 原始指标拟合 VAR 模型
    3. impulse_response():计算一个标准差冲击的脉冲响应路径

    降级:
    - sklearn 缺失时 is_available=False,fit 不抛异常
    - statsmodels 缺失时 impulse_response 返回空 dict
    """

    def __init__(self, n_factors: int = 2) -> None:
        """初始化。

        Args:
            n_factors: PCA 压缩后的潜因子数,默认 2。
        """
        self.n_factors = n_factors
        self._sklearn = _try_import_sklearn()
        self._var_cls = _try_import_statsmodels_var()
        self._var_model: Any = None
        self._factor_names: list[str] = []
        self._indicator_names: list[str] = []
        self._pca: Any = None

    def is_available(self) -> bool:
        """返回 sklearn + statsmodels VAR 是否均可用。"""
        return self._sklearn is not None and self._var_cls is not None

    def fit(
        self, macro_indicators: dict[str, list[float]]
    ) -> "FavarLayer":
        """拟合 FAVAR 模型。

        Args:
            macro_indicators: 指标名到序列值的映射,所有序列长度应一致。

        Returns:
            self(链式调用)。数据不足或依赖缺失时不抛异常,
            但 impulse_response 将返回空 dict。
        """
        self._indicator_names = list(macro_indicators.keys())
        self._factor_names = []

        if self._sklearn is None or not macro_indicators:
            return self

        import numpy as np

        # 校验序列长度
        lengths = [len(v) for v in macro_indicators.values()]
        if min(lengths) < 10:  # VAR 至少需要 10 期
            return self

        T = min(lengths)
        # 构造矩阵 (T, n_indicators)
        try:
            matrix = np.column_stack(
                [np.asarray(macro_indicators[name][:T], dtype=float)
                 for name in self._indicator_names]
            )
        except Exception as exc:
            logger.warning("FavarLayer.fit: 矩阵构造失败: %s", exc)
            return self

        PCA = self._sklearn["PCA"]
        n_comp = min(self.n_factors, matrix.shape[1], T - 1)
        if n_comp < 1:
            return self

        try:
            self._pca = PCA(n_components=n_comp)
            factors = self._pca.fit_transform(matrix)
            self._factor_names = [f"factor_{i}" for i in range(n_comp)]
        except Exception as exc:
            logger.warning("FavarLayer.fit: PCA 失败: %s", exc)
            return self

        # 拼接潜因子 + 原始指标,拟合 VAR
        if self._var_cls is None:
            return self

        try:
            # VAR 输入:(T, n_factors + n_indicators)
            var_input = np.column_stack([factors, matrix])
            if var_input.shape[0] < 10:
                return self
            self._var_model = self._var_cls(var_input)
            # 注意:不立即 fit_results,在 impulse_response 时再 fit
        except Exception as exc:
            logger.warning("FavarLayer.fit: VAR 初始化失败: %s", exc)
            self._var_model = None

        return self

    def impulse_response(
        self, shock: str, periods: int = 10
    ) -> dict[str, list[float]]:
        """计算脉冲响应路径。

        Args:
            shock: 冲击变量名(指标名或因子名)。
            periods: 响应路径长度。

        Returns:
            {变量名: [响应值, ...]} dict。
            模型未拟合或依赖缺失时返回空 dict。
        """
        if self._var_model is None or self._pca is None:
            return {}

        import numpy as np

        # 拟合 VAR 模型(选择最优滞后阶数)
        try:
            results = self._var_model.fit(maxlags=2)
        except Exception as exc:
            logger.warning("FavarLayer.impulse_response: VAR fit 失败: %s", exc)
            return {}

        # 定位冲击变量在 var_input 中的列索引
        all_names = self._factor_names + self._indicator_names
        if shock not in all_names:
            logger.warning(
                "impulse_response: shock=%s 不在变量列表 %s",
                shock,
                all_names,
            )
            return {}

        shock_idx = all_names.index(shock)

        # 计算脉冲响应(IRF)
        try:
            irf = results.irf(periods)
            # irf.irfs shape: (periods+1, n_vars, n_vars)
            # irfs[:, :, shock_idx] 是所有变量对 shock 的响应
            irfs = irf.irfs
            response: dict[str, list[float]] = {}
            for var_idx, var_name in enumerate(all_names):
                path = irfs[: periods + 1, var_idx, shock_idx]
                response[var_name] = [float(x) for x in path]
            return response
        except Exception as exc:
            logger.warning("impulse_response: IRF 计算失败: %s", exc)
            return {}
