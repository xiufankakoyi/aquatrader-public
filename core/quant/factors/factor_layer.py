"""
统一因子层（FactorLayer）

将分散的因子来源收敛为单一入口：
- daily 表列（如 ma5、rsi_14）
- FactorCalculator / Parquet 预计算因子
- FactorPrecomputeEngine DAG 计算因子（MA/RSI/MACD/形态等）

使用方式：
    layer = FactorLayer(engine)
    factors = layer.load_factors(
        required_factors=['ma20', 'rsi_14', 'apex_convergence'],
        trading_dates=trading_dates,
        stock_codes=stock_codes,
        preloaded_data={'daily': daily_df}
    )
    # factors: {factor_name: np.ndarray(T, N)}
"""

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from config.logger import get_logger

logger = get_logger(__name__)


class FactorNameNormalizer:
    """因子名称规范化器，处理大小写/下划线差异。"""

    ALIASES = {
        "ma5": "MA5",
        "ma10": "MA10",
        "ma20": "MA20",
        "ma60": "MA60",
        "rsi6": "RSI6",
        "rsi_6": "RSI6",
        "rsi14": "RSI14",
        "rsi_14": "RSI14",
        "gain_1d": "GAIN_1D",
        "gain1d": "GAIN_1D",
        "gain_5d": "GAIN_5D",
        "gain5d": "GAIN_5D",
        "gain_20d": "GAIN_20D",
        "gain20d": "GAIN_20D",
        "volatility_20": "VOLATILITY_20",
        "macd_dif": "MACD_DIF",
        "macd_dea": "MACD_DEA",
        "macd_hist": "MACD_HIST",
        "macd_histogram": "MACD_HIST",
    }

    @classmethod
    def normalize(cls, name: str) -> str:
        """统一转小写后映射别名；无别名则返回原值。"""
        key = name.strip().lower()
        return cls.ALIASES.get(key, name)


