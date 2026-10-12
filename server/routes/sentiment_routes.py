"""
情感分析相关路由
"""
from flask import Blueprint, jsonify, request
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Any
import json
import numpy as np
from pathlib import Path
import re
import warnings
import math
import polars as pl
from server.performance_utils import json_response

sentiment_bp = Blueprint('sentiment', __name__, url_prefix='/api/research')


def get_parquet_path() -> Path:
    """获取Parquet文件路径"""
    from config.config import Config
    return Path(Config.PARQUET_DIR) / 'guba_posts.parquet'


# ---------------------------------------------------------------------------
# Demo 数据 (T4.1) — 当真实股吧数据缺失/接口失败时, 降级为静态 Demo 聚合
# 严禁伪造真实股吧帖子内容, 仅用于演示舆情面板布局
# ---------------------------------------------------------------------------
_DEMO_FLAG = "static_demo"
_DEMO_NOTE = "暂无真实股吧舆论数据, 当前为 Demo 展示"


def _demo_stock_sentiment_items(limit: int = 50) -> List[Dict[str, Any]]:
    """Demo 股票舆情汇总 - 仅结构化聚合字段, 不含帖子内容。"""
    fixtures = [
        {
            "symbol": "000001.SZ",
            "stockCode": "000001",
            "stockName": "平安银行",
            "totalPosts": 1250,
            "totalClicks": 50000,
            "totalComments": 3200,
            "bullishCount": 800,
            "bearishCount": 300,
            "neutralCount": 150,
            "sentimentScore": 0.65,
            "lastPostTime": "2024-01-15 14:30:00",
            "activeDays": 30,
        },
        {
            "symbol": "000002.SZ",
            "stockCode": "000002",
            "stockName": "万科A",
            "totalPosts": 980,
            "totalClicks": 35000,
            "totalComments": 2100,
            "bullishCount": 400,
            "bearishCount": 450,
            "neutralCount": 130,
            "sentimentScore": -0.15,
            "lastPostTime": "2024-01-15 13:45:00",
            "activeDays": 28,
        },
        {
            "symbol": "600519.SH",
            "stockCode": "600519",
            "stockName": "贵州茅台",
            "totalPosts": 2100,
            "totalClicks": 120000,
            "totalComments": 8500,
            "bullishCount": 1500,
            "bearishCount": 350,
            "neutralCount": 250,
            "sentimentScore": 0.78,
            "lastPostTime": "2024-01-15 15:00:00",
            "activeDays": 30,
        },
    ]
    if limit > 0:
        fixtures = fixtures[:limit]
    return fixtures


def _demo_stock_word_cloud(symbol: str) -> Dict[str, Any]:
    """Demo 单股词云 - 仅结构化关键词权重, 不含原始帖子文本。"""
    return {
        "symbol": symbol,
        "stockCode": symbol[-6:] if symbol else "",
        "stockName": "",
        "totalPosts": 0,
        "totalClicks": 0,
        "totalComments": 0,
        "overallSentiment": None,
        "words": [],
    }


def clean_nan(obj):
    """递归清理 NaN 值"""
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nan(item) for item in obj]
    elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


