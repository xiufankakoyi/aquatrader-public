"""
股票数据服务
============

负责股票数据查询（K线、价格、股票信息、基准数据）

【架构】纯 Polars 流水线
- 零拷贝：Arrow Table -> Polars DataFrame
- 惰性计算：Lazy API 优化多步计算
- 统一缺失值：NaN/NaT/Null -> None (JSON null)
"""
from typing import Dict, List, Any, Optional
import polars as pl
from pathlib import Path
from server.utils.symbol_utils import normalize_symbol_code
from server.services.data_initialization_service import DataInitializationService
from config.logger import get_logger
from data_svc.storage.lancedb_reader import LanceDBDataReader
from data_svc.query.fast_market_query import get_fast_market_query


def normalize_nulls(df: pl.DataFrame) -> pl.DataFrame:
    """
    统一缺失值处理：将 NaN/NaT/Null 标准化为 None
    
    Polars 的 null 在 .to_dicts() 时会自动转为 Python None，
    但 NaN 需要显式处理。
    
    Args:
        df: Polars DataFrame
        
    Returns:
        处理后的 DataFrame，所有缺失值统一为 null
    """
    float_cols = [c for c, dtype in df.schema.items() if dtype in (pl.Float32, pl.Float64)]
    
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan())
            .then(None)
            .otherwise(pl.col(c))
            .alias(c)
            for c in float_cols
        ])
    
    return df


def safe_round(df: pl.DataFrame, columns: List[str], decimals: int = 2) -> pl.DataFrame:
    """
    安全的四舍五入，保留 null
    
    Args:
        df: Polars DataFrame
        columns: 需要四舍五入的列名列表
        decimals: 小数位数
        
    Returns:
        处理后的 DataFrame
    """
    exprs = []
    for c in columns:
        if c in df.columns:
            exprs.append(
                pl.when(pl.col(c).is_not_null())
                .then(pl.col(c).round(decimals))
                .otherwise(None)
                .alias(c)
            )
    
    if exprs:
        df = df.with_columns(exprs)
    
    return df


