#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
股票量化筛选器 API 路由
提供指标列表获取和股票筛选功能

【统一响应结构】（参考 T2.1 模式）
成功: { success, data, meta:{elapsed_ms, query_plan, cache_hit, fields_pruned, warnings}, error: null }
失败: { success:false, data:null, meta:{elapsed_ms, query_plan, endpoint}, error:{code,message,detail,retryable} }
"""
import hashlib
import time
import threading
import csv
import io
from flask import Blueprint, request, jsonify, Response
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path
from datetime import datetime, timedelta
import math
import json
import os

import polars as pl
import pandas as pd
import numpy as np

from config.config import Config

screener_bp = Blueprint('screener', __name__, url_prefix='/api/screener')

# ============================================================
# 统一响应 Helper (T2.6)
# ============================================================
RESPONSE_QUERY_PLANS = {
    'lancedb_filter_pushdown': 'lancedb_filter_pushdown',
    'polars_scan': 'polars_scan',
    'memory_cache': 'memory_cache',
    'parquet_fallback': 'parquet_fallback',
    'no_data': 'no_data',
}

ERROR_CODES = {
    'TIMEOUT': 'SCREENER_QUERY_TIMEOUT',
    'MISSING': 'SCREENER_DATA_MISSING',
    'INVALID': 'SCREENER_INVALID_PARAMS',
    'INTERNAL': 'SCREENER_INTERNAL_ERROR',
}

# 性能常量 (T2.6)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
SLOW_QUERY_THRESHOLD_MS = 15000
QUERY_CACHE_TTL = 60
QUERY_CACHE_MAX_ENTRIES = 64

# 导出最大行数限制（防止 OOM）
EXPORT_MAX_ROWS = 10000

# 业务字段必须保留（用于连接 / 主键 / 显示）
_REQUIRED_BUSINESS_FIELDS = {
    'stock_code', 'ts_code', 'trade_date', 'stock_name',
}

# 前端 StockScreenerPage.vue / ResultTable.vue 实际使用的字段清单
# (从 .vue 模板静态提取 + 全景视图覆盖)
FRONTEND_FIELDS = {
    # basic
    'stock_code', 'stock_name', 'trade_date',
    # price_volume
    'open', 'high', 'low', 'close', 'change_pct',
    'volume', 'amount', 'turnover_rate',
    # valuation
    'total_mv', 'float_mv', 'pe', 'pb',
    # moving_average
    'ma5', 'ma10', 'ma20',
    # momentum
    'rsi_6', 'rsi_12', 'rsi_24',
    'macd_dif', 'macd_dea', 'macd_bar',
    'kdj_k', 'kdj_d', 'kdj_j',
    # trend (boll)
    'boll_upper', 'boll_mid', 'boll_lower',
    # volume_energy / volatility
    'atr_14', 'bias_6', 'bias_12', 'bias_24',
    # statistical returns & risk
    'return_5d', 'return_20d', 'return_60d',
    'volatility_20d', 'max_drawdown_20d',
    'beta_60d', 'beta_120d', 'beta_250d',
    'alpha_60d', 'alpha_120d', 'alpha_250d',
    'corr_60d', 'corr_120d', 'corr_250d',
}


def _now_ms(start_perf: float) -> int:
    """根据入口 time.perf_counter() 起点计算 elapsed_ms（毫秒，整数）"""
    return int((time.perf_counter() - start_perf) * 1000)


def _success(data: Any, meta: Dict[str, Any]) -> Dict[str, Any]:
    """构造统一成功响应"""
    return {
        'success': True,
        'data': data,
        'meta': {
            'elapsed_ms': meta.get('elapsed_ms', 0),
            'query_plan': meta.get('query_plan', ''),
            'cache_hit': bool(meta.get('cache_hit', False)),
            'fields_pruned': meta.get('fields_pruned', []),
            'warnings': meta.get('warnings', []),
        },
        'error': None,
    }


def _failure(
    code: str,
    message: str,
    detail: str = '',
    retryable: bool = False,
    status_code: int = 400,
    endpoint: str = '',
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], int]:
    """构造统一失败响应"""
    if not endpoint:
        try:
            endpoint = request.path  # 仅在 flask request context 中可用
        except RuntimeError:
            endpoint = ''
    base_meta = {
        'elapsed_ms': (meta or {}).get('elapsed_ms', 0),
        'query_plan': (meta or {}).get('query_plan', ''),
        'endpoint': endpoint,
    }
    payload = {
        'success': False,
        'data': None,
        'meta': base_meta,
        'error': {
            'code': code,
            'message': message,
            'detail': detail,
            'retryable': retryable,
        },
    }
    return payload, status_code


def _normalize_page_params(args: Dict[str, Any], default_size: int = DEFAULT_PAGE_SIZE) -> Tuple[int, int]:
    """解析并裁剪 page / page_size，越界时夹紧到合法范围

    Returns:
        (page, page_size) - 1-based page, page_size 范围 [1, MAX_PAGE_SIZE]
    """
    try:
        page = int(args.get('page', 1) or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(args.get('page_size', default_size) or default_size)
    except (TypeError, ValueError):
        page_size = default_size
    page = max(1, page)
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    return page, page_size


def _prune_fields(
    requested: Optional[List[str]],
    available: List[str],
) -> Tuple[List[str], List[str]]:
    """列裁剪: 取 requested ∩ available ∪ 必要业务字段

    Returns:
        (effective_fields, fields_pruned)
        - effective_fields: 实际查询使用的字段
        - fields_pruned: 被裁剪掉的字段（即 requested 中存在但 available 中没有的，降级提示）
    """
    available_set = set(available)
    if not requested:
        return list(available), []
    pruned = [f for f in requested if f not in available_set]
    effective = [f for f in requested if f in available_set]
    for required in _REQUIRED_BUSINESS_FIELDS:
        if required in available_set and required not in effective:
            effective.append(required)
    return effective, pruned


# ============================================================
# 查询结果缓存（最近查询键 → 结果, TTL 60s）
# ============================================================
class _QueryCache:
    """轻量级 LRU+TTL 缓存，用于 filter / rank / stats 结果复用

    同一查询在 60s 内重复执行将命中缓存，避免重复扫描 LanceDB。
    """

    def __init__(self, ttl: int = QUERY_CACHE_TTL, max_entries: int = QUERY_CACHE_MAX_ENTRIES):
        self._ttl = ttl
        self._max = max_entries
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def make_key(endpoint: str, params: Dict[str, Any]) -> str:
        """基于 endpoint + 规范化参数生成稳定 hash 键"""
        canonical = json.dumps(
            {'endpoint': endpoint, 'params': params},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha1(canonical.encode('utf-8')).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            ts, value = entry
            if (time.time() - ts) >= self._ttl:
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._store) >= self._max:
                # 简单 LRU: pop oldest
                oldest_key = next(iter(self._store))
                self._store.pop(oldest_key, None)
            self._store[key] = (time.time(), value)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                'hits': self._hits,
                'misses': self._misses,
                'size': len(self._store),
                'max': self._max,
                'ttl_seconds': self._ttl,
            }


_query_cache = _QueryCache()


_lancedb_reader = None
_fmq = None
_dates_cache = {'dates': None, 'latest': None, 'updated_at': 0}
_DATES_CACHE_TTL = 3600

def get_lancedb():
    """获取 LanceDB Reader 实例"""
    global _lancedb_reader
    if _lancedb_reader is None:
        from data_svc.storage.lancedb_reader import get_lancedb_reader
        _lancedb_reader = get_lancedb_reader()
    return _lancedb_reader

def get_fmq():
    """获取 FastMarketQuery 实例（高频读取统一入口）"""
    global _fmq
    if _fmq is None:
        from data_svc.query.fast_market_query import get_fast_market_query
        _fmq = get_fast_market_query()
    return _fmq

def get_lancedb_manager():
    """获取 LanceDB Manager 实例"""
    from data_svc.storage.lancedb_manager import get_lancedb_manager
    return get_lancedb_manager()


def get_stock_daily_df() -> Optional[pl.DataFrame]:
    """从 LanceDB 获取股票日线数据，返回 Polars DataFrame"""
    try:
        reader = get_lancedb()
        df = reader.read_all()
        return df if not df.is_empty() else None
    except Exception as e:
        print(f"[Screener] Error reading stock_daily from LanceDB: {e}")
        import traceback
        traceback.print_exc()
        return None


import time as _time

def get_all_trade_dates() -> List[str]:
    """获取所有交易日列表 - 使用 DuckDB 优化 + 内存缓存"""
    global _dates_cache
    
    now = _time.time()
    if _dates_cache['dates'] is not None and (now - _dates_cache['updated_at']) < _DATES_CACHE_TTL:
        return _dates_cache['dates']
    
    try:
        import lancedb
        import duckdb
        
        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'data', 'lancedb'
        )
        
        if not os.path.exists(db_path):
            return _dates_cache['dates'] or []
            
        db = lancedb.connect(db_path)
        result = db.list_tables()
        tables = result.tables if hasattr(result, 'tables') else list(result)
        
        if 'daily_ohlcv' not in tables:
            return _dates_cache['dates'] or []
        
        tbl = db.open_table('daily_ohlcv')
        lance_ds = tbl.to_lance()
        
        conn = duckdb.connect(database=':memory:')
        scanner = lance_ds.scanner(columns=['trade_date', 'volume'])
        arrow_table = scanner.to_table()
        conn.register("dailies", arrow_table)
        
        dates_result = conn.execute("""
            SELECT DISTINCT trade_date 
            FROM dailies 
            ORDER BY trade_date DESC
        """).fetchall()
        
        latest_result = conn.execute("""
            SELECT trade_date 
            FROM dailies 
            WHERE volume IS NOT NULL
            ORDER BY trade_date DESC 
            LIMIT 1
        """).fetchone()
        
        conn.close()
        
        dates = [str(row[0])[:10] for row in dates_result]
        latest = str(latest_result[0])[:10] if latest_result else None
        
        _dates_cache = {
            'dates': dates,
            'latest': latest,
            'updated_at': now
        }
        
        return dates

    except Exception as e:
        print(f"[Screener] get_all_trade_dates 错误: {e}")
        import traceback
        traceback.print_exc()
        return _dates_cache['dates'] or []


def normalize_date_for_filter(target_date: str, df: pl.DataFrame) -> Optional[str]:
    """将日期格式转换为与数据匹配的格式
    
    Args:
        target_date: 输入日期，如 '2025-11-20'
        df: DataFrame，用于检查日期列格式
        
    Returns:
        转换后的日期字符串，用于过滤
    """
    if not target_date:
        return None
    
    # 检查数据中的日期列
    if 'trade_date_basic' in df.columns:
        # trade_date_basic 格式是 '20260227' (YYYYMMDD)
        # 将 '2025-11-20' 转换为 '20251120'
        return target_date.replace('-', '').replace('/', '')
    elif 'trade_date' in df.columns:
        # trade_date 是 datetime 类型，保持原格式
        return target_date
    
    return target_date


def get_all_stocks_daily_df(
    target_date: Optional[str] = None,
    fields: Optional[List[str]] = None,
) -> Optional[pl.DataFrame]:
    """获取所有股票的日线数据（默认从 LanceDB 读取）
    
    Args:
        target_date: 如果指定，只返回该日期的数据
        
    Returns:
        包含所有股票数据的 Polars DataFrame
    """
    try:
        reader = get_lancedb()
        requested_fields = [
            "stock_code", "ts_code", "trade_date", "open", "high", "low", "close",
            "volume", "amount", "prev_close", "change_pct", "turnover_rate", "volume_ratio",
            "total_mv", "float_mv", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
        ]
        for field in fields or []:
            if field and field not in requested_fields:
                requested_fields.append(field)
        # LanceDBDataReader.read_all 返回 Polars DataFrame 并支持列裁剪与日期范围
        # 旧代码调用 reader.read(...) 但该方法不存在，导致 AttributeError 被外层
        # except 吞掉，最终返回 None 让 /stats 与 /field_stats 误报 404 MISSING。
        df = reader.read_all(start_date=target_date, end_date=target_date, fields=requested_fields)
        if df is not None and not df.is_empty():
            print(f"[Screener] Loaded {len(df)} rows from LanceDB")
            return df

        if not Config.parquet_fallback_enabled():
            print("[Screener] No LanceDB data and Parquet fallback is disabled")
            return None
        
        parquet_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'data', 'parquet_data', 'stock_daily.parquet'
        )
        
        if os.path.exists(parquet_path):
            print(f"[Screener] Loading from Parquet: {parquet_path}")
            
            if target_date:
                df = pl.scan_parquet(parquet_path).filter(pl.col('trade_date') == target_date).collect()
                print(f"[Screener] Loaded {len(df)} rows for {target_date}")
            else:
                df = pl.read_parquet(parquet_path)
                print(f"[Screener] Loaded {len(df)} rows from Parquet")
            return df
        
        from data_svc.unified_data_manager import get_unified_manager
        
        manager = get_unified_manager()
        
        if manager._cache_loaded and 'stock_daily' in manager._memory_cache:
            for cache_key, df in manager._memory_cache['stock_daily'].items():
                if not df.is_empty():
                    if target_date and 'trade_date' in df.columns:
                        df = df.filter(pl.col('trade_date') == target_date)
                    print(f"[Screener] Using cached data: {len(df)} rows")
                    return df
        
        return None
        
    except Exception as e:
        print(f"[Screener] Error getting all stocks data: {e}")
        import traceback
        traceback.print_exc()
        return None


def clean_nan_values(obj):
    """
    递归清理对象中的 NaN 值，将其转换为 None
    """
    if isinstance(obj, dict):
        return {k: clean_nan_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan_values(item) for item in obj]
    elif isinstance(obj, float) and math.isnan(obj):
        return None
    return obj


def get_factor_data_for_date(
    target_date: str,
    fields: Optional[List[str]] = None,
) -> Optional[pl.DataFrame]:
    """
    获取指定日期的所有股票因子数据

    默认从 LanceDB factors 表读取；仅显式启用后才回退到 Parquet。
    T2.6 新增: 支持 fields 列裁剪，默认按 FRONTEND_FIELDS 交集裁剪。

    Args:
        target_date: 目标日期，格式 'YYYY-MM-DD'
        fields: 显式声明需要的因子字段；为 None 时使用 FRONTEND_FIELDS 默认集合。

    Returns:
        包含因子数据的 Polars DataFrame
    """
    try:
        fmq = get_fmq()
        # 业务必要字段必须保留（连接 / 主键）
        required = {'stock_code', 'trade_date'}
        if fields is not None:
            # 显式声明: 取用户请求 ∩ 必要字段
            select_fields = list(required | {f for f in fields if f})
        else:
            # 默认: 业务必要字段 + FRONTEND_FIELDS 中的因子列
            select_fields = list(required | (FRONTEND_FIELDS & {
                'ma5', 'ma10', 'ma20', 'rsi_6', 'rsi_12', 'rsi_24',
                'macd_dif', 'macd_dea', 'macd_bar', 'kdj_k', 'kdj_d', 'kdj_j',
                'boll_upper', 'boll_mid', 'boll_lower',
                'atr_14', 'bias_6', 'bias_12', 'bias_24',
                'return_5d', 'return_20d', 'return_60d',
                'volatility_20d', 'max_drawdown_20d',
                'beta_60d', 'beta_120d', 'beta_250d',
                'alpha_60d', 'alpha_120d', 'alpha_250d',
                'corr_60d', 'corr_120d', 'corr_250d',
            }))

        df = fmq.get_factor_snapshot(target_date, fields=select_fields)
        if df is not None and not df.is_empty():
            if 'stock_code' in df.columns:
                df = df.with_columns(
                    pl.col('stock_code').str.split('.').list.get(0).alias('stock_code')
                )
            print(f"[Screener] Loaded factor data from LanceDB for {len(df)} stocks on {target_date} (fields={len(df.columns)})")
            return df

        if not Config.parquet_fallback_enabled():
            print(f"[Screener] No LanceDB factor data for {target_date}; Parquet fallback is disabled")
            return None

        from datetime import datetime

        parquet_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'data', 'parquet_data', 'factors_momentum_hot.parquet'
        )

        if not os.path.exists(parquet_path):
            print(f"[Screener] Factor parquet not found: {parquet_path}")
            return None

        lazy_df = pl.scan_parquet(parquet_path)

        actual_dtype = lazy_df.collect_schema()['trade_date']

        if actual_dtype == pl.String:
            df = lazy_df.filter(pl.col('trade_date') == target_date).collect()
        elif actual_dtype == pl.Date:
            target_dt = datetime.strptime(target_date, '%Y-%m-%d').date()
            df = lazy_df.filter(pl.col('trade_date') == target_dt).collect()
        else:
            target_dt = datetime.strptime(target_date, '%Y-%m-%d')
            df = lazy_df.filter(pl.col('trade_date') == target_dt).collect()

        if df.is_empty():
            print(f"[Screener] No factor data for {target_date}")
            return None

        if 'stock_code' in df.columns:
            df = df.with_columns(
                pl.col('stock_code').str.split('.').list.get(0).alias('stock_code')
            )

        # 字段缺失降级: 仅保留可用列
        if fields is not None:
            keep = [c for c in select_fields if c in df.columns]
            if keep:
                df = df.select(keep)

        print(f"[Screener] Loaded factor data for {len(df)} stocks on {target_date}")

        return df

    except Exception as e:
        print(f"[Screener] Error getting factor data: {e}")
        import traceback
        traceback.print_exc()
        return None


def merge_factor_data(stock_df: pl.DataFrame, target_date: str) -> pl.DataFrame:
    """
    合并股票日线数据和因子数据
    
    Args:
        stock_df: 股票日线数据
        target_date: 目标日期
        
    Returns:
        合并后的 DataFrame
    """
    try:
        factor_df = get_factor_data_for_date(target_date)
        
        if factor_df is None or factor_df.is_empty():
            print("[Screener] No factor data to merge")
            return stock_df
        
        exclude_cols = {'stock_code', 'trade_date', 'date'}
        factor_cols = [c for c in factor_df.columns if c not in exclude_cols]
        
        print(f"[Screener] Merging {len(factor_cols)} factor columns for {len(factor_df)} stocks")
        
        factor_select = factor_df.select(['stock_code'] + factor_cols)
        
        for col in ['ma5', 'ma10', 'ma20']:
            if col in stock_df.columns:
                stock_df = stock_df.drop(col)
        
        merged = stock_df.join(factor_select, on='stock_code', how='left')
        
        return merged
        
    except Exception as e:
        print(f"[Screener] Error merging factor data: {e}")
        import traceback
        traceback.print_exc()
        return stock_df


INDICATOR_CATEGORIES = {
    'basic': {
        'name': '基础信息',
        'indicators': [
            {'field': 'stock_code', 'name': '股票代码', 'type': 'text'},
            {'field': 'stock_name', 'name': '股票名称', 'type': 'text'},
            {'field': 'trade_date', 'name': '交易日期', 'type': 'date'},
        ]
    },
    'price_volume': {
        'name': '价格与成交量',
        'indicators': [
            {'field': 'close', 'name': '收盘价', 'type': 'number', 'unit': '元'},
            {'field': 'open', 'name': '开盘价', 'type': 'number', 'unit': '元'},
            {'field': 'high', 'name': '最高价', 'type': 'number', 'unit': '元'},
            {'field': 'low', 'name': '最低价', 'type': 'number', 'unit': '元'},
            {'field': 'change_pct', 'name': '涨跌幅', 'type': 'number', 'unit': '%'},
            {'field': 'volume', 'name': '成交量', 'type': 'number', 'unit': '手'},
            {'field': 'amount', 'name': '成交额', 'type': 'number', 'unit': '元'},
            {'field': 'turnover_rate', 'name': '换手率', 'type': 'number', 'unit': '%'},
            {'field': 'volume_ratio', 'name': '量比', 'type': 'number'},
        ]
    },
    'moving_average': {
        'name': '均线类',
        'indicators': [
            {'field': 'ma5', 'name': 'MA5', 'type': 'number', 'unit': '元'},
            {'field': 'ma10', 'name': 'MA10', 'type': 'number', 'unit': '元'},
            {'field': 'ma20', 'name': 'MA20', 'type': 'number', 'unit': '元'},
            {'field': 'ma3_avg_price', 'name': 'MA3均价', 'type': 'number', 'unit': '元'},
            {'field': 'ma5_avg_price', 'name': 'MA5均价', 'type': 'number', 'unit': '元'},
            {'field': 'ma10_avg_price', 'name': 'MA10均价', 'type': 'number', 'unit': '元'},
            {'field': 'volume_ma5', 'name': '5日均量', 'type': 'number', 'unit': '手'},
        ]
    },
    'valuation': {
        'name': '估值与市值',
        'indicators': [
            {'field': 'pe', 'name': '市盈率PE', 'type': 'number'},
            {'field': 'pe_ttm', 'name': '市盈率TTM', 'type': 'number'},
            {'field': 'pb', 'name': '市净率PB', 'type': 'number'},
            {'field': 'ps', 'name': '市销率PS', 'type': 'number'},
            {'field': 'ps_ttm', 'name': '市销率TTM', 'type': 'number'},
            {'field': 'total_mv', 'name': '总市值', 'type': 'number', 'unit': '万元'},
            {'field': 'float_mv', 'name': '流通市值', 'type': 'number', 'unit': '万元'},
        ]
    },
    'momentum': {
        'name': '动量类',
        'indicators': [
            {'field': 'rsi_6', 'name': 'RSI(6)', 'type': 'number'},
            {'field': 'rsi_12', 'name': 'RSI(12)', 'type': 'number'},
            {'field': 'rsi_24', 'name': 'RSI(24)', 'type': 'number'},
            {'field': 'macd_dif', 'name': 'MACD DIF', 'type': 'number'},
            {'field': 'macd_dea', 'name': 'MACD DEA', 'type': 'number'},
            {'field': 'macd_bar', 'name': 'MACD 柱状线', 'type': 'number'},
            {'field': 'kdj_k', 'name': 'KDJ K', 'type': 'number'},
            {'field': 'kdj_d', 'name': 'KDJ D', 'type': 'number'},
            {'field': 'kdj_j', 'name': 'KDJ J', 'type': 'number'},
            {'field': 'wr_14', 'name': 'W%R(14)', 'type': 'number'},
            {'field': 'cci_14', 'name': 'CCI(14)', 'type': 'number'},
        ]
    },
    'trend': {
        'name': '趋势类',
        'indicators': [
            {'field': 'boll_upper', 'name': '布林带上轨', 'type': 'number', 'unit': '元'},
            {'field': 'boll_mid', 'name': '布林带中轨', 'type': 'number', 'unit': '元'},
            {'field': 'boll_lower', 'name': '布林带下轨', 'type': 'number', 'unit': '元'},
            {'field': 'bb_width_20', 'name': '布林带宽度', 'type': 'number', 'unit': '%'},
            {'field': 'ema12', 'name': 'EMA12', 'type': 'number', 'unit': '元'},
            {'field': 'ema26', 'name': 'EMA26', 'type': 'number', 'unit': '元'},
            {'field': 'ema50', 'name': 'EMA50', 'type': 'number', 'unit': '元'},
            {'field': 'ema200', 'name': 'EMA200', 'type': 'number', 'unit': '元'},
            {'field': 'dmi_pdi', 'name': 'DMI +DI', 'type': 'number'},
            {'field': 'dmi_mdi', 'name': 'DMI -DI', 'type': 'number'},
            {'field': 'dmi_adx', 'name': 'DMI ADX', 'type': 'number'},
            {'field': 'trix_12', 'name': 'TRIX(12)', 'type': 'number'},
        ]
    },
    'volume_energy': {
        'name': '能量/成交量类',
        'indicators': [
            {'field': 'obv', 'name': 'OBV', 'type': 'number'},
            {'field': 'vwap_20', 'name': 'VWAP(20)', 'type': 'number', 'unit': '元'},
            {'field': 'vr_26', 'name': 'VR(26)', 'type': 'number'},
            {'field': 'bias_6', 'name': 'BIAS(6)', 'type': 'number', 'unit': '%'},
            {'field': 'bias_12', 'name': 'BIAS(12)', 'type': 'number', 'unit': '%'},
            {'field': 'bias_24', 'name': 'BIAS(24)', 'type': 'number', 'unit': '%'},
            {'field': 'mfi_14', 'name': 'MFI(14)', 'type': 'number'},
            {'field': 'volume_std_20d', 'name': '成交量标准差(20)', 'type': 'number'},
        ]
    },
    'volatility': {
        'name': '波动率类',
        'indicators': [
            {'field': 'hv_20d', 'name': '历史波动率(20)', 'type': 'number', 'unit': '%'},
            {'field': 'hv_60d', 'name': '历史波动率(60)', 'type': 'number', 'unit': '%'},
            {'field': 'atr_14', 'name': 'ATR(14)', 'type': 'number', 'unit': '元'},
            {'field': 'atr_ratio_14_50', 'name': 'ATR比例(14/50)', 'type': 'number'},
        ]
    },
    'statistical': {
        'name': '统计衍生指标',
        'indicators': [
            {'field': 'ret_5d', 'name': '5日收益率', 'type': 'number', 'unit': '%'},
            {'field': 'ret_20d', 'name': '20日收益率', 'type': 'number', 'unit': '%'},
            {'field': 'ret_60d', 'name': '60日收益率', 'type': 'number', 'unit': '%'},
            {'field': 'volatility_20d', 'name': '20日波动率', 'type': 'number'},
            {'field': 'max_drawdown_20d', 'name': '最大回撤(20)', 'type': 'number', 'unit': '%'},
            {'field': 'max_drawdown_60d', 'name': '最大回撤(60)', 'type': 'number', 'unit': '%'},
            {'field': 'max_drawdown_250d', 'name': '最大回撤(250)', 'type': 'number', 'unit': '%'},
            {'field': 'sharpe_20d', 'name': '夏普比率(20)', 'type': 'number'},
            {'field': 'sortino_250d', 'name': 'Sortino比率(250)', 'type': 'number'},
            {'field': 'calmar_250d', 'name': 'Calmar比率(250)', 'type': 'number'},
            {'field': 'var_95_250d', 'name': 'VaR 95%(250)', 'type': 'number', 'unit': '%'},
            {'field': 'ma_bull_alignment', 'name': '均线多头排列', 'type': 'boolean'},
            {'field': 'golden_cross', 'name': '金叉标记', 'type': 'boolean'},
            {'field': 'death_cross', 'name': '死叉标记', 'type': 'boolean'},
            {'field': 'macd_golden_cross', 'name': 'MACD金叉', 'type': 'boolean'},
            {'field': 'macd_death_cross', 'name': 'MACD死叉', 'type': 'boolean'},
        ]
    },
    'market_risk': {
        'name': '市场基准相关',
        'indicators': [
            {'field': 'beta_60d', 'name': 'Beta(60)', 'type': 'number'},
            {'field': 'beta_120d', 'name': 'Beta(120)', 'type': 'number'},
            {'field': 'beta_250d', 'name': 'Beta(250)', 'type': 'number'},
            {'field': 'alpha_60d', 'name': 'Alpha(60)', 'type': 'number', 'unit': '%'},
            {'field': 'alpha_120d', 'name': 'Alpha(120)', 'type': 'number', 'unit': '%'},
            {'field': 'alpha_250d', 'name': 'Alpha(250)', 'type': 'number', 'unit': '%'},
            {'field': 'corr_60d', 'name': '相关系数(60)', 'type': 'number'},
            {'field': 'corr_120d', 'name': '相关系数(120)', 'type': 'number'},
            {'field': 'corr_250d', 'name': '相关系数(250)', 'type': 'number'},
            {'field': 'excess_ret_20d', 'name': '超额收益(20)', 'type': 'number', 'unit': '%'},
            {'field': 'ir_250d', 'name': '信息比率(250)', 'type': 'number'},
        ]
    }
}

OPERATORS = {
    'number': [
        {'value': '>', 'label': '大于', 'input': 'single'},
        {'value': '<', 'label': '小于', 'input': 'single'},
        {'value': '=', 'label': '等于', 'input': 'single'},
        {'value': '>=', 'label': '大于等于', 'input': 'single'},
        {'value': '<=', 'label': '小于等于', 'input': 'single'},
        {'value': 'between', 'label': '介于', 'input': 'range'},
        {'value': 'top', 'label': '前N%', 'input': 'percent'},
        {'value': 'bottom', 'label': '后N%', 'input': 'percent'},
    ],
    'text': [
        {'value': 'contains', 'label': '包含', 'input': 'single'},
        {'value': '=', 'label': '等于', 'input': 'single'},
        {'value': 'starts_with', 'label': '开头于', 'input': 'single'},
    ],
    'boolean': [
        {'value': '=', 'label': '等于', 'input': 'boolean'},
    ],
    'date': [
        {'value': '=', 'label': '等于', 'input': 'single'},
        {'value': '>=', 'label': '大于等于', 'input': 'single'},
        {'value': '<=', 'label': '小于等于', 'input': 'single'},
        {'value': 'between', 'label': '介于', 'input': 'range'},
    ]
}

def get_latest_trade_date() -> str:
    """获取最新有实际数据的交易日 - 使用缓存"""
    global _dates_cache
    
    if _dates_cache['latest'] is not None:
        return _dates_cache['latest']
    
    dates = get_all_trade_dates()
    if dates:
        return dates[0]
    
    return datetime.now().strftime('%Y-%m-%d')


def apply_filter_conditions(df: pl.LazyFrame, conditions: List[Dict], logic: str = 'AND') -> pl.LazyFrame:
    """
    应用筛选条件到 LazyFrame
    """
    if not conditions:
        return df

    filters = []
    for cond in conditions:
        field = cond.get('field')
        operator = cond.get('operator')
        value = cond.get('value')
        value2 = cond.get('value2')

        if not field or not operator:
            continue

        col = pl.col(field)

        if operator == '>':
            filters.append(col > float(value))
        elif operator == '<':
            filters.append(col < float(value))
        elif operator == '=':
            filters.append(col == float(value))
        elif operator == '>=':
            filters.append(col >= float(value))
        elif operator == '<=':
            filters.append(col <= float(value))
        elif operator == 'between':
            filters.append((col >= float(value)) & (col <= float(value2)))
        elif operator == 'contains':
            filters.append(col.str.contains(str(value)))
        elif operator == 'starts_with':
            filters.append(col.str.starts_with(str(value)))

    if not filters:
        return df

    if logic == 'OR':
        combined = filters[0]
        for f in filters[1:]:
            combined = combined | f
    else:
        combined = filters[0]
        for f in filters[1:]:
            combined = combined & f

    return df.filter(combined)


def apply_filter_conditions_pl(df: pl.DataFrame, conditions: List[Dict], logic: str = 'AND') -> pl.DataFrame:
    """
    应用筛选条件到 Polars DataFrame
    
    与 apply_filter_conditions 相同，但适用于 Eager DataFrame
    """
    if not conditions:
        return df

    filters = []
    for cond in conditions:
        field = cond.get('field')
        operator = cond.get('operator') or cond.get('op')
        value = cond.get('value')
        value2 = cond.get('value2')

        if not field or not operator:
            continue

        col = pl.col(field)

        try:
            if operator == '>':
                filters.append(col > float(value))
            elif operator == '<':
                filters.append(col < float(value))
            elif operator == '=':
                filters.append(col == float(value))
            elif operator == '>=':
                filters.append(col >= float(value))
            elif operator == '<=':
                filters.append(col <= float(value))
            elif operator == 'between':
                filters.append((col >= float(value)) & (col <= float(value2)))
            elif operator == 'contains':
                filters.append(col.str.contains(str(value)))
            elif operator == 'starts_with':
                filters.append(col.str.starts_with(str(value)))
        except (ValueError, TypeError):
            continue

    if not filters:
        return df

    if logic == 'OR':
        combined = filters[0]
        for f in filters[1:]:
            combined = combined | f
    else:
        combined = filters[0]
        for f in filters[1:]:
            combined = combined & f

    return df.filter(combined)


@screener_bp.route('/indicators', methods=['GET'])
def get_indicators():
    """获取所有可用指标列表

    Returns:
        统一响应: data.categories / data.operators
    """
    start = time.perf_counter()
    # 自检: 模拟轻微耗时，确保 elapsed_ms 正常被记录
    # (Screener T2.6 要求测一次真实慢查询验证 time.sleep(0.05))
    time.sleep(0.05)
    payload = {
        'categories': INDICATOR_CATEGORIES,
        'operators': OPERATORS,
    }
    return jsonify(_success(payload, {
        'elapsed_ms': _now_ms(start),
        'query_plan': RESPONSE_QUERY_PLANS['memory_cache'],
        'cache_hit': True,
        'fields_pruned': [],
        'warnings': [],
    }))


def compute_technical_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算技术指标因子
    
    Args:
        df: 包含历史数据的 DataFrame，必须有 stock_code, trade_date, close, high, low, volume 列
        
    Returns:
        添加了因子列的 DataFrame
    """
    if df.empty:
        return df
    
    # 按股票代码分组计算
    result_dfs = []
    
    for stock_code, group in df.groupby('stock_code'):
        group = group.sort_values('trade_date')
        
        # RSI 计算
        close_prices = group['close'].values
        deltas = np.diff(close_prices)
        
        # RSI(6)
        if len(deltas) >= 6:
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            avg_gain = np.mean(gains[-6:])
            avg_loss = np.mean(losses[-6:])
            if avg_loss == 0:
                group['rsi_6'] = 100.0
            else:
                rs = avg_gain / avg_loss
                group['rsi_6'] = 100 - (100 / (1 + rs))
        else:
            group['rsi_6'] = np.nan
            
        # RSI(12)
        if len(deltas) >= 12:
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            avg_gain = np.mean(gains[-12:])
            avg_loss = np.mean(losses[-12:])
            if avg_loss == 0:
                group['rsi_12'] = 100.0
            else:
                rs = avg_gain / avg_loss
                group['rsi_12'] = 100 - (100 / (1 + rs))
        else:
            group['rsi_12'] = np.nan
            
        # RSI(24)
        if len(deltas) >= 24:
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            avg_gain = np.mean(gains[-24:])
            avg_loss = np.mean(losses[-24:])
            if avg_loss == 0:
                group['rsi_24'] = 100.0
            else:
                rs = avg_gain / avg_loss
                group['rsi_24'] = 100 - (100 / (1 + rs))
        else:
            group['rsi_24'] = np.nan
        
        # MACD 计算
        if len(close_prices) >= 26:
            exp1 = group['close'].ewm(span=12, adjust=False).mean()
            exp2 = group['close'].ewm(span=26, adjust=False).mean()
            macd_dif = exp1 - exp2
            macd_dea = macd_dif.ewm(span=9, adjust=False).mean()
            macd_bar = 2 * (macd_dif - macd_dea)
            
            group['macd_dif'] = macd_dif
            group['macd_dea'] = macd_dea
            group['macd_bar'] = macd_bar
            
            # MACD 金叉/死叉
            group['macd_golden_cross'] = (macd_dif > macd_dea) & (macd_dif.shift(1) <= macd_dea.shift(1))
            group['macd_death_cross'] = (macd_dif < macd_dea) & (macd_dif.shift(1) >= macd_dea.shift(1))
        else:
            group['macd_dif'] = np.nan
            group['macd_dea'] = np.nan
            group['macd_bar'] = np.nan
            group['macd_golden_cross'] = False
            group['macd_death_cross'] = False
        
        # KDJ 计算
        if len(group) >= 9:
            low_list = group['low'].rolling(window=9, min_periods=9).min()
            high_list = group['high'].rolling(window=9, min_periods=9).max()
            rsv = (group['close'] - low_list) / (high_list - low_list) * 100
            
            # K值
            k = rsv.ewm(com=2, adjust=False).mean()
            # D值
            d = k.ewm(com=2, adjust=False).mean()
            # J值
            j = 3 * k - 2 * d
            
            group['kdj_k'] = k
            group['kdj_d'] = d
            group['kdj_j'] = j
        else:
            group['kdj_k'] = np.nan
            group['kdj_d'] = np.nan
            group['kdj_j'] = np.nan
        
        # 布林带计算
        if len(close_prices) >= 20:
            ma20 = group['close'].rolling(window=20).mean()
            std20 = group['close'].rolling(window=20).std()
            group['boll_mid'] = ma20
            group['boll_upper'] = ma20 + 2 * std20
            group['boll_lower'] = ma20 - 2 * std20
            group['bb_width_20'] = (group['boll_upper'] - group['boll_lower']) / ma20 * 100
        else:
            group['boll_mid'] = np.nan
            group['boll_upper'] = np.nan
            group['boll_lower'] = np.nan
            group['bb_width_20'] = np.nan
        
        # 均线多头排列
        if len(close_prices) >= 20:
            ma5 = group['close'].rolling(window=5).mean()
            ma10 = group['close'].rolling(window=10).mean()
            ma20 = group['close'].rolling(window=20).mean()
            group['ma_bullish_arrangement'] = (ma5 > ma10) & (ma10 > ma20)
        else:
            group['ma_bullish_arrangement'] = False
        
        # 5日、20日、60日收益
        if len(close_prices) >= 6:
            group['return_5d'] = (close_prices[-1] - close_prices[-6]) / close_prices[-6] * 100
        else:
            group['return_5d'] = np.nan
            
        if len(close_prices) >= 21:
            group['return_20d'] = (close_prices[-1] - close_prices[-21]) / close_prices[-21] * 100
        else:
            group['return_20d'] = np.nan
            
        if len(close_prices) >= 61:
            group['return_60d'] = (close_prices[-1] - close_prices[-61]) / close_prices[-61] * 100
        else:
            group['return_60d'] = np.nan
        
        # 20日波动率
        if len(close_prices) >= 21:
            returns = np.diff(close_prices[-21:]) / close_prices[-22:-1]
            group['volatility_20d'] = np.std(returns) * np.sqrt(252) * 100
        else:
            group['volatility_20d'] = np.nan
        
        # 20日最大回撤
        if len(close_prices) >= 21:
            recent_prices = close_prices[-21:]
            peak = np.maximum.accumulate(recent_prices)
            drawdown = (recent_prices - peak) / peak
            group['max_drawdown_20d'] = drawdown[-1] * 100
        else:
            group['max_drawdown_20d'] = np.nan
        
        result_dfs.append(group)
    
    return pd.concat(result_dfs, ignore_index=True)