@sentiment_bp.route('/stock_sentiment', methods=['GET'])
def get_stock_sentiment():
    """基于股吧爬虫数据的股票舆情汇总，使用 Polars 加速查询。"""
    try:
        limit_param = request.args.get('limit')
        try:
            limit = int(limit_param) if limit_param is not None else 50
        except (TypeError, ValueError):
            limit = 50

        from config.config import Config
        parquet_path = Path(Config.PARQUET_DIR) / 'guba_posts.parquet'
        if parquet_path.exists():
            try:
                df = pl.scan_parquet(str(parquet_path)).filter(
                    pl.col('symbol').is_not_null()
                ).group_by(['symbol', 'stockbar_code', 'stockbar_name']).agg([
                    pl.len().alias('totalPosts'),
                    pl.col('post_click_count').cast(pl.Int64).fill_null(0).sum().alias('totalClicks'),
                    pl.col('post_comment_count').cast(pl.Int64).fill_null(0).sum().alias('totalComments'),
                    (pl.col('bullish_bearish').cast(pl.Float64) > 0).sum().alias('bullishCount'),
                    (pl.col('bullish_bearish').cast(pl.Float64) < 0).sum().alias('bearishCount'),
                    ((pl.col('bullish_bearish').cast(pl.Float64) == 0) | pl.col('bullish_bearish').is_null()).sum().alias('neutralCount'),
                    pl.col('bullish_bearish').cast(pl.Float64).mean().fill_null(0.0).alias('sentimentScore'),
                    pl.col('post_publish_time').str.to_datetime('%Y-%m-%d %H:%M:%S', strict=False).max().alias('lastPostTime'),
                    pl.col('post_publish_time').str.to_datetime('%Y-%m-%d %H:%M:%S', strict=False).dt.date().n_unique().alias('activeDays'),
                ]).sort(['totalComments', 'totalPosts'], descending=True).head(limit).collect()

                results = []
                for row in df.iter_rows(named=True):
                    last_ts = row.get('lastPostTime')
                    last_str = str(last_ts) if last_ts else None

                    results.append({
                        "symbol": row.get('symbol') or '',
                        "stockCode": str(row.get('stockbar_code') or ''),
                        "stockName": str(row.get('stockbar_name') or ''),
                        "totalPosts": int(row.get('totalPosts') or 0),
                        "totalClicks": int(row.get('totalClicks') or 0),
                        "totalComments": int(row.get('totalComments') or 0),
                        "bullishCount": int(row.get('bullishCount') or 0),
                        "bearishCount": int(row.get('bearishCount') or 0),
                        "neutralCount": int(row.get('neutralCount') or 0),
                        "sentimentScore": float(row.get('sentimentScore') or 0.0),
                        "lastPostTime": last_str,
                        "activeDays": int(row.get('activeDays') or 0) if row.get('activeDays') else None,
                    })

                if results:
                    return json_response({
                        "success": True,
                        "data": results,
                        "demo_mode": False,
                        "source": "local_parquet",
                    })
            except Exception:
                pass

        from config.config import Config
        data_dir = Path(Config.SPIDER_DATA_DIR)
        csv_results: List[Dict[str, Any]] = []
        if data_dir.exists():
            for csv_path in sorted(data_dir.glob('*_posts.csv')):
                symbol_code = csv_path.stem.replace('_posts', '')

                try:
                    df = pd.read_csv(csv_path, encoding='utf-8-sig')
                except Exception:
                    continue

                if df is None or df.empty:
                    continue

                total_posts = int(len(df))

                if 'post_click_count' in df.columns:
                    total_clicks = int(pd.to_numeric(df['post_click_count'], errors='coerce').fillna(0).sum())
                else:
                    total_clicks = 0

                if 'post_comment_count' in df.columns:
                    total_comments = int(pd.to_numeric(df['post_comment_count'], errors='coerce').fillna(0).sum())
                else:
                    total_comments = 0

                bullish_count = 0
                bearish_count = 0
                neutral_count = 0
                sentiment_score = 0.0
                if 'bullish_bearish' in df.columns:
                    bb = pd.to_numeric(df['bullish_bearish'], errors='coerce').fillna(0)
                    bullish_count = int((bb > 0).sum())
                    bearish_count = int((bb < 0).sum())
                    neutral_count = int((bb == 0).sum())
                    sentiment_score = float(bb.mean()) if len(bb) > 0 else 0.0

                stock_code = None
                stock_name = None
                if 'stockbar_code' in df.columns:
                    try:
                        stock_code = str(df['stockbar_code'].iloc[0])
                    except Exception:
                        stock_code = None
                if 'stockbar_name' in df.columns:
                    try:
                        stock_name = str(df['stockbar_name'].iloc[0])
                    except Exception:
                        stock_name = None

                last_post_time = None
                active_days = None
                if 'post_publish_time' in df.columns:
                    try:
                        with warnings.catch_warnings():
                            warnings.filterwarnings('ignore', category=UserWarning)
                            try:
                                times = pd.to_datetime(df['post_publish_time'], format='mixed', errors='coerce')
                            except (ValueError, TypeError):
                                times = pd.to_datetime(df['post_publish_time'], errors='coerce')
                        if not times.isna().all():
                            last = times.max()
                            last_post_time = last.isoformat(sep=' ', timespec='seconds')
                            active_days = int(times.dt.date.nunique())
                    except Exception:
                        last_post_time = None
                        active_days = None

                csv_results.append({
                    "symbol": symbol_code,
                    "stockCode": stock_code or symbol_code[-6:],
                    "stockName": stock_name or "",
                    "totalPosts": total_posts,
                    "totalClicks": total_clicks,
                    "totalComments": total_comments,
                    "bullishCount": bullish_count,
                    "bearishCount": bearish_count,
                    "neutralCount": neutral_count,
                    "sentimentScore": sentiment_score,
                    "lastPostTime": last_post_time,
                    "activeDays": active_days,
                })

        if csv_results:
            csv_results.sort(key=lambda x: (x['totalComments'], x['totalPosts']), reverse=True)
            if limit > 0:
                csv_results = csv_results[:limit]
            return json_response({
                "success": True,
                "data": csv_results,
                "demo_mode": False,
                "source": "local_csv",
            })

        # T4.1: 数据缺失/未抓到, 降级为静态 Demo (不构造虚假帖子内容)
        return json_response({
            "success": True,
            "data": _demo_stock_sentiment_items(limit),
            "demo_mode": True,
            "source": _DEMO_FLAG,
            "message": _DEMO_NOTE,
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "data": _demo_stock_sentiment_items(50),
            "demo_mode": True,
            "source": _DEMO_FLAG,
            "message": _DEMO_NOTE,
        }), 500