class FactorLayer:
    """
    统一因子加载层。

    负责按优先级从多个数据源加载/计算策略声明的因子，
    输出统一的 {factor_name: (T, N) matrix} 字典。
    """

    def __init__(self, engine):
        """
        初始化因子层。

        Args:
            engine: UnifiedBacktestEngine 实例，用于访问 data_query、config 等共享状态。
        """
        self.engine = engine
        self._factor_calculator = None
        self._precompute_engine = None

    def load_factors(
        self,
        required_factors: List[str],
        trading_dates: List[str],
        stock_codes: List[str],
        preloaded_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, np.ndarray]:
        """
        加载策略所需的全部因子。

        Args:
            required_factors: 策略声明的因子名列表。
            trading_dates: 交易日期列表 (T,)，格式 YYYY-MM-DD。
            stock_codes: 股票代码列表 (N,)。
            preloaded_data: 回测预加载数据，通常包含 daily DataFrame。

        Returns:
            Dict[str, np.ndarray]: 因子名到 (T, N) 矩阵的映射。
        """
        if not required_factors:
            return {}

        results: Dict[str, np.ndarray] = {}
        remaining = list(required_factors)

        # 1. 优先从 daily 表列加载（零拷贝/最快）
        if preloaded_data and "daily" in preloaded_data:
            daily_df = preloaded_data["daily"]
            from_daily = self._load_from_daily(
                daily_df, remaining, trading_dates, stock_codes
            )
            results.update(from_daily)
            remaining = [f for f in remaining if f not in results]
            if remaining:
                logger.info(f"[FactorLayer] daily 未覆盖，剩余因子: {remaining}")

        # 2. 从 Parquet 数据库加载（FactorCalculator）
        if remaining:
            from_db = self._load_from_factor_calculator(
                remaining, trading_dates, stock_codes
            )
            results.update(from_db)
            remaining = [f for f in remaining if f not in results]

        # 3. 通过 DAG 计算（FactorPrecomputeEngine，含形态因子）
        if remaining:
            close_matrix = self._build_close_matrix(
                preloaded_data, trading_dates, stock_codes
            )
            if close_matrix is not None:
                from_dag = self._load_from_precompute_engine(
                    remaining, close_matrix, trading_dates, stock_codes
                )
                results.update(from_dag)
                remaining = [f for f in remaining if f not in results]

        if remaining:
            logger.warning(
                f"[FactorLayer] 以下因子无法加载或计算: {remaining}"
            )

        logger.info(
            f"[FactorLayer] 共加载 {len(results)} 个因子: {list(results.keys())}"
        )
        return results

    def inject_into_strategy(
        self,
        strategy: Any,
        factors: Dict[str, np.ndarray],
    ) -> None:
        """
        将因子注入策略实例。

        兼容现有约定：
        - strategy.factors = factors
        - 每个因子同时设为 strategy 的属性（如 strategy.ma20）
        """
        if not factors:
            return

        strategy.factors = factors
        for factor_name, matrix in factors.items():
            setattr(strategy, factor_name, matrix)

    def _load_from_daily(
        self,
        daily_df: Any,
        required_factors: List[str],
        trading_dates: List[str],
        stock_codes: List[str],
    ) -> Dict[str, np.ndarray]:
        """从 daily 表列直接读取因子并构造矩阵。"""
        if daily_df is None or len(daily_df) == 0:
            return {}

        available = [f for f in required_factors if f in daily_df.columns]
        if not available:
            return {}

        T, N = len(trading_dates), len(stock_codes)
        date_to_idx = {str(d)[:10]: i for i, d in enumerate(trading_dates)}
        code_to_idx = {str(c).zfill(6): i for i, c in enumerate(stock_codes)}

        matrices = {f: np.full((T, N), np.nan, dtype=np.float32) for f in available}

        # 统一处理 Polars / Pandas
        is_polars = hasattr(daily_df, "columns") and not isinstance(daily_df, pd.DataFrame)

        if is_polars:
            rows = daily_df.select(
                ["trade_date", "stock_code"] + available
            ).iter_rows(named=True)
        else:
            rows = daily_df[["trade_date", "stock_code"] + available].itertuples(index=False)
            rows = [r._asdict() for r in rows]

        for row in rows:
            date_str = str(row["trade_date"])[:10]
            code_str = str(row["stock_code"]).zfill(6)

            i = date_to_idx.get(date_str)
            j = code_to_idx.get(code_str)
            if i is None or j is None:
                continue

            for f in available:
                val = row.get(f)
                if val is None or (isinstance(val, float) and np.isnan(val)):
                    continue
                try:
                    matrices[f][i, j] = float(val)
                except (TypeError, ValueError):
                    pass

        return matrices

    def _load_from_factor_calculator(
        self,
        required_factors: List[str],
        trading_dates: List[str],
        stock_codes: List[str],
    ) -> Dict[str, np.ndarray]:
        """从 FactorCalculator（Parquet 数据库）加载因子。"""
        try:
            from core.quant.strategies.utils.factor_calculator import (
                get_factor_calculator,
            )

            if self._factor_calculator is None:
                self._factor_calculator = get_factor_calculator()

            return self._factor_calculator.load_factors(
                required_factors, trading_dates, stock_codes
            )
        except Exception as e:
            logger.warning(f"[FactorLayer] FactorCalculator 加载失败: {e}")
            return {}

    def _load_from_precompute_engine(
        self,
        required_factors: List[str],
        close_matrix: np.ndarray,
        trading_dates: List[str],
        stock_codes: List[str],
    ) -> Dict[str, np.ndarray]:
        """通过 FactorPrecomputeEngine DAG 计算因子。"""
        try:
            from core.quant.strategies.utils.factor_precompute import (
                FactorPrecomputeEngine,
            )

            if self._precompute_engine is None:
                self._precompute_engine = FactorPrecomputeEngine()

            date_range = (trading_dates[0], trading_dates[-1]) if trading_dates else ("", "")

            # 对未命中的因子尝试原始名及规范化名
            normalized_names = {
                FactorNameNormalizer.normalize(f): f for f in required_factors
            }
            dag_names = list(normalized_names.keys())

            computed = self._precompute_engine.compute_factors(
                close_matrix=close_matrix,
                factor_names=dag_names,
                date_range=date_range,
            )

            # 将 DAG 输出名映射回策略请求的原始名
            return {
                normalized_names.get(name, name): matrix
                for name, matrix in computed.items()
            }
        except Exception as e:
            logger.warning(f"[FactorLayer] FactorPrecomputeEngine 计算失败: {e}")
            return {}

    def _build_close_matrix(
        self,
        preloaded_data: Optional[Dict[str, Any]],
        trading_dates: List[str],
        stock_codes: List[str],
    ) -> Optional[np.ndarray]:
        """从预加载数据构建收盘价矩阵 (T, N)。"""
        if preloaded_data is None or "daily" not in preloaded_data:
            return None

        daily_df = preloaded_data["daily"]
        if daily_df is None or len(daily_df) == 0:
            return None

        if "close" not in daily_df.columns or "stock_code" not in daily_df.columns:
            return None

        T, N = len(trading_dates), len(stock_codes)
        date_to_idx = {str(d)[:10]: i for i, d in enumerate(trading_dates)}
        code_to_idx = {str(c).zfill(6): i for i, c in enumerate(stock_codes)}

        close_matrix = np.full((T, N), np.nan, dtype=np.float64)

        is_polars = hasattr(daily_df, "columns") and not isinstance(daily_df, pd.DataFrame)

        if is_polars:
            rows = daily_df.select(["trade_date", "stock_code", "close"]).iter_rows(named=True)
        else:
            rows = daily_df[["trade_date", "stock_code", "close"]].itertuples(index=False)
            rows = [r._asdict() for r in rows]

        for row in rows:
            date_str = str(row["trade_date"])[:10]
            code_str = str(row["stock_code"]).zfill(6)

            i = date_to_idx.get(date_str)
            j = code_to_idx.get(code_str)
            if i is None or j is None:
                continue

            val = row.get("close")
            if val is None or (isinstance(val, float) and np.isnan(val)):
                continue
            try:
                close_matrix[i, j] = float(val)
            except (TypeError, ValueError):
                pass

        return close_matrix
