# core/dragon_eye/manager.py
import os
import pandas as pd
import polars as pl
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
from config.config import Config
from config.logger import get_logger

logger = get_logger(__name__)


def _frame_is_empty(frame: pd.DataFrame | pl.DataFrame) -> bool:
    if frame is None:
        return True
    if isinstance(frame, pl.DataFrame):
        return frame.height == 0
    if isinstance(frame, pd.DataFrame):
        return frame.empty
    return True


def _has_columns(frame: pd.DataFrame | pl.DataFrame, columns: set[str]) -> bool:
    if frame is None or not columns:
        return False
    try:
        return columns.issubset(set(frame.columns))
    except (AttributeError, TypeError):
        return False


def _to_pandas(frame: pd.DataFrame | pl.DataFrame) -> pd.DataFrame:
    """Convert only at the LanceDB compatibility boundary."""
    if isinstance(frame, pd.DataFrame):
        return frame.copy()
    if isinstance(frame, pl.DataFrame):
        return pd.DataFrame(frame.to_dicts())
    raise TypeError(f"unsupported dataframe type: {type(frame).__name__}")


class _FallbackDataManager:
    """数据管理器备用实现（当 LanceDB 不可用时）"""
    def __init__(self, data_dir: Optional[str] = None, table_name: str = "default"):
        self.table_name = table_name
        self.data_dir = Path(data_dir) if data_dir else Path(Config.PARQUET_DIR) / "dragon_eye"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"FallbackDataManager for {table_name}")
    
    def upsert_daily_data(self, df: pd.DataFrame | pl.DataFrame) -> None:
        """使用 Parquet 文件作为备用存储"""
        if _frame_is_empty(df):
            return
            
        file_path = self.data_dir / f"{self.table_name}.parquet"
        
        try:
            incoming = (
                df if isinstance(df, pl.DataFrame)
                else pl.from_pandas(df, include_index=False)
            )
            if file_path.exists():
                existing = pl.read_parquet(file_path)
                combined = pl.concat(
                    [existing, incoming], how="diagonal_relaxed"
                )
                if "_id" in combined.columns:
                    combined = combined.unique(
                        subset=["_id"], keep="last", maintain_order=True
                    )
            else:
                combined = incoming

            combined.write_parquet(file_path)
            logger.info(f"Fallback: saved {incoming.height} rows to {file_path}")
        except Exception as e:
            logger.error(f"Fallback save failed: {e}")
    
    def load_to_polars(self, **kwargs) -> pl.DataFrame:
        """从 Parquet 文件加载数据"""
        file_path = self.data_dir / f"{self.table_name}.parquet"
        
        if not file_path.exists():
            return pl.DataFrame()
        
        try:
            df = pl.read_parquet(file_path)
            
            # 应用过滤条件
            start_date = kwargs.get('start_date')
            end_date = kwargs.get('end_date')
            
            if start_date and 'trade_date' in df.columns:
                trade_date = pl.col("trade_date").cast(pl.String).str.slice(0, 10)
                df = df.filter(trade_date >= pl.lit(str(start_date)))
            if end_date and 'trade_date' in df.columns:
                trade_date = pl.col("trade_date").cast(pl.String).str.slice(0, 10)
                df = df.filter(trade_date <= pl.lit(str(end_date)))
            
            return df
        except Exception as e:
            logger.error(f"Fallback load failed: {e}")
            return pl.DataFrame()


