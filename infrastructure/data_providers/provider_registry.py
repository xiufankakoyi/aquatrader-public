"""Provider registry with failover, local-first reads and source logging.

8 大数据源 + 智能降级：
1. 通达信(TDX)    — 实时行情、5档盘口、K线
2. 腾讯财经       — PE/PB/市值/换手率
3. 同花顺(iFinD)  — 专业行情、股息率、热点题材
4. 东方财富(Efinance) — 行情、板块资金流向
5. AKShare        — 研报、公告、资金流向、板块排行
6. 巨潮资讯(CNINFO) — 公告全文搜索
7. Tushare Pro    — 日线、公告、基础数据
8. 宝塔数据(Baostock) — 日线、基础数据（补充）

需额外配置的数据源：
- iWencai(问财)  — 语义搜索选股（需 WENCAI_TOKEN）
- JQData(聚宽)   — 量化指标、财报因子（需 JQ_USER/JQ_PASS）
- RiceQuant(米筐) — 量化回测数据（需 RQ_USER/RQ_PASS）
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from config.config import Config
from infrastructure.data_providers.market.akshare_provider import AkshareProvider
from infrastructure.data_providers.market.baostock_provider import BaostockProvider
from infrastructure.data_providers.base import (
    ALL_A_SPOT_COLUMNS,
    BOARD_FUND_FLOW_COLUMNS,
    EARNINGS_FORECAST_COLUMNS,
    FINANCE_SNAPSHOT_COLUMNS,
    CONSENSUS_ESTIMATE_COLUMNS,
    VALUATION_HISTORY_COLUMNS,
    CONCEPT_BOARD_MEMBERS_COLUMNS,
    CONCEPT_BOARDS_COLUMNS,
    DAILY_BARS_COLUMNS,
    LIMIT_UP_POOL_COLUMNS,
    MARKET_OVERVIEW_COLUMNS,
    MARKET_SNAPSHOT_COLUMNS,
    STOCK_BELONG_BOARDS_COLUMNS,
    STOCK_BASIC_INFO_COLUMNS,
    STOCK_FUND_FLOW_COLUMNS,
    BaseMarketDataProvider,
    empty_frame,
    ensure_columns,
    normalize_symbols,
    normalize_trade_date,
    now_text,
    pick_column,
    to_numeric_series,
)
from infrastructure.data_providers.research.cninfo_provider import CninfoProvider
from infrastructure.data_providers.market.efinance_provider import EfinanceProvider
from infrastructure.data_providers.research.ifind_provider import IfindProvider
from infrastructure.data_providers.market.tdx_provider import TdxProvider
from infrastructure.data_providers.market.tencent_provider import TencentProvider
from infrastructure.data_providers.market.tushare_provider import TushareProvider
from infrastructure.data_providers.research.wencai_provider import WencaiProvider
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderHealthSnapshot:
    """Compatibility health snapshot consumed by ResearchEngine."""

    enabled: bool
    status: str
    capabilities: list[str]
    last_error: str = ""
    sample_event_count: int = 0


def _project_root() -> Path:
    """项目根目录。"""
    return Path(__file__).resolve().parent.parent.parent


class ProviderRegistry:
    """统一降级门面 — 8大数据源自动切换。"""

    def __init__(
        self,
        providers: list[BaseMarketDataProvider] | None = None,
        parquet_dir: Path | None = None,
        interaction_timeout_seconds: float = 10.0,
        interaction_circuit_seconds: float = 300.0,
    ) -> None:
        provider_list = providers or [
            # 行情数据源（按优先级排序）
            TencentProvider(),      # 1. 腾讯财经：当前网络已实测稳定
            TdxProvider(),          # 2. 通达信：连接成功后补5档盘口/K线
            EfinanceProvider(),     # 3. 东方财富：行情+板块资金流向
            IfindProvider(),        # 4. 同花顺iFinD：专业行情+热点题材
            AkshareProvider(),      # 5. AKShare：研报+公告+资金流向
            CninfoProvider(),       # 6. 巨潮资讯：公告全文搜索
            TushareProvider(),      # 7. Tushare Pro：日线+公告+基础数据
            BaostockProvider(),     # 8. 宝塔数据：日线+基础数据（补充）
            # 语义搜索（需额外配置）
            WencaiProvider(),       # 问财：语义搜索选股
        ]
        self.providers: dict[str, BaseMarketDataProvider] = {
            provider.name: provider for provider in provider_list
        }
        self.parquet_dir = parquet_dir or (_project_root() / "data" / "parquet_data")
        self.logs: list[dict[str, Any]] = []
        self._interaction_timeout_seconds = max(0.01, float(interaction_timeout_seconds))
        self._interaction_circuit_seconds = max(0.0, float(interaction_circuit_seconds))
        self._interaction_lock = threading.Lock()
        self._interaction_inflight: threading.Thread | None = None
        self._interaction_circuit_open_until = 0.0

    # ------------------------------------------------------------------
    # 行情查询（智能降级：腾讯 → TDX → Efinance → iFinD）
    # ------------------------------------------------------------------

    def get_realtime_quotes(
        self,
        symbols: list[str] | None = None,
        trade_date: str | None = None,
    ) -> pd.DataFrame:
        """实时行情：腾讯 → TDX(5档盘口) → Efinance → iFinD

        Args:
                symbols: 股票代码列表，None 时由各 provider 决定返回范围
                trade_date: 交易日期（仅用于日志记录）
        """
        return self._failover(
            method_name="get_realtime_quotes",
            provider_order=["tencent", "tdx", "efinance", "akshare", "ifind"],
            columns=MARKET_SNAPSHOT_COLUMNS,
            symbols=symbols,
            trade_date=trade_date,
        )

    def get_market_overview(self, trade_date: str | None = None) -> pd.DataFrame:
        """市场整体概况（涨跌家数 + 总成交额 + 热门板块）：AKShare → Efinance

        用于替换市场情绪卡片中的占位值。聚合全市场实时行情后统计。

        Args:
            trade_date: 交易日期（仅用于标注，实际返回实时数据）
        """
        return self._failover(
            method_name="get_market_overview",
            provider_order=["akshare", "efinance"],
            columns=MARKET_OVERVIEW_COLUMNS,
            trade_date=trade_date,
        )

    def get_all_a_spot(self) -> pd.DataFrame:
        """全 A 股实时行情快照（含涨速、量比、5/15 分钟涨跌）。

        主要用于盘中选股器做全市场初筛。AKShare 返回字段最全，
        失败时降级到 Efinance 基础行情（无涨速/分钟涨跌）。
        """
        provider = self.providers.get("akshare")
        if provider and getattr(provider, "is_available", lambda: False)():
            try:
                df = provider.get_all_a_spot()
                if df is not None and not df.empty:
                    return df
            except Exception as exc:
                logger.warning("ProviderRegistry get_all_a_spot AKShare 失败: %s", exc)

        provider = self.providers.get("efinance")
        if provider and getattr(provider, "is_available", lambda: False)():
            try:
                df = provider.get_realtime_quotes()
                if df is not None and not df.empty:
                    return ensure_columns(df, ALL_A_SPOT_COLUMNS)
            except Exception as exc:
                logger.warning("ProviderRegistry get_all_a_spot Efinance 失败: %s", exc)

        return empty_frame(ALL_A_SPOT_COLUMNS)

    def smart_quote(self, code: str) -> dict[str, Any]:
        """智能行情查询：三层降级 TDX → 腾讯 → iFinD，合并财务数据。

        参考 china_stock.py 的 smart_quote() 逻辑：
        1. 先用 TDX 获取实时盘口（含5档买卖盘）
        2. 再用腾讯补充 PE/市值/换手率
        3. 都失败时用 iFinD
        """
        # 第一层：TDX（实时盘口）
        tdx = self.providers.get("tdx")
        if tdx and tdx.is_available():
            try:
                kline = tdx.get_kline(code, period="daily", count=1)
                if kline and kline.get("bars"):
                    bar = kline["bars"][-1]
                    result = {
                        "source": "通达信",
                        "code": code,
                        "price": bar.get("close", 0),
                        "open": bar.get("open", 0),
                        "high": bar.get("high", 0),
                        "low": bar.get("low", 0),
                        "volume": bar.get("volume", 0),
                        "amount": bar.get("amount", 0),
                    }
                    # 补充腾讯财务数据
                    tencent = self.providers.get("tencent")
                    if tencent and tencent.is_available():
                        try:
                            df = tencent.get_realtime_quotes(symbols=[code])
                            if not df.empty:
                                row = df.iloc[0]
                                result["name"] = row.get("stock_name", "")
                                result["pe"] = row.get("pe", 0)
                                result["turnover_rate"] = row.get("turnover_rate", 0)
                                result["market_cap"] = row.get("total_market_cap", 0)
                                result["circulating_cap"] = row.get("float_market_cap", 0)
                        except Exception as exc:
                            logger.debug(
                                "Provider tencent.get_realtime_quotes enrichment failed for %s: %s",
                                code,
                                exc,
                            )
                    return result
            except Exception as exc:
                logger.warning("Provider tdx.smart_quote failed for %s: %s", code, exc)

        # 第二层：腾讯财经
        tencent = self.providers.get("tencent")
        if tencent and tencent.is_available():
            try:
                df = tencent.get_realtime_quotes(symbols=[code])
                if not df.empty:
                    row = df.iloc[0]
                    return {
                        "source": "腾讯财经",
                        "code": code,
                        "name": row.get("stock_name", ""),
                        "price": row.get("price", 0),
                        "change_pct": row.get("pct_chg", 0),
                        "pe": row.get("pe", 0),
                        "turnover_rate": row.get("turnover_rate", 0),
                        "market_cap": row.get("total_market_cap", 0),
                    }
            except Exception as exc:
                logger.warning(
                    "Provider tencent.get_realtime_quotes failed for %s: %s",
                    code,
                    exc,
                )

        # 第三层：iFinD
        ifind = self.providers.get("ifind")
        if ifind and ifind.is_available():
            try:
                df = ifind.get_realtime_quotes(symbols=[code])
                if not df.empty:
                    row = df.iloc[0]
                    return {"source": "iFinD", "code": code, **row.to_dict()}
            except Exception as exc:
                logger.warning(
                    "Provider ifind.get_realtime_quotes failed for %s: %s",
                    code,
                    exc,
                )

        return {"error": f"所有数据源均无法获取 {code} 的行情"}

    # ------------------------------------------------------------------
    # K线数据
    # ------------------------------------------------------------------

    def get_kline(self, code: str, period: str = "daily", count: int = 30) -> dict[str, Any]:
        """K线查询：TDX → AKShare → Efinance"""
        for provider_name in ["tdx", "akshare", "efinance"]:
            provider = self.providers.get(provider_name)
            if provider and provider.is_available():
                try:
                    result = provider.get_kline(code, period=period, count=count)
                    if result and result.get("bars"):
                        return result
                except Exception as exc:
                    logger.warning(
                        "Provider %s.get_kline failed for %s: %s",
                        provider_name,
                        code,
                        exc,
                    )
                    continue
        return {"error": f"K线查询失败: {code}", "bars": []}

    # ------------------------------------------------------------------
    # 日线数据
    # ------------------------------------------------------------------

    def get_daily_bars(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """日线数据：Tushare → Efinance → AKShare → Baostock"""
        provider_order = ["tushare", "efinance", "akshare", "baostock"]
        if Config.parquet_fallback_enabled():
            provider_order.insert(0, "local_parquet")
        return self._failover(
            method_name="get_daily_bars",
            provider_order=provider_order,
            columns=DAILY_BARS_COLUMNS,
            start_date=start_date,
            end_date=end_date,
            symbols=symbols,
        )

    # ------------------------------------------------------------------
    # 板块数据
    # ------------------------------------------------------------------

    def get_concept_boards(self, trade_date: str | None = None) -> pd.DataFrame:
        """概念板块：AKShare → Efinance → iFinD"""
        return self._failover(
            method_name="get_concept_boards",
            provider_order=["akshare", "efinance", "ifind"],
            columns=CONCEPT_BOARDS_COLUMNS,
            trade_date=trade_date,
        )

    def get_concept_board_members(
        self,
        board_code_or_name: str,
        trade_date: str | None = None,
        board_name: str | None = None,
    ) -> pd.DataFrame:
        """板块成分股：AKShare → Efinance"""
        return self._failover(
            method_name="get_concept_board_members",
            provider_order=["akshare", "efinance"],
            columns=CONCEPT_BOARD_MEMBERS_COLUMNS,
            board_code_or_name=board_code_or_name,
            trade_date=trade_date,
            board_name=board_name,
            context=board_name or board_code_or_name,
        )

    def get_limit_up_pool(self, trade_date: str) -> pd.DataFrame:
        """涨停池：AKShare"""
        return self._failover(
            method_name="get_limit_up_pool",
            provider_order=["akshare"],
            columns=LIMIT_UP_POOL_COLUMNS,
            trade_date=trade_date,
        )

    # ------------------------------------------------------------------
    # 资金流向
    # ------------------------------------------------------------------

    def get_board_fund_flow(self, trade_date: str) -> pd.DataFrame:
        """板块资金流向：Efinance → AKShare"""
        return self._failover(
            method_name="get_board_fund_flow",
            provider_order=["efinance", "akshare"],
            columns=BOARD_FUND_FLOW_COLUMNS,
            trade_date=trade_date,
        )

    def get_stock_fund_flow(self, trade_date: str, symbols: list[str] | None = None) -> pd.DataFrame:
        """个股资金流向：Efinance → AKShare → Tushare"""
        return self._failover(
            method_name="get_stock_fund_flow",
            provider_order=["efinance", "akshare", "tushare"],
            columns=STOCK_FUND_FLOW_COLUMNS,
            trade_date=trade_date,
            symbols=symbols,
            context=f"{len(symbols)} symbols" if symbols else "",
        )

    def get_earnings_forecasts(
        self, start_date: str, end_date: str, symbols: list[str] | None = None
    ) -> pd.DataFrame:
        """业绩预告方向与区间：Tushare。"""
        return self._failover(
            method_name="get_earnings_forecasts",
            provider_order=["tushare"],
            columns=EARNINGS_FORECAST_COLUMNS,
            start_date=start_date,
            end_date=end_date,
            symbols=symbols,
            context=f"{start_date}..{end_date}",
        )

    def get_finance_snapshot(
        self,
        symbols: list[str] | None = None,
        as_of: str | None = None,
        periods: int = 12,
    ) -> pd.DataFrame:
        """财务历史快照：优先使用带公告日的 Tushare 财务报表数据。"""
        return self._failover(
            method_name="get_finance_snapshot",
            provider_order=["tushare"],
            columns=FINANCE_SNAPSHOT_COLUMNS,
            symbols=symbols,
            as_of=as_of,
            periods=periods,
            context=f"{len(symbols)} symbols" if symbols else "",
        )

    def get_consensus_estimates(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """卖方盈利预测明细：Tushare report_rc。"""
        return self._failover(
            method_name="get_consensus_estimates",
            provider_order=["tushare"],
            columns=CONSENSUS_ESTIMATE_COLUMNS,
            start_date=start_date,
            end_date=end_date,
            symbols=symbols,
            context=f"{start_date}..{end_date}",
        )

    def get_valuation_history(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        """Point-in-time daily valuation history from Tushare daily_basic."""
        return self._failover(
            method_name="get_valuation_history",
            provider_order=["tushare"],
            columns=VALUATION_HISTORY_COLUMNS,
            start_date=start_date,
            end_date=end_date,
            symbols=symbols,
            context=f"{start_date}..{end_date}",
        )

    # ------------------------------------------------------------------
    # 基础信息
    # ------------------------------------------------------------------

    def get_stock_basic_info(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """股票基础信息：Tushare → AKShare → Efinance → Baostock"""
        provider_order = ["tushare", "akshare", "efinance", "baostock"]
        if Config.parquet_fallback_enabled():
            provider_order.insert(0, "local_stock_info")
        return self._failover(
            method_name="get_stock_basic_info",
            provider_order=provider_order,
            columns=STOCK_BASIC_INFO_COLUMNS,
            symbols=symbols,
        )

    def get_stock_belong_boards(self, symbols: list[str], trade_date: str | None = None) -> pd.DataFrame:
        """股票所属板块：Efinance → AKShare"""
        return self._failover(
            method_name="get_stock_belong_boards",
            provider_order=["efinance", "akshare"],
            columns=STOCK_BELONG_BOARDS_COLUMNS,
            symbols=symbols,
            trade_date=trade_date,
            context=f"{len(symbols)} symbols",
        )

    # ------------------------------------------------------------------
    # 公告 / 研报 / 热点
    # ------------------------------------------------------------------

    def get_announcements(self, code: str, count: int = 10) -> list[dict[str, Any]]:
        """公告查询：巨潮资讯 → Tushare → AKShare（三层降级）"""
        for provider_name in ["cninfo", "tushare", "akshare"]:
            provider = self.providers.get(provider_name)
            if provider and provider.is_available():
                try:
                    result = provider.get_announcements(code=code, count=count)
                    if result:
                        return result
                except Exception as exc:
                    logger.warning(
                        "Provider %s.get_announcements failed for %s: %s",
                        provider_name,
                        code,
                        exc,
                    )
                    continue
        return []

    def get_research_reports(self, code: str, count: int = 5) -> list[dict[str, Any]]:
        """研报查询：AKShare"""
        for provider_name in ["akshare"]:
            provider = self.providers.get(provider_name)
            if provider and provider.is_available():
                try:
                    result = provider.get_research_reports(code=code, count=count)
                    if result:
                        return result
                except Exception as exc:
                    logger.warning(
                        "Provider %s.get_research_reports failed for %s: %s",
                        provider_name,
                        code,
                        exc,
                    )
                    continue
        return []

    def get_periodic_reports(
        self,
        code: str,
        report_types: list[str] | None = None,
        count: int = 10,
    ) -> list[dict[str, Any]]:
        """Periodic reports: CNINFO -> AKShare."""
        for provider_name in ["cninfo", "akshare"]:
            provider = self.providers.get(provider_name)
            if provider and provider.is_available():
                try:
                    result = provider.get_periodic_reports(
                        code=code,
                        report_types=report_types,
                        count=count,
                    )
                    if result:
                        return result
                except Exception as exc:
                    logger.warning(
                        "Provider %s.get_periodic_reports failed for %s: %s",
                        provider_name,
                        code,
                        exc,
                    )
                    continue
        return []

    def get_investor_interactions(self, code: str, count: int = 10) -> list[dict[str, Any]]:
        """Board secretary / IR interactions: AKShare CNINFO IRM or SSE e-interaction."""
        for provider_name in ["akshare"]:
            provider = self.providers.get(provider_name)
            if provider and provider.is_available():
                now = time.monotonic()
                with self._interaction_lock:
                    if now < self._interaction_circuit_open_until:
                        logger.warning("investor interactions circuit open: %s", provider_name)
                        return []
                    if self._interaction_inflight is not None and self._interaction_inflight.is_alive():
                        logger.warning("investor interactions request already running: %s", provider_name)
                        return []

                    outcome: dict[str, Any] = {}

                    def _fetch() -> None:
                        try:
                            outcome["result"] = provider.get_investor_interactions(
                                code=code, count=count
                            )
                        except Exception as exc:
                            outcome["error"] = exc

                    worker = threading.Thread(
                        target=_fetch,
                        name="investor-interactions",
                        daemon=True,
                    )
                    self._interaction_inflight = worker
                    worker.start()

                worker.join(self._interaction_timeout_seconds)
                if worker.is_alive():
                    with self._interaction_lock:
                        self._interaction_circuit_open_until = (
                            time.monotonic() + self._interaction_circuit_seconds
                        )
                    logger.warning(
                        "investor interactions timed out after %.2fs: %s",
                        self._interaction_timeout_seconds,
                        provider_name,
                    )
                    return []

                with self._interaction_lock:
                    if self._interaction_inflight is worker:
                        self._interaction_inflight = None
                if "error" in outcome:
                    logger.warning(
                        "investor interactions failed: %s: %s",
                        provider_name,
                        outcome["error"],
                    )
                    continue
                result = outcome.get("result") or []
                if result:
                    return result
        return []

    def get_hot_themes(self) -> dict[str, Any]:
        """热点题材：iFinD → AKShare（同花顺页面）"""
        for provider_name in ["ifind", "akshare"]:
            provider = self.providers.get(provider_name)
            if provider and provider.is_available():
                try:
                    result = provider.get_hot_themes()
                    if result and "error" not in result:
                        return result
                except Exception as exc:
                    logger.warning(
                        "Provider %s.get_hot_themes failed: %s",
                        provider_name,
                        exc,
                    )
                    continue
        return {"error": "热点题材查询失败"}

    def semantic_search(self, query: str, count: int = 20) -> dict[str, Any]:
        """语义搜索：问财 → iFinD → AKShare 板块近似搜索 → 百度AI搜索。

        五级降级（含熔断）：
        1. 问财：自然语言语义搜索选股（最强但每日额度有限）
        2. iFinD：专业金融语义搜索（需付费）
        3. AKShare：从查询中提取板块关键词，查板块成分股（近似语义搜索）
        4. 百度AI搜索：通用搜索降级
        熔断：已失败的数据源直接跳过，不重复支付失败成本
        """
        # 熔断管理器
        try:
            from infrastructure.data_providers.market.provider_health import provider_health
        except Exception:
            provider_health = None

        # 第一优先级：问财 / iFinD
        for provider_name in ["wencai", "ifind"]:
            # 熔断检查
            if provider_health and not provider_health.is_available(provider_name):
                logger.info("semantic_search: %s 熔断中，跳过", provider_name)
                continue

            provider = self.providers.get(provider_name)
            if provider and provider.is_available():
                try:
                    result = provider.semantic_search(query=query, count=count)
                    if result and result.get("results"):
                        if provider_health:
                            provider_health.report_success(provider_name)
                        return result
                    else:
                        # 返回空结果，检查是否需要熔断
                        error = (result or {}).get("error", "")
                        if error and provider_health:
                            provider_health.auto_detect_failure(provider_name, error)
                except Exception as e:
                    if provider_health:
                        provider_health.auto_detect_failure(provider_name, str(e))
                    continue

        # 第二优先级：AKShare 板块近似搜索
        akshare_available = True
        if provider_health and not provider_health.is_available("akshare"):
            logger.info("semantic_search: AKShare 熔断中，跳过")
            akshare_available = False

        if akshare_available:
            clean_query = re.sub(r"(概念股|上市公司|龙头|A股|股票|概念|板块|相关公司)$", "", query).strip()
            if clean_query:
                akshare = self.providers.get("akshare")
                if akshare and akshare.is_available():
                    try:
                        df = akshare.get_concept_board_members(clean_query)
                        results = self._concept_member_search_results(df, count=count)
                        if results:
                            if provider_health:
                                provider_health.report_success("akshare")
                            return {
                                "source": "akshare_board_fallback",
                                "query": query,
                                "results": results,
                                "code_count": len(df),
                            }
                    except Exception as e:
                        if provider_health:
                            provider_health.auto_detect_failure("akshare", str(e))

        # 第三优先级：百度AI搜索
        baidu_available = True
        if provider_health and not provider_health.is_available("baidu"):
            logger.info("semantic_search: 百度熔断中，跳过")
            baidu_available = False

        if baidu_available:
            try:
                from infrastructure.data_providers.research.baidu_search_provider import BaiduSearchProvider
                baidu = BaiduSearchProvider()
                if baidu.is_available():
                    search_results = baidu.search(query, count=min(count, 5))
                    if search_results:
                        if provider_health:
                            provider_health.report_success("baidu")
                        return {
                            "source": "baidu_search_fallback",
                            "query": query,
                            "results": search_results,
                            "code_count": len(search_results),
                        }
                    elif provider_health:
                        provider_health.auto_detect_failure("baidu", "百度搜索返回空结果")
            except Exception as e:
                if provider_health:
                    provider_health.auto_detect_failure("baidu", str(e))

        # 第四优先级：DuckDuckGo/Bing 兜底（不受其他源熔断影响）
        ddg_available = True
        if provider_health and not provider_health.is_available("duckduckgo"):
            ddg_available = False

        if ddg_available:
            try:
                from ddgs import DDGS
            except ImportError:
                try:
                    from duckduckgo_search import DDGS  # type: ignore[no-redef]
                except ImportError:
                    DDGS = None  # type: ignore[assignment, misc]

            if DDGS is not None:
                try:
                    ddg_results = []
                    with DDGS() as ddgs:
                        for r in ddgs.text(query, max_results=min(count, 10)):
                            ddg_results.append({
                                "title": str(r.get("title", "")),
                                "snippet": str(r.get("body", "")),
                                "url": str(r.get("href", "")),
                                "source": "duckduckgo",
                            })
                    if ddg_results:
                        if provider_health:
                            provider_health.report_success("duckduckgo")
                        return {
                            "source": "duckduckgo_fallback",
                            "query": query,
                            "results": ddg_results,
                            "code_count": len(ddg_results),
                        }
                except Exception as e:
                    if provider_health:
                        provider_health.auto_detect_failure("duckduckgo", str(e))

        return {"source": "none", "query": query, "results": [], "error": f"语义搜索失败: {query}"}

    # ------------------------------------------------------------------
    # 状态检查
    # ------------------------------------------------------------------

    def get_all(self) -> dict[str, BaseMarketDataProvider]:
        """Return registered provider instances.

        Older research-engine components use this accessor when iterating
        provider capabilities. Keeping it here prevents those paths from
        bypassing the registry failover layer.
        """
        return dict(self.providers)

    def get_available_providers(self) -> list[str]:
        """Return provider names that report themselves as available."""
        available: list[str] = []
        for name, provider in self.providers.items():
            try:
                if provider.is_available():
                    available.append(name)
            except Exception:
                continue
        return available

    def health_check_all(self) -> dict[str, ProviderHealthSnapshot]:
        """Return ResearchEngine-compatible provider health snapshots."""
        snapshots: dict[str, ProviderHealthSnapshot] = {}
        for name, provider in self.providers.items():
            try:
                raw = provider.health()
                available = bool(raw.get("available", False))
                snapshots[name] = ProviderHealthSnapshot(
                    enabled=True,
                    status="ok" if available else "unavailable",
                    capabilities=list(raw.get("capabilities") or []),
                    last_error=str(raw.get("last_error") or ""),
                    sample_event_count=int(raw.get("sample_event_count") or 0),
                )
            except Exception as exc:
                snapshots[name] = ProviderHealthSnapshot(
                    enabled=True,
                    status="error",
                    capabilities=[],
                    last_error=str(exc),
                    sample_event_count=0,
                )
        return snapshots

    def status(self) -> dict[str, Any]:
        """所有数据源健康状态。"""
        return {name: provider.health() for name, provider in self.providers.items()}

    def log_frame(self) -> pd.DataFrame:
        columns = [
            "trade_date", "method", "context", "provider_used",
            "success", "row_count", "fetch_time", "error_message", "updated_at",
        ]
        if not self.logs:
            return empty_frame(columns)
        return ensure_columns(pd.DataFrame(self.logs), columns)

    # ------------------------------------------------------------------
    # 内部降级机制
    # ------------------------------------------------------------------

    def _failover(
        self,
        method_name: str,
        provider_order: list[str],
        columns: list[str],
        **kwargs: Any,
    ) -> pd.DataFrame:
        context = str(kwargs.pop("context", "") or "")
        trade_date = kwargs.get("trade_date") or kwargs.get("end_date") or kwargs.get("start_date")
        for provider_name in provider_order:
            start = time.perf_counter()
            try:
                df = self._call_provider(provider_name, method_name, **kwargs)
                elapsed = round(time.perf_counter() - start, 4)
                if df is not None and not df.empty:
                    result = ensure_columns(df, columns)
                    self._record_log(trade_date, method_name, context, provider_name, True, len(result), elapsed, "")
                    return result
                self._record_log(trade_date, method_name, context, provider_name, False, 0, elapsed, "empty result")
            except Exception as exc:
                elapsed = round(time.perf_counter() - start, 4)
                self._record_log(trade_date, method_name, context, provider_name, False, 0, elapsed, str(exc))
                logger.warning("Provider %s.%s failed: %s", provider_name, method_name, exc)
        return empty_frame(columns)

    def _call_provider(self, provider_name: str, method_name: str, **kwargs: Any) -> pd.DataFrame:
        if provider_name == "local_parquet":
            return self._get_local_daily_bars(
                start_date=str(kwargs.get("start_date") or ""),
                end_date=str(kwargs.get("end_date") or ""),
                symbols=kwargs.get("symbols"),
            )
        if provider_name == "local_stock_info":
            return self._get_local_stock_basic_info(symbols=kwargs.get("symbols"))

        provider = self.providers.get(provider_name)
        if provider is None:
            raise RuntimeError("provider not registered")
        if not provider.is_available():
            raise RuntimeError("provider unavailable")
        method: Callable[..., pd.DataFrame] | None = getattr(provider, method_name, None)
        if method is None:
            raise RuntimeError("method unsupported")
        # 过滤掉 None 值的参数，避免签名不匹配
        filtered = {k: v for k, v in kwargs.items() if v is not None}
        return method(**filtered)

    # ------------------------------------------------------------------
    # 本地 Parquet 降级
    # ------------------------------------------------------------------

    def _get_local_daily_bars(
        self,
        start_date: str,
        end_date: str,
        symbols: list[str] | None = None,
    ) -> pd.DataFrame:
        parquet_path = self.parquet_dir / "stock_daily.parquet"
        if not parquet_path.exists():
            return empty_frame(DAILY_BARS_COLUMNS)
        try:
            import polars as pl

            lf = pl.scan_parquet(str(parquet_path))
            schema_names = set(lf.collect_schema().names())
            date_col = "trade_date" if "trade_date" in schema_names else "date"
            start_norm = normalize_trade_date(start_date)
            end_norm = normalize_trade_date(end_date)
            lf = lf.filter((pl.col(date_col).cast(pl.Utf8) >= start_norm) & (pl.col(date_col).cast(pl.Utf8) <= end_norm))
            symbol_col = _first_existing(schema_names, ["symbol", "ts_code", "stock_code", "code"])
            if symbols and symbol_col:
                symbol_values = set(normalize_symbols(symbols))
                code_values = {item.split(".")[0] for item in symbol_values}
                lf = lf.filter(pl.col(symbol_col).cast(pl.Utf8).is_in(list(symbol_values | code_values)))
            df = lf.collect().to_pandas()
        except Exception:
            df = pd.read_parquet(parquet_path)

        if df.empty:
            return empty_frame(DAILY_BARS_COLUMNS)
        if "trade_date" not in df.columns and "date" in df.columns:
            df["trade_date"] = df["date"]
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        start_norm = normalize_trade_date(start_date)
        end_norm = normalize_trade_date(end_date)
        df = df[(df["trade_date"] >= start_norm) & (df["trade_date"] <= end_norm)]

        raw_symbol = pick_column(df, ["symbol", "ts_code", "stock_code", "code"])
        result = pd.DataFrame(index=df.index)
        result["trade_date"] = df["trade_date"]
        result["symbol"] = normalize_symbols(raw_symbol)
        result["stock_name"] = pick_column(df, ["stock_name", "stockName", "name"], "").astype(str)
        result["open"] = to_numeric_series(pick_column(df, ["open"]))
        result["high"] = to_numeric_series(pick_column(df, ["high"]))
        result["low"] = to_numeric_series(pick_column(df, ["low"]))
        result["close"] = to_numeric_series(pick_column(df, ["close"]))
        result["pre_close"] = to_numeric_series(pick_column(df, ["pre_close", "prev_close"]))
        result["pct_chg"] = to_numeric_series(pick_column(df, ["pct_chg", "change_pct"]))
        result["amount"] = to_numeric_series(pick_column(df, ["amount", "turnover"]))
        result["volume"] = to_numeric_series(pick_column(df, ["volume", "vol"]))
        result["turnover_rate"] = to_numeric_series(pick_column(df, ["turnover_rate"]))
        result["volume_ratio"] = to_numeric_series(pick_column(df, ["volume_ratio"]))
        result["total_market_cap"] = to_numeric_series(pick_column(df, ["total_market_cap", "total_mv"]))
        result["float_market_cap"] = to_numeric_series(pick_column(df, ["float_market_cap", "float_mv", "circ_mv"]))
        result["provider"] = "local_parquet"
        result["updated_at"] = now_text()
        return ensure_columns(result[result["symbol"].astype(bool)], DAILY_BARS_COLUMNS)

    def _get_local_stock_basic_info(self, symbols: list[str] | None = None) -> pd.DataFrame:
        parquet_path = self.parquet_dir / "stock_info.parquet"
        if not parquet_path.exists():
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)
        df = pd.read_parquet(parquet_path)
        if df.empty:
            return empty_frame(STOCK_BASIC_INFO_COLUMNS)
        result = pd.DataFrame(index=df.index)
        result["symbol"] = normalize_symbols(pick_column(df, ["symbol", "ts_code", "stock_code", "code"]))
        result["stock_name"] = pick_column(df, ["stock_name", "stockName", "name"], "").astype(str)
        result["industry"] = pick_column(df, ["industry"], "").astype(str)
        result["area"] = pick_column(df, ["area"], "").astype(str)
        result["list_date"] = pick_column(df, ["list_date"], "").astype(str)
        result["business_scope"] = pick_column(df, ["business_scope"], "").astype(str)
        result["main_business"] = pick_column(df, ["main_business"], "").astype(str)
        result["provider"] = "local_stock_info"
        result["updated_at"] = now_text()
        result = ensure_columns(result[result["symbol"].astype(bool)], STOCK_BASIC_INFO_COLUMNS)
        if symbols:
            symbol_set = set(normalize_symbols(symbols))
            result = result[result["symbol"].isin(symbol_set)]
        return result

    def _concept_member_search_results(self, df: pd.DataFrame | None, count: int) -> list[dict[str, Any]]:
        if df is None or df.empty:
            return []
        results: list[dict[str, Any]] = []
        for _, row in df.head(count).iterrows():
            results.append(
                {
                    "股票代码": str(row.get("symbol", row.get("code", ""))).strip(),
                    "股票简称": str(row.get("stock_name", row.get("name", ""))).strip(),
                    "板块名称": str(row.get("board_name", "")).strip(),
                    "来源": str(row.get("provider", "")).strip(),
                }
            )
        return results

    def _record_log(
        self,
        trade_date: str | None,
        method: str,
        context: str,
        provider: str,
        success: bool,
        row_count: int,
        fetch_time: float,
        error_message: str,
    ) -> None:
        self.logs.append(
            {
                "trade_date": normalize_trade_date(trade_date),
                "method": method,
                "context": context,
                "provider_used": provider,
                "success": success,
                "row_count": int(row_count),
                "fetch_time": float(fetch_time),
                "error_message": error_message,
                "updated_at": now_text(),
            }
        )


def _first_existing(names: set[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in names:
            return candidate
    return None
