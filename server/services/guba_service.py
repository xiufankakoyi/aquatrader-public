"""
股吧服务
负责股吧数据加载和处理
"""
from typing import Dict, List, Any, Optional
import pandas as pd
try:
    import cupy as np
except ImportError:
    import numpy as np
from pathlib import Path
import polars as pl
from server.services.data_initialization_service import DataInitializationService
from server.services.stock_data_service import StockDataService
from server.utils.symbol_utils import normalize_symbol_key
from config.logger import get_logger


# T4.1: Demo 模式常量, 当股吧数据缺失时降级使用, 不构造虚假帖子内容
_DEMO_FLAG = "static_demo"
_DEMO_NOTE = "暂无真实股吧舆论数据, 当前为 Demo 展示"


def _demo_scatter_points() -> List[Dict[str, Any]]:
    """Demo 散点图数据 (仅结构化字段, 不含原始帖子文本)"""
    return [
        {
            "symbol": "000001.SZ",
            "name": "平安银行",
            "comment_count": 3200,
            "comment_count_normalized": 0.85,
            "sentiment": 0.65,
            "market_cap": 2.1e11,
        },
        {
            "symbol": "000002.SZ",
            "name": "万科A",
            "comment_count": 2100,
            "comment_count_normalized": 0.55,
            "sentiment": -0.15,
            "market_cap": 1.2e11,
        },
        {
            "symbol": "600519.SH",
            "name": "贵州茅台",
            "comment_count": 8500,
            "comment_count_normalized": 1.0,
            "sentiment": 0.78,
            "market_cap": 2.1e12,
        },
    ]


def _demo_wrap(result: Dict[str, Any]) -> Dict[str, Any]:
    """为返回结果附加 demo_mode/source 标记"""
    if not isinstance(result, dict):
        return result
    return {
        **result,
        "demo_mode": True,
        "source": _DEMO_FLAG,
        "message": _DEMO_NOTE,
    }


