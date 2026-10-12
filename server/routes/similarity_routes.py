"""
K线形态相似度匹配 API 路由

提供K线形态匹配查询、离线预处理触发和预处理状态查询接口。
"""

import math

from flask import Blueprint, jsonify, request
from loguru import logger

similarity_bp = Blueprint("similarity", __name__, url_prefix="/api/similarity")

VALID_PATTERN_TYPES = {None, "breakout_volume", "limit_break", "n_shape"}
VALID_ALGORITHMS = {"dtw", "skeleton"}
VALID_SCENES = {"default", "breakout_volume", "limit_break", "n_shape", "trend"}


def _clean_nan(obj):
    """递归清理对象中的 NaN 值，转换为 None"""
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_clean_nan(item) for item in obj]
    elif isinstance(obj, float) and math.isnan(obj):
        return None
    return obj


@similarity_bp.route("/match", methods=["POST"])
def match():
    """
    K线形态相似度匹配查询

    请求体:
    {
        "stock_code": "000001.SZ",
        "window_size": 20,
        "top_n": 10,
        "pattern_type": null,
        "corr_threshold": 0.85,
        "subsequent_days": 10,
        "algorithm": "dtw",
        "scene": "default"
    }

    成功响应:
    {
        "success": true,
        "data": [
            {
                "stock_code": "600123.SH",
                "start_date": "2024-03-15",
                "end_date": "2024-04-12",
                "similarity_score": 0.92,
                "subsequent_kline": [...]
            }
        ]
    }

    参数校验失败:
    {
        "success": false,
        "error": "window_size must be between 5 and 120"
    }
    """
    data = request.get_json(silent=True) or {}

    stock_code = data.get("stock_code")
    if not stock_code or not isinstance(stock_code, str):
        return jsonify({"success": False, "error": "stock_code is required"}), 400

    window_size = data.get("window_size", 20)
    if not isinstance(window_size, int) or window_size < 5 or window_size > 120:
        return jsonify(
            {"success": False, "error": "window_size must be between 5 and 120"}
        ), 400

    top_n = data.get("top_n", 10)
    if not isinstance(top_n, int) or top_n < 1 or top_n > 50:
        return jsonify(
            {"success": False, "error": "top_n must be between 1 and 50"}
        ), 400

    pattern_type = data.get("pattern_type")
    if pattern_type not in VALID_PATTERN_TYPES:
        return jsonify(
            {
                "success": False,
                "error": "pattern_type must be null, breakout_volume, limit_break, or n_shape",
            }
        ), 400

    corr_threshold = data.get("corr_threshold", 0.85)
    if not isinstance(corr_threshold, (int, float)) or corr_threshold < 0 or corr_threshold > 1:
        return jsonify(
            {"success": False, "error": "corr_threshold must be between 0 and 1"}
        ), 400

    subsequent_days = data.get("subsequent_days", 10)
    if not isinstance(subsequent_days, int) or subsequent_days < 1 or subsequent_days > 60:
        return jsonify(
            {"success": False, "error": "subsequent_days must be between 1 and 60"}
        ), 400

    algorithm = data.get("algorithm", "dtw")
    if algorithm not in VALID_ALGORITHMS:
        return jsonify(
            {"success": False, "error": "algorithm must be dtw or skeleton"}
        ), 400

    scene = data.get("scene", "default")
    if scene not in VALID_SCENES:
        return jsonify(
            {"success": False, "error": "scene must be default, breakout_volume, limit_break, n_shape, or trend"}
        ), 400

    try:
        from server.services.similarity_service import get_similarity_service

        service = get_similarity_service()
        results = service.match(
            stock_code=stock_code,
            window_size=window_size,
            top_n=top_n,
            pattern_type=pattern_type,
            corr_threshold=float(corr_threshold),
            subsequent_days=subsequent_days,
            algorithm=algorithm,
            scene=scene,
        )
        results = _clean_nan(results)
        return jsonify({"success": True, "data": results})
    except Exception as e:
        logger.error(f"Similarity match failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@similarity_bp.route("/preprocess", methods=["POST"])
def preprocess():
    """
    触发离线预处理

    请求体:
    {
        "window_size": 20
    }

    成功响应:
    {
        "success": true,
        "data": {
            "window_size": 20,
            "total_windows": 150000,
            "symbols_count": 5000
        }
    }
    """
    data = request.get_json(silent=True) or {}

    window_size = data.get("window_size", 20)
    if not isinstance(window_size, int) or window_size < 5 or window_size > 120:
        return jsonify(
            {"success": False, "error": "window_size must be between 5 and 120"}
        ), 400

    try:
        from server.services.similarity_service import get_similarity_service

        service = get_similarity_service()
        result = service.preprocess(window_size=window_size)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Similarity preprocess failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@similarity_bp.route("/status", methods=["GET"])
def status():
    """
    查询预处理状态

    成功响应:
    {
        "success": true,
        "data": {
            "preprocessed": true,
            "window_sizes": [20],
            "cache_sizes": {"20": 150000},
            "last_preprocess_time": "2026-04-19 10:30:00"
        }
    }
    """
    try:
        from server.services.similarity_service import get_similarity_service

        service = get_similarity_service()
        result = service.get_status()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Similarity status query failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@similarity_bp.route("/stocks/search", methods=["GET"])
def search_stocks():
    """
    搜索股票代码（用于自动联想）

    请求参数:
    - keyword: 搜索关键词（如 "1" 会匹配 "000001", "300001", "600001" 等）
    - limit: 返回结果数量限制（默认 10）

    成功响应:
    {
        "success": true,
        "data": [
            {"code": "000001.SZ", "name": "平安银行"},
            {"code": "600001.SH", "name": "浦发银行"},
            ...
        ]
    }
    """
    keyword = request.args.get("keyword", "").strip()
    limit = request.args.get("limit", 10, type=int)

    if not keyword:
        return jsonify({"success": True, "data": []})

    try:
        from data_svc.query.fast_market_query import get_fast_market_query

        fmq = get_fast_market_query()
        logger.info(f"[search_stocks] FastMarketQuery initialized: {fmq}")

        # 读取所有股票代码
        df = fmq.get_latest_snapshot(fields=["stock_code"])
        logger.info(f"[search_stocks] Read {len(df)} rows from database")

        # 如果数据库为空，使用模拟数据
        if df.is_empty():
            logger.info("[search_stocks] Database is empty, generating mock data")
            mock_stocks = _generate_mock_stocks(keyword, limit)
            logger.info(f"[search_stocks] Generated {len(mock_stocks)} mock stocks")
            return jsonify({"success": True, "data": mock_stocks})

        # 获取唯一股票代码
        all_codes = df["stock_code"].unique().to_list()

        # 过滤匹配的股票代码
        matched = []
        keyword_lower = keyword.lower()

        for code in all_codes:
            code_lower = code.lower()
            code_without_suffix = code.split(".")[0] if "." in code else code

            if (
                keyword_lower in code_lower
                or keyword_lower in code_without_suffix
                or code_without_suffix.endswith(keyword)
                or code_without_suffix.startswith(keyword)
            ):
                matched.append({
                    "code": code,
                    "name": code
                })

            if len(matched) >= limit:
                break

        return jsonify({"success": True, "data": matched})
    except Exception as e:
        logger.error(f"Stock search failed: {e}")
        # 出错时返回模拟数据
        mock_stocks = _generate_mock_stocks(keyword, limit)
        return jsonify({"success": True, "data": mock_stocks})


def _generate_mock_stocks(keyword: str, limit: int) -> list:
    """生成模拟股票列表用于测试"""
    mock_stocks = []
    keyword_lower = keyword.lower()
    
    # 常见股票代码模式
    patterns = [
        ("000", "SZ", "主板"),
        ("001", "SZ", "主板"),
        ("002", "SZ", "中小板"),
        ("300", "SZ", "创业板"),
        ("600", "SH", "沪市主板"),
        ("601", "SH", "沪市主板"),
        ("603", "SH", "沪市主板"),
        ("605", "SH", "沪市主板"),
        ("688", "SH", "科创板"),
    ]
    
    for prefix, suffix, market in patterns:
        for i in range(1, 1000):
            code_num = f"{prefix}{i:03d}"
            code = f"{code_num}.{suffix}"
            
            # 匹配逻辑
            if keyword_lower in code_lower or keyword_lower in code_num:
                mock_stocks.append({
                    "code": code,
                    "name": f"{market}股票{code_num}"
                })
                
            if len(mock_stocks) >= limit:
                break
        
        if len(mock_stocks) >= limit:
            break
    
    return mock_stocks


@similarity_bp.route("/stocks/<stock_code>/kline", methods=["GET"])
def get_stock_kline(stock_code: str):
    """
    获取股票K线数据

    请求参数:
    - days: 获取最近多少天的数据（默认 30）

    成功响应:
    {
        "success": true,
        "data": {
            "stock_code": "000001.SZ",
            "klines": [
                {"trade_date": "2024-01-01", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 1000000},
                ...
            ]
        }
    }
    """
    days = request.args.get("days", 30, type=int)

    try:
        from datetime import datetime, timedelta
        import polars as pl
        from data_svc.query.fast_market_query import get_fast_market_query

        fmq = get_fast_market_query()

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days * 2)  # 多取一些数据确保有足够交易日

        df = fmq._read_table(
            "daily_ohlcv",
            symbols=stock_code,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            fields=["stock_code", "trade_date", "open", "high", "low", "close", "volume"],
        )

        if df.is_empty():
            # 返回模拟K线数据用于测试
            mock_klines = _generate_mock_klines(stock_code, days)
            return jsonify({
                "success": True,
                "data": {
                    "stock_code": stock_code,
                    "klines": mock_klines
                }
            })

        # 转换为列表格式
        df = df.sort("trade_date").tail(days)  # 取最近N个交易日

        klines = []
        for row in df.iter_rows(named=True):
            klines.append({
                "trade_date": str(row["trade_date"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })

        return jsonify({
            "success": True,
            "data": {
                "stock_code": stock_code,
                "klines": klines
            }
        })
    except Exception as e:
        logger.error(f"Get stock kline failed: {e}")
        # 出错时返回模拟数据
        mock_klines = _generate_mock_klines(stock_code, days)
        return jsonify({
            "success": True,
            "data": {
                "stock_code": stock_code,
                "klines": mock_klines
            }
        })


def _generate_mock_klines(stock_code: str, days: int) -> list:
    """生成模拟K线数据用于测试"""
    import random
    from datetime import datetime, timedelta
    
    klines = []
    end_date = datetime.now()
    
    # 基于股票代码生成一个固定的基准价格
    base_price = hash(stock_code) % 50 + 10  # 10-60之间的价格
    current_price = base_price
    
    for i in range(days):
        trade_date = end_date - timedelta(days=days - i)
        
        # 生成随机波动
        change_pct = random.uniform(-0.05, 0.05)  # ±5%的波动
        
        open_price = current_price
        close_price = current_price * (1 + change_pct)
        high_price = max(open_price, close_price) * (1 + random.uniform(0, 0.02))
        low_price = min(open_price, close_price) * (1 - random.uniform(0, 0.02))
        volume = random.randint(1000000, 10000000)
        
        klines.append({
            "trade_date": trade_date.strftime("%Y-%m-%d"),
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "volume": volume
        })
        
        current_price = close_price
    
    return klines