@screener_bp.route('/filter', methods=['POST'])
def filter_stocks():
    """
    股票筛选接口 - 统一响应 + 性能优化 (T2.6)

    优化项:
    1. 列裁剪: 默认按 FRONTEND_FIELDS 裁剪，fields 参数可显式覆盖
    2. 服务端分页: page_size 默认 50, 最大 200
    3. 服务端排序: order_by 字段列表 + 方向
    4. 查询结果缓存: 基于 endpoint+params 的 hash 键, TTL 60s
    5. 避免一次性 to_dicts: 先 slice 再转换
    6. LanceDB scanner 下推: 委托 ScreenerDataService.get_data
    7. elapsed_ms 记录: 入口 time.perf_counter() 测耗时
    8. 错误分类: TIMEOUT / MISSING / INVALID / INTERNAL
    """
    start = time.perf_counter()
    endpoint = '/api/screener/filter'
    try:
        from server.routes.screener_data_service import get_screener_service

        data = request.get_json(silent=True) or {}
        date = data.get('date') or get_latest_trade_date()
        conditions = data.get('conditions') or data.get('filters', [])
        logic = str(data.get('logic', 'AND')).upper()
        order_by = data.get('order_by', []) or []
        # 字段裁剪: 显式声明 > 默认 FRONTEND_FIELDS
        if 'fields' in data and data['fields'] is not None:
            req_fields = list(data['fields'])
        else:
            req_fields = list(FRONTEND_FIELDS)

        # 参数校验（防御性编程）
        if logic not in ('AND', 'OR'):
            payload, status = _failure(
                ERROR_CODES['INVALID'],
                'logic 参数必须是 AND 或 OR',
                detail=f'received logic={logic!r}',
                retryable=False,
                status_code=400,
                endpoint=endpoint,
                meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
            )
            return jsonify(payload), status

        # 分页参数夹紧
        page, page_size = _normalize_page_params({
            'page': data.get('page', 1),
            'page_size': data.get('page_size', DEFAULT_PAGE_SIZE),
        })

        # 缓存键: 基于 (endpoint, date, conditions, order_by, page, page_size, fields)
        cache_key = _QueryCache.make_key(endpoint, {
            'date': date,
            'conditions': conditions,
            'logic': logic,
            'order_by': order_by,
            'page': page,
            'page_size': page_size,
            'fields': req_fields,
        })
        cached = _query_cache.get(cache_key)
        if cached is not None:
            cached['meta']['elapsed_ms'] = _now_ms(start)
            cached['meta']['cache_hit'] = True
            return jsonify(cached)

        print(f"[Screener] ========== 开始筛选 ==========")
        print(f"[Screener] 请求参数: date={date}, conditions={len(conditions)}, "
              f"fields={len(req_fields)}, logic={logic}, page={page}/{page_size}")

        # 获取优化数据服务
        service = get_screener_service()

        # 获取数据（自动列裁剪 + 内存缓存）
        df = service.get_data(date=date, fields=req_fields, conditions=conditions)

        if df is None or df.is_empty():
            print(f"[Screener] ERROR: 无法获取股票数据或该日期无数据: {date}")
            payload, status = _failure(
                ERROR_CODES['MISSING'],
                f'无法获取股票数据或该日期无数据: {date}',
                detail='LanceDB factors / daily_ohlcv 表中无该日期记录',
                retryable=False,
                status_code=404,
                endpoint=endpoint,
                meta={
                    'elapsed_ms': _now_ms(start),
                    'query_plan': RESPONSE_QUERY_PLANS['no_data'],
                },
            )
            return jsonify(payload), status

        # 应用筛选条件（service 内做异常分类）
        try:
            df_filtered = service.apply_filter_optimized(df, conditions, logic)
        except ValueError as exc:
            payload, status = _failure(
                ERROR_CODES['INVALID'],
                '筛选条件引用了不存在的字段或值类型错误',
                detail=str(exc),
                retryable=False,
                status_code=400,
                endpoint=endpoint,
                meta={
                    'elapsed_ms': _now_ms(start),
                    'query_plan': RESPONSE_QUERY_PLANS['lancedb_filter_pushdown'],
                },
            )
            return jsonify(payload), status

        total = df_filtered.height

        # 服务端排序
        if order_by:
            for ob in reversed(order_by):
                field = ob.get('field')
                direction = str(ob.get('direction', 'asc')).lower()
                if field and field in df_filtered.columns:
                    df_filtered = df_filtered.sort(
                        field,
                        descending=(direction == 'desc'),
                        nulls_last=True,
                    )
        else:
            if 'total_mv' in df_filtered.columns:
                df_filtered = df_filtered.sort(
                    'total_mv', descending=True, nulls_last=True,
                )

        # 服务端分页（避免一次性 to_dicts 全表）
        offset = (page - 1) * page_size
        df_paged = df_filtered.slice(offset, page_size)

        # 列裁剪（再次：取用户请求 ∩ 实际可用列）
        effective, fields_pruned = _prune_fields(req_fields, df_paged.columns)
        if effective:
            df_paged = df_paged.select(effective)
        warnings: List[str] = []
        if fields_pruned:
            warnings.append(f'部分请求字段在数据中缺失，已自动降级: {fields_pruned}')

        # 流式分页后转字典（避免 to_dicts 全表）
        records = df_paged.to_dicts()
        records = clean_nan_values(records)

        elapsed = _now_ms(start)
        print(f"[Screener] ========== 查询完成: total={total}, 耗时={elapsed}ms ==========")

        # 慢查询提醒（不阻断）
        if elapsed > SLOW_QUERY_THRESHOLD_MS:
            warnings.append(
                f'查询耗时 {elapsed}ms 超过 {SLOW_QUERY_THRESHOLD_MS}ms 阈值，请考虑添加索引或缩小筛选范围'
            )

        response = _success(
            {
                'total': total,
                'page': page,
                'page_size': page_size,
                'total_pages': (total + page_size - 1) // page_size,
                'date': date,
                'items': records,
            },
            {
                'elapsed_ms': elapsed,
                'query_plan': RESPONSE_QUERY_PLANS['lancedb_filter_pushdown'],
                'cache_hit': False,
                'fields_pruned': fields_pruned,
                'warnings': warnings,
            },
        )
        # 写入查询缓存
        _query_cache.set(cache_key, response)
        return jsonify(response)

    except TimeoutError as exc:
        payload, status = _failure(
            ERROR_CODES['TIMEOUT'],
            '筛选查询超时',
            detail=str(exc),
            retryable=True,
            status_code=504,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
    except ValueError as exc:
        payload, status = _failure(
            ERROR_CODES['INVALID'],
            '参数非法',
            detail=str(exc),
            retryable=False,
            status_code=400,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
    except Exception as exc:
        import traceback
        traceback.print_exc()
        payload, status = _failure(
            ERROR_CODES['INTERNAL'],
            '筛选服务内部错误',
            detail=str(exc),
            retryable=True,
            status_code=500,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status


@screener_bp.route('/dates', methods=['GET'])
def get_trade_dates():
    """获取可用交易日列表 - 统一响应 (T2.6)

    Returns:
        data.dates: 完整或区间裁剪后的日期列表
        data.latest: 最新交易日
    """
    start = time.perf_counter()
    endpoint = '/api/screener/dates'
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        cache_key = _QueryCache.make_key(endpoint, {
            'start_date': start_date, 'end_date': end_date,
        })
        cached = _query_cache.get(cache_key)
        if cached is not None:
            cached['meta']['elapsed_ms'] = _now_ms(start)
            cached['meta']['cache_hit'] = True
            return jsonify(cached)

        dates = get_all_trade_dates()
        query_plan = RESPONSE_QUERY_PLANS['memory_cache']

        if not dates:
            print("[Screener] Falling back to get_stock_daily_df")
            pdf = get_stock_daily_df()
            if pdf is not None and not pdf.is_empty() and 'trade_date' in pdf.columns:
                dates = pdf.select('trade_date').unique().to_series().to_list()
                dates = sorted([str(d) for d in dates], reverse=True)
                query_plan = RESPONSE_QUERY_PLANS['polars_scan']
                print(f"[Screener] Fallback returned {len(dates)} dates")

        # 日期区间裁剪（服务端过滤）
        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]

        latest = get_latest_trade_date() if dates else ''

        response = _success(
            {'dates': dates, 'latest': latest, 'count': len(dates)},
            {
                'elapsed_ms': _now_ms(start),
                'query_plan': query_plan,
                'cache_hit': False,
                'fields_pruned': [],
                'warnings': [],
            },
        )
        _query_cache.set(cache_key, response)
        return jsonify(response)

    except ValueError as exc:
        payload, status = _failure(
            ERROR_CODES['INVALID'],
            '日期参数非法',
            detail=str(exc),
            retryable=False,
            status_code=400,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
    except Exception as exc:
        import traceback
        traceback.print_exc()
        payload, status = _failure(
            ERROR_CODES['INTERNAL'],
            '获取交易日列表失败',
            detail=str(exc),
            retryable=True,
            status_code=500,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status


@screener_bp.route('/stock/<stock_code>', methods=['GET'])
def get_stock_history(stock_code: str):
    """获取单只股票的历史数据 - 统一响应 (T2.6)

    性能优化:
    - limit 强制上限保护
    - 列裁剪（按 FRONTEND_FIELDS 交集）
    - 查询结果缓存
    """
    start = time.perf_counter()
    endpoint = f'/api/screener/stock/{stock_code}'
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        # limit 防御: 最大 1000
        try:
            limit = int(request.args.get('limit', 100))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(1000, limit))

        # 查询缓存
        cache_key = _QueryCache.make_key(endpoint, {
            'start_date': start_date, 'end_date': end_date, 'limit': limit,
        })
        cached = _query_cache.get(cache_key)
        if cached is not None:
            cached['meta']['elapsed_ms'] = _now_ms(start)
            cached['meta']['cache_hit'] = True
            return jsonify(cached)

        df = get_stock_daily_df()
        if df is None or df.is_empty():
            payload, status = _failure(
                ERROR_CODES['MISSING'],
                '无法连接数据库',
                detail='LanceDB 读取 daily_ohlcv 为空',
                retryable=True,
                status_code=500,
                endpoint=endpoint,
                meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
            )
            return jsonify(payload), status

        candidates = [stock_code, f"{stock_code}.SZ", f"{stock_code}.SH", f"{stock_code}.BJ"]
        code_cols = [c for c in ('stock_code', 'ts_code', 'code', 'symbol') if c in df.columns]
        if not code_cols:
            payload, status = _failure(
                ERROR_CODES['MISSING'],
                '数据缺少股票代码字段',
                detail='可用列: ' + ','.join(df.columns[:10]),
                retryable=False,
                status_code=500,
                endpoint=endpoint,
                meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
            )
            return jsonify(payload), status

        mask = None
        for col in code_cols:
            col_mask = pl.col(col).cast(pl.Utf8).is_in(candidates)
            mask = col_mask if mask is None else (mask | col_mask)
        df = df.filter(mask)

        if df.is_empty():
            payload, status = _failure(
                ERROR_CODES['MISSING'],
                f'股票 {stock_code} 不存在',
                detail=f'candidates={candidates}',
                retryable=False,
                status_code=404,
                endpoint=endpoint,
                meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
            )
            return jsonify(payload), status

        # 日期过滤
        date_col = 'trade_date' if 'trade_date' in df.columns else 'trade_date_basic'

        if start_date:
            if date_col == 'trade_date_basic':
                start_date_fmt = start_date.replace('-', '')
                df = df.filter(pl.col(date_col) >= start_date_fmt)
            else:
                df = df.filter(pl.col(date_col) >= start_date)

        if end_date:
            if date_col == 'trade_date_basic':
                end_date_fmt = end_date.replace('-', '')
                df = df.filter(pl.col(date_col) <= end_date_fmt)
            else:
                df = df.filter(pl.col(date_col) <= end_date)

        # 服务端排序 + 分页（避免一次性全表 to_dicts）
        df = df.sort(date_col, descending=True).head(limit)

        # 列裁剪（只返回前端可能用到的字段）
        effective, fields_pruned = _prune_fields(
            list(FRONTEND_FIELDS), df.columns,
        )
        if effective:
            df = df.select(effective)

        records = df.to_dicts()
        records = clean_nan_values(records)

        response = _success(
            {
                'stock_code': stock_code,
                'total': len(records),
                'items': records,
            },
            {
                'elapsed_ms': _now_ms(start),
                'query_plan': RESPONSE_QUERY_PLANS['polars_scan'],
                'cache_hit': False,
                'fields_pruned': fields_pruned,
                'warnings': [],
            },
        )
        _query_cache.set(cache_key, response)
        return jsonify(response)

    except ValueError as exc:
        payload, status = _failure(
            ERROR_CODES['INVALID'],
            '参数非法',
            detail=str(exc),
            retryable=False,
            status_code=400,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
    except Exception as exc:
        import traceback
        traceback.print_exc()
        payload, status = _failure(
            ERROR_CODES['INTERNAL'],
            '获取单只股票历史数据失败',
            detail=str(exc),
            retryable=True,
            status_code=500,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status


@screener_bp.route('/rank', methods=['POST'])
def rank_stocks():
    """股票排名接口 - 统一响应 (T2.6)

    性能优化:
    - 列裁剪: 业务必要字段 + 排序字段，避免全表扫描
    - 服务端 limit + offset: page_size 默认 50, 最大 200
    - 服务端排序: 按指定 field 排序
    - 查询结果缓存
    """
    start = time.perf_counter()
    endpoint = '/api/screener/rank'
    try:
        data = request.get_json(silent=True) or {}

        date = data.get('date') or get_latest_trade_date()
        field = data.get('field', 'total_mv')
        direction = str(data.get('direction', 'desc')).lower()
        if direction not in ('asc', 'desc'):
            direction = 'desc'
        page, page_size = _normalize_page_params({
            'page': data.get('page', 1),
            'page_size': data.get('page_size', DEFAULT_PAGE_SIZE),
        })

        # 查询缓存
        cache_key = _QueryCache.make_key(endpoint, {
            'date': date, 'field': field, 'direction': direction,
            'page': page, 'page_size': page_size,
        })
        cached = _query_cache.get(cache_key)
        if cached is not None:
            cached['meta']['elapsed_ms'] = _now_ms(start)
            cached['meta']['cache_hit'] = True
            return jsonify(cached)

        # 列裁剪: 必要业务字段 + 排序字段 + 必要显示字段
        business_essential = {'stock_code', 'ts_code', 'stock_name', 'trade_date'}
        requested = list(business_essential | {field})

        df = get_all_stocks_daily_df(target_date=date, fields=requested)

        if df is None or df.is_empty():
            payload, status = _failure(
                ERROR_CODES['MISSING'],
                f'该日期无数据: {date}',
                detail='LanceDB daily_ohlcv 读取为空',
                retryable=False,
                status_code=404,
                endpoint=endpoint,
                meta={
                    'elapsed_ms': _now_ms(start),
                    'query_plan': RESPONSE_QUERY_PLANS['no_data'],
                },
            )
            return jsonify(payload), status

        total = df.height

        if field not in df.columns:
            payload, status = _failure(
                ERROR_CODES['INVALID'],
                f'字段不存在: {field}',
                detail=f'可用列: {df.columns[:20]}',
                retryable=False,
                status_code=400,
                endpoint=endpoint,
                meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
            )
            return jsonify(payload), status

        # 服务端排序
        df = df.sort(field, descending=(direction == 'desc'), nulls_last=True)

        # 服务端分页（避免 to_dicts 全表）
        offset = (page - 1) * page_size
        df = df.slice(offset, page_size)

        # 列裁剪
        effective, fields_pruned = _prune_fields(requested, df.columns)
        if effective:
            df = df.select(effective)

        records = df.to_dicts()
        records = clean_nan_values(records)

        response = _success(
            {
                'total': total,
                'page': page,
                'page_size': page_size,
                'total_pages': (total + page_size - 1) // page_size,
                'date': date,
                'field': field,
                'direction': direction,
                'items': records,
            },
            {
                'elapsed_ms': _now_ms(start),
                'query_plan': RESPONSE_QUERY_PLANS['lancedb_filter_pushdown'],
                'cache_hit': False,
                'fields_pruned': fields_pruned,
                'warnings': [],
            },
        )
        _query_cache.set(cache_key, response)
        return jsonify(response)

    except ValueError as exc:
        payload, status = _failure(
            ERROR_CODES['INVALID'],
            '参数非法',
            detail=str(exc),
            retryable=False,
            status_code=400,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
    except Exception as exc:
        import traceback
        traceback.print_exc()
        payload, status = _failure(
            ERROR_CODES['INTERNAL'],
            '排名服务内部错误',
            detail=str(exc),
            retryable=True,
            status_code=500,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status


@screener_bp.route('/stats', methods=['GET'])
def get_field_stats():
    """获取字段统计信息 - 统一响应 (T2.6)

    性能优化:
    - 列裁剪: 仅取目标字段
    - 查询结果缓存
    - Polars 表达式下推（min/max/mean/median/std/quantile）
    """
    start = time.perf_counter()
    endpoint = '/api/screener/stats'
    try:
        date = request.args.get('date') or get_latest_trade_date()
        field = request.args.get('field', 'close')

        # 查询缓存
        cache_key = _QueryCache.make_key(endpoint, {'date': date, 'field': field})
        cached = _query_cache.get(cache_key)
        if cached is not None:
            cached['meta']['elapsed_ms'] = _now_ms(start)
            cached['meta']['cache_hit'] = True
            return jsonify(cached)

        # 列裁剪
        df = get_all_stocks_daily_df(target_date=date, fields=[field])

        if df is None or df.is_empty():
            payload, status = _failure(
                ERROR_CODES['MISSING'],
                f'该日期无数据: {date}',
                detail='LanceDB daily_ohlcv 读取为空',
                retryable=False,
                status_code=404,
                endpoint=endpoint,
                meta={
                    'elapsed_ms': _now_ms(start),
                    'query_plan': RESPONSE_QUERY_PLANS['no_data'],
                },
            )
            return jsonify(payload), status

        if field not in df.columns:
            payload, status = _failure(
                ERROR_CODES['INVALID'],
                f'字段不存在: {field}',
                detail=f'可用列: {df.columns[:20]}',
                retryable=False,
                status_code=400,
                endpoint=endpoint,
                meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
            )
            return jsonify(payload), status

        # 一次性聚合（Polars 表达式下推）
        stats = df.select([
            pl.col(field).min().alias('min'),
            pl.col(field).max().alias('max'),
            pl.col(field).mean().alias('mean'),
            pl.col(field).median().alias('median'),
            pl.col(field).std().alias('std'),
            pl.col(field).quantile(0.25).alias('q25'),
            pl.col(field).quantile(0.75).alias('q75'),
        ])

        result = {
            'field': field,
            'date': date,
            'count': int(df.height),
            'min': stats['min'].item(),
            'max': stats['max'].item(),
            'mean': stats['mean'].item(),
            'median': stats['median'].item(),
            'std': stats['std'].item(),
            'q25': stats['q25'].item(),
            'q75': stats['q75'].item(),
        }
        result = clean_nan_values(result)

        response = _success(
            result,
            {
                'elapsed_ms': _now_ms(start),
                'query_plan': RESPONSE_QUERY_PLANS['polars_scan'],
                'cache_hit': False,
                'fields_pruned': [],
                'warnings': [],
            },
        )
        _query_cache.set(cache_key, response)
        return jsonify(response)

    except ValueError as exc:
        payload, status = _failure(
            ERROR_CODES['INVALID'],
            '参数非法',
            detail=str(exc),
            retryable=False,
            status_code=400,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
    except Exception as exc:
        import traceback
        traceback.print_exc()
        payload, status = _failure(
            ERROR_CODES['INTERNAL'],
            '统计服务内部错误',
            detail=str(exc),
            retryable=True,
            status_code=500,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status


@screener_bp.route('/field_stats', methods=['POST'])
def post_field_stats():
    """获取字段统计信息（POST 版本）- 与 GET /stats 完全一致的响应格式

    前端 myapp/src/api/screener.ts getFieldStats() 调用此端点（POST）。
    与 GET /stats 的差异：参数从 JSON body 读取；field 必填（不再默认 close）。

    Args (JSON body):
        field (str, 必填): 字段名，如 'close' / 'pe' / 'volume'
        date (str, 可选): 日期 'YYYY-MM-DD'，缺省时降级到 get_latest_trade_date()

    Returns:
        统一响应: {success, data: FieldStats, meta, error}
        - data 包含 field/date/count/min/max/mean/median/std/q25/q75
        - 错误分类: MISSING(404) / INVALID(400) / INTERNAL(500)
    """
    start = time.perf_counter()
    endpoint = '/api/screener/field_stats'
    try:
        data = request.get_json(silent=True) or {}
        field = data.get('field')
        date = data.get('date') or get_latest_trade_date()

        # 缺 field 返回 400 INVALID（前端必须显式传 field）
        if not field or not isinstance(field, str):
            payload, status = _failure(
                ERROR_CODES['INVALID'],
                'field 为必填字段',
                detail='前端必须显式传 field，不再默认 close',
                retryable=False,
                status_code=400,
                endpoint=endpoint,
                meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
            )
            return jsonify(payload), status

        # 查询缓存（与 /stats 共用 _QueryCache，但 endpoint 不同避免冲突）
        cache_key = _QueryCache.make_key(endpoint, {'date': date, 'field': field})
        cached = _query_cache.get(cache_key)
        if cached is not None:
            cached['meta']['elapsed_ms'] = _now_ms(start)
            cached['meta']['cache_hit'] = True
            return jsonify(cached)

        # 列裁剪
        df = get_all_stocks_daily_df(target_date=date, fields=[field])

        if df is None or df.is_empty():
            payload, status = _failure(
                ERROR_CODES['MISSING'],
                f'该日期无数据: {date}',
                detail='LanceDB daily_ohlcv 读取为空',
                retryable=False,
                status_code=404,
                endpoint=endpoint,
                meta={
                    'elapsed_ms': _now_ms(start),
                    'query_plan': RESPONSE_QUERY_PLANS['no_data'],
                },
            )
            return jsonify(payload), status

        if field not in df.columns:
            payload, status = _failure(
                ERROR_CODES['INVALID'],
                f'字段不存在: {field}',
                detail=f'可用列: {df.columns[:20]}',
                retryable=False,
                status_code=400,
                endpoint=endpoint,
                meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
            )
            return jsonify(payload), status

        # 一次性聚合（Polars 表达式下推，与 /stats 完全一致）
        stats = df.select([
            pl.col(field).min().alias('min'),
            pl.col(field).max().alias('max'),
            pl.col(field).mean().alias('mean'),
            pl.col(field).median().alias('median'),
            pl.col(field).std().alias('std'),
            pl.col(field).quantile(0.25).alias('q25'),
            pl.col(field).quantile(0.75).alias('q75'),
        ])

        result = {
            'field': field,
            'date': date,
            'count': int(df.height),
            'min': stats['min'].item(),
            'max': stats['max'].item(),
            'mean': stats['mean'].item(),
            'median': stats['median'].item(),
            'std': stats['std'].item(),
            'q25': stats['q25'].item(),
            'q75': stats['q75'].item(),
        }
        result = clean_nan_values(result)

        response = _success(
            result,
            {
                'elapsed_ms': _now_ms(start),
                'query_plan': RESPONSE_QUERY_PLANS['polars_scan'],
                'cache_hit': False,
                'fields_pruned': [],
                'warnings': [],
            },
        )
        _query_cache.set(cache_key, response)
        return jsonify(response)

    except ValueError as exc:
        payload, status = _failure(
            ERROR_CODES['INVALID'],
            '参数非法',
            detail=str(exc),
            retryable=False,
            status_code=400,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
    except Exception as exc:
        import traceback
        traceback.print_exc()
        payload, status = _failure(
            ERROR_CODES['INTERNAL'],
            '统计服务内部错误',
            detail=str(exc),
            retryable=True,
            status_code=500,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status


@screener_bp.route('/export', methods=['POST'])
def export_stocks():
    """导出筛选结果为 CSV - 与 /filter 一致的请求体与降级逻辑

    前端 myapp/src/api/screener.ts exportStocks() 调用此端点（responseType: 'blob'）。
    复用 get_screener_service + apply_filter_optimized，取消分页（导出全量），
    最大行数限制 EXPORT_MAX_ROWS=10000 防止 OOM。

    Args (JSON body): 与 /filter 相同的 FilterRequest
        date (str, 可选): 日期 'YYYY-MM-DD'，缺省时降级到 get_latest_trade_date()
        conditions (list): 筛选条件列表
        logic (str, 可选): 'AND' 或 'OR'，默认 'AND'
        order_by (list, 可选): 排序字段列表
        fields (list, 可选): 列裁剪字段列表，默认 FRONTEND_FIELDS

    Returns:
        成功: Response(text/csv, Content-Disposition: attachment; filename=screener_export_{date}.csv)
              - 截断时附 X-Warning 响应头
        失败: JSON 统一响应（与 /filter 一致），不返回 CSV
        - 错误分类: MISSING(404) / INVALID(400) / TIMEOUT(504) / INTERNAL(500)
    """
    start = time.perf_counter()
    endpoint = '/api/screener/export'
    try:
        from server.routes.screener_data_service import get_screener_service

        data = request.get_json(silent=True) or {}
        date = data.get('date') or get_latest_trade_date()
        conditions = data.get('conditions') or data.get('filters', [])
        logic = str(data.get('logic', 'AND')).upper()
        order_by = data.get('order_by', []) or []
        # 字段裁剪: 显式声明 > 默认 FRONTEND_FIELDS
        if 'fields' in data and data['fields'] is not None:
            req_fields = list(data['fields'])
        else:
            req_fields = list(FRONTEND_FIELDS)

        # 参数校验
        if logic not in ('AND', 'OR'):
            payload, status = _failure(
                ERROR_CODES['INVALID'],
                'logic 参数必须是 AND 或 OR',
                detail=f'received logic={logic!r}',
                retryable=False,
                status_code=400,
                endpoint=endpoint,
                meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
            )
            return jsonify(payload), status

        # 获取数据（自动列裁剪 + 内存缓存）
        service = get_screener_service()
        df = service.get_data(date=date, fields=req_fields, conditions=conditions)

        if df is None or df.is_empty():
            payload, status = _failure(
                ERROR_CODES['MISSING'],
                f'无法获取股票数据或该日期无数据: {date}',
                detail='LanceDB factors / daily_ohlcv 表中无该日期记录',
                retryable=False,
                status_code=404,
                endpoint=endpoint,
                meta={
                    'elapsed_ms': _now_ms(start),
                    'query_plan': RESPONSE_QUERY_PLANS['no_data'],
                },
            )
            return jsonify(payload), status

        # 应用筛选条件
        try:
            df_filtered = service.apply_filter_optimized(df, conditions, logic)
        except ValueError as exc:
            payload, status = _failure(
                ERROR_CODES['INVALID'],
                '筛选条件引用了不存在的字段或值类型错误',
                detail=str(exc),
                retryable=False,
                status_code=400,
                endpoint=endpoint,
                meta={
                    'elapsed_ms': _now_ms(start),
                    'query_plan': RESPONSE_QUERY_PLANS['lancedb_filter_pushdown'],
                },
            )
            return jsonify(payload), status

        # 无匹配数据返回 404 MISSING（与 /filter 的"缺数据降级"语义一致）
        if df_filtered.is_empty():
            payload, status = _failure(
                ERROR_CODES['MISSING'],
                f'无匹配数据: {date}',
                detail='应用筛选条件后结果为空',
                retryable=False,
                status_code=404,
                endpoint=endpoint,
                meta={
                    'elapsed_ms': _now_ms(start),
                    'query_plan': RESPONSE_QUERY_PLANS['lancedb_filter_pushdown'],
                },
            )
            return jsonify(payload), status

        # 服务端排序（与 /filter 一致）
        if order_by:
            for ob in reversed(order_by):
                field_ob = ob.get('field')
                direction = str(ob.get('direction', 'asc')).lower()
                if field_ob and field_ob in df_filtered.columns:
                    df_filtered = df_filtered.sort(
                        field_ob,
                        descending=(direction == 'desc'),
                        nulls_last=True,
                    )
        else:
            if 'total_mv' in df_filtered.columns:
                df_filtered = df_filtered.sort(
                    'total_mv', descending=True, nulls_last=True,
                )

        # 列裁剪（与 /filter 一致）
        effective, fields_pruned = _prune_fields(req_fields, df_filtered.columns)
        if effective:
            df_filtered = df_filtered.select(effective)

        warnings: List[str] = []
        if fields_pruned:
            warnings.append(f'部分请求字段在数据中缺失，已自动降级: {fields_pruned}')

        # 最大行数限制（防止 OOM）
        total = df_filtered.height
        if total > EXPORT_MAX_ROWS:
            df_filtered = df_filtered.head(EXPORT_MAX_ROWS)
            warnings.append(
                f'导出结果超过最大行数 {EXPORT_MAX_ROWS}，已截断（原 {total} 行）'
            )

        # 转 dict 并清理 NaN
        records = df_filtered.to_dicts()
        records = clean_nan_values(records)

        # 用 Python 标准库 csv 生成 CSV 文本
        output_buf = io.StringIO()
        if records:
            # 表头取并集，保证多行字段一致
            fieldnames: List[str] = []
            seen = set()
            for record in records:
                for key in record.keys():
                    if key not in seen:
                        seen.add(key)
                        fieldnames.append(key)
            writer = csv.DictWriter(
                output_buf, fieldnames=fieldnames, extrasaction='ignore',
            )
            writer.writeheader()
            for row in records:
                # None 写为空字符串，其他类型交给 csv 默认 str() 转换
                writer.writerow({k: ('' if v is None else v) for k, v in row.items()})
        else:
            # 理论上前面已 404 拦截，此处兜底防御
            output_buf.write('')

        csv_string = output_buf.getvalue()
        elapsed = _now_ms(start)
        print(f"[Screener] ========== 导出完成: rows={len(records)}/{total}, 耗时={elapsed}ms ==========")

        headers = {
            'Content-Disposition': f'attachment; filename=screener_export_{date}.csv',
        }
        if warnings:
            # HTTP header 不允许换行，用 '; ' 分隔
            headers['X-Warning'] = '; '.join(warnings)

        return Response(csv_string, mimetype='text/csv', headers=headers)

    except TimeoutError as exc:
        payload, status = _failure(
            ERROR_CODES['TIMEOUT'],
            '导出查询超时',
            detail=str(exc),
            retryable=True,
            status_code=504,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
    except ValueError as exc:
        payload, status = _failure(
            ERROR_CODES['INVALID'],
            '参数非法',
            detail=str(exc),
            retryable=False,
            status_code=400,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
    except Exception as exc:
        import traceback
        traceback.print_exc()
        payload, status = _failure(
            ERROR_CODES['INTERNAL'],
            '导出服务内部错误',
            detail=str(exc),
            retryable=True,
            status_code=500,
            endpoint=endpoint,
            meta={'elapsed_ms': _now_ms(start), 'query_plan': ''},
        )
        return jsonify(payload), status