class GubaService:
    """股吧服务类"""
    
    def __init__(self, init_service: DataInitializationService, stock_data_service: StockDataService):
        self.init_service = init_service
        self.stock_data_service = stock_data_service
    
    def load_guba_posts_from_parquet(self, symbol: Optional[str] = None, sample_size: int = 50) -> pd.DataFrame:
        """
        从 Parquet 文件加载股吧数据
        如果指定 symbol，返回该股票的评论数据（可抽样）
        否则返回所有股票的聚合数据
        """
        logger = get_logger(__name__)
        
        base_dir = Path(__file__).parent.parent
        parquet_path = base_dir / 'parquet_data' / 'guba_posts.parquet'
        
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
                
                df = lazy_df.filter(
                    (pl.col('symbol') == full_symbol) |
                    (pl.col('symbol') == code_6) |
                    (pl.col('symbol').str.slice(-6) == code_6) |
                    (pl.col('stockbar_code').cast(pl.Utf8) == code_6)
                ).filter(
                    pl.col('post_comment_count').cast(pl.Int64).is_not_null() &
                    (pl.col('post_comment_count').cast(pl.Int64) > 0) &
                    pl.col('bullish_bearish').cast(pl.Float64).is_not_null()
                ).select([
                    pl.col('symbol'),
                    pl.col('stockbar_code').cast(pl.Utf8).alias('stockCode'),
                    pl.col('stockbar_name').cast(pl.Utf8).fill_null('').alias('stockName'),
                    pl.col('post_comment_count').cast(pl.Int64).fill_null(0).alias('commentCount'),
                    pl.col('bullish_bearish').cast(pl.Float64).fill_null(0.0).alias('sentiment'),
                    pl.col('post_title').cast(pl.Utf8).fill_null('').alias('postTitle'),
                    pl.col('post_publish_time').cast(pl.Datetime).alias('postPublishTime')
                ]).collect()
                
                logger.debug(f"查询个股散点图数据: symbol={symbol}, code_6={code_6}")
                logger.info(f"查询返回 {len(df)} 条记录")
                
                if len(df) > sample_size:
                    df = df.sample(n=sample_size, seed=42)
                    logger.info(f"随机抽样后: {len(df)} 条记录")
            else:
                df = lazy_df.filter(
                    pl.col('symbol').is_not_null() &
                    (pl.col('post_comment_count').cast(pl.Int64) > 0)
                ).select([
                    pl.col('symbol'),
                    pl.col('stockbar_code').cast(pl.Utf8).alias('stockCode'),
                    pl.col('stockbar_name').cast(pl.Utf8).fill_null('').alias('stockName'),
                    pl.col('post_comment_count').cast(pl.Int64).alias('commentCount'),
                    pl.col('bullish_bearish').cast(pl.Float64).alias('sentiment')
                ]).group_by(['symbol', 'stockCode', 'stockName']).agg([
                    pl.col('commentCount').sum().alias('commentCount'),
                    pl.col('sentiment').mean().fill_null(0.0).alias('sentiment')
                ]).sort('commentCount', descending=True).head(200).collect()
                
                logger.info(f"从 Parquet 查询到 {len(df)} 只股票（按评论数排序的前200只）")
            
            return df.to_pandas()
        except Exception as e:
            logger.warning(f"从 Parquet 读取股吧数据失败: {e}", exc_info=True)
            return pd.DataFrame()
    
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
        logger = get_logger(__name__)
        
        try:
            if not self.init_service._initialized:
                self.init_service.ensure_initialized()
            
            import time
            start_time = time.perf_counter()
            
            logger.info(f"开始加载股吧数据: symbol={symbol}")
            df = self.load_guba_posts_from_parquet(symbol)
            load_time = time.perf_counter() - start_time
            logger.info(f"股吧数据加载完成: {len(df)} 条记录, 耗时 {load_time:.2f} 秒")
            
            if df.empty:
                logger.warning(f"股吧数据为空，返回空数据 (symbol={symbol})")
                if symbol:
                    # 个股模式: 保留空数据, 由前端 EmptyState 提示
                    return {'success': True, 'data': [], 'demo_mode': False, 'source': 'empty'}
                # 聚合模式: 降级为 Demo
                return _demo_wrap({'success': True, 'data': _demo_scatter_points()})
            
            if symbol:
                raw_symbol = str(df.iloc[0].get('symbol') or '')
                stock_code_raw = df.iloc[0].get('stockCode', '')
                stock_code = str(stock_code_raw or '')[-6:] if stock_code_raw else ''
                symbol_key = normalize_symbol_key(raw_symbol, stock_code)
                needed_symbols = [symbol_key]
                logger.info(f"单个股票模式: raw_symbol={raw_symbol}, stock_code={stock_code}, symbol_key={symbol_key}")
            else:
                MAX_STOCKS = 200
                df_limited = df.head(MAX_STOCKS) if len(df) > MAX_STOCKS else df
                
                def extract_symbol_key(row):
                    try:
                        raw_symbol = str(row.get('symbol') or '')
                        stock_code_raw = row.get('stockCode', '')
                        stock_code = str(stock_code_raw or '')[-6:] if stock_code_raw else ''
                        return normalize_symbol_key(raw_symbol, stock_code)
                    except Exception:
                        return None
                
                needed_symbols = df_limited.apply(extract_symbol_key, axis=1).dropna().unique().tolist()
                
                if len(df) > MAX_STOCKS:
                    logger.info(f"聚合模式: 限制处理数量，从 {len(df)} 只股票中提取前 {MAX_STOCKS} 只")
                
                logger.info(f"聚合模式: 提取到 {len(needed_symbols)} 个唯一股票代码")
            
            info_start = time.perf_counter()
            logger.info(f"开始查询股票市值信息: {len(needed_symbols)} 只股票")
            stock_info = self.stock_data_service.get_stock_info_with_market_cap(needed_symbols)
            info_time = time.perf_counter() - info_start
            logger.info(f"股票市值信息查询完成: {len(stock_info)} 条记录, 耗时 {info_time:.2f} 秒")
            
            results = []
            
            if symbol:
                symbol_key = needed_symbols[0] if needed_symbols else ''
                info = stock_info.get(symbol_key, {})
                
                stock_name_from_parquet = ''
                if not df.empty and 'stockName' in df.columns:
                    stock_name_from_parquet = str(df.iloc[0].get('stockName', '')).strip()
                
                display_name = stock_name_from_parquet or info.get('name', '')
                
                for _, row in df.iterrows():
                    comment_count = int(row.get('commentCount') or 0)
                    sentiment = float(row.get('sentiment') or 0.0)
                    post_title = str(row.get('postTitle') or '')[:50]
                    
                    if comment_count <= 0:
                        continue
                    
                    if pd.isna(sentiment) or abs(sentiment) > 100:
                        sentiment = 0.0
                    
                    results.append({
                        'symbol': symbol_key,
                        'name': display_name,
                        'comment_count': comment_count,
                        'sentiment': round(sentiment, 3),
                        'post_title': post_title,
                        'is_comment': True
                    })
            else:
                try:
                    df['raw_symbol'] = df['symbol'].astype(str).fillna('')
                    df['stock_code_raw'] = df['stockCode'].astype(str).fillna('')
                    df['stock_code_6'] = df['stock_code_raw'].apply(
                        lambda x: x[-6:] if len(x) >= 6 else x.zfill(6) if x else ''
                    )
                    df['symbol_key'] = df.apply(
                        lambda row: normalize_symbol_key(row['raw_symbol'], row['stock_code_6']),
                        axis=1
                    )
                    
                    df['comment_count'] = pd.to_numeric(df['commentCount'], errors='coerce').fillna(0).astype(int)
                    
                    df['sentiment'] = pd.to_numeric(df['sentiment'], errors='coerce').fillna(0.0)
                    df['sentiment'] = df['sentiment'].apply(lambda x: 0.0 if abs(x) > 100 else x)
                    
                    valid_mask = df['comment_count'] > 0
                    df_valid = df[valid_mask].copy()
                    
                    if not df_valid.empty:
                        df_valid['market_cap'] = df_valid['symbol_key'].apply(
                            lambda key: stock_info.get(key, {}).get('market_cap', 0.0)
                        )
                        df_valid['info_name'] = df_valid['symbol_key'].apply(
                            lambda key: stock_info.get(key, {}).get('name', '')
                        )
                        
                        df_valid['stock_name_parquet'] = df_valid['stockName'].astype(str).fillna('').str.strip()
                        df_valid['display_name'] = df_valid.apply(
                            lambda row: row['stock_name_parquet'] or row['info_name'],
                            axis=1
                        )
                        
                        results = df_valid.apply(
                            lambda row: {
                                'symbol': row['symbol_key'],
                                'name': row['display_name'],
                                'comment_count': int(row['comment_count']),
                                'sentiment': round(float(row['sentiment']), 3),
                                'market_cap': round(float(row['market_cap']), 2) if not pd.isna(row['market_cap']) else 0.0
                            },
                            axis=1
                        ).tolist()
                        
                except Exception as e:
                    logger.warning(f"向量化处理失败，回退到循环方法: {e}")
                    results = []
                    for idx, row in df.iterrows():
                        try:
                            raw_symbol = str(row.get('symbol', '') or '')
                            stock_code_raw = row.get('stockCode', '')
                            stock_code = str(stock_code_raw or '')[-6:] if stock_code_raw else ''
                            symbol_key = normalize_symbol_key(raw_symbol, stock_code)
                            
                            info = stock_info.get(symbol_key, {})
                            
                            comment_count = int(row.get('commentCount') or 0)
                            if comment_count <= 0:
                                continue
                            
                            sentiment = float(row.get('sentiment') or 0.0)
                            if pd.isna(sentiment) or abs(sentiment) > 100:
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
                
                results.sort(key=lambda x: (
                    x.get('market_cap', 0.0) if x.get('market_cap', 0.0) > 0 else 0,
                    x.get('comment_count', 0.0)
                ), reverse=True)
                results = results[:100]
            
            if results:
                comment_counts = [r.get('comment_count', 0) for r in results if r.get('comment_count', 0) > 0]
                sentiments = [r.get('sentiment', 0.0) for r in results]
                
                min_comment = min(comment_counts) if comment_counts else 0
                max_comment = max(comment_counts) if comment_counts else 1
                comment_range = max_comment - min_comment if max_comment != min_comment else 1
                
                use_normalization = len(comment_counts) > 1 and comment_range > 0
                
                for result in results:
                    comment_count = result.get('comment_count', 0)
                    sentiment = result.get('sentiment', 0.0)
                    
                    if use_normalization:
                        normalized_comment = (comment_count - min_comment) / comment_range
                    else:
                        if symbol:
                            normalized_comment = comment_count if comment_count > 0 else 0.5
                        else:
                            normalized_comment = 0.5
                    
                    result['comment_count_normalized'] = round(normalized_comment, 4)
                    
                    if abs(sentiment) > 1:
                        normalized_sentiment = max(-1, min(1, sentiment / 100))
                        result['sentiment'] = round(normalized_sentiment, 3)
            
            def sanitize_data(data):
                """递归清洗数据，将 NaN/Infinity 转换为 null"""
                import math
                try:
                    np_floating = np.floating
                    np_integer = np.integer
                except AttributeError:
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
            logger.info(f"散点图数据获取完成: {len(cleaned_data)} 条数据, 总耗时 {total_time:.2f} 秒")

            if not cleaned_data:
                # T4.1: 数据为空, 降级为 Demo (不构造虚假帖子内容)
                logger.warning("股吧散点图数据为空, 降级为静态 Demo")
                return _demo_wrap({'success': True, 'data': _demo_scatter_points()})

            return {'success': True, 'data': cleaned_data, 'demo_mode': False, 'source': 'local_parquet'}

        except Exception as e:
            logger.error(f"获取散点图数据失败: {e}", exc_info=True)
            # T4.1: 异常时同样降级为 Demo
            return _demo_wrap({'success': False, 'error': str(e), 'data': _demo_scatter_points()})
