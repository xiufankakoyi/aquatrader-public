"""
DragonEye 数据适配器
====================

将 DragonEye 爬虫的原始 JSON 数据转换为 Polars DataFrame，
并通过 ingestion_pipeline 写入 LanceDB。

星型模型:
  - dragon_eye_limit_up: 涨停连板事实表 (主表)
  - dragon_eye_sector:   梯队/门派维度表

使用示例:
    adapter = DragonEyeAdapter()
    adapter.crawl("2026-04-24")
    rows = adapter.ingest_to_lancedb("2026-04-24")
"""

import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple

import polars as pl

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config.config import Config
from config.logger import get_logger
from data_svc.ingestion.crawler_etiquette import CrawlerEtiquette, crawler_retry

logger = get_logger(__name__)


# ============================================================
# JSON → Polars 转换器
# ============================================================

class DragonEyeTransformer:
    """
    将 DragonEye 原始 JSON 拍平为星型模型 DataFrame

    输入: data_lake/{date}/ 下的 JSON 文件
    输出: df_limit_up (涨停事实表) + df_sector (梯队维度表)
    """

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        try:
            if value is None or value == "":
                return default
            return int(round(float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _tags(value: Any) -> List[str]:
        if isinstance(value, list):
            return [str(item) for item in value if item]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    @staticmethod
    def transform_limit_up(
        target_date: str,
        stocks: List[Dict[str, Any]],
        stock_regulation: Optional[set] = None,
        stock_institution_buy: Optional[Dict[str, bool]] = None,
    ) -> pl.DataFrame:
        """
        将 limit_up_filter.json 的 stocks 列表拍平为事实表

        Args:
            target_date: 交易日期
            stocks: 涨停股票列表
            stock_regulation: 监管股票集合
            stock_institution_buy: 机构买入股票字典

        Returns:
            Polars DataFrame (星型模型主表)
        """
        if not stocks:
            return pl.DataFrame()

        stock_regulation = stock_regulation or set()
        stock_institution_buy = stock_institution_buy or {}

        records = []
        for stock in stocks:
            first_limit_ts = stock.get("first_limit_up_time", 0)
            if first_limit_ts:
                try:
                    dt = datetime.fromtimestamp(int(first_limit_ts))
                    minutes_from_open = max(0, (dt.hour * 60 + dt.minute) - (9 * 60 + 30))
                except (TypeError, ValueError, OSError, OverflowError):
                    minutes_from_open = 240
            else:
                minutes_from_open = 240

            market_cap_yi = DragonEyeTransformer._float(stock.get("total_market_cap")) / 1e8

            records.append({
                "trade_date": target_date,
                "stock_code": str(stock.get("code") or ""),
                "stock_name": str(stock.get("name") or ""),
                "continue_num": DragonEyeTransformer._int(stock.get("continue_num")),
                "high_days": str(stock.get("high_days") or ""),
                "limit_up_type": str(stock.get("limit_up_type") or ""),
                "open_num": (
                    DragonEyeTransformer._int(stock.get("open_num"))
                    if stock.get("open_num") is not None
                    else None
                ),
                "first_limit_up_minutes": minutes_from_open,
                "order_amount": DragonEyeTransformer._int(stock.get("order_amount")),
                "turnover_rate": DragonEyeTransformer._float(stock.get("turnover_rate")),
                "actual_turnover_rate": DragonEyeTransformer._float(stock.get("actual_turnover_rate")),
                "market_cap_yi": round(market_cap_yi, 2),
                "latest_price": DragonEyeTransformer._float(stock.get("latest")),
                "change_rate": DragonEyeTransformer._float(stock.get("change_rate")),
                "trading_amount": DragonEyeTransformer._float(stock.get("trading_amount")),
                "limit_up_suc_rate": DragonEyeTransformer._float(stock.get("limit_up_suc_rate")),
                "is_again_limit": DragonEyeTransformer._int(stock.get("is_again_limit")),
                "is_new": DragonEyeTransformer._int(stock.get("is_new")),
                "change_tag": str(stock.get("change_tag") or ""),
                "theme": str(stock.get("jiuyangongshe_category_name") or ""),
                "reason_type": str(stock.get("reason_type") or ""),
                "industry": str(stock.get("industry") or ""),
                "is_regulation": str(stock.get("code") or "") in stock_regulation,
                "is_institution_buy": str(stock.get("code") or "") in stock_institution_buy,
                "leader_tag": ",".join(DragonEyeTransformer._tags(stock.get("tags"))),
            })

        return pl.DataFrame(records)

    @staticmethod
    def transform_sector(
        target_date: str,
        ladder_detail: Dict[str, Any],
    ) -> pl.DataFrame:
        """
        将 ladder_hierarchy_detail.json 的梯队数据拍平为维度表

        JSON 结构:
          dates[0].boards[i] = { level, stocks: [{name, code, continue_num, ...}, ...] }

        Args:
            target_date: 交易日期
            ladder_detail: 梯队详情 JSON (完整文件内容)

        Returns:
            Polars DataFrame (梯队维度表)
        """
        if not ladder_detail:
            return pl.DataFrame()

        dates = ladder_detail.get("dates", [])
        if not dates:
            return pl.DataFrame()

        boards = dates[0].get("boards", [])
        if not boards:
            return pl.DataFrame()

        records = []
        level_map = {5: "leader", 4: "fourth", 3: "third", 2: "second", 1: "first"}

        for board in boards:
            level_key = DragonEyeTransformer._int(board.get("level"))
            level_name = level_map.get(level_key, f"level_{level_key}")
            stocks = board.get("stocks", [])

            for stock in stocks:
                if not isinstance(stock, dict):
                    continue
                records.append({
                    "trade_date": target_date,
                    "sector_level": level_key,
                    "sector_name": level_name,
                    "stock_code": str(stock.get("code") or ""),
                    "stock_name": str(stock.get("name") or ""),
                    "continue_num": DragonEyeTransformer._int(stock.get("continue_num")),
                    "order_amount": DragonEyeTransformer._int(stock.get("order_amount")),
                    "turnover_rate": DragonEyeTransformer._float(stock.get("turnover_rate")),
                    "leader_tag": ",".join(DragonEyeTransformer._tags(stock.get("tags"))),
                })

        return pl.DataFrame(records) if records else pl.DataFrame()

    @staticmethod
    def transform_sentiment(
        target_date: str,
        sentiment_data: Dict[str, Any],
    ) -> pl.DataFrame:
        """
        将 market_sentiment_cycle.json 转换为情绪指标表

        Args:
            target_date: 交易日期
            sentiment_data: 情绪周期 JSON

        Returns:
            Polars DataFrame
        """
        data_list = sentiment_data.get("data", [])
        current_data = None
        for item in data_list:
            if item.get("date") == target_date or item.get("date") == target_date.replace("-", ""):
                current_data = item
                break

        if not current_data:
            return pl.DataFrame()

        emotion = current_data.get("emotionMetrics", {})
        ladder = current_data.get("ladder", {})
        themes = current_data.get("themes", [])[:2]
        market = current_data.get("marketSentiment", {})

        limit_up_count = sum(len(stocks) for stocks in ladder.values()) if ladder else 0
        max_height = max(
            [DragonEyeTransformer._int(value) for value in ladder.keys()],
            default=0,
        )

        return pl.DataFrame([{
            "trade_date": target_date,
            "broken_ratio": DragonEyeTransformer._float(emotion.get("brokenRatio")),
            "broken_count": DragonEyeTransformer._int(emotion.get("brokenCount")),
            "limit_down_count": DragonEyeTransformer._int(emotion.get("limitDownCount")),
            "limit_up_count": limit_up_count,
            "max_height": max_height,
            "main_themes": ",".join([str(t.get("name") or "") for t in themes]),
            "rise_count": DragonEyeTransformer._int(market.get("rise")),
            "fall_count": DragonEyeTransformer._int(market.get("fall")),
        }])


# ============================================================
# DragonEye 适配器 (爬虫 + 入库)
# ============================================================

class DragonEyeAdapter(CrawlerEtiquette):
    """
    DragonEye 数据适配器

    职责:
    1. 调用爬虫获取数据 (继承 CrawlerEtiquette 防封禁)
    2. 将 JSON 转换为 Polars DataFrame
    3. 通过 ingestion_pipeline 写入 LanceDB
    """

    def __init__(self):
        super().__init__(
            name="DragonEye",
            min_delay=2.5,
            max_delay=5.5,
            max_retries=3,
            base_backoff=2.0,
        )
        self.project_root = Path(Config.BASE_DIR)
        self.data_lake_dir = self._resolve_data_lake_dir()
        self.transformer = DragonEyeTransformer()

    def _resolve_data_lake_dir(self) -> Path:
        """解析 data_lake 目录位置"""
        candidates = [
            self.project_root / "data" / "spider_data" / "dragon_eye" / "data_lake",
            self.project_root / "quant" / "data" / "data_lake",
        ]
        for d in candidates:
            if d.exists():
                return d
        default = candidates[0]
        default.mkdir(parents=True, exist_ok=True)
        return default

    def _resolve_latest_date(self) -> Optional[str]:
        """
        推算最新有数据的交易日

        策略：
        1. 从今天往前遍历，跳过周末，找到最近的工作日
        2. 检查 data_lake 中该日期目录是否存在且有数据文件
        3. 如果没有，往前最多找 10 个工作日
        4. 若仍无，回退到 data_lake 中已有数据的最晚日期

        Returns:
            日期字符串 (YYYY-MM-DD) 或 None
        """
        from datetime import datetime, timedelta

        today = datetime.now()

        for offset in range(20):
            d = today - timedelta(days=offset)
            if d.weekday() >= 5:
                continue
            date_str = d.strftime("%Y-%m-%d")
            date_dir = self.data_lake_dir / date_str
            if date_dir.exists() and any(date_dir.iterdir()):
                return date_str

        for d in sorted(self.data_lake_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            try:
                datetime.strptime(d.name, "%Y-%m-%d")
                if any(d.iterdir()):
                    return d.name
            except ValueError:
                continue

        return None

    def _scan_missing_dates(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        max_days: int = 90,
    ) -> List[str]:
        """
        扫描 data_lake 空白期，返回缺失的日期列表

        Args:
            start_date: 起始日期，默认 data_lake 中最早的日期
            end_date: 结束日期，默认昨天
            max_days: 最大扫描范围（防止无限扫描）

        Returns:
            缺失的日期列表 (YYYY-MM-DD)，按日期升序
        """
        from datetime import datetime, timedelta

        today = datetime.now()
        if end_date is None:
            end = today - timedelta(days=1)
            end_str = end.strftime("%Y-%m-%d")
        else:
            end = datetime.strptime(end_date, "%Y-%m-%d")
            end_str = end_date

        if start_date is None:
            existing = sorted([
                d.name for d in self.data_lake_dir.iterdir()
                if d.is_dir() and d.name.startswith("20")
            ])
            if existing:
                start = datetime.strptime(existing[0], "%Y-%m-%d")
            else:
                start = today - timedelta(days=max_days)
        else:
            start = datetime.strptime(start_date, "%Y-%m-%d")

        start = max(start, today - timedelta(days=max_days))

        from data_svc.ingestion.gap_checker import get_expected_trading_dates

        expected_dates = get_expected_trading_dates(
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )
        return [
            date_text
            for date_text in expected_dates
            if not self.inspect_local_date(date_text)["complete"]
        ]

    def scan_missing_dates(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        max_days: int = 90,
    ) -> List[str]:
        """Public wrapper for the unified updater backfill flow."""

        return self._scan_missing_dates(start_date, end_date, max_days)

    # ----------------------------------------------------------
    # 爬虫执行
    # ----------------------------------------------------------

    def crawl(
        self,
        target_date: Optional[str] = None,
        backfill: bool = False,
    ) -> bool:
        """
        执行爬虫获取指定日期数据

        Args:
            target_date: 目标日期 (YYYY-MM-DD)，默认自动推算最新交易日
            backfill: 是否扫描空白期并补爬

        Returns:
            是否成功
        """
        if backfill:
            return self._crawl_with_backfill(target_date)

        if target_date is None:
            target_date = self._resolve_latest_date() or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        return self._crawl_single(target_date)

    def _crawl_single(self, target_date: str) -> bool:
        """爬取单个日期"""
        local_status = self.inspect_local_date(target_date)
        if local_status["complete"]:
            logger.info(f"[DragonEye] {target_date} 本地数据完整，跳过")
            return True

        spider_path = self.project_root / "data_svc" / "spiders" / "dragon_spider" / "main.py"
        if not spider_path.exists():
            logger.warning(f"[DragonEye] 爬虫脚本不存在: {spider_path}")
            return False

        try:
            import subprocess
            cmd = [sys.executable, str(spider_path), "--date", target_date]
            logger.info(f"[DragonEye] 启动爬虫: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(spider_path.parent),
                encoding='utf-8',
                errors='replace',
            )
            if result.returncode == 0:
                logger.info(f"[DragonEye] {target_date} 爬虫成功")
            else:
                logger.warning(f"[DragonEye] 爬虫返回非零: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            logger.warning("[DragonEye] 爬虫执行超时 (300s)")
        except Exception as e:
            logger.warning(f"[DragonEye] 爬虫执行异常: {e}")

        refreshed_status = self.inspect_local_date(target_date)
        if refreshed_status["has_leader_data"]:
            if not refreshed_status["complete"]:
                logger.warning(
                    "[DragonEye] %s 龙头数据可用，但辅助文件仍不完整: %s",
                    target_date,
                    refreshed_status["missing_components"],
                )
            return True
        logger.warning(f"[DragonEye] {target_date} 无数据可用")
        return False

    def _crawl_with_backfill(self, end_date: Optional[str] = None) -> bool:
        """
        扫描空白期并逐个补爬

        Returns:
            是否至少有一个日期成功
        """
        missing = self._scan_missing_dates(end_date=end_date)
        if not missing:
            print("[DragonEye] 无缺失日期需要补爬")
            return True

        print(f"[DragonEye] 发现 {len(missing)} 个缺失日期: {missing[0]} ~ {missing[-1]}")
        success_count = 0
        for i, date_str in enumerate(missing, 1):
            print(f"[DragonEye] [{i}/{len(missing)}] 补爬: {date_str}")
            ok = self._crawl_single(date_str)
            if ok:
                success_count += 1
                self._random_delay()
            else:
                logger.warning(f"[DragonEye] {date_str} 补爬失败，继续下一个")

        print(f"[DragonEye] 补爬完成: {success_count}/{len(missing)} 成功")
        return success_count > 0

    def _random_delay(self):
        """随机延时（防止请求过快）"""
        import random
        delay = random.uniform(2.5, 5.5)
        time.sleep(delay)

    # ----------------------------------------------------------
    # JSON → DataFrame
    # ----------------------------------------------------------

    def load_json_data(self, target_date: str) -> Dict[str, Any]:
        """
        加载指定日期的所有 JSON 文件

        Returns:
            {
                "limit_up": {...},
                "ladder_detail": {...},
                "sentiment": {...},
                "dragon_tiger": {...},
                "risk_monitor": {...},
            }
        """
        date_dir = self.data_lake_dir / target_date
        result = {}

        file_map = {
            "limit_up": "limit_up_filter.json",
            "ladder_detail": "ladder_hierarchy_detail.json",
            "sentiment": "market_sentiment_cycle.json",
            "dragon_tiger": "dragon_tiger_list.json",
            "risk_monitor": "risk_monitor_list.json",
            "sector_heat_stats": "sector_heat_stats.json",
        }

        for key, filename in file_map.items():
            path = date_dir / filename
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        result[key] = json.load(f)
                except Exception as e:
                    logger.warning(f"[DragonEye] 读取 {filename} 失败: {e}")
                    result[key] = {}
            else:
                result[key] = {}

        return result

    REQUIRED_COMPONENTS = (
        "ladder",
        "limit_up",
        "sentiment",
        "theme_flow",
    )

    def inspect_local_date(self, target_date: str) -> Dict[str, Any]:
        """Inspect whether local JSON can produce leader and sentiment rows.

        完整性字段:
        - has_ladder:    梯队数据 (ladder_hierarchy_detail.json)
        - has_limit_up:  涨停事实 (limit_up_filter.json)
        - has_sentiment: 情绪周期 (market_sentiment_cycle.json)
        - has_theme_flow:板块热度 (sector_heat_stats.json)
        - evidence_date: 本次检查的交易日
        - completeness_score: 0.0 ~ 1.0，四个组件中有多少个齐备
        - missing_parts: 缺哪个组件的清单
        """

        raw = self.load_json_data(target_date)
        limit_stocks = (
            (raw.get("limit_up", {}).get("data") or {}).get("stocks") or []
        )
        ladder_stocks = self._extract_limit_up_from_ladder(raw.get("ladder_detail", {}))
        sentiment_items = raw.get("sentiment", {}).get("data") or []
        sector_data = raw.get("sector_heat_stats", {}) or {}
        theme_payload = sector_data.get("data") if isinstance(sector_data, dict) else None
        has_theme_flow = bool(theme_payload)

        has_ladder = bool(limit_stocks or ladder_stocks)
        has_limit_up = bool(limit_stocks)
        has_sentiment = bool(sentiment_items)

        flags = {
            "ladder": has_ladder,
            "limit_up": has_limit_up,
            "sentiment": has_sentiment,
            "theme_flow": has_theme_flow,
        }
        missing_components = [name for name, ok in flags.items() if not ok]
        completeness_score = round(sum(1 for ok in flags.values() if ok) / len(flags), 2)
        complete = has_ladder and has_sentiment

        return {
            "target_date": target_date,
            "evidence_date": target_date,
            "complete": complete,
            "has_ladder": has_ladder,
            "has_limit_up": has_limit_up,
            "has_sentiment": has_sentiment,
            "has_theme_flow": has_theme_flow,
            "has_leader_data": has_ladder,
            "limit_up_count": len(limit_stocks),
            "ladder_stock_count": len(ladder_stocks),
            "sentiment_item_count": len(sentiment_items),
            "missing_components": missing_components,
            "missing_parts": missing_components,
            "completeness_score": completeness_score,
            "status": "complete" if complete else "partial",
        }

    def _extract_limit_up_from_ladder(self, ladder_detail: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        当 limit_up_filter.json 无数据时，从 ladder JSON 兜底提取涨停股票

        JSON 结构: dates[0].boards[i].stocks[{}]  含 name, code, continue_num 等
        """
        stocks = []
        dates = ladder_detail.get("dates", [])
        if not dates:
            return stocks

        boards = dates[0].get("boards", [])
        for board in boards:
            for s in board.get("stocks", []):
                if isinstance(s, dict) and s.get("code"):
                    stocks.append({
                        "code": s.get("code", ""),
                        "name": s.get("name", ""),
                        "continue_num": s.get("continue_num", 0),
                        "high_days": s.get("high_days", ""),
                        "limit_up_type": s.get("limit_up_type", ""),
                        "open_num": s.get("open_num"),
                        "first_limit_up_time": s.get("first_limit_up_time", 0),
                        "order_amount": s.get("order_amount", 0),
                        "turnover_rate": s.get("turnover_rate", 0),
                        "actual_turnover_rate": s.get("actual_turnover_rate", 0),
                        "total_market_cap": s.get("currency_value", 0),
                        "latest": s.get("latest", 0) or s.get("price", 0),
                        "change_rate": s.get("change_rate", 0),
                        "trading_amount": s.get("trading_amount", 0),
                        "limit_up_suc_rate": s.get("limit_up_suc_rate", 0),
                        "is_again_limit": s.get("is_again_limit", 0),
                        "is_new": s.get("is_new", 0),
                        "change_tag": s.get("change_tag", ""),
                        "jiuyangongshe_category_name": s.get("jiuyangongshe_category_name", ""),
                        "reason_type": s.get("reason_type", ""),
                        "industry": s.get("industry", ""),
                        "tags": s.get("tags", []),
                    })
        return stocks

    @staticmethod
    def _enrich_limit_up_from_ladder(
        stocks: List[Dict[str, Any]],
        ladder_stocks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Fill leader tags and sparse fields from the ladder payload by code."""

        ladder_by_code = {
            str(stock.get("code", "")): stock
            for stock in ladder_stocks
            if stock.get("code")
        }
        enriched = []
        for stock in stocks:
            item = dict(stock)
            ladder_item = ladder_by_code.get(str(item.get("code", "")), {})
            for field in (
                "tags", "continue_num", "high_days", "limit_up_type", "open_num",
                "first_limit_up_time", "order_amount", "turnover_rate",
                "jiuyangongshe_category_name", "reason_type", "industry",
            ):
                if (
                    item.get(field) in (None, "", [], 0)
                    and ladder_item.get(field) not in (None, "", [])
                ):
                    item[field] = ladder_item[field]
            enriched.append(item)
        return enriched

    def build_dataframes(self, target_date: str) -> Tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
        """
        从 JSON 构建三个 DataFrame

        Returns:
            (df_limit_up, df_sector, df_sentiment)
        """
        raw = self.load_json_data(target_date)

        # 构建辅助集合
        stock_regulation = set()
        risk_data = raw.get("risk_monitor", {})
        for stock in risk_data.get("data", []):
            code = stock.get("code", "")
            if code:
                stock_regulation.add(code)

        stock_institution_buy = {}
        dragon_data = raw.get("dragon_tiger", {})
        for record in dragon_data.get("data", []):
            code = record.get("stockCode", "")
            lhb = record.get("lhbBranch", {})
            buy_branches = lhb.get("buyBranches", [])
            if any(b.get("branchName") == "机构专用" for b in buy_branches):
                stock_institution_buy[code] = True

        # 涨停事实表
        # 优先从 limit_up_filter.json 提取，兜底从 ladder_hierarchy_detail.json
        ladder_detail = raw.get("ladder_detail", {})
        ladder_stocks = self._extract_limit_up_from_ladder(ladder_detail)
        stocks = raw.get("limit_up", {}).get("data", {}).get("stocks", [])
        if stocks:
            stocks = self._enrich_limit_up_from_ladder(stocks, ladder_stocks)
        else:
            stocks = ladder_stocks
        df_limit_up = self.transformer.transform_limit_up(
            target_date, stocks, stock_regulation, stock_institution_buy
        )

        # 梯队维度表
        df_sector = self.transformer.transform_sector(target_date, ladder_detail)

        # 情绪指标表
        sentiment = raw.get("sentiment", {})
        df_sentiment = self.transformer.transform_sentiment(target_date, sentiment)

        return df_limit_up, df_sector, df_sentiment

    # ----------------------------------------------------------
    # 入库 (通过 ingestion_pipeline)
    # ----------------------------------------------------------

    def ingest_to_lancedb(self, target_date: str) -> int:
        """
        将 DragonEye 数据通过 ingestion_pipeline 写入 LanceDB

        Args:
            target_date: 目标日期

        Returns:
            总写入行数
        """
        df_limit_up, df_sector, df_sentiment = self.build_dataframes(target_date)
        total_rows = 0

        # 1. 涨停事实表 → dragon_eye_limit_up
        if not df_limit_up.is_empty():
            rows = self._write_table("dragon_eye_limit_up", df_limit_up, target_date)
            total_rows += rows
            logger.info(f"[DragonEye] 涨停事实表写入 {rows} 行")

        # 2. 梯队维度表 → dragon_eye_sector
        if not df_sector.is_empty():
            rows = self._write_table("dragon_eye_sector", df_sector, target_date)
            total_rows += rows
            logger.info(f"[DragonEye] 梯队维度表写入 {rows} 行")

        # 3. 情绪指标表 → dragon_eye_sentiment
        if not df_sentiment.is_empty():
            rows = self._write_table("dragon_eye_sentiment", df_sentiment, target_date)
            total_rows += rows
            logger.info(f"[DragonEye] 情绪指标表写入 {rows} 行")

        if total_rows == 0:
            logger.warning(f"[DragonEye] {target_date} 无数据写入")

        return total_rows

    def _write_table(self, table_name: str, df: pl.DataFrame, target_date: str) -> int:
        """
        写入单个表到 LanceDB

        使用 LanceDB 直接写入 (因为 ingestion_pipeline 的 REQUIRED_COLUMNS
        限制为 OHLCV 格式，不适用于 DragonEye 数据)
        """
        try:
            import lancedb

            lancedb_path = Path(Config.LANCEDB_PATH)
            db = lancedb.connect(str(lancedb_path))

            if table_name in db.table_names():
                table = db.open_table(table_name)
                try:
                    table.delete(f'trade_date = "{target_date}"')
                except Exception:
                    pass
                table.add(df.to_arrow(), mode="append")
            else:
                table = db.create_table(table_name, df.to_arrow())
                try:
                    table.create_scalar_index("stock_code")
                    table.create_scalar_index("trade_date")
                except Exception:
                    pass

            rows = len(df)
            self._update_watermark(table_name, target_date, rows)
            return rows

        except Exception as e:
            logger.error(f"[DragonEye] 写入 {table_name} 失败: {e}")
            return 0

    def _update_watermark(self, table_name: str, target_date: str, rows: int):
        """更新 Redis 水位表"""
        try:
            from data_svc.ingestion.watermark_manager import get_watermark_manager
            wm = get_watermark_manager()
            if wm.is_available:
                wm.update(
                    stock_code=f"dragon_eye:{table_name}",
                    last_update_date=target_date,
                    rows_added=rows,
                    data_source="crawler",
                )
        except Exception as e:
            logger.debug(f"[DragonEye] 水位表更新跳过: {e}")
