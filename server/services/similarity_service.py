"""
K线相似度查询服务

封装 core.quant.similarity.SimilarityEngine，对外提供：
- match: 根据模板股票的 K 线形态在全市场候选窗口中搜索最相似的股票
- preprocess: 离线构建候选窗口缓存
- get_status: 查询预处理状态

所有数据访问通过 data_svc.query.fast_market_query 进行，服务层只负责编排。
"""

import math
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import polars as pl
from loguru import logger

from core.quant.similarity import SimilarityEngine, generate_sliding_windows, normalize_kline


# 候选股票数据回看的交易日数（约 1 年）
_CANDIDATE_LOOKBACK_DAYS = 365
# 候选股票数量上限，控制单次匹配的计算量
_MAX_CANDIDATE_STOCKS = 1000


_similarity_service_instance: Optional["SimilarityService"] = None
_similarity_service_lock = threading.Lock()


def get_similarity_service() -> "SimilarityService":
    """获取 SimilarityService 单例"""
    global _similarity_service_instance
    if _similarity_service_instance is None:
        with _similarity_service_lock:
            if _similarity_service_instance is None:
                _similarity_service_instance = SimilarityService()
    return _similarity_service_instance


def _normalize_code(code: str) -> str:
    """将股票代码统一为 6 位数字字符串，用于比较"""
    if not code:
        return ""
    pure = code.split(".")[0].strip()
    return pure.zfill(6)[-6:]