@sentiment_bp.route('/stock_sentiment_words', methods=['GET'])
def get_stock_sentiment_words():
    """返回单只股票用于词云的关键词和情绪权重。"""
    try:
        from config.config import Config

        symbol = request.args.get('symbol')
        if not symbol:
            return jsonify({"success": False, "error": "缺少 symbol 参数"}), 400

        parquet_path = Path(Config.PARQUET_DIR) / 'guba_posts.parquet'
        df_pd = None

        if parquet_path.exists():
            try:
                df_pl = pl.scan_parquet(str(parquet_path)).filter(
                    pl.col('symbol') == symbol
                ).select([
                    'symbol', 'stockbar_code', 'stockbar_name', 'post_title',
                    'post_click_count', 'post_comment_count', 'post_forward_count',
                    'post_publish_time', 'bullish_bearish'
                ]).collect()
                df_pd = df_pl.to_pandas()
            except Exception:
                df_pd = None

        if df_pd is None:
            csv_path = Path(Config.SPIDER_DATA_DIR) / f'{symbol}_posts.csv'
            if csv_path.exists():
                try:
                    df_pd = pd.read_csv(csv_path, encoding='utf-8-sig')
                    df_pd = df_pd.copy()
                    df_pd['symbol'] = symbol
                except Exception:
                    df_pd = None

        if df_pd is None or df_pd.empty:
            return json_response({
                "success": True,
                "data": _demo_stock_word_cloud(symbol),
                "demo_mode": True,
                "source": _DEMO_FLAG,
                "message": _DEMO_NOTE,
            })

        for col in ("post_click_count", "post_comment_count", "post_forward_count"):
            if col in df_pd.columns:
                df_pd[col] = pd.to_numeric(df_pd[col], errors='coerce').fillna(0)
            else:
                df_pd[col] = 0

        total_posts = int(len(df_pd))
        total_clicks = int(df_pd["post_click_count"].sum())
        total_comments = int(df_pd["post_comment_count"].sum())

        from server.utils.nlp_constants import (
            STOPWORDS, ANNOUNCEMENT_PATTERNS, AD_KEYWORDS,
            STOCK_TERMS, _init_jieba, jieba
        )

        try:
            from snownlp import SnowNLP
            _init_jieba()
            sentiment_available = True
        except Exception:
            SnowNLP = None
            sentiment_available = False

        word_stats: Dict[str, Dict[str, float]] = {}
        total_sentiment = 0.0
        sentiment_weight_sum = 0.0

        def tokenize(text: str):
            if not text:
                return []

            words = []

            if sentiment_available:
                _init_jieba()
                if jieba is not None:
                    try:
                        seg_list = jieba.cut(text, cut_all=False)

                        for word in seg_list:
                            word = word.strip()
                            if not word or word.isspace():
                                continue

                            word_len = len(word)

                            if word_len < 2 or word_len > 4:
                                if word_len > 4:
                                    sub_words = jieba.cut(word, cut_all=False)
                                    for sub_word in sub_words:
                                        sub_word = sub_word.strip()
                                        sub_len = len(sub_word)
                                        if 2 <= sub_len <= 4 and sub_word not in STOPWORDS:
                                            words.append(sub_word)
                                continue

                            if word in STOPWORDS:
                                continue

                            if word.isdigit():
                                continue

                            words.append(word)
                    except Exception:
                        words = [w.strip() for w in re.findall(r"[\u4e00-\u9fff]{2,4}", text)
                                if w.strip() and w.strip() not in STOPWORDS and not w.strip().isdigit()]
            else:
                words = [w.strip() for w in re.findall(r"[\u4e00-\u9fff]{2,4}", text)
                        if w.strip() and w.strip() not in STOPWORDS and not w.strip().isdigit()]

            seen = set()
            unique_words = []
            for w in words:
                if w not in seen:
                    seen.add(w)
                    unique_words.append(w)

            return unique_words

        def should_filter_title(title: str) -> bool:
            if not title:
                return True

            if len(title) > 35:
                return True

            for pattern in ANNOUNCEMENT_PATTERNS:
                if re.search(pattern, title):
                    return True

            for keyword in AD_KEYWORDS:
                if keyword in title:
                    return True

            punctuation_count = len(re.findall(r'[，。、；：！？]', title))
            if punctuation_count > 3:
                return True

            return False

        for _, row in df_pd.iterrows():
            title = str(row.get("post_title") or "").strip()
            if not title:
                continue

            if should_filter_title(title):
                continue

            clicks = float(row.get("post_click_count") or 0.0)
            comments = float(row.get("post_comment_count") or 0.0)
            forwards = float(row.get("post_forward_count") or 0.0)

            weight = 1.0 + math.log1p(clicks) + 2.0 * math.log1p(comments) + 1.5 * math.log1p(forwards)

            score = None
            bb_value = None
            if 'bullish_bearish' in df_pd.columns:
                try:
                    bb_value = row.get("bullish_bearish")
                    if bb_value is not None and pd.notna(bb_value):
                        bb_float = float(bb_value)
                        if bb_float > 0:
                            score = 0.5 + (bb_float * 0.5)
                        elif bb_float < 0:
                            score = 0.5 + (bb_float * 0.5)
                        else:
                            score = 0.5
                except (ValueError, TypeError):
                    score = None

            if score is None and sentiment_available and SnowNLP is not None:
                try:
                    score = float(SnowNLP(title).sentiments)
                except Exception:
                    score = None

            if score is not None:
                total_sentiment += score * weight
                sentiment_weight_sum += weight

            if score is None:
                label = "neutral"
            elif score >= 0.55:
                label = "positive"
            elif score <= 0.45:
                label = "negative"
            else:
                label = "neutral"

            tokens = tokenize(title)
            for word in tokens:
                if word not in word_stats:
                    word_stats[word] = {"positive": 0.0, "negative": 0.0, "neutral": 0.0, "count": 0}
                word_stats[word][label] += weight
                word_stats[word]["count"] += 1

        words = []
        for word, stats in word_stats.items():
            if stats["count"] < 2:
                continue

            total = stats["positive"] + stats["negative"] + stats["neutral"]
            if total == 0:
                continue

            pos_ratio = stats["positive"] / total
            neg_ratio = stats["negative"] / total

            sentiment = "neutral"
            if pos_ratio > 0.5:
                sentiment = "positive"
            elif neg_ratio > 0.5:
                sentiment = "negative"

            words.append({
                "text": word,
                "value": int(stats["count"]),
                "sentiment": sentiment,
                "posCount": int(stats["positive"]),
                "negCount": int(stats["negative"]),
            })

        words.sort(key=lambda x: x["value"], reverse=True)
        words = words[:100]

        overall_sentiment = None
        if sentiment_weight_sum > 0:
            avg_sentiment = total_sentiment / sentiment_weight_sum
            if avg_sentiment >= 0.55:
                overall_sentiment = "positive"
            elif avg_sentiment <= 0.45:
                overall_sentiment = "negative"
            else:
                overall_sentiment = "neutral"

        stock_code = None
        stock_name = None
        if 'stockbar_code' in df_pd.columns:
            try:
                stock_code = str(df_pd['stockbar_code'].iloc[0])
            except Exception:
                stock_code = None
        if 'stockbar_name' in df_pd.columns:
            try:
                stock_name = str(df_pd['stockbar_name'].iloc[0])
            except Exception:
                stock_name = None

        return json_response({
            "success": True,
            "data": {
                "symbol": symbol,
                "stockCode": stock_code or symbol[-6:],
                "stockName": stock_name or "",
                "totalPosts": total_posts,
                "totalClicks": total_clicks,
                "totalComments": total_comments,
                "overallSentiment": overall_sentiment,
                "words": words,
            },
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "data": None}), 500