class StockDataService:
    """股票数据服务类"""
    
    INDEX_MAPPING = {
        '000300': '000300.SH',
        '000905': '000905.SH',
        '000001': '000001.SH',
        '399001': '399001.SZ',
        '000016': '000016.SH',
        '399006': '399006.SZ',
    }
    
    def __init__(self, init_service: DataInitializationService):
        self.init_service = init_service
        self._logger = get_logger(__name__)
        self._fmq = get_fast_market_query()

    @property
    def lancedb_reader(self) -> LanceDBDataReader:
        """获取 LanceDB 读取器（通过 FMQ 底层 _reader 访问，保持向后兼容）"""
        return self._fmq._reader
    
    @property
    def data_query(self):
        """获取数据查询对象"""
        return self.init_service.data_query
    
    @property
    def stock_info_map(self) -> Dict[str, str]:
        """获取股票信息映射"""
        return self.init_service.stock_info_map
    
    def get_benchmark_data_from_db(self, benchmark_code: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """
        获取基准数据（从 LanceDB 读取指数数据）
        
        Args:
            benchmark_code: 基准代码（如 '000300'）
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            
        Returns:
            包含 date 和 close 字段的字典列表
        """
        normalized_code = str(benchmark_code or '').strip().upper()
        if normalized_code in self.INDEX_MAPPING:
            ts_code = self.INDEX_MAPPING[normalized_code]
        elif normalized_code in self.INDEX_MAPPING.values():
            ts_code = normalized_code
        else:
            self._logger.warning(f"不支持的基准代码: {benchmark_code}")
            return []

        try:
            df = self._fmq._read_table(
                "index_daily",
                symbols=ts_code,
                start_date=start_date,
                end_date=end_date,
                fields=["trade_date", "close"],
            )

            if df.is_empty():
                self._logger.warning(f"LanceDB 中未找到基准数据: {ts_code}, {start_date} ~ {end_date}")
                return []
            
            df = df.select([
                pl.col("trade_date")
                .cast(pl.Date, strict=False)
                .dt.strftime("%Y-%m-%d")
                .alias("date"),
                pl.col("close").cast(pl.Float64),
            ]).sort("date")
            
            df = normalize_nulls(df)
            df = safe_round(df, ['close'])
            
            return df.to_dicts()
            
        except Exception as e:
            self._logger.warning(f"读取基准数据时发生错误: {e}")
            return []
    
    def _get_index_kline_from_lancedb(self, symbol_code: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """
        从 LanceDB index_daily 表获取指数K线数据
        
        支持：000300(沪深300), 000001(上证指数), 399001(深证成指), 
              000016(上证50), 399006(创业板), 000905(中证500)
        
        Args:
            symbol_code: 指数代码
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            K线数据字典列表
        """
        if symbol_code not in self.INDEX_MAPPING:
            return []
        
        ts_code = self.INDEX_MAPPING[symbol_code]
        
        try:
            import lancedb
            from pathlib import Path
            
            lancedb_path = Path(__file__).parent.parent.parent / "data" / "lancedb"
            db = lancedb.connect(str(lancedb_path))
            
            result = db.list_tables()
            tables = result.tables if hasattr(result, 'tables') else list(result)
            
            if 'index_daily' not in tables:
                return []
            
            table = db.open_table('index_daily')
            ds = table.to_lance()
            
            scanner = ds.scanner(
                filter=f"symbol = '{ts_code}'",
                columns=['trade_date', 'open', 'high', 'low', 'close', 'volume']
            )
            arrow_table = scanner.to_table()
            df = pl.from_arrow(arrow_table)
            
            if df.is_empty():
                return []
            
            df = df.rename({'trade_date': 'date'})
            
            if df['date'].dtype == pl.Datetime:
                df = df.with_columns(pl.col('date').dt.date())
            
            from datetime import datetime
            start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
            end_dt = datetime.strptime(end_date, '%Y-%m-%d').date()
            
            df = df.filter(
                (pl.col("date") >= start_dt) & (pl.col("date") <= end_dt)
            ).sort("date")
            
            if df.is_empty():
                return []
            
            price_cols = ['open', 'high', 'low', 'close']
            df = safe_round(df, price_cols, decimals=2)
            df = safe_round(df, ['volume'], decimals=0)
            
            df = normalize_nulls(df)
            
            result_cols = ['date', 'open', 'high', 'low', 'close', 'volume']
            existing_cols = [c for c in result_cols if c in df.columns]
            
            return df.select(existing_cols).to_dicts()
            
        except Exception as e:
            self._logger.warning(f"从 LanceDB 读取指数数据失败 {symbol_code}: {e}")
            return []
    
    def get_symbol_kline(self, symbol_code: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """
        获取K线数据（强制全局前复权）
        
        使用全局最新因子作为基准进行前复权计算。
        
        Args:
            symbol_code: 股票代码
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            
        Returns:
            K线数据字典列表，包含：
            - date: 交易日期
            - open, high, low, close: 前复权价格
            - volume: 成交量
            - ma5, ma10, ma20: 均线（可选）
        """
        self._logger.debug(f"[K线查询] 开始查询 {symbol_code} 从 {start_date} 到 {end_date}")
        
        if not self.init_service._initialized:
            self._logger.debug("[K线查询] 服务未初始化，尝试初始化...")
            self.init_service.ensure_initialized()

        symbol_code = normalize_symbol_code(symbol_code)
        self._logger.debug(f"[K线查询] 标准化后的代码: {symbol_code}")
        
        if not symbol_code:
            self._logger.debug("[K线查询] 代码标准化失败，返回空数组")
            return []
        
        index_data = self._get_index_kline_from_lancedb(symbol_code, start_date, end_date)
        self._logger.debug(f"[K线查询] 指数数据查询结果: {len(index_data)} 条")
        
        if index_data:
            return index_data
        
        try:
            history_df = self._get_stock_history_polars(
                symbol_code, start_date, end_date,
                columns=["stock_code", "trade_date", "open", "high", "low", "close", 
                         "volume", "adj_factor", "ma5", "ma10", "ma20"]
            )
            
            if history_df is None or history_df.is_empty():
                self._logger.debug("[K线查询] 数据为空，返回空数组")
                return []

            base_factor = self.init_service.get_global_latest_factor(symbol_code)
            self._logger.debug(f"[K线查询] 全局最新因子: {base_factor}")
            
            history_df = self._calculate_qfq(history_df, base_factor)

            result = self._format_kline_output(history_df)
            self._logger.debug(f"[K线查询] 返回 {len(result)} 条记录")
            return result
            
        except Exception as e:
            self._logger.error(f"[K线查询] 获取失败 {symbol_code}: {e}")
            return []
    
    def _get_stock_history_polars(
        self, 
        stock_code: str, 
        start_date: str, 
        end_date: str,
        columns: Optional[List[str]] = None
    ) -> Optional[pl.DataFrame]:
        """
        获取股票历史数据（LanceDB 实现）
        
        使用 LanceDBDataReader 读取数据，零拷贝 Arrow -> Polars。
        
        Args:
            stock_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            columns: 需要的列名列表
            
        Returns:
            Polars DataFrame 或 None
        """
        if columns is None:
            columns = ["stock_code", "trade_date", "open", "high", "low", "close", "volume", "adj_factor"]
        
        try:
            df = self._fmq.get_stock_history(
                stock_code,
                start_date,
                end_date,
                fields=columns
            )
            
            if df.is_empty():
                return None
            
            df = df.sort("trade_date")
            existing_cols = [c for c in columns if c in df.columns]
            df = df.select(existing_cols)
            
            return df
            
        except Exception as e:
            self._logger.error(f"获取股票历史数据失败 {stock_code}: {e}")
            return None
    
    def _calculate_qfq(self, df: pl.DataFrame, base_factor: float) -> pl.DataFrame:
        """
        计算前复权价格
        
        公式：QFQ = Raw * (Current_Factor / Global_Latest_Factor)
        
        Args:
            df: 原始数据 DataFrame
            base_factor: 全局最新复权因子
            
        Returns:
            包含前复权价格的 DataFrame
        """
        if base_factor is None or base_factor == 0:
            base_factor = 1.0
        
        price_cols = ['open', 'high', 'low', 'close', 'ma5', 'ma10', 'ma20']
        
        df = df.with_columns([
            (pl.col('adj_factor') / base_factor).alias('qfq_ratio')
        ])
        
        exprs = []
        for col in price_cols:
            if col in df.columns:
                exprs.append(
                    (pl.col(col) * pl.col('qfq_ratio')).alias(col)
                )
        
        if exprs:
            df = df.with_columns(exprs)
        
        return df.drop('qfq_ratio')
    
    def _format_kline_output(self, df: pl.DataFrame) -> List[Dict[str, Any]]:
        """
        格式化K线输出
        
        统一缺失值处理，确保前端 JSON 结构兼容。
        
        Args:
            df: 包含K线数据的 DataFrame
            
        Returns:
            格式化后的字典列表
        """
        output_cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'ma5', 'ma10', 'ma20']
        
        df = df.with_columns([
            pl.col('trade_date').alias('date')
        ])
        
        price_cols = ['open', 'high', 'low', 'close', 'ma5', 'ma10', 'ma20']
        df = safe_round(df, price_cols, decimals=2)
        df = safe_round(df, ['volume'], decimals=0)
        
        df = normalize_nulls(df)
        
        df = df.filter(
            pl.col('open').is_not_null() & pl.col('close').is_not_null()
        )
        
        existing_cols = [c for c in output_cols if c in df.columns]
        
        return df.select(existing_cols).to_dicts()
    
    def get_latest_prices(self, symbol_codes: List[str], target_date: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        获取最新价格（强制全局前复权）
        
        Args:
            symbol_codes: 股票代码列表
            target_date: 目标日期（可选，默认最新交易日）
            
        Returns:
            {symbol: {date: str, price: float, prev_close: float}}
        """
        if not self.init_service._initialized:
            self.init_service.ensure_initialized()

        if not symbol_codes:
            return {}

        normalized_codes = [normalize_symbol_code(c) for c in symbol_codes]
        normalized_codes = [c for c in normalized_codes if c]
        latest_map = {}
        
        try:
            if self.data_query is None:
                return {}
            
            for code in set(normalized_codes):
                base_factor = self.init_service.get_global_latest_factor(code)
                
                result = self._get_latest_price_polars(code, target_date)
                
                if result is None:
                    continue
                
                trade_date, raw_open, raw_close, raw_prev, current_factor = result
                
                raw_price = raw_open if raw_open is not None else raw_close
                if raw_price is None:
                    continue
                    
                ratio = current_factor / base_factor if base_factor else 1.0
                qfq_price = raw_price * ratio
                qfq_prev = (raw_prev * ratio) if raw_prev is not None else qfq_price

                latest_map[code] = {
                    "date": trade_date,
                    "price": round(qfq_price, 2),
                    "prev_close": round(qfq_prev, 2)
                }
                
        except Exception as e:
            self._logger.error(f"获取最新价格失败: {e}")
            
        return latest_map
    
    def _get_latest_price_polars(
        self, 
        stock_code: str, 
        target_date: Optional[str] = None
    ) -> Optional[tuple]:
        """
        获取单只股票的最新价格（LanceDB 实现）
        
        Args:
            stock_code: 股票代码
            target_date: 目标日期（可选）
            
        Returns:
            (trade_date, open, close, prev_close, adj_factor) 或 None
        """
        try:
            columns = ['trade_date', 'open', 'close', 'prev_close', 'adj_factor']

            if target_date:
                df = self._fmq._read_table(
                    "daily_ohlcv",
                    symbols=stock_code,
                    end_date=target_date,
                    fields=columns
                )
            else:
                df = self._fmq._read_table(
                    "daily_ohlcv",
                    symbols=stock_code,
                    fields=columns
                )
            
            if df.is_empty():
                return None
            
            df = df.sort('trade_date', descending=True).head(1)
            
            row = df.row(0, named=True)
            
            return (
                row['trade_date'],
                row.get('open'),
                row.get('close'),
                row.get('prev_close'),
                float(row.get('adj_factor', 1.0) or 1.0)
            )
            
        except Exception as e:
            self._logger.error(f"获取最新价格失败 {stock_code}: {e}")
            return None
    
    def get_stock_info_with_market_cap(self, needed_symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        """
        获取股票基础信息（名称、市值）
        
        使用 LanceDB 读取数据。
        
        Args:
            needed_symbols: 可选，如果提供则只查询这些股票，否则查询所有股票
            
        Returns:
            {symbol: {name: str, market_cap: float, original_code: str}}
        """
        if not self.init_service._initialized:
            self.init_service.ensure_initialized()
        
        stock_info = {}
        
        try:
            columns = ['stock_code', 'total_mv']

            df = self._fmq._read_table(
                "daily_ohlcv",
                fields=columns
            )
            
            if df.is_empty():
                self._logger.warning("LanceDB 中无数据")
                return {}
            
            if needed_symbols:
                stock_codes = self._extract_stock_codes(needed_symbols)
                
                if stock_codes:
                    df = df.filter(
                        (pl.col('total_mv').is_not_null()) &
                        (pl.col('stock_code').is_in(stock_codes))
                    )
                else:
                    self._logger.warning(f"无法从 needed_symbols 提取有效的股票代码: {needed_symbols}")
                    return {}
            else:
                df = df.filter(pl.col('total_mv').is_not_null())
            
            df_market_cap = df.group_by('stock_code').agg([
                pl.col('total_mv').max().alias('market_cap')
            ])
            
            stock_name_map = self._load_stock_names_from_lancedb()
            
            stock_info = self._build_stock_info_dict(df_market_cap, stock_name_map)
            
        except Exception as e:
            self._logger.warning(f"从 LanceDB 获取股票市值失败: {e}")
            
        return stock_info
    
    def _extract_stock_codes(self, needed_symbols: List[str]) -> List[str]:
        """
        从股票代码列表中提取标准化的6位代码
        
        Args:
            needed_symbols: 原始股票代码列表
            
        Returns:
            标准化的6位代码列表
        """
        stock_codes = []
        for sym in needed_symbols:
            if sym.startswith(('sz', 'sh')):
                code = sym[2:]
            else:
                code = sym
            
            if len(code) >= 6:
                stock_codes.append(code[-6:].zfill(6))
            elif len(code) == 6:
                stock_codes.append(code.zfill(6))
        
        return stock_codes
    
    def _load_stock_names_from_lancedb(self) -> Dict[str, str]:
        """
        从 LanceDB 加载股票名称映射
        
        Returns:
            {stock_code: stock_name} 映射
        """
        try:
            import lancedb
            db = lancedb.connect(self._fmq._reader.db_path)
            
            result = db.list_tables()
            tables = result.tables if hasattr(result, 'tables') else list(result)
            if 'stock_info' not in tables:
                return {}
            
            table = db.open_table('stock_info')
            ds = table.to_lance()
            
            schema_names = {field.name for field in table.schema}
            name_col = 'stock_name' if 'stock_name' in schema_names else 'name' if 'name' in schema_names else None
            code_col = 'stock_code' if 'stock_code' in schema_names else 'ts_code' if 'ts_code' in schema_names else None
            if not code_col or not name_col:
                return {}

            if hasattr(ds, 'scanner'):
                scanner = ds.scanner(columns=[code_col, name_col])
                arrow_table = scanner.to_table()
            else:
                arrow_table = table.to_arrow()
            
            df = pl.from_arrow(arrow_table)
            if code_col != 'stock_code':
                df = df.rename({code_col: 'stock_code'})
            if name_col != 'stock_name':
                df = df.rename({name_col: 'stock_name'})
            
            if 'stock_code' not in df.columns or 'stock_name' not in df.columns:
                return {}
            
            return dict(zip(
                df['stock_code'].to_list(),
                df['stock_name'].to_list()
            ))
        except Exception as e:
            self._logger.debug(f"从 LanceDB 加载股票名称失败: {e}")
            return {}
    
    def _load_stock_names(self, stock_info_path: Path) -> Dict[str, str]:
        """
        加载股票名称映射
        
        Args:
            stock_info_path: stock_info.parquet 文件路径
            
        Returns:
            {stock_code: stock_name} 映射
        """
        if not stock_info_path.exists():
            return {}
        
        try:
            df = pl.scan_parquet(str(stock_info_path)).select([
                'stock_code', 'stock_name'
            ]).collect()
            
            return dict(zip(
                df['stock_code'].to_list(),
                df['stock_name'].to_list()
            ))
        except Exception as e:
            self._logger.debug(f"加载股票名称失败: {e}")
            return {}
    
    def _build_stock_info_dict(
        self, 
        df_market_cap: pl.DataFrame, 
        stock_name_map: Dict[str, str]
    ) -> Dict[str, Dict[str, Any]]:
        """
        构建股票信息字典
        
        Args:
            df_market_cap: 包含市值的 DataFrame
            stock_name_map: 股票名称映射
            
        Returns:
            {symbol: {name, market_cap, original_code}}
        """
        stock_info = {}
        
        for row in df_market_cap.iter_rows(named=True):
            symbol_code = str(row.get('stock_code', '')).zfill(6)
            market_cap = float(row.get('market_cap') or 0.0) / 10000.0
            
            if symbol_code.startswith('0'):
                full_symbol = f"sz{symbol_code}"
            elif symbol_code.startswith('6'):
                full_symbol = f"sh{symbol_code}"
            else:
                full_symbol = symbol_code
            
            stock_name = stock_name_map.get(symbol_code, '')
            
            if not stock_name:
                stock_name = self.stock_info_map.get(symbol_code, '')
            
            stock_info[full_symbol] = {
                'name': stock_name,
                'market_cap': market_cap,
                'original_code': symbol_code
            }
        
        return stock_info