class SimilarityService:
    """
    K线相似度查询服务

    基于 SimilarityEngine 实现“以股搜股”：
    1. 加载模板股票最近 window_size 个交易日的收盘价
    2. 从全市场历史 K 线中生成候选窗口
    3. 调用 SimilarityEngine 进行相关性粗筛 + DTW 精排
    4. 为匹配结果补齐后续走势，便于前端展示
    """

    def __init__(self):
        self._precomputed_windows: Dict[int, List[Dict[str, Any]]] = {}
        self._preprocess_meta: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def match(
        self,
        stock_code: str,
        window_size: int = 20,
        top_n: int = 10,
        pattern_type: Optional[str] = None,
        corr_threshold: float = 0.85,
        subsequent_days: int = 10,
        algorithm: str = "dtw",
        scene: str = "default",
    ) -> List[Dict[str, Any]]:
        """
        根据模板股票的 K 线形态搜索相似股票

        Args:
            stock_code: 模板股票代码，如 "000001.SZ"
            window_size: 模板窗口长度
            top_n: 返回的最大结果数
            pattern_type: 模式增强类型（None / breakout_volume / limit_break / n_shape）
            corr_threshold: 相关性粗筛阈值
            subsequent_days: 为每个匹配结果补齐后续 K 线的天数
            algorithm: 匹配算法（保留字段，当前仅支持 dtw）
            scene: 使用场景（保留字段，用于后续扩展）

        Returns:
            匹配结果列表，每个元素包含：
            - stock_code: 候选股票代码
            - start_date / end_date: 匹配窗口起止日期
            - similarity_score: DTW 相似度得分
            - corr_score: 皮尔逊相关系数（如果通过粗筛）
            - subsequent_kline: 后续走势的 K 线数据
        """
        if algorithm not in {"dtw", "skeleton"}:
            raise ValueError(f"不支持的算法: {algorithm}")
        if scene not in {"default", "breakout_volume", "limit_break", "n_shape", "trend"}:
            logger.warning(f"未知 scene: {scene}，使用 default")

        # 1. 加载模板 K 线
        template_close, template_dates = self._load_template(stock_code, window_size)
        if template_close is None or len(template_close) < window_size:
            logger.warning(f"模板股票 {stock_code} 数据不足，无法匹配")
            return []

        template_normalized = normalize_kline(template_close)

        # 2. 获取候选窗口（优先使用预处理缓存，否则实时构建）
        with self._lock:
            windows = self._precomputed_windows.get(window_size)

        if windows is None:
            logger.info(f"窗口大小 {window_size} 未预处理，实时构建候选窗口")
            candidate_df = self._load_candidate_data(window_size)
            if candidate_df is None or candidate_df.is_empty():
                return []
            windows = self._build_windows(candidate_df, window_size)
            with self._lock:
                self._precomputed_windows[window_size] = windows
                self._preprocess_meta[window_size] = {
                    "preprocessed": True,
                    "total_windows": len(windows),
                    "symbols_count": candidate_df["stock_code"].n_unique(),
                    "last_preprocess_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }

        if not windows:
            return []

        # 3. 排除模板股票自身的窗口，避免自匹配
        template_code_normalized = _normalize_code(stock_code)
        windows = [
            w for w in windows
            if _normalize_code(w.get("metadata", {}).get("stock_code", "")) != template_code_normalized
        ]

        # 4. 执行相似度匹配
        engine = SimilarityEngine(
            corr_threshold=corr_threshold,
            pattern_type=pattern_type,
        )
        matched = engine.match(template_normalized, windows, top_n=top_n)

        # 5. 补齐后续走势
        candidate_df = self._load_candidate_data(window_size)
        results = self._enrich_results(matched, candidate_df, subsequent_days)

        logger.info(
            f"相似度匹配完成: template={stock_code}, candidates={len(windows)}, "
            f"results={len(results)}"
        )
        return results

    def preprocess(self, window_size: int) -> Dict[str, Any]:
        """
        离线预处理指定窗口大小的候选窗口

        Args:
            window_size: 滑动窗口大小

        Returns:
            预处理统计信息
        """
        candidate_df = self._load_candidate_data(window_size)
        if candidate_df is None or candidate_df.is_empty():
            raise RuntimeError("无法获取候选数据，预处理失败")

        windows = self._build_windows(candidate_df, window_size)

        meta = {
            "preprocessed": True,
            "window_size": window_size,
            "total_windows": len(windows),
            "symbols_count": candidate_df["stock_code"].n_unique(),
            "last_preprocess_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        with self._lock:
            self._precomputed_windows[window_size] = windows
            self._preprocess_meta[window_size] = meta

        logger.info(f"相似度预处理完成: {meta}")
        return meta

    def get_status(self) -> Dict[str, Any]:
        """查询预处理状态"""
        with self._lock:
            window_sizes = list(self._precomputed_windows.keys())
            cache_sizes = {
                str(ws): len(windows)
                for ws, windows in self._precomputed_windows.items()
            }
            last_times = {
                str(ws): meta.get("last_preprocess_time", "")
                for ws, meta in self._preprocess_meta.items()
            }
            return {
                "preprocessed": len(window_sizes) > 0,
                "window_sizes": window_sizes,
                "cache_sizes": cache_sizes,
                "last_preprocess_time": last_times.get(str(window_sizes[-1])) if window_sizes else None,
            }

    def _get_fmq(self):
        """懒加载 FastMarketQuery，避免模块启动时强依赖"""
        try:
            from data_svc.query.fast_market_query import get_fast_market_query
            return get_fast_market_query()
        except Exception as e:
            logger.error(f"FastMarketQuery 不可用: {e}")
            raise RuntimeError(f"数据查询服务不可用: {e}") from e

    def _load_template(self, stock_code: str, window_size: int) -> tuple:
        """
        加载模板股票的收盘价序列

        Returns:
            (close_array, date_array)
        """
        fmq = self._get_fmq()
        end_date = datetime.now()
        start_date = end_date - timedelta(days=window_size * 2 + 30)

        try:
            df = fmq.get_stock_history(
                stock_code,
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
                fields=["stock_code", "trade_date", "close"],
            )
            if df is None or df.is_empty():
                return None, None

            df = df.sort("trade_date").tail(window_size)
            close_arr = df["close"].cast(pl.Float64).to_numpy()
            date_arr = df["trade_date"].to_numpy()
            return close_arr, date_arr
        except Exception as e:
            logger.error(f"加载模板股票 {stock_code} 失败: {e}")
            return None, None

    def _load_candidate_data(self, window_size: int) -> Optional[pl.DataFrame]:
        """
        加载候选股票的历史 K 线数据

        为减少计算量，默认只取最近一年的数据，并按流动性筛选到最多 1000 只股票。
        """
        fmq = self._get_fmq()
        end_date = datetime.now()
        start_date = end_date - timedelta(days=_CANDIDATE_LOOKBACK_DAYS + window_size)

        fields = ["stock_code", "trade_date", "open", "high", "low", "close", "volume"]
        optional_fields = ["total_mv"]

        try:
            # 尝试读取可选字段（如 total_mv），不存在则忽略
            df = fmq._read_table(
                "daily_ohlcv",
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                fields=fields + optional_fields,
            )
        except Exception as e:
            logger.warning(f"读取候选数据（含 total_mv）失败: {e}，尝试基础字段")
            df = fmq._read_table(
                "daily_ohlcv",
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                fields=fields,
            )

        if df is None or df.is_empty():
            return None

        # 统一字段类型
        available_cols = set(df.columns)
        if "stock_code" not in available_cols or "trade_date" not in available_cols:
            raise RuntimeError("候选数据缺少必要字段 stock_code / trade_date")

        df = df.with_columns([
            pl.col("stock_code").cast(pl.Utf8).alias("stock_code"),
            pl.col("trade_date").cast(pl.Utf8).alias("trade_date"),
            pl.col("close").cast(pl.Float64).alias("close"),
            pl.col("volume").cast(pl.Float64).alias("volume"),
        ])

        # 按流动性/市值筛选候选股票，降低计算量
        df = self._filter_universe(df)
        return df

    def _filter_universe(self, df: pl.DataFrame) -> pl.DataFrame:
        """
        从全市场股票中筛选候选池

        优先使用 total_mv 取市值最大的前 N 只；
        若 total_mv 不存在，则使用平均成交量作为流动性代理。
        """
        if "total_mv" in df.columns:
            try:
                latest = df.sort("trade_date", descending=True).group_by("stock_code").first()
                top_codes = latest.sort("total_mv", descending=True).head(_MAX_CANDIDATE_STOCKS)["stock_code"].to_list()
                return df.filter(pl.col("stock_code").is_in(top_codes))
            except Exception as e:
                logger.warning(f"按市值筛选失败: {e}")

        if "volume" in df.columns:
            try:
                avg_volume = df.group_by("stock_code").agg(
                    pl.col("volume").mean().alias("avg_volume")
                ).sort("avg_volume", descending=True).head(_MAX_CANDIDATE_STOCKS)
                top_codes = avg_volume["stock_code"].to_list()
                return df.filter(pl.col("stock_code").is_in(top_codes))
            except Exception as e:
                logger.warning(f"按成交量筛选失败: {e}")

        return df

    def _build_windows(self, df: pl.DataFrame, window_size: int) -> List[Dict[str, Any]]:
        """从候选 DataFrame 生成滑动窗口"""
        try:
            return generate_sliding_windows(df, window_size=window_size)
        except Exception as e:
            logger.error(f"生成滑动窗口失败: {e}")
            return []

    def _enrich_results(
        self,
        matched: List[Dict[str, Any]],
        candidate_df: Optional[pl.DataFrame],
        subsequent_days: int,
    ) -> List[Dict[str, Any]]:
        """
        为匹配结果补齐后续 K 线

        Args:
            matched: SimilarityEngine 返回的匹配结果
            candidate_df: 候选股票完整数据
            subsequent_days: 后续天数

        Returns:
            补齐后的结果列表
        """
        if candidate_df is None or candidate_df.is_empty():
            return matched

        results = []
        for item in matched:
            result = dict(item)
            code = result.get("stock_code")
            end_date = result.get("end_date")
            if not code or not end_date:
                result["subsequent_kline"] = []
                results.append(result)
                continue

            subsequent = self._get_subsequent_kline(
                candidate_df, code, end_date, subsequent_days
            )
            result["subsequent_kline"] = subsequent
            results.append(result)

        return results

    def _get_subsequent_kline(
        self,
        df: pl.DataFrame,
        stock_code: str,
        end_date: str,
        days: int,
    ) -> List[Dict[str, Any]]:
        """获取指定股票在 end_date 之后的 days 天 K 线"""
        try:
            end_dt = datetime.strptime(str(end_date)[:10], "%Y-%m-%d")
            start_dt = end_dt + timedelta(days=1)
            end_limit = end_dt + timedelta(days=days + 7)

            sub = df.filter(
                (pl.col("stock_code") == stock_code)
                & (pl.col("trade_date") >= start_dt.strftime("%Y-%m-%d"))
                & (pl.col("trade_date") <= end_limit.strftime("%Y-%m-%d"))
            ).sort("trade_date").head(days)

            if sub.is_empty():
                return []

            cols = [c for c in ["trade_date", "open", "high", "low", "close", "volume"] if c in sub.columns]
            klines = []
            for row in sub.select(cols).iter_rows(named=True):
                kline = {"trade_date": str(row.get("trade_date"))}
                for key in ["open", "high", "low", "close", "volume"]:
                    val = row.get(key)
                    if val is None:
                        kline[key] = None
                    elif isinstance(val, float):
                        kline[key] = None if math.isnan(val) else round(val, 4)
                    else:
                        kline[key] = val
                klines.append(kline)
            return klines
        except Exception as e:
            logger.warning(f"获取后续 K 线失败 {stock_code} {end_date}: {e}")
            return []