@sentiment_bp.route('/stock_sentiment_timeline', methods=['GET'])
def get_stock_sentiment_timeline():
    """获取个股多空博弈时间序列数据（按时间分组，显示看多、看空、中立三条线）"""
    try:
        symbol = request.args.get('symbol')
        if not symbol:
            return jsonify({"success": False, "error": "缺少 symbol 参数", "data": []}), 400

        from config.config import Config
        parquet_path = Path(Config.PARQUET_DIR) / 'guba_posts.parquet'

        if not parquet_path.exists():
            return jsonify({"success": False, "error": "Parquet 数据文件不存在", "data": []}), 500

        if symbol.startswith('sz') or symbol.startswith('sh'):
            code_6 = symbol[2:] if len(symbol) > 2 else symbol
            full_symbol = symbol
        else:
            code_6 = symbol[-6:] if len(symbol) >= 6 else symbol.zfill(6)
            full_symbol = symbol

        df = pl.scan_parquet(str(parquet_path)).filter(
            (pl.col('symbol') == full_symbol) |
            (pl.col('symbol') == code_6) |
            (pl.col('symbol').str.slice(-6) == code_6) |
            (pl.col('stockbar_code').cast(pl.Utf8) == code_6)
        ).filter(
            pl.col('post_publish_time').cast(pl.Datetime).is_not_null()
        ).with_columns([
            pl.col('post_publish_time').cast(pl.Datetime).dt.truncate('1h').alias('time_hour'),
            (pl.col('bullish_bearish').cast(pl.Float64) > 0).alias('is_bullish'),
            (pl.col('bullish_bearish').cast(pl.Float64) < 0).alias('is_bearish'),
            ((pl.col('bullish_bearish').cast(pl.Float64) == 0) | pl.col('bullish_bearish').is_null()).alias('is_neutral'),
        ]).group_by(['time_hour', 'stockbar_name']).agg([
            pl.col('is_bullish').sum().alias('bullishCount'),
            pl.col('is_bearish').sum().alias('bearishCount'),
            pl.col('is_neutral').sum().alias('neutralCount'),
            pl.len().alias('totalCount'),
        ]).sort('time_hour').collect()

        if df.is_empty():
            return json_response({"success": True, "data": [], "stockName": ""})

        stock_name = str(df.row(0, named=True).get('stockbar_name', '')) if 'stockbar_name' in df.columns else ''

        results = []
        for row in df.iter_rows(named=True):
            time_hour = row.get('time_hour')
            if time_hour is None:
                continue

            time_str = time_hour.strftime('%Y-%m-%d %H:%M') if hasattr(time_hour, 'strftime') else str(time_hour)

            results.append({
                'time': time_str,
                'bullishCount': int(row.get('bullishCount', 0)),
                'bearishCount': int(row.get('bearishCount', 0)),
                'neutralCount': int(row.get('neutralCount', 0)),
                'totalCount': int(row.get('totalCount', 0))
            })

        return jsonify({
            "success": True,
            "data": results,
            "stockName": stock_name
        })
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"获取个股多空博弈时间序列数据失败: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e), "data": []}), 500


