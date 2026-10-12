import json
import pandas as pd
import polars as pl
try:
    import cupy as np
except ImportError:
    import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Generator
from threading import Event
from functools import lru_cache
from data_svc.database.optimized_data_query import OptimizedStockDataQuery
from core.quant.backtest.unified_engine import UnifiedBacktestEngine
from core.quant.strategies.strategy_factory import StrategyFactory
import os
import sys
from pathlib import Path
import re
import pyarrow.parquet as pq

try:
    from data_svc.storage.lancedb_reader import LanceDBDataReader
    LANCEDB_READER_AVAILABLE = True
except ImportError:
    LANCEDB_READER_AVAILABLE = False

# 添加项目根目录到 Python 路径
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config.config import Config  # type: ignore

class BacktestVisualizationAPI:
    """回测数据可视化API接口"""
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or Config.DB_PATH
        self.data_query: Optional[OptimizedStockDataQuery] = None
        self.backtest_engine: Optional[UnifiedBacktestEngine] = None
        self.stock_info_map: Dict[str, str] = {}
        self._initialized = False
        self.initial_capital = Config.INITIAL_CAPITAL

    @staticmethod
    def _normalize_symbol_code(symbol_code: Optional[str]) -> str:
        if not symbol_code: return ''
        code = str(symbol_code).strip().upper()
        match = re.search(r'(\d+)', code)
        if match:
            # CHANGED: Back to stripping zeros for database compatibility.
            # The database stores '1', not '000001'. Padding is handled at the UI layer.
            return str(int(match.group(1)))
        return code
    
    def _ensure_initialized(self) -> None:
        if self._initialized: return
        from config.logger import get_logger
        logger = get_logger(__name__)
        try:
            # CHANGED: Switch to Unified Data Layer
            use_unified = os.getenv("USE_UNIFIED_DATA", "1") == "1"
            
            if use_unified:
                try:
                    from data_svc.unified_data_query import UnifiedDataQueryAdapter
                    self.data_query = UnifiedDataQueryAdapter(self.db_path)
                except Exception as e:
                    logger.warning(f"Failed to load UnifiedDataManager: {e}")
                    self.data_query = OptimizedStockDataQuery(self.db_path)
            else:
                self.data_query = OptimizedStockDataQuery(self.db_path)
                
            self.backtest_engine = UnifiedBacktestEngine(self.data_query)
            self.stock_info_map = self._load_stock_info()
            self._initialized = True
        except Exception as e:
            logger.error(f"[ERROR] 数据库初始化失败: {e}", exc_info=True)
            raise

    def _load_stock_info(self) -> Dict[str, str]:
        """
        【核心修复】使用 data_query 方法加载股票信息，支持 Polars + Parquet 后端
        """
        from config.logger import get_logger
        try:
            if self.data_query is None:
                return {}
            
            get_stock_basic_polars = getattr(self.data_query, "get_stock_basic_polars", None)
            if callable(get_stock_basic_polars):
                df = get_stock_basic_polars()
            else:
                # 仅保留旧查询适配器作为兼容边界，内部优先走 Polars。
                df = self.data_query._query_df(
                    "SELECT stock_code, stock_name FROM stock_info"
                )

            if df is None:
                return {}
            if isinstance(df, pl.DataFrame):
                if df.is_empty():
                    return {}
            elif df.empty:
                return {}

            if isinstance(df, pl.DataFrame):
                columns = [column for column in ("stock_code", "stock_name") if column in df.columns]
                if "stock_code" not in columns:
                    return {}
                rows = df.select(columns).to_dicts()
            else:
                if "stock_code" not in df.columns:
                    return {}
                columns = ["stock_code"]
                if "stock_name" in df.columns:
                    columns.append("stock_name")
                rows = df[columns].to_dict("records")

            stock_info_dict = {
                self._normalize_symbol_code(row.get("stock_code")): str(row.get("stock_name", "")).strip()
                for row in rows
                if row.get("stock_code") is not None
            }
            
            logger = get_logger(__name__)
            logger.debug(f"加载股票信息: {len(stock_info_dict)} 条记录")
            return stock_info_dict
        except Exception as e:
            logger = get_logger(__name__)
            logger.warning(f"加载股票信息失败: {e}")
            return {}

    def _load_missing_stock_names(self, stock_codes: List[str]) -> Dict[str, str]:
        """Batch-load names absent from the in-memory stock map."""
        missing = []
        for code in stock_codes:
            normalized = self._normalize_symbol_code(code)
            if normalized and not self.stock_info_map.get(normalized) and normalized not in missing:
                missing.append(normalized)
        if not missing or self.data_query is None:
            return {}

        query_df = getattr(self.data_query, "_query_df", None)
        if not callable(query_df):
            return {}

        try:
            placeholders = ",".join("?" for _ in missing)
            frame = query_df(
                f"SELECT stock_code, stock_name FROM stock_info WHERE stock_code IN ({placeholders})",
                missing,
            )
            if frame is None or (frame.is_empty() if isinstance(frame, pl.DataFrame) else frame.empty):
                return {}
            rows = frame.to_dicts() if isinstance(frame, pl.DataFrame) else frame.to_dict("records")
            names = {}
            for row in rows:
                code = self._normalize_symbol_code(row.get("stock_code"))
                value = row.get("stock_name", "")
                if code and value is not None and not pd.isna(value) and str(value).strip():
                    names[code] = str(value).strip()
            return names
        except Exception as exc:
            from config.logger import get_logger
            get_logger(__name__).debug("批量查询股票名称失败: %s", exc)
            return {}

    def _get_global_latest_factors(self, symbol_codes: List[str]) -> Dict[str, float]:
        """Batch-load the latest adjustment factor for each requested symbol."""
        codes = []
        for code in symbol_codes:
            normalized = self._normalize_symbol_code(code)
            if normalized and normalized not in codes:
                codes.append(normalized)
        if not codes or self.data_query is None:
            return {}

        query_df = getattr(self.data_query, "_query_df", None)
        if not callable(query_df):
            return {code: self._get_global_latest_factor(code) for code in codes}

        try:
            placeholders = ",".join("?" for _ in codes)
            frame = query_df(
                f"""
                SELECT stock_code, adj_factor
                FROM (
                    SELECT stock_code, adj_factor,
                           ROW_NUMBER() OVER (
                               PARTITION BY stock_code ORDER BY trade_date DESC
                           ) AS row_number
                    FROM stock_daily
                    WHERE stock_code IN ({placeholders})
                ) latest
                WHERE row_number = 1
                """,
                codes,
            )
            if frame is None or (frame.is_empty() if isinstance(frame, pl.DataFrame) else frame.empty):
                return {code: 1.0 for code in codes}
            rows = frame.to_dicts() if isinstance(frame, pl.DataFrame) else frame.to_dict("records")
            factors = {code: 1.0 for code in codes}
            for row in rows:
                code = self._normalize_symbol_code(row.get("stock_code"))
                value = row.get("adj_factor", 1.0)
                if code in factors and value is not None and not pd.isna(value):
                    factors[code] = float(value)
            return factors
        except Exception as exc:
            from config.logger import get_logger
            get_logger(__name__).debug("批量查询最新复权因子失败，回退逐股查询: %s", exc)
            return {code: self._get_global_latest_factor(code) for code in codes}

    def _get_global_latest_factor(self, symbol_code: str) -> float:
        """
        【核心修复】获取该股票在数据库中最新（最后一天）的复权因子
        使用 data_query 方法，支持 Polars + Parquet 后端
        """
        from config.logger import get_logger
        try:
            if self.data_query is None:
                return 1.0
            
            # CHANGED: Adapter Support
            if hasattr(self.data_query, 'get_latest_daily'):
                row = self.data_query.get_latest_daily(symbol_code)
                factor = row.get('adj_factor', 1.0) if row else 1.0
                return float(factor) if factor is not None and not pd.isna(factor) else 1.0

            # 使用 data_query 的 _query_df 方法，支持 Polars 和 SQLite
            query = """
                SELECT adj_factor 
                FROM stock_daily 
                WHERE stock_code = ? 
                ORDER BY trade_date DESC 
                LIMIT 1
            """
            df = self.data_query._query_df(query, [symbol_code])
            
            if df.empty or 'adj_factor' not in df.columns:
                return 1.0
            
            factor = df.iloc[0]['adj_factor']
            return float(factor) if factor is not None and not pd.isna(factor) else 1.0
        except Exception as e:
            logger = get_logger(__name__)
            logger.warning(f"获取最新复权因子失败 ({symbol_code}): {e}")
            return 1.0
    # --- 【【新增】】统一的前复权计算工具函数 ---
    def _calculate_qfq_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        将 DataFrame 中的价格列转换为前复权 (QFQ) 价格
        逻辑：QFQ = Raw * (AdjFactor / LatestFactor)
        
        使用这段时间内的最新复权因子作为基准，确保价格序列平滑连续
        
        【性能优化】使用 Polars 零拷贝，比 Pandas 快 4-10 倍
        """
        if df is None or df.empty or 'adj_factor' not in df.columns:
            return df
        
        price_cols = ['open', 'high', 'low', 'close', 'prev_close', 'ma5', 'ma10', 'ma20']
        available_price_cols = [c for c in price_cols if c in df.columns]
        
        if not available_price_cols:
            return df
        
        try:
            df_pl = pl.from_pandas(df)
            
            if 'trade_date' in df_pl.columns:
                latest_factors = (df_pl
                    .sort('trade_date', descending=True)
                    .group_by('stock_code')
                    .agg(pl.col('adj_factor').first().alias('latest_factor'))
                )
            else:
                latest_factors = (df_pl
                    .group_by('stock_code')
                    .agg(pl.col('adj_factor').last().alias('latest_factor'))
                )
            
            df_pl = df_pl.join(latest_factors, on='stock_code', how='left')
            
            df_pl = df_pl.with_columns([
                pl.when(pl.col('latest_factor') == 0)
                  .then(1.0)
                  .otherwise(pl.col('latest_factor'))
                  .alias('latest_factor_safe')
            ])
            
            qfq_ratio = pl.col('adj_factor') / pl.col('latest_factor_safe')
            
            exprs = [pl.col('latest_factor_safe').alias('qfq_ratio')]
            for col in available_price_cols:
                exprs.append((pl.col(col).cast(pl.Float64) * qfq_ratio).alias(col))
            
            df_pl = df_pl.with_columns(exprs)
            df_pl = df_pl.drop(['latest_factor', 'latest_factor_safe', 'qfq_ratio'])
            
            return df_pl.to_pandas(use_pyarrow_extension_array=True)
            
        except Exception as e:
            from config.logger import get_logger
            logger = get_logger(__name__)
            logger.warning(f"Polars QFQ 计算失败，回退到 Pandas: {e}")
            
            df = df.copy()
            
            if 'trade_date' in df.columns:
                df_sorted = df.sort_values(['stock_code', 'trade_date'])
                latest_factors = df_sorted.groupby('stock_code')['adj_factor'].last()
                df['latest_factor'] = df['stock_code'].map(latest_factors)
            else:
                latest_factors = df.groupby('stock_code')['adj_factor'].transform('last')
                df['latest_factor'] = latest_factors
            
            df['latest_factor'] = df['latest_factor'].replace(0, 1.0)
            qfq_ratio = df['adj_factor'] / df['latest_factor']
            
            for col in available_price_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce') * qfq_ratio
            
            df.drop(columns=['latest_factor'], inplace=True, errors='ignore')
            
            return df

    def get_strategy_list(self) -> List[Dict]:
        """获取策略列表（不需要数据库连接）"""
        strategies = []
        try:
            # 直接导入并使用get_factory函数
            from core.quant.strategies.strategy_factory import get_factory
            
            factory = get_factory()
            
            # 使用list_strategies方法获取更详细的策略信息
            strategy_info_list = factory.list_strategies()
            
            # 确保strategy_info_list是列表类型
            if not isinstance(strategy_info_list, list):
                print(f"[ERROR] 策略工厂返回的不是列表，而是: {type(strategy_info_list)}")
                return []
            
            for idx, strategy_info in enumerate(strategy_info_list):
                # 移除 DEBUG 日志
                
                # 安全地获取策略信息
                strategy_id = strategy_info.get('name', '')
                class_name = strategy_info.get('class_name', '')
                
                # 如果name为空，尝试使用class_name
                if not strategy_id and class_name:
                    strategy_id = class_name
                    # 移除 DEBUG 日志
                
                if not strategy_id:
                    # 移除 DEBUG 日志
                    continue
                
                strategy_name = strategy_id  # 使用名称作为ID和名称
                
                # 创建策略字典并添加到结果列表
                strategy_dict = {
                    "id": strategy_id,
                    "name": strategy_name,
                    "description": f"{strategy_name}的描述",
                    "createdDate": "2024-01-01",
                    "lastUpdated": "2024-01-01",
                    "performance": 0.0,
                    "status": "active"
                }
                strategies.append(strategy_dict)
                # 移除 DEBUG 日志
            
            # 移除 DEBUG 日志
            return strategies
        except Exception as e:
            print(f"[ERROR] 获取策略列表失败: {e}")
            import traceback
            traceback.print_exc()
            return []  # 返回空列表而不是报错
    
    def get_strategy_logic(self, strategy_id: str) -> Dict[str, Any]:
        """
        获取策略的逻辑描述
        【优化】直接读取策略类的 DocString，不再维护硬编码字典
        """
        try:
            strategy_class = StrategyFactory.get_factory().get_strategy_by_name(strategy_id)
            if not strategy_class:
                return {"buy_logic": "策略未找到", "sell_logic": "", "description": ""}
            
            # 1. 优先尝试调用策略类的 get_logic_description 方法 (如果你实现了的话)
            if hasattr(strategy_class, "get_logic_description"):
                return strategy_class.get_logic_description()

            # 2. 否则自动解析 Python 文档注释 (__doc__)
            doc = strategy_class.__doc__ or "暂无描述"
            
            # 简单粗暴的解析：假设文档里写了 "买入逻辑：" 和 "卖出逻辑："
            # 如果没写，就全部丢给 description
            return {
                "buy_logic": "请在策略代码类注释中补充买入逻辑...", # 前端兼容占位
                "sell_logic": "请在策略代码类注释中补充卖出逻辑...", 
                "description": doc.strip()
            }
        except Exception as e:
            print(f"获取策略逻辑失败: {e}")
            return {"buy_logic": "获取失败", "sell_logic": "", "description": ""}
    
    def get_strategy_params(self, strategy_id: str) -> list[dict]:
        """
        返回给前端用的参数列表。
        【优化】彻底移除 param_metadata 映射和 default_min/max 猜测逻辑。
        只有策略类明确说了范围 (get_param_spec) 才返回，否则一律 None，由前端接管。
        """
        # 1. 找到策略类
        strategy_class = StrategyFactory.get_factory().get_strategy_by_name(strategy_id)
        if strategy_class is None:
            raise ValueError(f"未找到策略：{strategy_id}")

        # 2. 优先使用策略类提供的 PARAM_SPEC
        if hasattr(strategy_class, "get_param_spec"):
            spec = strategy_class.get_param_spec()
            if spec:
                # 完全放权给前端：不返回 min/max，让前端自主决定搜索范围
                return [
                    {
                        "key": item["key"],
                        "label": item.get("label", item["key"]),
                        "group": item.get("group", "其它"),
                        "type": item.get("type", "float"),
                        "min": None,  # 不返回范围限制，完全由前端控制
                        "max": None,  # 不返回范围限制，完全由前端控制
                        "step": item.get("step"),
                        "default": item.get("default"),
                        "optimize": item.get("optimize", True),
                        "description": item.get("description", ""),
                    }
                    for item in spec
                ]

        # 3. 退回 dataclass 自动推断（支持 field metadata）
        strategy = StrategyFactory.get_factory().create_strategy(strategy_id)
        if hasattr(strategy, "config"):
            from dataclasses import is_dataclass, asdict, fields

            config = strategy.config
            if is_dataclass(config):
                # 使用 fields() 获取 field metadata
                config_fields = fields(config)
                config_dict = asdict(config)
                
                params = []
                
                for field_obj in config_fields:
                    field_name = field_obj.name
                    field_value = config_dict[field_name]
                    
                    # 跳过非优化参数
                    if isinstance(field_value, (list, dict)) or field_name.startswith('_'):
                        continue
                    
                    # 从 metadata 中提取参数信息
                    metadata = field_obj.metadata if hasattr(field_obj, 'metadata') else {}
                    
                    # 如果 metadata 中明确标记了 optimize=False，跳过
                    if metadata.get("optimize", True) is False:
                        continue
                    
                    # 从 metadata 中获取类型，否则根据默认值推断
                    param_type = metadata.get("type") or ('int' if isinstance(field_value, int) else 'float')
                    
                    # 从 metadata 中获取 min/max，如果没有则返回 None（让前端决定）
                    param_min = metadata.get("min")
                    param_max = metadata.get("max")
                    
                    # 如果 metadata 中没有 min/max，返回 None（让前端决定）
                    # 前端可以通过 CUSTOM_PARAM_CONFIG 覆盖，或使用默认范围生成器
                    
                    params.append({
                        "key": field_name,
                        "label": metadata.get("label", field_name),
                        "group": metadata.get("group", "其它"),
                        "type": param_type,
                        "min": param_min,
                        "max": param_max,
                        "step": metadata.get("step"),
                        "default": field_value,
                        "description": metadata.get("description", ""),
                        "optimize": metadata.get("optimize", True),
                    })
                
                return params
            elif isinstance(config, dict):
                # 兼容旧式字典配置（无 metadata）
                params = []
                for field_name, field_value in config.items():
                    if isinstance(field_value, (list, dict)) or field_name.startswith('_'):
                        continue
                    param_type = 'int' if isinstance(field_value, int) else 'float'
                    params.append({
                        "key": field_name,
                        "label": field_name,
                        "type": param_type,
                        "min": None,
                        "max": None,
                        "default": field_value,
                        "description": ""
                    })
                return params
            else:
                return []

        # 4. 实在啥都没有，就返回空
        return []
    
    def _get_benchmark_data_from_db(self, benchmark_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取基准数据（从 LanceDB 读取指数数据）"""
        from config.logger import get_logger
        logger = get_logger(__name__)
        
        INDEX_MAPPING = {
            '000300': '000300.SH',
            '000905': '000905.SH',
            '000001': '000001.SH',
            '399001': '399001.SZ',
            '000016': '000016.SH',
            '399006': '399006.SZ',
        }

        normalized_code = str(benchmark_code or '').strip().upper()
        if normalized_code in INDEX_MAPPING:
            ts_code = INDEX_MAPPING[normalized_code]
        elif normalized_code in INDEX_MAPPING.values():
            ts_code = normalized_code
        else:
            logger.warning(f"不支持的基准代码: {benchmark_code}")
            return pd.DataFrame()
        
        try:
            from data_svc.query.fast_market_query import get_fast_market_query

            df = get_fast_market_query()._read_table(
                "index_daily",
                symbols=ts_code,
                start_date=start_date,
                end_date=end_date,
                fields=["trade_date", "close"],
            )

            if df.is_empty():
                logger.warning(f"LanceDB 中未找到基准数据: {ts_code}, {start_date} ~ {end_date}")
                return pd.DataFrame()

            return (
                df.select(
                    pl.col("trade_date")
                    .cast(pl.Date, strict=False)
                    .dt.strftime("%Y-%m-%d")
                    .alias("date"),
                    pl.col("close").cast(pl.Float64),
                )
                .sort("date")
                .to_pandas()
            )

        except Exception as e:
            logger.warning(f"读取基准数据时发生错误: {e}")
            return pd.DataFrame()

    
    # --- 【【代码还原】】: 还原旧的、阻塞的 API 函数 (作为备用) ---
    def _get_index_kline_from_parquet(self, symbol_code: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """
        获取指数K线数据
        
        优先从 LanceDB index_daily 表读取，回退到 Parquet
        支持：000300(沪深300), 000905(中证500), 000001(上证指数), 399001(深证成指), 000016(上证50), 399006(创业板)
        """
        INDEX_SYMBOL_MAPPING = {
            '000300': '000300.SH',
            '000905': '000905.SH',
            '000001': '000001.SH',
            '399001': '399001.SZ',
            '000016': '000016.SH',
            '399006': '399006.SZ',
        }
        
        INDEX_PARQUET_MAPPING = {
            '000300': 'hs300_daily.parquet',
            '000905': 'zz500_daily.parquet',
            '000001': 'sh_index_daily.parquet',
            '399001': 'sz_index_daily.parquet',
            '000016': 'sz50_daily.parquet',
            '399006': 'cyb_index_daily.parquet',
        }
        
        if symbol_code not in INDEX_SYMBOL_MAPPING:
            return []
        
        lancedb_symbol = INDEX_SYMBOL_MAPPING[symbol_code]
        
        try:
            import lancedb
            lancedb_path = Path(__file__).parent.parent / "data" / "lancedb"
            db = lancedb.connect(str(lancedb_path))
            
            result = db.list_tables()
            tables = result.tables if hasattr(result, 'tables') else list(result)
            
            if 'index_daily' in tables:
                table = db.open_table('index_daily')
                ds = table.to_lance()
                
                if hasattr(ds, 'scanner'):
                    scanner = ds.scanner(
                        filter=f"symbol = '{lancedb_symbol}'",
                        columns=['trade_date', 'open', 'high', 'low', 'close', 'volume']
                    )
                    arrow_table = scanner.to_table()
                else:
                    arrow_table = table.to_arrow()
                
                df = pl.from_arrow(arrow_table)
                
                if not df.is_empty():
                    df = df.rename({'trade_date': 'date'})
                    
                    if df['date'].dtype == pl.Datetime:
                        df = df.with_columns(
                            pl.col('date').dt.date().alias('date')
                        )
                    
                    start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
                    end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
                    
                    df = df.filter(
                        (pl.col("date") >= start_dt) & (pl.col("date") <= end_dt)
                    ).sort("date")
                    
                    if not df.is_empty():
                        records = []
                        for row in df.iter_rows(named=True):
                            date_val = row['date']
                            if hasattr(date_val, 'strftime'):
                                date_str = date_val.strftime('%Y-%m-%d')
                            else:
                                date_str = str(date_val)[:10]
                            
                            records.append({
                                "date": date_str,
                                "open": float(f"{row['open']:.2f}"),
                                "high": float(f"{row['high']:.2f}"),
                                "low": float(f"{row['low']:.2f}"),
                                "close": float(f"{row['close']:.2f}"),
                                "volume": float(row['volume']) if row.get('volume') is not None else 0,
                                "change_pct": None
                            })
                        return records
                    
        except Exception as e:
            logger.warning(f"从 LanceDB 读取指数数据失败: {e}")
        
        if not Config.parquet_fallback_enabled():
            logger.info("Parquet fallback disabled, skip index parquet data for %s", symbol_code)
            return []

        try:
            parquet_file = Path('data/parquet_data') / INDEX_PARQUET_MAPPING[symbol_code]
            
            if not parquet_file.exists():
                return []
            
            df = pl.scan_parquet(str(parquet_file)).filter(
                (pl.col("date") >= start_date) & (pl.col("date") <= end_date)
            ).sort("date").collect()
            
            if df.is_empty():
                return []
            
            records = []
            for row in df.iter_rows(named=True):
                change_pct = row.get('change_pct')
                if change_pct is not None:
                    if isinstance(change_pct, str):
                        change_pct = float(change_pct.replace('%', ''))
                    else:
                        change_pct = float(change_pct)
                
                date_val = row['date']
                if hasattr(date_val, 'strftime'):
                    date_str = date_val.strftime('%Y-%m-%d')
                else:
                    date_str = str(date_val)[:10]
                
                records.append({
                    "date": date_str,
                    "open": float(f"{row['open']:.2f}"),
                    "high": float(f"{row['high']:.2f}"),
                    "low": float(f"{row['low']:.2f}"),
                    "close": float(f"{row['close']:.2f}"),
                    "volume": float(row['volume']) if row.get('volume') is not None else 0,
                    "change_pct": change_pct
                })
            return records
            
        except Exception as e:
            logger.warning(f"从 parquet 读取指数数据失败 {symbol_code}: {e}")
            return []

    def get_symbol_kline(self, symbol_code: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """
        获取K线数据（强制全局前复权）
        修复：不再使用区间内的最新因子，而是使用数据库里的全局最新因子。
        新增：支持从 index parquet 文件读取指数数据
        """
        if not self._initialized:
            self._ensure_initialized()

        # 保存原始代码用于指数查询（_normalize_symbol_code 会去掉前导零）
        original_symbol_code = symbol_code.strip().upper() if symbol_code else ''
        
        symbol_code = self._normalize_symbol_code(symbol_code)
        if not symbol_code:
            return []
        
        # 首先尝试从 index parquet 读取（指数数据）- 使用原始代码
        index_data = self._get_index_kline_from_parquet(original_symbol_code, start_date, end_date)
        if index_data:
            return index_data
        
        # 尝试从 LanceDB 读取股票数据
        if LANCEDB_READER_AVAILABLE:
            try:
                reader = LanceDBDataReader()
                # 将股票代码转换为 ts_code 格式
                ts_code = self._convert_to_ts_code(original_symbol_code)
                
                df = reader.read(ts_code, start_date, end_date)
                
                if not df.is_empty():
                    records = []
                    for row in df.iter_rows(named=True):
                        trade_date = row.get('trade_date')
                        if hasattr(trade_date, 'strftime'):
                            date_str = trade_date.strftime('%Y-%m-%d')
                        else:
                            date_str = str(trade_date)[:10]
                        
                        record = {
                            "date": date_str,
                            "open": float(f"{row['open']:.2f}") if row.get('open') is not None else None,
                            "high": float(f"{row['high']:.2f}") if row.get('high') is not None else None,
                            "low": float(f"{row['low']:.2f}") if row.get('low') is not None else None,
                            "close": float(f"{row['close']:.2f}") if row.get('close') is not None else None,
                            "volume": float(row['volume']) if row.get('volume') is not None else 0,
                            "ma5": float(f"{row['ma5']:.2f}") if row.get('ma5') is not None else None,
                            "ma10": float(f"{row['ma10']:.2f}") if row.get('ma10') is not None else None,
                            "ma20": float(f"{row['ma20']:.2f}") if row.get('ma20') is not None else None,
                        }
                        if record['open'] is not None and record['close'] is not None:
                            records.append(record)
                    return records
            except Exception as e:
                print(f"[K线查询] LanceDB 读取失败: {e}")
        
        if not Config.parquet_fallback_enabled():
            print(f"[Kline] LanceDB returned no data; Parquet fallback disabled: {symbol_code}")
            return []

        try:
            import time
            t0 = time.perf_counter()

            # 1. 获取全局最新因子作为基准
            base_factor = self._get_global_latest_factor(symbol_code)

            # 2. 直接使用 Polars 读取 Parquet（零拷贝）
            parquet_dir = os.getenv("PARQUET_DIR", str(Path(__file__).parent.parent / "data" / "parquet"))
            stock_daily_path = Path(parquet_dir) / "stock_daily.parquet"

            if not stock_daily_path.exists():
                print(f"[K线查询] Parquet文件不存在: {stock_daily_path}")
                return []

            # 需要读取的列（精确投影）
            needed_cols = ["stock_code", "trade_date", "open", "high", "low", "close", "volume", "adj_factor", "ma5", "ma10", "ma20"]

            t1 = time.perf_counter()

            # 使用 Polars Lazy API 进行谓词下推 + 精确投影
            # 优化: 先过滤 stock_code（最有效），再过滤日期范围
            lazy_df = (
                pl.scan_parquet(str(stock_daily_path), schema=needed_cols)
                .filter(pl.col("stock_code") == symbol_code)
                .filter(pl.col("trade_date") >= start_date)
                .filter(pl.col("trade_date") <= end_date)
                .sort("trade_date")
            )

            # 性能诊断: 打印执行计划
            if os.getenv("DEBUG_QUERY", "0") == "1":
                print(f"[DEBUG] 执行计划:\n{lazy_df.explain()}")

            # Parquet 行组诊断
            if os.getenv("DEBUG_PARQUET", "0") == "1":
                pf = pq.ParquetFile(str(stock_daily_path))
                print(f"[DEBUG] Parquet 元数据:")
                print(f"  - 行组数: {pf.metadata.num_row_groups}")
                print(f"  - 总行数: {pf.metadata.num_rows}")
                print(f"  - 列数: {pf.metadata.num_columns}")
                rg = pf.metadata.row_group(0)
                print(f"  - 行组0 列数: {rg.num_columns}, 行数: {rg.num_rows}")
                # 估算 stock_code 列的统计信息
                for col_idx in range(min(3, rg.num_columns)):
                    col = rg.column(col_idx)
                    print(f"  - 列{col_idx} ({col.path_in_schema}): min={col.statistics.min}, max={col.statistics.max}")

            # 收集为 Polars DataFrame
            df = lazy_df.collect()
            t2 = time.perf_counter()
            print(f"[PERF] Polars查询: {(t2-t1)*1000:.1f}ms, {len(df)}行")

            if df.is_empty():
                return []

            # 3. 计算前复权 (Polars 向量化操作)
            qfq_ratio = pl.col("adj_factor") / base_factor
            price_cols = [c for c in ["open", "high", "low", "close", "ma5", "ma10", "ma20"] if c in df.columns]

            for col in price_cols:
                df = df.with_columns(
                    (pl.col(col) * qfq_ratio).round(2).alias(col)
                )

            # 4. 格式化日期和成交量为最终输出
            df = df.with_columns(
                pl.col("trade_date").dt.to_string("%Y-%m-%d").alias("date"),
                pl.col("volume").cast(pl.Int64).cast(str).alias("volume_str")
            )

            t3 = time.perf_counter()

            # 5. 直接构建字典列表（Polars to_dicts 是高效的）
            records = []
            for row in df.iter_rows(named=True):
                date_val = row.get('date', '')
                open_val = row.get('open')
                close_val = row.get('close')

                if open_val is None or close_val is None:
                    continue

                record = {
                    "date": date_val,
                    "open": round(float(open_val), 2),
                    "high": round(float(row.get('high', 0)), 2),
                    "low": round(float(row.get('low', 0)), 2),
                    "close": round(float(close_val), 2),
                    "volume": int(float(row.get('volume', 0))) if row.get('volume') is not None else 0,
                }

                # MA 指标（如果存在）
                for ma in ['ma5', 'ma10', 'ma20']:
                    if ma in row and row[ma] is not None:
                        record[ma] = round(float(row[ma]), 2)

                records.append(record)

            t4 = time.perf_counter()
            print(f"[PERF] Polars处理: {(t3-t2)*1000:.1f}ms, 构建记录: {(t4-t3)*1000:.1f}ms")
            print(f"[PERF] 总耗时: {(t4-t1)*1000:.1f}ms, {len(records)}条记录")

            return records
        except Exception as e:
            print(f"K线获取失败 {symbol_code}: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def _convert_to_ts_code(self, symbol_code: str) -> str:
        """
        将股票代码转换为 ts_code 格式
        
        Args:
            symbol_code: 股票代码，如 '601398.SH' 或 '601398'
        
        Returns:
            ts_code 格式的股票代码，如 '601398.SH'
        """
        if not symbol_code:
            return ''
        
        code = symbol_code.strip().upper()
        
        # 如果已经包含后缀，直接返回
        if '.' in code:
            return code
        
        # 根据代码规则判断市场
        match = re.search(r'(\d{6})', code)
        if not match:
            return code
        
        pure_code = match.group(1)
        
        # 上海市场：6开头
        if pure_code.startswith('6'):
            return f"{pure_code}.SH"
        # 深圳市场：0、3开头
        elif pure_code.startswith('0') or pure_code.startswith('3'):
            return f"{pure_code}.SZ"
        # 北交所：8、4开头
        elif pure_code.startswith('8') or pure_code.startswith('4'):
            return f"{pure_code}.BJ"
        else:
            return f"{pure_code}.SZ"  # 默认深圳

    def get_latest_prices(self, symbol_codes: List[str], target_date: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """获取最新价格（强制全局前复权）"""
        if not self._initialized:
            self._ensure_initialized()

        if not symbol_codes:
            return {}

        normalized_codes = [self._normalize_symbol_code(c) for c in symbol_codes]
        latest_map = {}
        
        try:
            if self.data_query is None:
                return {}

            unique_codes = list(dict.fromkeys(normalized_codes))
            base_factors = self._get_global_latest_factors(unique_codes)
            for code in unique_codes:
                # 1. 获取全局最新因子（基准）
                base_factor = base_factors.get(code, 1.0)

                # 2. 查询目标日期的价格
                # CHANGED: Adapter Support
                if hasattr(self.data_query, 'get_latest_daily'):
                    row = self.data_query.get_latest_daily(code, target_date)
                    if not row:
                        continue
                    trade_date = row.get('trade_date')
                    raw_open = row.get('open')
                    raw_close = row.get('close')
                    raw_prev = row.get('prev_close')
                    current_factor = float(row.get('adj_factor', 1.0) or 1.0)
                else:
                    # Legacy SQL
                    if target_date:
                        query = """
                            SELECT trade_date, open, close, prev_close, adj_factor 
                            FROM stock_daily 
                            WHERE stock_code = ? AND trade_date <= ?
                            ORDER BY trade_date DESC 
                            LIMIT 1
                        """
                        params = [code, target_date]
                    else:
                        query = """
                            SELECT trade_date, open, close, prev_close, adj_factor 
                            FROM stock_daily 
                            WHERE stock_code = ?
                            ORDER BY trade_date DESC 
                            LIMIT 1
                        """
                        params = [code]
                    
                    df = self.data_query._query_df(query, params)
                    if df.empty:
                        continue
                    
                    row = df.iloc[0]
                    trade_date = row['trade_date']
                    raw_open = row.get('open')
                    raw_close = row.get('close')
                    raw_prev = row.get('prev_close')
                    current_factor = float(row.get('adj_factor', 1.0) or 1.0)
                
                # 3. 前复权计算，优先使用开盘价；没有开盘价时回退收盘价
                raw_price = raw_open if raw_open is not None and not pd.isna(raw_open) else raw_close
                if raw_price is None or pd.isna(raw_price):
                    continue
                ratio = current_factor / base_factor if base_factor else 1.0
                qfq_price = raw_price * ratio
                qfq_prev = (raw_prev * ratio) if raw_prev is not None and not pd.isna(raw_prev) else qfq_price

                latest_map[code] = {
                    "date": trade_date,
                    "price": float(f"{qfq_price:.2f}"),
                    "prev_close": float(f"{qfq_prev:.2f}"),
                    "name": self.stock_info_map.get(code, "") 
                }
        except Exception as e:
            print(f"获取最新价格失败: {e}")
        return latest_map

    def run_backtest_and_get_data(self, strategy_name: str, start_date: str, end_date: str, params: Optional[Dict[str, Any]] = None) -> Dict:
        """
        运行回测并获取数据（真实数据，作为备用）
        CHANGED: 使用流式回测并收集结果
        """
        if not self._initialized:
            self._ensure_initialized()

        # CHANGED: Switch to Unified Data Layer
        # NOTE: UnifiedDataQueryAdapter 缺少 get_stock_pool 方法，暂时禁用
        use_unified = os.getenv("USE_UNIFIED_DATA", "0") == "1"
        
        if use_unified:
            try:
                from data_svc.unified_data_query import UnifiedDataQueryAdapter
                local_data_query = UnifiedDataQueryAdapter(self.db_path)
                # print("DEBUG: Using UnifiedDataQueryAdapter for local run")
            except Exception as e:
                print(f"Failed to load UnifiedDataManager, falling back: {e}")
                local_data_query = OptimizedStockDataQuery(self.db_path)
        else:
            local_data_query = OptimizedStockDataQuery(self.db_path)
            
        local_engine = UnifiedBacktestEngine(local_data_query)
        
        try:
            creation_kwargs: Dict[str, Any] = params or {}
            strategy = StrategyFactory.create_strategy(strategy_name, use_simple=True, **creation_kwargs)
            
            # CHANGED: 使用流式回测并收集所有结果
            results_list = []
            trades_log = []
            final_metrics = None
            
            # 运行流式回测并收集数据（使用本地引擎，线程隔离）
            for update in local_engine.run_backtest_streaming(start_date, end_date, strategy):
                update_type = update.get('type')
                data = update.get('data', {})
            
                if update_type == 'daily_equity_engine':
                    results_list.append({
                        'date': data.get('date'),
                        'total_value': data.get('strategyReturn', 0)
                    })
                elif update_type == 'new_trade' or update_type == 'new_trade_engine':
                    # CHANGED: 监听正确的事件类型，同时兼容两种事件名
                    # CHANGED: 收集完整的交易记录，包括所有字段
                    trade = {
                        'date': data.get('date'),
                        'symbol': data.get('symbol') or data.get('symbol_code', ''),
                        'action': data.get('action'),
                        'price': data.get('price', 0),
                        'quantity': data.get('quantity', 0),
                        'commission': data.get('commission', 0),
                        'entry_date': data.get('entry_date'),
                        'exit_date': data.get('exit_date'),
                        'position_id': data.get('position_id'),
                        'profit_loss': data.get('profit_loss', 0),
                        'roi': data.get('roi'),
                        'holding_days': data.get('holding_days')
                    }
                    trades_log.append(trade)
                elif update_type == 'final_metrics':
                    final_metrics = data
                elif update_type == 'error':
                    error_msg = data.get('message', '回测发生错误')
                    return {"error": error_msg}
            
            # 转换为 DataFrame
            if not results_list:
                error_msg = (
                    f"回测没有产生任何结果。\n\n"
                    f"请检查数据库 '{self.db_path}' 中 "
                    f"是否存在 {start_date} 到 {end_date} 期间的股票数据。"
                )
                return {"error": error_msg}
            
            results_df = pd.DataFrame(results_list)
            results_df['date'] = pd.to_datetime(results_df['date'])
            results_df = results_df.sort_values('date')
            
            # 使用收集到的指标或计算指标
            if final_metrics:
                metrics = final_metrics
            else:
                metrics = self._calculate_metrics_from_df(results_df, trades_log)
            
            # 调用转换函数
            return self._convert_backtest_results(
                results_df, trades_log, strategy_name, start_date, end_date
            )
        except Exception as e:
            print(f"运行回测错误: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e)}
        finally:
            try:
                local_data_query.close()
            except Exception:
                pass
    
    def _convert_backtest_results(self, results_df: pd.DataFrame, trades_log: List[Dict], 
                                 strategy_name: str, start_date: str, end_date: str) -> Dict:
        """
        (此函数保持不变, 作为备用)
        转换回测结果
        """
        from datetime import datetime
        
        metrics = self._calculate_metrics_from_df(results_df, trades_log)
        equity_curve = self._extract_equity_curve_from_df(results_df)
        risk_data = self._calculate_risk_from_df(results_df)
        
        # (格式化交易记录)
        paged_trades = trades_log[:10]
        formatted_trades = []
        positions = {}
        cumulative_pnl = 0
        
        for trade in reversed(trades_log): 
            symbol = trade['symbol']
            qty = trade['quantity']
            price = trade['price']
            action = trade['action']
            pnl = 0
            
            if action == 'buy':
                if symbol not in positions:
                    positions[symbol] = {'qty': 0, 'cost': 0}
                positions[symbol]['cost'] = (positions[symbol]['cost'] * positions[symbol]['qty'] + qty * price) / (positions[symbol]['qty'] + qty)
                positions[symbol]['qty'] += qty
            elif action == 'sell':
                if symbol in positions and positions[symbol]['qty'] > 0:
                    avg_cost = positions[symbol].get('cost', price)
                    pnl = (price - avg_cost) * qty
                    cumulative_pnl += pnl
                    positions[symbol]['qty'] -= qty
            
            trade['profitLoss'] = pnl
            trade['cumulativePnL'] = cumulative_pnl

        trades_log.reverse()
        # CHANGED: 返回所有交易记录，而不是只返回前10条
        for trade in trades_log:
             raw_symbol_code = self._normalize_symbol_code(trade.get('symbol') or trade.get('symbol_code', ''))
             entry_date = trade.get('entry_date')
             exit_date = trade.get('exit_date')
             
             # CHANGED: 计算持有周期（天数）
             # 优先使用引擎计算好的 holding_days
             holding_days = trade.get('holding_days')
             if holding_days is None:
                 if entry_date and exit_date:
                     try:
                         from datetime import datetime
                         entry_dt = datetime.strptime(entry_date, '%Y-%m-%d')
                         exit_dt = datetime.strptime(exit_date, '%Y-%m-%d')
                         holding_days = (exit_dt - entry_dt).days
                     except:
                         pass
                 elif entry_date and trade['action'] == 'sell':
                     try:
                         from datetime import datetime
                         entry_dt = datetime.strptime(entry_date, '%Y-%m-%d')
                         trade_dt = datetime.strptime(trade['date'], '%Y-%m-%d')
                         holding_days = (trade_dt - entry_dt).days
                     except:
                         pass

             
             # CHANGED: 手续费为万分之五，不足五元按五元算
             commission_base = trade.get('commission', trade['price'] * trade['quantity'] * 0.0005)
             commission = max(commission_base, 5.0) if commission_base > 0 else 5.0
             
             # CHANGED: 提取 FIFO 匹配的结果（如果存在）
             # 优先使用回测引擎计算好的精确值，防止前端再次计算出错
             entry_price = trade.get('entry_price')
             exit_price = trade.get('exit_price')
             profit_loss = trade.get('profit_loss')
             roi = trade.get('roi')
             holding_days_from_engine = trade.get('holding_days')
             
             # 如果引擎提供了持有周期，优先使用
             if holding_days_from_engine is not None:
                 holding_days = holding_days_from_engine
             
             # CHANGED: 确保返回给前端的代码始终补齐 6 位，以便 UI 匹配名称和图片
             # 内部数据库匹配依然使用 raw_symbol_code (已 strip 零)
             ui_symbol_code = raw_symbol_code.zfill(6) if raw_symbol_code.isdigit() else raw_symbol_code

             formatted_trades.append({
                "id": f"trade-{trade['date']}-{raw_symbol_code}-{trade['action']}",
                "date": trade['date'],
                "symbol": self.stock_info_map.get(ui_symbol_code, self.stock_info_map.get(raw_symbol_code, raw_symbol_code)), # 这里的 key 改为补齐后的值
                "symbolCode": ui_symbol_code,  # UI 使用补齐后的代码
                "symbol_code": ui_symbol_code, # 保持一致
                "action": trade['action'],
                "price": trade['price'],  # 前复权价格（来自回测引擎）
                "quantity": trade['quantity'],
                "value": trade.get('price', 0) * trade.get('quantity', 0),
                "commission": commission,
                "profitLoss": profit_loss if profit_loss is not None else trade.get('profitLoss', 0),
                "cumulativePnL": trade.get('cumulativePnL', 0),
                "entryDate": entry_date,
                "exitDate": exit_date,
                "entry_price": entry_price,  # CHANGED: FIFO 匹配的开仓价
                "exit_price": exit_price,  # CHANGED: FIFO 匹配的平仓价
                "profit_loss": profit_loss,  # CHANGED: FIFO 计算的盈亏
                "roi": roi,  # CHANGED: FIFO 计算的收益率
                "positionId": trade.get('position_id'),
                "holdingDays": holding_days  # CHANGED: 持有周期（天数）
            })

        trade_records_data = {
            "trades": formatted_trades,
            "total": len(trades_log),
        }

        # 实际回测使用的日期范围（可能已被引擎自动截断到数据库可用范围）
        actual_start = pd.to_datetime(results_df['date']).min().strftime('%Y-%m-%d') if not results_df.empty else start_date
        actual_end = pd.to_datetime(results_df['date']).max().strftime('%Y-%m-%d') if not results_df.empty else end_date

        return {
            "versionId": strategy_name,  # 添加 versionId 字段
            "strategyInfo": {
                "name": strategy_name,
                "description": f"{strategy_name}回测结果",
                "period": f"{actual_start} 至 {actual_end}",
                "lastUpdated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "metrics": metrics,  # CHANGED: 使用 metrics 而不是 performanceMetrics
            "performanceMetrics": metrics,  # 保留兼容性
            # CHANGED: equityCurve 应该是包含 date 和 equity 的对象数组
            "equityCurve": [
                {
                    "date": date,
                    "equity": equity,
                    "benchmarkEquity": benchmark_equity if "benchmarkReturns" in equity_curve else None
                }
                for date, equity, benchmark_equity in zip(
                    equity_curve.get("dates", []),
                    equity_curve.get("strategyReturns", []),
                    equity_curve.get("benchmarkReturns", [])
                )
            ],
            "equityCurveData": equity_curve,  # 保留完整数据
            "actualStartDate": actual_start,
            "actualEndDate": actual_end,
            "monthlyReturns": risk_data.get("monthlyReturns", []),  # CHANGED: 添加 monthlyReturns
            "riskAnalysisData": risk_data,
            "trades": formatted_trades,  # CHANGED: 直接返回 trades 数组，而不是 tradeRecords
            "tradeRecords": trade_records_data  # 保留兼容性
        }

    def _calculate_metrics_from_df(self, results_df: pd.DataFrame, trades_log: List[Dict]) -> Dict:
        """ 用于流式回测的最终指标计算 """
        initial_capital = self.initial_capital
        final_value = results_df['total_value'].iloc[-1]
        total_return = (final_value / initial_capital - 1) * 100

        days = len(results_df)
        years = days / 252.0
        annualized_return = ((final_value / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else total_return

        results_df['cummax'] = results_df['total_value'].cummax()
        results_df['drawdown'] = (results_df['total_value'] - results_df['cummax']) / results_df['cummax']
        max_drawdown = results_df['drawdown'].min() * 100

        # 日收益率
        daily_returns = results_df['total_value'].pct_change().dropna()
        if len(daily_returns) > 1:
            dr_mean = daily_returns.mean()
            dr_std = daily_returns.std()
            sharpe_ratio = (dr_mean / dr_std) * np.sqrt(252) if dr_std > 0 else 0

            downside_returns = daily_returns[daily_returns < 0]
            if len(downside_returns) > 0:
                downside_std = downside_returns.std()
                sortino_ratio = (dr_mean / downside_std) * np.sqrt(252) if downside_std > 0 else 0
            else:
                sortino_ratio = 0

            volatility = dr_std * np.sqrt(252) * 100  # 年化波动率（%）
        else:
            sharpe_ratio = 0
            sortino_ratio = 0
            volatility = 0

        # 交易统计
        trades_count = len(trades_log)
        win_trades = 0
        total_pnl = 0
        total_profit = 0
        total_loss = 0

        max_win_streak = 0
        max_loss_streak = 0
        current_win_streak = 0
        current_loss_streak = 0

        sum_trade_return_pct = 0.0  # 用来算“平均每笔收益率（%）”

        # 使用回测引擎已经计算好的profit_loss字段，而不是重新计算
        for trade in trades_log:
            action = trade['action']
            
            if action == 'sell':
                # 优先使用回测引擎已经计算好的profit_loss字段
                pnl = trade.get('profit_loss', 0)
                
                # 如果没有profit_loss字段，再使用回测引擎计算的roi和value来计算
                if pnl == 0 and 'roi' in trade and 'value' in trade:
                    pnl = (trade['roi'] / 100) * trade['value']

                # 统计盈亏次数 & 盈利/亏损总额
                if pnl > 0:
                    win_trades += 1
                    total_profit += pnl
                    current_win_streak += 1
                    max_win_streak = max(max_win_streak, current_win_streak)
                    current_loss_streak = 0
                elif pnl < 0:
                    total_loss += abs(pnl)
                    current_loss_streak += 1
                    max_loss_streak = max(max_loss_streak, current_loss_streak)
                    current_win_streak = 0
                else:
                    current_win_streak = 0
                    current_loss_streak = 0

                total_pnl += pnl

                # 单笔收益率（相对于成本）
                # 使用回测引擎已经计算好的trade_return_pct或roi字段
                if 'roi' in trade:
                    sum_trade_return_pct += trade['roi']
                elif 'trade_return_pct' in trade:
                    sum_trade_return_pct += trade['trade_return_pct']
                else:
                    # 如果没有这些字段，再使用pnl和cost计算
                    avg_cost = trade.get('entry_price', trade.get('price', 0))
                    exposure = avg_cost * trade.get('quantity', 0)
                    if exposure > 0:
                        trade_return_pct = (pnl / exposure) * 100
                        sum_trade_return_pct += trade_return_pct

        sell_trades_count = len([t for t in trades_log if t['action'] == 'sell'])
        win_rate = (win_trades / sell_trades_count) * 100 if sell_trades_count > 0 else 0
        profit_factor = total_profit / total_loss if total_loss > 0 else 0
        avg_trade_return = (sum_trade_return_pct / sell_trades_count) if sell_trades_count > 0 else 0

        def _to_scalar(x: Any) -> float:
            try:
                if isinstance(x, (int, float)):
                    return float(x)
                arr = np.asarray(x)
                if arr.shape == ():
                    return float(arr)
                return float(arr.mean())
            except Exception:
                return 0.0

        total_return_v = _to_scalar(total_return)
        annualized_return_v = _to_scalar(annualized_return)
        max_drawdown_v = _to_scalar(max_drawdown)
        sharpe_ratio_v = _to_scalar(sharpe_ratio)
        sortino_ratio_v = _to_scalar(sortino_ratio)
        volatility_v = _to_scalar(volatility)
        win_rate_v = _to_scalar(win_rate)
        profit_factor_v = _to_scalar(profit_factor)
        avg_trade_return_v = _to_scalar(avg_trade_return)

        # Calmar 比率 = 年化收益率 / |最大回撤|
        calmar_ratio_v = 0.0
        if abs(max_drawdown_v) > 1e-8:
            calmar_ratio_v = annualized_return_v / abs(max_drawdown_v)

        return {
            "totalReturn": round(total_return_v, 2),
            "annualizedReturn": round(annualized_return_v, 2),
            "maxDrawdown": round(max_drawdown_v, 2),
            "sharpeRatio": round(sharpe_ratio_v, 2),
            "sortinoRatio": round(sortino_ratio_v, 2),
            "volatility": round(volatility_v, 2),
            "winRate": round(win_rate_v, 1),
            "profitFactor": round(profit_factor_v, 2),
            "tradesCount": trades_count,
            "avgTradeReturn": round(avg_trade_return_v, 2),       # 【注意】驼峰命名，单位 %
            "maxWinningStreak": int(max_win_streak),
            "maxLosingStreak": int(max_loss_streak),
            "calmarRatio": round(calmar_ratio_v, 3),
        }


    def _extract_equity_curve_from_df(self, results_df: pd.DataFrame) -> Dict:
        """
        (此函数保持不变, 作为备用)
        CHANGED: 确保数据库已初始化
        """
        if not self._initialized:
            self._ensure_initialized()
        
        initial_capital = self.backtest_engine.initial_capital
        # (我们在这里也修复一下基准，让它查询数据库)
        # 将日期统一格式化为字符串，避免 pandas.Timestamp 直接传入 SQL 参数
        date_series = pd.to_datetime(results_df['date'])
        dates = date_series.dt.strftime('%Y-%m-%d').tolist()
        start_date_str = dates[0]
        end_date_str = dates[-1]
        print(f"[DEBUG _extract_equity_curve] dates: {len(dates)}, {start_date_str} ~ {end_date_str}")
        benchmark_df = self._get_benchmark_data_from_db('000300', start_date_str, end_date_str)
        print(f"[DEBUG _extract_equity_curve] benchmark_df: {len(benchmark_df)} rows")
        benchmark_curve = []

        if not benchmark_df.empty:
             try:
                print(f"[DEBUG _extract_equity_curve] benchmark_df columns: {benchmark_df.columns.tolist()}")
                print(f"[DEBUG _extract_equity_curve] benchmark_df date type: {type(benchmark_df['date'].iloc[0])}, sample: {benchmark_df['date'].tolist()[:3]}")
                strategy_dates_df = pd.DataFrame({'date': dates})
                merged_df = pd.merge(strategy_dates_df, benchmark_df, on='date', how='left')
                print(f"[DEBUG _extract_equity_curve] merged_df: {len(merged_df)} rows, close na: {merged_df['close'].isna().sum()}")
                merged_df['close'] = merged_df['close'].ffill().bfill()
                first_valid_benchmark = merged_df['close'].dropna().iloc[0]
                print(f"[DEBUG _extract_equity_curve] first_valid_benchmark: {first_valid_benchmark}")
                if first_valid_benchmark > 0:
                    normalized_curve = (merged_df['close'] / first_valid_benchmark) * initial_capital
                    benchmark_curve = normalized_curve.fillna(initial_capital).tolist()
                    print(f"[DEBUG _extract_equity_curve] benchmark_curve: {len(benchmark_curve)}, first 3: {benchmark_curve[:3]}")
             except Exception as e:
                 print(f"[DEBUG _extract_equity_curve] error: {e}")
                 import traceback
                 traceback.print_exc()
                 pass # (忽略错误)
        
        if not benchmark_curve:
             benchmark_curve = np.linspace(initial_capital, initial_capital, len(results_df)).tolist() 

        return {
            "dates": dates,
            "strategyReturns": results_df['total_value'].tolist(),
            "benchmarkReturns": benchmark_curve
        }
    
    def _calculate_risk_from_df(self, results_df: pd.DataFrame) -> Dict:
        if results_df is None or results_df.empty:
            return {
                "drawdowns": [],
                "volatility": [],
                "returnDistribution": [],
                "monthlyReturns": []
            }
        # 1. 规范化基础数据
        df = results_df.copy()

        # 只保留我们关心的两列
        if 'total_value' not in df.columns:
            # 兜底：如果有 equity 或类似字段，可以在这里做一层映射
            raise ValueError("results_df 缺少 total_value 列，无法计算风险指标")

        df = df[['date', 'total_value']].copy()
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')

        # 2. 从 total_value 计算逐日回撤
        df['cummax'] = df['total_value'].cummax()
        # drawdown 是一个负数（回撤），比如 -0.15 = 回撤 15%
        df['drawdown'] = df['total_value'] / df['cummax'] - 1.0

        # 3. 最大回撤 Top 5（按回撤深度排序，取最深的前 5 个点）
        drawdown_data = []
        drawdowns = df[df['drawdown'] < 0][['date', 'drawdown']]

        if not drawdowns.empty:
            for _, row in drawdowns.sort_values('drawdown').head(5).iterrows():
                drawdown_data.append({
                    "startDate": row['date'].strftime('%Y-%m-%d'),
                    "endDate": row['date'].strftime('%Y-%m-%d'),
                    "value": round(row['drawdown'] * 100, 2)  # 转成百分比
                })

        # 4. 月度收益热力图
        heatmap_data = []
        try:
            df_equity = df[['date', 'total_value']].copy()
            df_equity = df_equity.set_index('date')
            df_equity['year'] = df_equity.index.year
            df_equity['month'] = df_equity.index.month

            monthly_returns = []
            for (year, month), group in df_equity.groupby(['year', 'month']):
                first_value = group['total_value'].iloc[0]
                last_value = group['total_value'].iloc[-1]
                if first_value <= 0:
                    continue
                monthly_returns.append({
                    'year': int(year),
                    'month': int(month),
                    'return': (last_value / first_value - 1)
                })

            monthly_returns_df = pd.DataFrame(monthly_returns)
            if not monthly_returns_df.empty:
                monthly_returns_df = monthly_returns_df.sort_values(['year', 'month'])
                for year, group in monthly_returns_df.groupby('year'):
                    year_data = {
                        'year': int(year),
                        'months': [None] * 12   # 1~12 月
                    }
                    for _, row in group.iterrows():
                        m = int(row['month']) - 1
                        year_data['months'][m] = round(row['return'] * 100, 2)  # 百分比
                    heatmap_data.append(year_data)

        except Exception as e:
            print(f"计算月度收益失败: {e}")

        return {
            "drawdowns": drawdown_data,
            "volatility": [],          # 你后面如果想补充波动率 / 分布可以用这两个
            "returnDistribution": [],
            "monthlyReturns": heatmap_data
        }
    def stream_backtest(
        self,
        strategy_name: str,
        start_date: str,
        end_date: str,
        benchmark_code: Optional[str] = None,
        stop_event: Optional[Event] = None,
        params: Optional[Dict[str, Any]] = None,
        backtest_config: Optional[Dict[str, Any]] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        CHANGED: 流式 API 层（支持懒加载初始化）
        
        1. 首次调用时初始化数据库连接
        2. 发送初始化状态事件给前端
        3. 调用流式引擎
        4. 包装引擎数据，添加基准和实时指标
        """
        import time
        import sys
        sys.stderr.write(f"[DEBUG] stream_backtest 被调用: {strategy_name}, {start_date}~{end_date}\n")
        sys.stderr.flush()
        # 性能监控：记录各模块耗时
        perf_timings = {
            'total': 0,
            'initialization': 0,
            'get_trading_dates': 0,
            'get_benchmark_data': 0,
            'benchmark_processing': 0,
            'strategy_creation': 0,
            'backtest_execution': 0,
            'data_processing': 0,
            'risk_calculation': 0,
        }
        total_start = time.perf_counter()
        
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.debug(f"stream_backtest 方法开始: {strategy_name}, {start_date}~{end_date}")
        
        # ==============================================================================
        # 【并发安全修复】为每个请求创建独立的引擎实例，避免并发请求导致状态污染
        # 原问题：所有请求共享同一个 self.backtest_engine，导致并发时数据交叉污染
        # 解决方案：在 stream_backtest 内部创建局部实例，确保线程安全
        # ==============================================================================
        local_data_query = None
        local_engine = None
        update_count = 0 # Initialize here to prevent UnboundLocalError in finally block
        
        # CHANGED: 确保数据库已初始化（懒加载）
        init_start = time.perf_counter()
        if not self._initialized:
            # 发送初始化开始事件
            yield {
                "type": "initializing",
                "data": {
                    "message": "正在初始化数据库连接...",
                    "progress": 0
                }
            }
            
            try:
                self._ensure_initialized()
                perf_timings['initialization'] = (time.perf_counter() - init_start) * 1000  # ms
                # 发送初始化完成事件
                yield {
                    "type": "initialized",
                    "data": {
                        "message": "数据库连接初始化完成",
                        "progress": 100
                    }
                }
            except Exception as e:
                # 发送初始化失败事件
                yield {
                    "type": "error",
                    "data": {
                        "message": f"数据库连接初始化失败: {e}"
                    }
                }
                return
        else:
            perf_timings['initialization'] = (time.perf_counter() - init_start) * 1000  # ms
        
        # 【并发安全修复】创建独立的查询对象和引擎对象
        try:
            local_data_query = OptimizedStockDataQuery(self.db_path)
            
            # 应用回测配置（初始资金、佣金、滑点）
            engine_kwargs = {}
            if backtest_config:
                if backtest_config.get('initial_capital') is not None:
                    engine_kwargs['initial_capital'] = backtest_config['initial_capital']
                if backtest_config.get('commission') is not None:
                    engine_kwargs['commission_rate'] = backtest_config['commission']
            
            local_engine = UnifiedBacktestEngine(local_data_query, **engine_kwargs)
            initial_capital = local_engine.initial_capital
            logger.debug(f"创建独立的引擎实例完成，初始资金: {initial_capital}")
        except Exception as e:
            logger.error(f"创建独立引擎实例失败: {e}", exc_info=True)
            yield {
                "type": "error",
                "data": {"message": f"创建回测引擎失败: {e}"}
            }
            return
        
        # 1. (准备基准数据 - 这仍然是一次性加载)
        t_dates_start = time.perf_counter()
        dates = local_data_query.get_trading_dates(start_date, end_date)
        perf_timings['get_trading_dates'] = (time.perf_counter() - t_dates_start) * 1000  # ms
        logger.debug(f"获取交易日期完成: {len(dates)} 个日期, 耗时 {perf_timings['get_trading_dates']:.2f}ms")
        
        benchmark_curve = []
        if benchmark_code:
            t_benchmark_start = time.perf_counter()
            benchmark_df = self._get_benchmark_data_from_db(benchmark_code, start_date, end_date)
            perf_timings['get_benchmark_data'] = (time.perf_counter() - t_benchmark_start) * 1000  # ms
            logger.debug(f"获取基准数据完成: {len(benchmark_df) if benchmark_df is not None and not benchmark_df.empty else 0} 行, 耗时 {perf_timings['get_benchmark_data']:.2f}ms")
        else:
            benchmark_df = pd.DataFrame() 

        t_benchmark_proc_start = time.perf_counter()
        if benchmark_code and not benchmark_df.empty:
            try:
                strategy_dates_df = pd.DataFrame({'date': dates})
                merged_df = pd.merge(strategy_dates_df, benchmark_df, on='date', how='left')
                merged_df['close'] = merged_df['close'].ffill().bfill()
                first_valid_benchmark = merged_df['close'].dropna().iloc[0]
                if first_valid_benchmark > 0:
                    normalized_curve = (merged_df['close'] / first_valid_benchmark) * initial_capital
                    benchmark_curve = normalized_curve.fillna(initial_capital).tolist()
                else:
                    benchmark_curve = [initial_capital] * len(dates)
            except Exception as e:
                print(f"标准化基准时出错: {e}")
                benchmark_curve = [initial_capital] * len(dates)
        
        if not benchmark_curve:
            benchmark_curve = [initial_capital] * len(dates)
        perf_timings['benchmark_processing'] = (time.perf_counter() - t_benchmark_proc_start) * 1000  # ms
            
        # (将基准数据转换为 日期 -> 值 的映射，以便快速查找)
        benchmark_map = dict(zip(dates, benchmark_curve))

        # (用于计算实时回撤)
        cummax_equity = initial_capital
        
        # 收集权益曲线，用于最后计算风险指标
        equity_records = []
        benchmark_records = []  # 【修复】添加基准记录收集
        
        try:
            # 调用流式引擎（使用局部引擎实例）
            t_strategy_start = time.perf_counter()
            creation_kwargs: Dict[str, Any] = params or {}
            strategy = StrategyFactory.create_strategy(strategy_name, use_simple=True, **creation_kwargs)
            perf_timings['strategy_creation'] = (time.perf_counter() - t_strategy_start) * 1000  # ms
            logger.debug(f"策略创建完成，耗时 {perf_timings['strategy_creation']:.2f}ms")
            
            t_backtest_start = time.perf_counter()
            # 【并发安全修复】使用局部引擎实例，而不是共享的 self.backtest_engine
            engine_generator = local_engine.run_backtest_streaming(
                start_date, end_date, strategy, stop_event=stop_event
            )
            logger.debug("engine_generator 创建完成，开始迭代")

            # 3. 循环遍历引擎的【产出】
            data_processing_time = 0
            for update in engine_generator:
                update_count += 1
                t_data_start = time.perf_counter()
                if stop_event and stop_event.is_set():
                    yield {
                        "type": "cancelled",
                        "data": {"message": "回测已取消"}
                    }
                    break
                
                # A. 如果是交易或错误，直接转发
                if update['type'] == 'new_trade_engine':
                    
                    trade_data = update['data']
                    symbol_code = self._normalize_symbol_code(trade_data.get('symbol') or trade_data.get('symbol_code') or trade_data.get('code'))
                    if not symbol_code:
                        continue
                    position_id = trade_data.get('position_id') or f"{symbol_code}_{trade_data['date']}"
                    symbol_name = self.stock_info_map.get(symbol_code, symbol_code)

                    # 简化：直接转发引擎数据，引擎已提供所有必要信息
                    yield {
                        "type": "new_trade",
                        "data": {
                            "id": f"trade-{trade_data['date']}-{symbol_code}",
                            "date": trade_data['date'],
                            "symbol": symbol_name,
                            "symbolCode": symbol_code,
                            "symbol_code": symbol_code,
                            "action": trade_data['action'],
                            "price": trade_data['price'],
                            "quantity": trade_data.get('quantity') or trade_data.get('shares'),
                            "profitLoss": trade_data.get('profit_loss', 0),
                            "cumulativePnL": trade_data.get('cumulative_pnl', 0),
                            "positionId": position_id,
                            "entryDate": trade_data.get('entry_date'),
                            "exitDate": trade_data.get('exit_date'),
                            # CHANGED: 添加 FIFO 匹配的结果
                            "entry_price": trade_data.get('entry_price'),
                            "exit_price": trade_data.get('exit_price'),
                            "profit_loss": trade_data.get('profit_loss'),
                            "roi": trade_data.get('roi'),
                            "holding_days": trade_data.get('holding_days')
                        }
                    }
                    data_processing_time += (time.perf_counter() - t_data_start) * 1000  # ms
                # 修改后: 添加 initializing 和 initialized
                elif update['type'] in [
                    'initializing',   # 新增: 通知前端开始加载数据
                    'initialized',    # 新增: 通知前端数据加载完成
                    'backtest_start', 
                    'daily_update', 
                    'order_update', 
                    'backtest_end',
                    'error',           # 建议: 确保错误事件也能透传
                    'final_metrics',
                    'stream_complete',
                    'progress'
                ]:
                    if update['type'] == 'stream_complete':
                        # 回测结束，计算最终风险指标
                        t_risk_start = time.perf_counter()
                        risk_data = {}
                        if equity_records:
                            try:
                                results_df = pd.DataFrame(equity_records)
                                risk_data = self._calculate_risk_from_df(results_df)
                                yield {
                                    "type": "risk_data",
                                    "data": risk_data
                                }
                            except Exception as e:
                                print(f"计算风险数据失败: {e}")
                        perf_timings['risk_calculation'] = (time.perf_counter() - t_risk_start) * 1000  # ms
                        
                        # 【修复】将 monthlyReturns 添加到 stream_complete 数据中
                        if risk_data.get('monthlyReturns'):
                            update['data']['monthlyReturns'] = risk_data['monthlyReturns']
                        
                        # 计算回测执行总耗时
                        perf_timings['backtest_execution'] = (time.perf_counter() - t_backtest_start) * 1000  # ms
                        perf_timings['data_processing'] = data_processing_time
                        perf_timings['total'] = (time.perf_counter() - total_start) * 1000  # ms
                        
                        # 输出性能报告（使用 sys.stderr 确保输出到控制台）
                        import sys
                        sys.stderr.write("\n" + "="*60 + "\n")
                        sys.stderr.write("[PERF] 回测性能分析报告\n")
                        sys.stderr.write("="*60 + "\n")
                        sys.stderr.write(f"[PERF] 总耗时: {perf_timings['total']:.2f} ms ({perf_timings['total']/1000:.2f} s)\n")
                        sys.stderr.write(f"[PERF]   - 初始化: {perf_timings['initialization']:.2f} ms ({perf_timings['initialization']/perf_timings['total']*100:.1f}%)\n")
                        sys.stderr.write(f"[PERF]   - 获取交易日期: {perf_timings['get_trading_dates']:.2f} ms ({perf_timings['get_trading_dates']/perf_timings['total']*100:.1f}%)\n")
                        sys.stderr.write(f"[PERF]   - 获取基准数据: {perf_timings['get_benchmark_data']:.2f} ms ({perf_timings['get_benchmark_data']/perf_timings['total']*100:.1f}%)\n")
                        sys.stderr.write(f"[PERF]   - 基准数据处理: {perf_timings['benchmark_processing']:.2f} ms ({perf_timings['benchmark_processing']/perf_timings['total']*100:.1f}%)\n")
                        sys.stderr.write(f"[PERF]   - 策略创建: {perf_timings['strategy_creation']:.2f} ms ({perf_timings['strategy_creation']/perf_timings['total']*100:.1f}%)\n")
                        sys.stderr.write(f"[PERF]   - 回测执行: {perf_timings['backtest_execution']:.2f} ms ({perf_timings['backtest_execution']/perf_timings['total']*100:.1f}%)\n")
                        # 如果有更详细的回测执行数据，在这里输出
                        if 'backtest_details' in perf_timings:
                            details = perf_timings['backtest_details']
                            sys.stderr.write(f"[PERF]      - 数据加载: {details.get('data_loading', 0):.2f} ms\n")
                            sys.stderr.write(f"[PERF]      - 指标计算: {details.get('indicator_calculation', 0):.2f} ms\n")
                            sys.stderr.write(f"[PERF]      - 信号生成: {details.get('signal_generation', 0):.2f} ms\n")
                            sys.stderr.write(f"[PERF]      - 交易执行: {details.get('trade_execution', 0):.2f} ms\n")
                            sys.stderr.write(f"[PERF]      - 结果产出: {details.get('result_output', 0):.2f} ms\n")
                        sys.stderr.write(f"[PERF]   - 数据处理: {perf_timings['data_processing']:.2f} ms ({perf_timings['data_processing']/perf_timings['total']*100:.1f}%)\n")
                        sys.stderr.write(f"[PERF]   - 风险计算: {perf_timings['risk_calculation']:.2f} ms ({perf_timings['risk_calculation']/perf_timings['total']*100:.1f}%)\n")
                        sys.stderr.write(f"[PERF] 处理更新数: {update_count}\n")
                        if update_count > 0:
                            sys.stderr.write(f"[PERF] 平均每个更新耗时: {perf_timings['data_processing']/update_count:.2f} ms\n")
                        sys.stderr.write("="*60 + "\n\n")
                        sys.stderr.flush()
                        
                        # 【修复】覆盖引擎返回的 benchmarkCurve，确保日期与策略一致
                        # 引擎返回的 benchmarkCurve 可能包含更多日期，导致与 equityCurve 日期不匹配
                        sys.stderr.write(f"[DEBUG stream_complete] benchmark_records: {len(benchmark_records)}\n")
                        sys.stderr.write(f"[DEBUG stream_complete] update['data'] keys: {list(update['data'].keys())}\n")
                        if 'benchmarkCurve' in update['data']:
                            sys.stderr.write(f"[DEBUG stream_complete] 原始 benchmarkCurve: {len(update['data']['benchmarkCurve'])}\n")
                        if benchmark_records:
                            update['data']['benchmarkCurve'] = [
                                {"date": r['date'], "equity": round(r['total_value'], 2)}
                                for r in benchmark_records
                            ]
                            sys.stderr.write(f"[DEBUG stream_complete] 覆盖后 benchmarkCurve: {len(update['data']['benchmarkCurve'])}\n")
                        sys.stderr.flush()
                        
                        yield update
                        return
                    else:
                        yield update
                        data_processing_time += (time.perf_counter() - t_data_start) * 1000  # ms
                elif update['type'] == 'daily_equity_engine':
                    engine_data = update['data']
                    current_date = engine_data['date']
                    # 【防御性修复】优先使用 strategyReturn，回退到 equity，最后默认为 0
                    equity = engine_data.get('strategyReturn', engine_data.get('equity', 0.0))
                    
                    # 记录回测进度数据
                    if update_count <= 3 or update_count % 50 == 0:
                        logger.debug(f"[STREAM] visualization_api 收到 daily_equity_engine: date={current_date}, equity={equity:.2f}")
                    
                    # (计算实时指标)
                    current_total_return = (equity / initial_capital - 1) * 100
                    if equity > cummax_equity:
                        cummax_equity = equity
                    current_drawdown = ((equity - cummax_equity) / cummax_equity) * 100
                    
                    # (添加基准)
                    benchmark_val = benchmark_map.get(current_date, initial_capital)
                    
                    # 收集权益数据用于后续计算风险指标
                    equity_records.append({
                        'date': current_date,
                        'total_value': equity
                    })
                    
                    # 【修复】收集基准数据用于构建 benchmarkCurve
                    benchmark_records.append({
                        'date': current_date,
                        'total_value': benchmark_val
                    })
                    
                    # (打包成前端需要的 'daily_update' 格式)
                    daily_equity_data = {
                        "type": "daily_equity", # (这是 app.py 认识的名字)
                        "data": {
                            "date": current_date,
                            "strategyReturn": equity,
                            "benchmarkReturn": benchmark_val,
                            "totalReturn": current_total_return, 
                            "currentDrawdown": current_drawdown  
                        }
                    }
                    
                    # 调试：记录即将 yield 的 daily_equity 事件
                    if update_count <= 3 or update_count % 50 == 0:
                        logger.debug(f"[STREAM] visualization_api 准备 yield daily_equity: date={current_date}")
                    
                    yield daily_equity_data
                    data_processing_time += (time.perf_counter() - t_data_start) * 1000  # ms

                if stop_event and stop_event.is_set():
                    yield {
                        "type": "cancelled",
                        "data": {"message": "回测已取消"}
                    }
                    break
            
            # 循环结束后，无论是否收到 stream_complete，都输出性能报告
            # 计算回测执行总耗时
            perf_timings['backtest_execution'] = (time.perf_counter() - t_backtest_start) * 1000  # ms
            perf_timings['data_processing'] = data_processing_time
            perf_timings['total'] = (time.perf_counter() - total_start) * 1000  # ms
            
            logger.debug(f"循环结束，update_count: {update_count}, data_processing_time: {data_processing_time:.2f}ms")
            
            # 输出性能报告（使用 sys.stderr 确保输出到控制台）
            if perf_timings['total'] > 0:  # 避免除零错误
                sys.stderr.write("\n" + "="*60 + "\n")
                sys.stderr.write("[PERF] 回测性能分析报告\n")
                sys.stderr.write("="*60 + "\n")
                sys.stderr.write(f"[PERF] 总耗时: {perf_timings['total']:.2f} ms ({perf_timings['total']/1000:.2f} s)\n")
                sys.stderr.write(f"[PERF]   - 初始化: {perf_timings['initialization']:.2f} ms ({perf_timings['initialization']/perf_timings['total']*100:.1f}%)\n")
                sys.stderr.write(f"[PERF]   - 获取交易日期: {perf_timings['get_trading_dates']:.2f} ms ({perf_timings['get_trading_dates']/perf_timings['total']*100:.1f}%)\n")
                sys.stderr.write(f"[PERF]   - 获取基准数据: {perf_timings['get_benchmark_data']:.2f} ms ({perf_timings['get_benchmark_data']/perf_timings['total']*100:.1f}%)\n")
                sys.stderr.write(f"[PERF]   - 基准数据处理: {perf_timings['benchmark_processing']:.2f} ms ({perf_timings['benchmark_processing']/perf_timings['total']*100:.1f}%)\n")
                sys.stderr.write(f"[PERF]   - 策略创建: {perf_timings['strategy_creation']:.2f} ms ({perf_timings['strategy_creation']/perf_timings['total']*100:.1f}%)\n")
                sys.stderr.write(f"[PERF]   - 回测执行: {perf_timings['backtest_execution']:.2f} ms ({perf_timings['backtest_execution']/perf_timings['total']*100:.1f}%)\n")
                sys.stderr.write(f"[PERF]   - 数据处理: {perf_timings['data_processing']:.2f} ms ({perf_timings['data_processing']/perf_timings['total']*100:.1f}%)\n")
                sys.stderr.write(f"[PERF]   - 风险计算: {perf_timings['risk_calculation']:.2f} ms ({perf_timings['risk_calculation']/perf_timings['total']*100:.1f}%)\n")
                sys.stderr.write(f"[PERF] 处理更新数: {update_count}\n")
                if update_count > 0:
                    sys.stderr.write(f"[PERF] 平均每个更新耗时: {perf_timings['data_processing']/update_count:.2f} ms\n")
                sys.stderr.write("="*60 + "\n\n")
                sys.stderr.flush()

        except Exception as e:
            # 即使发生异常，也尝试输出性能报告
            perf_timings['total'] = (time.perf_counter() - total_start) * 1000  # ms
            logger.error(f"回测异常，已执行时间: {perf_timings['total']:.2f}ms, 处理更新数: {update_count}", exc_info=True)
            yield {
                "type": "error",
                "data": {"message": str(e)}
            }
        finally:
            # 【并发安全修复】清理局部资源
            if local_data_query is not None:
                try:
                    local_data_query.close()
                    logger.debug("局部数据查询对象已关闭")
                except Exception as e:
                    logger.warning(f"关闭局部数据查询对象失败: {e}")

    def _get_stock_info_with_market_cap(self, needed_symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        """
        获取股票基础信息（名称、市值）
        复用 OptimizedStockDataQuery 和 stock_info_map，避免重复查询
        
        Args:
            needed_symbols: 可选，如果提供则只查询这些股票，否则查询所有股票
        
        返回: {symbol: {name: str, market_cap: float}}
        """
        if not self._initialized:
            self._ensure_initialized()
        
        from config.logger import get_logger
        logger = get_logger(__name__)
        stock_info = {}
        
        try:
            # 【核心修复】使用 data_query 方法，支持 Polars + Parquet 后端
            if self.data_query is None:
                return {}
            
            # CHANGED: 如果指定了需要的股票列表，只查询这些股票，大幅减少查询时间
            if needed_symbols:
                # 提取6位股票代码
                stock_codes = []
                for sym in needed_symbols:
                    # 处理 sz/sh 前缀
                    if sym.startswith(('sz', 'sh')):
                        code = sym[2:]  # 去掉 sz/sh 前缀
                    else:
                        code = sym
                    # 提取6位数字代码
                    if len(code) >= 6:
                        code_6 = code[-6:].zfill(6)  # 确保是6位
                        stock_codes.append(code_6)
                    elif len(code) == 6:
                        stock_codes.append(code.zfill(6))
                
                logger.debug(f"从 needed_symbols 提取的股票代码: {needed_symbols} -> {stock_codes}")
                
                if stock_codes:
                    # 使用 IN 查询，只查询需要的股票
                    placeholders = ','.join(['?' for _ in stock_codes])
                    query = f"""
                        SELECT 
                            stock_code,
                            MAX(total_mv) as market_cap
                        FROM stock_daily
                        WHERE total_mv IS NOT NULL
                            AND stock_code IN ({placeholders})
                        GROUP BY stock_code
                    """
                    df = self.data_query._query_df(query, stock_codes)
                    logger.debug(f"市值查询结果: {len(df)} 条记录")
                else:
                    df = pd.DataFrame()
                    logger.warning(f"无法从 needed_symbols 提取有效的股票代码: {needed_symbols}")
            else:
                # 获取每只股票的最新市值（取最新交易日的市值）
                query = """
                    SELECT 
                        stock_code,
                        MAX(total_mv) as market_cap
                    FROM stock_daily
                    WHERE total_mv IS NOT NULL
                    GROUP BY stock_code
                """
                df = self.data_query._query_df(query)
            
            name_map = self._load_missing_stock_names(
                [str(row.get('stock_code', '')) for _, row in df.iterrows()]
            )

            for _, row in df.iterrows():
                symbol_code = str(row['stock_code']).zfill(6)
                market_cap = float(row['market_cap'] or 0.0) / 10000.0  # 万元转亿元
                
                # 构建标准股票代码
                if symbol_code.startswith('0'):
                    full_symbol = f"sz{symbol_code}"
                elif symbol_code.startswith('6'):
                    full_symbol = f"sh{symbol_code}"
                else:
                    full_symbol = symbol_code
                
                # CHANGED: 从 stock_info_map 获取名称（已加载）
                stock_name = self.stock_info_map.get(symbol_code, '') or name_map.get(symbol_code, '')
                
                logger.debug(f"股票信息: {full_symbol} (代码: {symbol_code}) -> 名称: {stock_name or '(空)'}")
                
                stock_info[full_symbol] = {
                    'name': stock_name,
                    'market_cap': market_cap,
                    'original_code': symbol_code
                }
        except Exception as e:
            logger.warning(f"从数据库获取股票市值失败，尝试 Parquet: {e}")
            
            if not Config.parquet_fallback_enabled():
                logger.info("Parquet fallback disabled, skip market cap fallback for %s symbols", len(needed_symbols or []))
                return stock_info

            try:
                import polars as pl
                
                base_dir = Path(__file__).parent
                stock_daily_parquet = base_dir / 'parquet_data' / 'stock_daily.parquet'
                stock_info_parquet = base_dir / 'parquet_data' / 'stock_info.parquet'
                
                if stock_daily_parquet.exists():
                    lazy_df = pl.scan_parquet(str(stock_daily_parquet)).filter(
                        pl.col("total_mv").is_not_null()
                    )
                    
                    if needed_symbols:
                        stock_codes = []
                        for sym in needed_symbols:
                            code = sym[2:] if sym.startswith(('sz', 'sh')) else sym
                            if len(code) >= 6:
                                stock_codes.append(code[-6:])
                        if stock_codes:
                            lazy_df = lazy_df.filter(pl.col("stock_code").is_in(stock_codes))
                    
                    if stock_info_parquet.exists():
                        info_lazy = pl.scan_parquet(str(stock_info_parquet)).select(["stock_code", "stock_name"])
                        lazy_df = lazy_df.join(info_lazy, on="stock_code", how="left")
                        lazy_df = lazy_df.with_columns(
                            pl.col("stock_name").fill_null("")
                        )
                    else:
                        lazy_df = lazy_df.with_columns(pl.lit("").alias("stock_name"))
                    
                    df_stock = lazy_df.group_by("stock_code").agg([
                        pl.col("stock_name").first(),
                        pl.col("total_mv").max().alias("market_cap")
                    ]).collect()
                    
                    for row in df_stock.iter_rows(named=True):
                        symbol_code = str(row.get('stock_code')).zfill(6)
                        stock_name = row.get('stock_name') or ''
                        market_cap = float(row.get('market_cap') or 0.0) / 10000.0
                        
                        if symbol_code.startswith('0'):
                            full_symbol = f"sz{symbol_code}"
                        elif symbol_code.startswith('6'):
                            full_symbol = f"sh{symbol_code}"
                        else:
                            full_symbol = symbol_code
                        
                        if not stock_name:
                            stock_name = self.stock_info_map.get(symbol_code, '')
                            if not stock_name and self.data_query is not None:
                                try:
                                    name_df = self.data_query.get_stock_info(symbol_code)
                                    if not name_df.empty and 'name' in name_df.columns:
                                        stock_name = str(name_df.iloc[0]['name']) or ''
                                except Exception:
                                    pass
                        
                        stock_info[full_symbol] = {
                            'name': stock_name,
                            'market_cap': market_cap,
                            'original_code': symbol_code
                        }
            except Exception as e2:
                logger.warning(f"从 Parquet 获取股票信息也失败: {e2}")
        
        return stock_info

    @lru_cache(maxsize=16)
    def _load_guba_posts_cached(self, symbol_key: str, sample_size: int) -> pd.DataFrame:
        """
        【性能优化】缓存版本的股吧数据加载
        使用 LRU 缓存，缓存最近 16 次不同的请求，避免重复的 Parquet 解析开销
        
        Args:
            symbol_key: 股票代码（字符串，None 用 "__ALL__" 表示）
            sample_size: 抽样大小
        """
        # 将 symbol_key 转换回 Optional[str]
        symbol = None if symbol_key == "__ALL__" else symbol_key
        return self._load_guba_posts_from_parquet_impl(symbol, sample_size)
    
    def _load_guba_posts_from_parquet(self, symbol: Optional[str] = None, sample_size: int = 50) -> pd.DataFrame:
        """
        从 Parquet 文件加载股吧数据（带缓存）
        如果指定 symbol，返回该股票的评论数据（可抽样）
        否则返回所有股票的聚合数据
        
        【性能优化】使用 LRU 缓存，避免重复的 Parquet 解析开销
        """
        # 使用缓存版本（symbol 为 None 时转换为字符串，确保可哈希）
        cache_key_symbol = symbol if symbol is not None else "__ALL__"
        return self._load_guba_posts_cached(cache_key_symbol, sample_size)
    
    def _load_guba_posts_from_parquet_impl(self, symbol: Optional[str] = None, sample_size: int = 50) -> pd.DataFrame:
        """
        从 Parquet 文件加载股吧数据（实际实现）
        如果指定 symbol，返回该股票的评论数据（可抽样）
        否则返回所有股票的聚合数据
        
        【Polars 实现】直接读取 Parquet 文件
        """
        from config.logger import get_logger
        logger = get_logger(__name__)
        
        try:
            import polars as pl
        except ImportError:
            return pd.DataFrame()

        from config.config import Config
        parquet_path = Path(Config.PARQUET_DIR) / 'guba_posts.parquet'

        if not parquet_path.exists():
            logger.warning(f"Parquet 文件不存在: {parquet_path}")
            return pd.DataFrame()
        
        try:
            lazy_df = pl.scan_parquet(str(parquet_path))
            
            if symbol:
                if symbol.startswith('sz') or symbol.startswith('sh'):
                    code_6 = symbol[2:] if len(symbol) > 2 else symbol
                    full_symbol = symbol
                else:
                    code_6 = symbol[-6:] if len(symbol) >= 6 else symbol.zfill(6)
                    full_symbol = symbol
                
                lazy_df = lazy_df.filter(
                    (pl.col("symbol") == full_symbol) |
                    (pl.col("symbol") == code_6) |
                    (pl.col("symbol").str.ends_with(code_6)) |
                    (pl.col("stockbar_code") == code_6) |
                    (pl.col("symbol").str.contains(code_6))
                )
                
                lazy_df = lazy_df.filter(
                    pl.col("post_comment_count").is_not_null() &
                    (pl.col("post_comment_count") > 0) &
                    pl.col("bullish_bearish").is_not_null()
                )
                
                df = lazy_df.select([
                    pl.col("symbol"),
                    pl.col("stockbar_code").cast(pl.Utf8).fill_null(pl.col("symbol").str.slice(-6)).alias("stockCode"),
                    pl.col("stockbar_name").cast(pl.Utf8).fill_null("").alias("stockName"),
                    pl.col("post_comment_count").cast(pl.Int64).fill_null(0).alias("commentCount"),
                    pl.col("bullish_bearish").cast(pl.Float64).fill_null(0.0).alias("sentiment"),
                    pl.col("post_title").cast(pl.Utf8).fill_null("").alias("postTitle"),
                    pl.col("post_publish_time").cast(pl.Datetime).alias("postPublishTime")
                ]).collect()
                
                logger.info(f"查询返回 {len(df)} 条记录")
                
                if len(df) > sample_size:
                    df = df.sample(n=sample_size, seed=42)
                    logger.info(f"随机抽样后: {len(df)} 条记录")
            else:
                df = lazy_df.filter(
                    pl.col("symbol").is_not_null() &
                    (pl.col("post_comment_count").cast(pl.Int64) > 0)
                ).select([
                    pl.col("symbol"),
                    pl.col("stockbar_code").cast(pl.Utf8).fill_null(pl.col("symbol").str.slice(-6)).alias("stockCode"),
                    pl.col("stockbar_name").cast(pl.Utf8).fill_null("").alias("stockName"),
                    pl.col("post_comment_count").cast(pl.Int64).alias("commentCount"),
                    pl.col("bullish_bearish").cast(pl.Float64).alias("sentiment")
                ]).group_by(["symbol", "stockCode", "stockName"]).agg([
                    pl.col("commentCount").sum().alias("commentCount"),
                    pl.col("sentiment").mean().fill_null(0.0).alias("sentiment")
                ]).sort("commentCount", descending=True).limit(200).collect()
                
                logger.info(f"从 Parquet 查询到 {len(df)} 只股票（按评论数排序的前200只）")
            
            return df.to_pandas()
            
        except Exception as e:
            logger.warning(f"从 Parquet 读取股吧数据失败: {e}", exc_info=True)
            return pd.DataFrame()

    def _normalize_symbol_key(self, raw_symbol: str, stock_code: str) -> str:
        """规范化股票代码为标准格式（sz/sh + 6位数字）"""
        symbol_key = raw_symbol
        if not symbol_key and stock_code:
            code_6 = stock_code[-6:] if len(stock_code) >= 6 else stock_code.zfill(6)
            if code_6.startswith('0'):
                symbol_key = f"sz{code_6}"
            elif code_6.startswith('6'):
                symbol_key = f"sh{code_6}"
            else:
                symbol_key = code_6
        return symbol_key

    def get_scatter_data(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        获取情感-热度散点图数据
        
        Args:
            symbol: 可选，如果指定则返回该股票的评论数据（随机抽样50个），否则返回所有股票的聚合数据
        
        Returns:
            {
                'success': bool,
                'data': List[Dict]  # 散点图数据点列表
            }
        """
        from config.logger import get_logger
        logger = get_logger(__name__)
        
        try:
            # 确保已初始化
            if not self._initialized:
                self._ensure_initialized()
            
            import time
            start_time = time.perf_counter()
            
            # CHANGED: 优化查询顺序 - 先获取股吧数据（已优化，只返回前100只），再只查询这些股票的市值
            # 1. 从 Parquet 加载股吧数据（已优化：只返回评论数最多的前100只股票，带缓存）
            logger.info(f"开始加载股吧数据: symbol={symbol}")
            df = self._load_guba_posts_from_parquet(symbol)
            load_time = time.perf_counter() - start_time
            logger.info(f"股吧数据加载完成: {len(df)} 条记录, 耗时 {load_time:.2f} 秒 (可能来自缓存)")
            
            if df.empty:
                logger.warning(f"股吧数据为空，返回空数据 (symbol={symbol})")
                return {'success': True, 'data': []}
            
            # CHANGED: 添加数据检查日志
            logger.info(f"DataFrame 信息: shape={df.shape}, columns={list(df.columns)}, dtypes={df.dtypes.to_dict()}")
            if not df.empty:
                logger.info(f"前3行数据样例:\n{df.head(3)}")
            
            # 【性能优化】2. 提取需要查询的股票代码列表（限制数量，只查询前N只）
            # 优化：使用向量化操作提取股票代码，比循环快 5-10 倍
            if symbol:
                # 单个股票：直接获取该股票信息
                raw_symbol = str(df.iloc[0].get('symbol') or '')
                stock_code_raw = df.iloc[0].get('stockCode', '')
                stock_code = str(stock_code_raw or '')[-6:] if stock_code_raw else ''
                symbol_key = self._normalize_symbol_key(raw_symbol, stock_code)
                needed_symbols = [symbol_key]
                logger.info(f"单个股票模式: raw_symbol={raw_symbol}, stock_code={stock_code}, symbol_key={symbol_key}")
            else:
                # 【性能优化】多个股票：使用向量化操作提取股票代码
                # 限制数量：只处理前200只股票（避免查询过多）
                MAX_STOCKS = 200
                df_limited = df.head(MAX_STOCKS) if len(df) > MAX_STOCKS else df
                
                # 向量化提取：使用 apply 一次性处理所有行（比循环快）
                needed_symbols = list(dict.fromkeys(
                    self._normalize_symbol_key(
                        str(raw_symbol or ''),
                        str(stock_code_raw or '')[-6:] if stock_code_raw else '',
                    )
                    for raw_symbol, stock_code_raw in zip(
                        df_limited['symbol'].fillna(''),
                        df_limited['stockCode'].fillna(''),
                    )
                ))
                needed_symbols = [item for item in needed_symbols if item]
                
                if len(df) > MAX_STOCKS:
                    logger.info(f"聚合模式: 限制处理数量，从 {len(df)} 只股票中提取前 {MAX_STOCKS} 只")
                
                logger.info(f"聚合模式: 提取到 {len(needed_symbols)} 个唯一股票代码")
            
            # 3. 只查询需要的股票的市值信息（而不是所有股票）
            info_start = time.perf_counter()
            logger.info(f"开始查询股票市值信息: {len(needed_symbols)} 只股票")
            stock_info = self._get_stock_info_with_market_cap(needed_symbols)
            info_time = time.perf_counter() - info_start
            logger.info(f"股票市值信息查询完成: {len(stock_info)} 条记录, 耗时 {info_time:.2f} 秒")
            
            # 4. 处理数据
            results = []
            
            if symbol:
                # 单个股票的评论数据
                info = stock_info.get(symbol_key, {})
                
                logger.info(f"处理个股评论数据: df长度={len(df)}, symbol_key={symbol_key}, stock_info存在={symbol_key in stock_info}")
                if symbol_key in stock_info:
                    logger.info(f"股票信息详情: {stock_info[symbol_key]}")
                else:
                    logger.warning(f"股票信息不存在: symbol_key={symbol_key}, stock_info keys={list(stock_info.keys())[:10]}")
                
                # CHANGED: 从 Parquet 数据中获取股票名称（借鉴 /api/stock_sentiment 的方式）
                stock_name_from_parquet = ''
                if not df.empty and 'stockName' in df.columns:
                    stock_name_from_parquet = str(df.iloc[0].get('stockName', '')).strip()
                
                # 优先使用 Parquet 中的名称，如果没有则使用 stock_info 中的名称
                display_name = stock_name_from_parquet or info.get('name', '')
                
                logger.info(f"股票名称查找: symbol_key={symbol_key}, stock_name_from_parquet={stock_name_from_parquet}, info_name={info.get('name', '')}, display_name={display_name}")
                
                for _, row in df.iterrows():
                    comment_count = int(row.get('commentCount') or 0)
                    sentiment = float(row.get('sentiment') or 0.0)
                    post_title = str(row.get('postTitle') or '')[:50]
                    
                    # CHANGED: 确保 comment_count > 0，避免显示无效数据点
                    if comment_count <= 0:
                        continue
                    
                    # CHANGED: 确保 sentiment 是有效数值
                    if pd.isna(sentiment) or abs(sentiment) > 100:
                        sentiment = 0.0
                    
                    results.append({
                        'symbol': symbol_key,
                        'name': display_name,  # CHANGED: 使用从 Parquet 获取的名称
                        'comment_count': comment_count,
                        'sentiment': round(sentiment, 3),
                        'post_title': post_title,
                        'is_comment': True
                    })
                
                logger.info(f"个股评论数据处理完成: {len(results)} 条有效记录")
            else:
                # 【性能优化】所有股票的聚合数据 - 使用向量化操作替代循环
                logger.info(f"开始处理聚合数据: df长度={len(df)}, df列={list(df.columns)}")
                
                # 【性能优化】向量化处理：一次性处理所有行，比循环快 5-10 倍
                try:
                    # 1. 向量化提取股票代码
                    df['raw_symbol'] = df['symbol'].fillna('').astype(str)
                    df['stock_code_raw'] = df['stockCode'].fillna('').astype(str)
                    code_length = df['stock_code_raw'].str.len()
                    df['stock_code_6'] = df['stock_code_raw'].str[-6:]
                    short_code = df['stock_code_raw'].str.zfill(6)
                    df['stock_code_6'] = df['stock_code_6'].where(
                        code_length >= 6,
                        short_code.where(df['stock_code_raw'].ne(''), ''),
                    )
                    df['symbol_key'] = [
                        self._normalize_symbol_key(raw_symbol, stock_code)
                        for raw_symbol, stock_code in zip(
                            df['raw_symbol'], df['stock_code_6']
                        )
                    ]
                    
                    # 2. 向量化处理 commentCount
                    df['comment_count'] = pd.to_numeric(df['commentCount'], errors='coerce').fillna(0).astype(int)
                    
                    # 3. 向量化处理 sentiment
                    df['sentiment'] = pd.to_numeric(
                        df['sentiment'], errors='coerce'
                    ).fillna(0.0)
                    df['sentiment'] = df['sentiment'].where(
                        df['sentiment'].abs() <= 100, 0.0
                    )
                    
                    # 4. 过滤无效数据（向量化）
                    valid_mask = df['comment_count'] > 0
                    df_valid = df[valid_mask].copy()
                    
                    if df_valid.empty:
                        logger.warning("所有数据都被过滤掉（comment_count <= 0）")
                    else:
                        # 5. 向量化获取股票信息
                        market_caps = {
                            key: value.get('market_cap', 0.0)
                            for key, value in stock_info.items()
                        }
                        info_names = {
                            key: value.get('name', '')
                            for key, value in stock_info.items()
                        }
                        df_valid['market_cap'] = (
                            df_valid['symbol_key'].map(market_caps).fillna(0.0)
                        )
                        df_valid['info_name'] = (
                            df_valid['symbol_key'].map(info_names).fillna('')
                        )
                        
                        # 6. 向量化获取股票名称
                        df_valid['stock_name_parquet'] = df_valid['stockName'].astype(str).fillna('').str.strip()
                        df_valid['display_name'] = df_valid['stock_name_parquet'].where(
                            df_valid['stock_name_parquet'].ne(''),
                            df_valid['info_name'],
                        )
                        
                        # 7. 构建结果列表（向量化）
                        results = [
                            {
                                'symbol': row['symbol_key'],
                                'name': row['display_name'],
                                'comment_count': int(row['comment_count']),
                                'sentiment': round(float(row['sentiment']), 3),
                                'market_cap': round(float(row['market_cap']), 2),
                            }
                            for row in df_valid.to_dict('records')
                        ]
                        
                except Exception as e:
                    # 回退到循环方法（如果向量化失败）
                    logger.warning(f"向量化处理失败，回退到循环方法: {e}")
                    results = []
                    for idx, row in df.iterrows():
                        try:
                            raw_symbol = str(row.get('symbol', '') or '')
                            stock_code_raw = row.get('stockCode', '')
                            stock_code = str(stock_code_raw or '')[-6:] if stock_code_raw else ''
                            symbol_key = self._normalize_symbol_key(raw_symbol, stock_code)
                            
                            info = stock_info.get(symbol_key, {})
                            
                            comment_count_raw = row.get('commentCount', 0)
                            if pd.isna(comment_count_raw) or comment_count_raw is None:
                                comment_count = 0
                            else:
                                try:
                                    comment_count = int(float(comment_count_raw))
                                except (ValueError, TypeError):
                                    comment_count = 0
                            
                            if comment_count <= 0:
                                continue
                            
                            sentiment_raw = row.get('sentiment')
                            if pd.isna(sentiment_raw) or sentiment_raw is None:
                                sentiment = 0.0
                            else:
                                try:
                                    sentiment = float(sentiment_raw)
                                    if abs(sentiment) > 100:
                                        sentiment = 0.0
                                except (ValueError, TypeError):
                                    sentiment = 0.0
                            
                            stock_name_from_parquet = str(row.get('stockName', '') or '').strip()
                            display_name = stock_name_from_parquet or info.get('name', '')
                            
                            results.append({
                                'symbol': symbol_key,
                                'name': display_name,
                                'comment_count': comment_count,
                                'sentiment': round(sentiment, 3),
                                'market_cap': round(info.get('market_cap', 0.0), 2) if not pd.isna(info.get('market_cap', 0.0)) else 0.0
                            })
                        except Exception as row_err:
                            logger.warning(f"处理第 {idx} 行数据时出错: {row_err}")
                            continue
                
                logger.info(f"散点图数据：查询到 {len(df)} 只股票，处理后有 {len(results)} 只股票")
                if len(results) == 0 and len(df) > 0:
                    logger.warning(f"警告：查询到 {len(df)} 只股票，但处理后结果为空！可能所有行都被过滤掉了。")
                    # 输出前几行的详细信息用于调试
                    for idx, row in df.head(5).iterrows():
                        logger.warning(f"  行 {idx}: symbol={row.get('symbol')}, stockCode={row.get('stockCode')}, "
                                     f"commentCount={row.get('commentCount')}, sentiment={row.get('sentiment')}")
                
                # CHANGED: 按市值排序（有市值的优先），如果没有市值则按评论数排序
                # 增加显示数量到 100
                results.sort(key=lambda x: (
                    x.get('market_cap', 0.0) if x.get('market_cap', 0.0) > 0 else 0,
                    x.get('comment_count', 0.0)
                ), reverse=True)
                results = results[:100]
                logger.info(f"最终返回 {len(results)} 只股票（按市值和评论数排序）")
                
                # 如果结果少于50，尝试不按市值过滤，显示所有有数据的股票
                if len(results) < 50:
                    logger.warning(f"散点图数据较少（{len(results)} 只），尝试显示所有有数据的股票")
                    # 重新处理，不要求市值信息
                    results = []
                    for _, row in df.iterrows():
                        raw_symbol = str(row.get('symbol') or '')
                        stock_code = str(row.get('stockCode') or '')[-6:]
                        symbol_key = self._normalize_symbol_key(raw_symbol, stock_code)
                        
                        info = stock_info.get(symbol_key, {})
                        
                        comment_count = int(row.get('commentCount') or 0)
                        sentiment = float(row.get('sentiment') or 0.0)
                        
                        results.append({
                            'symbol': symbol_key,
                            'name': info.get('name', '') or str(row.get('stockName', '')).strip(),
                            'comment_count': comment_count,
                            'sentiment': round(sentiment, 3) if not (pd.isna(sentiment) or abs(sentiment) > 100) else 0.0,
                            'market_cap': round(info.get('market_cap', 0.0), 2) if not pd.isna(info.get('market_cap', 0.0)) else 0.0
                        })
                    
                    # 按评论数排序，取前100
                    results.sort(key=lambda x: x.get('comment_count', 0.0), reverse=True)
                    results = results[:100]
                    logger.info(f"重新处理后：{len(results)} 只股票")
                else:
                    results = results[:100]  # 增加显示数量到100
            
            # 5. 对数据进行归一化处理，解决散点图数据点被压缩的问题
            if results:
                # 收集所有 comment_count 和 sentiment 值
                comment_counts = [r.get('comment_count', 0) for r in results if r.get('comment_count', 0) > 0]
                sentiments = [r.get('sentiment', 0.0) for r in results]
                
                # 计算最小值和最大值
                min_comment = min(comment_counts) if comment_counts else 0
                max_comment = max(comment_counts) if comment_counts else 1
                comment_range = max_comment - min_comment if max_comment != min_comment else 1
                
                min_sentiment = min(sentiments) if sentiments else -1
                max_sentiment = max(sentiments) if sentiments else 1
                sentiment_range = max_sentiment - min_sentiment if max_sentiment != min_sentiment else 2
                
                # CHANGED: 对于单个数据点或所有值相同的情况，使用原始值而不是归一化
                # 这样可以避免所有点都显示在同一个位置
                use_normalization = len(comment_counts) > 1 and comment_range > 0
                
                # 对每个数据点进行归一化
                for result in results:
                    comment_count = result.get('comment_count', 0)
                    sentiment = result.get('sentiment', 0.0)
                    
                    # Min-Max 归一化：将 comment_count 映射到 [0, 1] 范围
                    if use_normalization:
                        normalized_comment = (comment_count - min_comment) / comment_range
                    else:
                        # CHANGED: 如果只有一个数据点或所有值相同，对于个股模式使用原始值
                        # 对于聚合模式，如果只有一个股票，也使用原始值
                        # 前端会根据 is_comment 字段判断是否使用归一化值
                        if symbol:
                            # 个股模式：使用原始 comment_count（前端会处理）
                            normalized_comment = comment_count if comment_count > 0 else 0.5
                        else:
                            # 聚合模式：如果只有一个股票，归一化到 0.5
                            normalized_comment = 0.5
                    
                    result['comment_count_normalized'] = round(normalized_comment, 4)
                    
                    # 确保 sentiment 在 [-1, 1] 范围内
                    if abs(sentiment) > 1:
                        normalized_sentiment = max(-1, min(1, sentiment / 100))  # 处理异常值
                        result['sentiment'] = round(normalized_sentiment, 3)
                    else:
                        # 如果原始值在 [-1, 1] 范围内，也可以选择归一化到 [0, 1]
                        # 但为了保持情感的正负性，我们保持原值
                        pass
                
                logger.info(f"数据归一化完成: comment_count 范围 [{min_comment}, {max_comment}], "
                          f"sentiment 范围 [{min_sentiment:.3f}, {max_sentiment:.3f}], "
                          f"使用归一化={use_normalization}, 数据点数量={len(results)}")
            
            # 6. 清洗数据（防止 JSON 序列化报错）
            def sanitize_data(data):
                """递归清洗数据，将 NaN/Infinity 转换为 null"""
                import math
                try:
                    np_floating = np.floating
                    np_integer = np.integer
                except AttributeError:
                    # 兼容性处理：如果 np.floating 不存在，只检查 float
                    np_floating = float
                    np_integer = int
                
                if isinstance(data, dict):
                    return {k: sanitize_data(v) for k, v in data.items()}
                elif isinstance(data, list):
                    return [sanitize_data(item) for item in data]
                elif isinstance(data, (float, np_floating)):
                    if math.isnan(data) or math.isinf(data):
                        return None
                    return float(data)
                elif isinstance(data, (int, np_integer)):
                    return int(data)
                elif pd.isna(data):
                    return None
                else:
                    return data
            
            cleaned_data = sanitize_data(results)
            
            total_time = time.perf_counter() - start_time
            
            # CHANGED: 添加详细的数据检查日志
            if symbol:
                logger.info(f"个股散点图数据获取完成: {len(cleaned_data)} 条数据, 总耗时 {total_time:.2f} 秒")
                if cleaned_data:
                    sample_item = cleaned_data[0]
                    logger.info(f"数据样例: symbol={sample_item.get('symbol')}, comment_count={sample_item.get('comment_count')}, "
                              f"comment_count_normalized={sample_item.get('comment_count_normalized')}, "
                              f"sentiment={sample_item.get('sentiment')}, is_comment={sample_item.get('is_comment')}")
                else:
                    logger.warning(f"个股散点图数据为空！df长度={len(df)}, results长度={len(results)}")
            else:
                logger.info(f"散点图数据获取完成: {len(cleaned_data)} 条数据, 总耗时 {total_time:.2f} 秒 (加载: {load_time:.2f}s, 市值: {info_time:.2f}s)")
            
            return {'success': True, 'data': cleaned_data}
            
        except Exception as e:
            logger.error(f"获取散点图数据失败: {e}", exc_info=True)
            return {'success': False, 'error': str(e), 'data': []}