class DragonEyeManager:
    """DragonEye 模块数据管理器，专注于龙头股与市场情绪数据的持久化
    
    【架构变更说明】
    已从 LanceDB 迁移到 LanceDB 三层架构。
    - 如果 LanceDB 可用，使用 LanceDB 存储
    - 如果不可用，使用 Parquet 文件作为备用存储
    """
    
    def __init__(self):
        # 使用统一的数据目录
        self.data_dir = Path(Config.PARQUET_DIR) / "dragon_eye"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # 尝试使用 LanceDB，失败则使用备用实现
        try:
            if Config.is_lancedb_backend():
                from data_svc.storage.lancedb_manager import get_lancedb_manager
                self.lancedb_mgr = get_lancedb_manager()
                self.stock_mgr = None  # 使用 LanceDB 直接操作
                self.sentiment_mgr = None
                logger.info("DragonEyeManager initialized with LanceDB")
            else:
                raise ImportError("LanceDB backend not enabled")
        except Exception as e:
            logger.warning(f"LanceDB not available: {e}, using fallback mode (Parquet)")
            self.lancedb_mgr = None
            self.stock_mgr = _FallbackDataManager(data_dir=str(self.data_dir), table_name="dragon_stock")
            self.sentiment_mgr = _FallbackDataManager(data_dir=str(self.data_dir), table_name="market_sentiment")

    def upsert_stocks(self, df_stocks: pd.DataFrame | pl.DataFrame):
        """更新龙头个股数据"""
        if _frame_is_empty(df_stocks):
            return
        if not _has_columns(df_stocks, {"stock_code", "trade_date"}):
            logger.warning("Skipping dragon stock frame without stock_code/trade_date")
            return

        if self.lancedb_mgr:
            df_stocks = _to_pandas(df_stocks)

        logger.info(f"Upserting {len(df_stocks)} records into dragon_stock")

        if self.lancedb_mgr:
            # 使用 LanceDB 存储
            try:
                # 补全 _id 字段用于 Upsert
                if "_id" not in df_stocks.columns:
                    df_stocks["_id"] = df_stocks["stock_code"].astype(str) + "_" + df_stocks["trade_date"].astype(str)
                # 龙头股数据是多条记录共享同一个日期索引
                # 不能使用 append 模式，因为会导致按索引去重只保留最后一条
                # 改为使用 stock_code + trade_date 作为复合索引
                df_to_write = df_stocks.copy()
                if 'trade_date' in df_to_write.columns:
                    df_to_write['trade_date'] = pd.to_datetime(df_to_write['trade_date'])
                
                # 创建复合索引: stock_code + trade_date
                df_to_write['index_key'] = df_to_write['stock_code'].astype(str) + '_' + df_to_write['trade_date'].dt.strftime('%Y%m%d')
                df_to_write.set_index('index_key', inplace=True)
                
                # 读取已有数据
                lib = self.lancedb_mgr._get_or_create_library("factor")
                try:
                    existing = lib.read("dragon_stock")
                    if existing is not None and not existing.data.empty:
                        # 合并数据，按 index_key 去重
                        combined = pd.concat([existing.data, df_to_write])
                        combined = combined[~combined.index.duplicated(keep='last')]
                        df_to_write = combined
                        logger.info(f"Merged with existing data, total {len(df_to_write)} records")
                except Exception:
                    # 数据不存在，直接写入
                    pass
                
                # 写入数据（不使用 write_daily_data 的 append 逻辑）
                meta = {
                    "type": "dragon_eye",
                    "updated_at": datetime.now().isoformat(),
                    "rows": len(df_to_write),
                    "columns": list(df_to_write.columns),
                }
                lib.write("dragon_stock", df_to_write, metadata=meta)
                logger.info(f"Successfully wrote {len(df_to_write)} records to dragon_stock")
            except Exception as e:
                logger.error(f"LanceDB upsert failed: {e}")
        elif self.stock_mgr:
            if isinstance(df_stocks, pl.DataFrame) and "_id" not in df_stocks.columns:
                df_stocks = df_stocks.with_columns(
                    (
                        pl.col("stock_code").cast(pl.String)
                        + "_"
                        + pl.col("trade_date").cast(pl.String)
                    ).alias("_id")
                )
            elif isinstance(df_stocks, pd.DataFrame) and "_id" not in df_stocks.columns:
                df_stocks = df_stocks.copy()
                df_stocks["_id"] = df_stocks["stock_code"].astype(str) + "_" + df_stocks["trade_date"].astype(str)
            self.stock_mgr.upsert_daily_data(df_stocks)

    def upsert_sentiment(self, df_sentiment: pd.DataFrame | pl.DataFrame):
        """更新当日市场情绪大盘指标"""
        if _frame_is_empty(df_sentiment):
            return
        if not _has_columns(df_sentiment, {"trade_date"}):
            logger.warning("Skipping sentiment frame without trade_date")
            return

        if self.lancedb_mgr:
            df_sentiment = _to_pandas(df_sentiment)

        trade_date = (
            df_sentiment["trade_date"][0]
            if isinstance(df_sentiment, pl.DataFrame)
            else df_sentiment["trade_date"].iloc[0]
        )
        logger.info(f"Upserting sentiment record for {trade_date}")

        if self.lancedb_mgr:
            # 使用 LanceDB 存储
            try:
                # 补全 _id
                if "_id" not in df_sentiment.columns:
                    df_sentiment["_id"] = df_sentiment["trade_date"].astype(str)
                # LanceDB 要求 DatetimeIndex
                df_to_write = df_sentiment.copy()
                if 'trade_date' in df_to_write.columns:
                    df_to_write['trade_date'] = pd.to_datetime(df_to_write['trade_date'])
                    df_to_write.set_index('trade_date', inplace=True)
                
                self.lancedb_mgr.write_daily_data(
                    library="factor",
                    symbol="market_sentiment",
                    df=df_to_write,
                    metadata={"type": "dragon_eye", "updated_at": datetime.now().isoformat()}
                )
            except Exception as e:
                logger.error(f"LanceDB upsert failed: {e}")
        elif self.sentiment_mgr:
            if isinstance(df_sentiment, pl.DataFrame) and "_id" not in df_sentiment.columns:
                df_sentiment = df_sentiment.with_columns(
                    pl.col("trade_date").cast(pl.String).alias("_id")
                )
            elif isinstance(df_sentiment, pd.DataFrame) and "_id" not in df_sentiment.columns:
                df_sentiment = df_sentiment.copy()
                df_sentiment["_id"] = df_sentiment["trade_date"].astype(str)
            self.sentiment_mgr.upsert_daily_data(df_sentiment)

    def get_historical_dragon(self, start_date: str, end_date: str) -> pl.DataFrame:
        """获取历史龙头股列表"""
        if self.lancedb_mgr:
            try:
                # 直接读取数据，不过滤日期范围（因为索引是 stock_code_date 格式）
                lib = self.lancedb_mgr._get_or_create_library("factor")
                try:
                    result = lib.read("dragon_stock")
                    if result is not None and not result.data.empty:
                        df = result.data
                        # 恢复索引为列
                        df = df.reset_index()
                        # 如果 trade_date 列存在，过滤日期范围
                        if 'trade_date' in df.columns:
                            df['trade_date'] = pd.to_datetime(df['trade_date'])
                            start_dt = pd.to_datetime(start_date)
                            end_dt = pd.to_datetime(end_date)
                            df = df[(df['trade_date'] >= start_dt) & (df['trade_date'] <= end_dt)]
                        return pl.from_pandas(df)
                except Exception:
                    pass
                return pl.DataFrame()
            except Exception as e:
                logger.error(f"LanceDB read failed: {e}")
                return pl.DataFrame()
        elif self.stock_mgr:
            return self.stock_mgr.load_to_polars(start_date=start_date, end_date=end_date)
        return pl.DataFrame()

    def get_market_sentiment(self, start_date: str, end_date: str) -> pl.DataFrame:
        """获取历史大盘情绪指标"""
        if self.lancedb_mgr:
            try:
                df = self.lancedb_mgr.read_data(
                    library="factor",
                    symbol="market_sentiment",
                    start=start_date,
                    end=end_date
                )
                if df is not None:
                    # 恢复索引为列
                    if df.index.name == 'trade_date':
                        df = df.reset_index()
                    return pl.from_pandas(df)
                return pl.DataFrame()
            except Exception as e:
                logger.error(f"LanceDB read failed: {e}")
                return pl.DataFrame()
        elif self.sentiment_mgr:
            return self.sentiment_mgr.load_to_polars(start_date=start_date, end_date=end_date)
        return pl.DataFrame()