@sentiment_bp.route('/sentiment_trends', methods=['GET'])
def get_sentiment_trends():
    """获取情感趋势数据（从真实数据库查询）"""
    try:
        symbol = request.args.get('symbol')
        days = int(request.args.get('days', 7))

        from config.config import Config
        parquet_path = Path(Config.PARQUET_DIR) / 'guba_posts.parquet'

        if not parquet_path.exists():
            return jsonify({
                'success': False,
                'error': 'Parquet 数据文件不存在',
                'data': []
            }), 500

        # 先获取数据中的最新日期，使用数据日期而非当前日期
        lazy_df = pl.scan_parquet(str(parquet_path)).filter(
            pl.col('post_publish_time').is_not_null()
        ).with_columns([
            pl.col('post_publish_time').str.to_datetime('%Y-%m-%d %H:%M:%S', strict=False).alias('publish_datetime')
        ])

        # 获取数据中的日期范围
        date_range = lazy_df.select([
            pl.col('publish_datetime').dt.date().min().alias('min_date'),
            pl.col('publish_datetime').dt.date().max().alias('max_date')
        ]).collect()

        max_date = date_range[0, 'max_date']
        min_date = date_range[0, 'min_date']

        # 使用数据中的最新日期作为结束日期，往前推 days 天
        if max_date:
            end_date = max_date
            start_date = end_date - timedelta(days=days-1)
            # 确保不早于数据最早日期
            if min_date and start_date < min_date:
                start_date = min_date
        else:
            # 回退到当前日期
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days-1)

        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')

        lazy_df = lazy_df.filter(
            pl.col('publish_datetime').dt.date() >= pl.lit(start_date_str).str.to_date()
        ).filter(
            pl.col('publish_datetime').dt.date() <= pl.lit(end_date_str).str.to_date()
        )

        if symbol:
            if symbol.startswith('sz') or symbol.startswith('sh'):
                code_6 = symbol[2:] if len(symbol) > 2 else symbol
                full_symbol = symbol
            else:
                code_6 = symbol[-6:] if len(symbol) >= 6 else symbol.zfill(6)
                full_symbol = symbol

            lazy_df = lazy_df.filter(
                (pl.col('symbol') == full_symbol) |
                (pl.col('symbol') == code_6) |
                (pl.col('symbol').str.slice(-6) == code_6) |
                (pl.col('stockbar_code').cast(pl.Utf8) == code_6)
            )

        df = lazy_df.with_columns([
            pl.col('publish_datetime').dt.date().alias('date')
        ]).group_by('date').agg([
            pl.len().alias('post_count'),
            pl.col('bullish_bearish').cast(pl.Float64).mean().fill_null(0.0).alias('avg_sentiment')
        ]).sort('date').collect()

        data = []
        for row in df.iter_rows(named=True):
            date_val = row.get('date')
            if date_val is None:
                continue

            date_str = date_val.strftime('%Y-%m-%d') if hasattr(date_val, 'strftime') else str(date_val)

            data.append({
                'date': date_str,
                'post_count': int(row.get('post_count', 0)),
                'avg_sentiment': round(float(row.get('avg_sentiment', 0.0)), 3)
            })

        data.sort(key=lambda x: x['date'])

        return jsonify({
            'success': True,
            'data': data,
            'message': f'Successfully retrieved sentiment trend for {symbol or "all stocks"}'
        })
    except Exception as e:
        from config.logger import get_logger
        logger = get_logger(__name__)
        logger.error(f"获取情感趋势数据失败: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'error': str(e),
            'data': []
        }), 500


@sentiment_bp.route('/lda_topics', methods=['GET'])
def get_lda_topics():
    """Return persisted local LDA evidence only."""
    candidates = [
        Path("data/reports/lda_topics_latest.json"),
        Path("data/analytics/lda_topics.json"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            # 归一化：确保 data 字段为 { topics, scores } 格式
            if isinstance(payload, dict) and "topics" in payload and "scores" in payload:
                normalized = payload
            elif isinstance(payload, list) and len(payload) > 0:
                # 尝试从列表中提取 topics/scores
                normalized = {"topics": [], "scores": []}
                for item in payload:
                    if isinstance(item, dict):
                        name = item.get("name") or item.get("topic") or ""
                        score = item.get("score") or item.get("weight") or 0
                        if name:
                            normalized["topics"].append(str(name))
                            normalized["scores"].append(float(score))
            else:
                normalized = {"topics": [], "scores": []}
            return jsonify({
                "success": True,
                "data": normalized,
                "source": "local_structured_evidence",
                "message": "查询完成",
            })
        except Exception:
            continue
    return jsonify({
        "success": True,
        "data": {"topics": [], "scores": []},
        "source": "local_structured_evidence",
        "message": "暂无本地证据",
    })
