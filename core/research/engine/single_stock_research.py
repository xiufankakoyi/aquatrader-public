"""单股深度研报生成器。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.research.evidence.evidence_center import Evidence, EvidenceCenter
from core.events.models import NormalizedEvent, RawEvent
from core.research.engine.models import (
    Entity,
    EntityRegistry,
    EvidenceGraph,
    MoatDetector,
    Relation,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _mapping_list(value: Any) -> list[Dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _safe_limit(value: Any, default: int = 10) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


class SingleStockResearch:
    """Generate deep research report for a single stock.

    Report structure (12 sections):
    1. 公司基本信息
    2. 近期行情表现
    3. 关键公告
    4. 关键新闻
    5. 研报观点
    6. 财报信号
    7. 题材归属
    8. 护城河判断依据
    9. 风险点
    10. 量化确认
    11. 图谱关系
    12. 结论等级: DEEP_RESEARCH_READY / WATCH / INSUFFICIENT_EVIDENCE / BLOCK
    """

    def __init__(
        self,
        provider_registry: Optional[Any] = None,
        evidence_center: Optional[EvidenceCenter] = None,
        entity_registry: Optional[EntityRegistry] = None,
        evidence_graph: Optional[EvidenceGraph] = None,
        small_batch_processor: Optional[Any] = None,
        fast_validation_loop: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ) -> None:
        """初始化单股深度研报生成器。

        Args:
            provider_registry: 数据提供者注册表
            evidence_center: 证据中心实例
            entity_registry: 实体注册表实例
            evidence_graph: 证据图谱实例
            small_batch_processor: 小批量处理器实例
            fast_validation_loop: 快速验证循环实例
            event_bus: 事件总线实例
        """
        self.provider_registry = provider_registry
        self.evidence_center = evidence_center or EvidenceCenter()
        self.entity_registry = entity_registry or EntityRegistry()
        self.evidence_graph = evidence_graph or EvidenceGraph(self.entity_registry)
        self.small_batch_processor = small_batch_processor
        self.fast_validation_loop = fast_validation_loop
        self.event_bus = event_bus
        self.moat_detector = MoatDetector(self.evidence_graph)
        self._report_dir = PROJECT_ROOT / "data" / "reports" / "single_stock"

    def research(self, symbol: str, days: int = 365) -> Dict[str, Any]:
        """生成单只股票的深度研报。

        流程:
        1. 从 Provider 获取公司概况
        2. 获取 K 线数据
        3. 获取公告
        4. 获取新闻
        5. 获取研报
        6. 获取财务数据
        7. 对新闻/研报/公告运行 SmallBatchProcessor
        8. 收集证据
        9. 构建实体注册表和证据图谱
        10. 运行 FastValidationLoop
        11. 编制报告

        Args:
            symbol: 股票代码
            days: 回看天数

        Returns:
            包含 12 个部分的研报字典
        """
        symbol = str(symbol or "").strip()
        try:
            days = max(1, int(days))
        except (TypeError, ValueError):
            days = 365
        start_time = datetime.now()
        trade_date = start_time.strftime("%Y-%m-%d")

        logger.info("开始深度研报: %s, 回看 %d 天", symbol, days)

        # 1. 获取公司概况
        company_profile = self._fetch_company_profile(symbol)

        # 2. 获取 K 线数据
        kline_data = self._fetch_kline(symbol, days)

        # 3. 获取公告
        announcements = self._fetch_announcements(symbol, limit=10)

        # 4. 获取新闻
        news = self._fetch_news(symbol, limit=10)

        # 5. 获取研报
        reports = self._fetch_reports(symbol, limit=10)

        # 6. 获取财务数据
        financial = self._fetch_financial(symbol)

        # 7. 运行 SmallBatchProcessor
        extraction_results = self._run_extraction(announcements, news, reports)

        # 8. 收集证据
        self._collect_evidence(symbol, company_profile, announcements, news,
                               reports, financial, extraction_results, trade_date)

        # 9. 构建实体注册表和证据图谱
        self._build_graph(symbol, company_profile, extraction_results)

        # 10. 运行 FastValidationLoop
        validation_result = self._run_validation(symbol, trade_date)

        # 11. 编制报告
        data = {
            "company_profile": company_profile,
            "kline": kline_data,
            "announcements": announcements,
            "news": news,
            "reports": reports,
            "financial": financial,
            "extraction_results": extraction_results,
            "validation": validation_result,
        }
        report = self._compile_report(symbol, data)

        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(
            "深度研报完成: %s, 耗时 %.1f 秒, 决策=%s",
            symbol, elapsed, report.get("conclusion_level", "UNKNOWN"),
        )

        return report

    # ------------------------------------------------------------------
    # 数据获取方法
    # ------------------------------------------------------------------

    def _fetch_company_profile(self, symbol: str) -> Dict[str, Any]:
        """从 Provider 获取公司基本信息。

        Args:
            symbol: 股票代码

        Returns:
            公司概况字典
        """
        profile: Dict[str, Any] = {"symbol": symbol}

        # 1. 尝试从 ProviderRegistry 获取
        if self.provider_registry is not None:
            try:
                providers = self.provider_registry.get_all()
                for name, provider in providers.items():
                    if not provider.enabled:
                        continue
                    if not hasattr(provider, "fetch_company_profile"):
                        continue
                    try:
                        events = provider.fetch_company_profile(symbols=[symbol])
                        if events:
                            ev = events[0]
                            profile["stock_name"] = ev.stock_name or ""
                            profile["provider"] = name
                            # 从 payload 中提取详细信息
                            if ev.payload:
                                profile.update(ev.payload)
                            break
                    except Exception as exc:
                        logger.debug("Provider %s 获取公司概况失败: %s", name, exc)
            except Exception as exc:
                logger.debug("ProviderRegistry 获取公司概况失败: %s", exc)

        # 2. 尝试从 LanceDB stock_info 获取
        if not profile.get("stock_name"):
            try:
                from data_svc.query.fast_market_query import get_fast_market_query

                fmq = get_fast_market_query()
                df = fmq._read_table(
                    "stock_info",
                    fields=["stock_code", "name", "industry"],
                )
                if df is not None and not df.is_empty():
                    import polars as pl
                    row = df.filter(pl.col("stock_code") == symbol)
                    if not row.is_empty():
                        r = row.row(0, named=True)
                        profile["stock_name"] = str(r.get("name", ""))
                        profile["industry"] = str(r.get("industry", ""))
            except Exception:
                logger.debug("LanceDB 获取公司概况失败: %s", symbol, exc_info=True)

        # 3. 从证据中心获取
        if not profile.get("stock_name"):
            for ev in self.evidence_center.evidences:
                if symbol in ev.symbols and ev.entity_name:
                    profile["stock_name"] = ev.entity_name
                    break

        if not profile.get("stock_name"):
            profile["stock_name"] = symbol

        return profile

    def _fetch_kline(self, symbol: str, days: int) -> List[Dict[str, Any]]:
        """获取 K 线数据。

        Args:
            symbol: 股票代码
            days: 回看天数

        Returns:
            K 线数据列表
        """
        days = max(1, _safe_limit(days, 365))
        kline: List[Dict[str, Any]] = []

        # 1. 尝试从 ProviderRegistry 获取
        if self.provider_registry is not None:
            try:
                end_date = datetime.now().strftime("%Y-%m-%d")
                start_date = (
                    datetime.now() - timedelta(days=days)
                ).strftime("%Y-%m-%d")

                providers = self.provider_registry.get_all()
                for name, provider in providers.items():
                    if not provider.enabled:
                        continue
                    if not hasattr(provider, "fetch_kline"):
                        continue
                    try:
                        events = provider.fetch_kline(
                            symbols=[symbol],
                            start_date=start_date,
                            end_date=end_date,
                        )
                        if events:
                            for ev in events:
                                kline.append({
                                    "trade_date": ev.trade_date,
                                    "close": ev.payload.get("close") if ev.payload else None,
                                    "open": ev.payload.get("open") if ev.payload else None,
                                    "high": ev.payload.get("high") if ev.payload else None,
                                    "low": ev.payload.get("low") if ev.payload else None,
                                    "volume": ev.payload.get("volume") if ev.payload else None,
                                })
                            break
                    except Exception as exc:
                        logger.debug("Provider %s 获取 K 线失败: %s", name, exc)
            except Exception as exc:
                logger.debug("ProviderRegistry 获取 K 线失败: %s", exc)

        # 2. 尝试从 LanceDB 获取
        kline = _mapping_list(kline)
        if not kline:
            try:
                from data_svc.query.fast_market_query import get_fast_market_query

                fmq = get_fast_market_query()
                latest_date = fmq.get_latest_trade_date()
                if latest_date:
                    start = (
                        datetime.strptime(latest_date, "%Y-%m-%d") - timedelta(days=days)
                    ).strftime("%Y-%m-%d")
                    df = fmq._read_table(
                        "daily_ohlcv",
                        start_date=start,
                        end_date=latest_date,
                        fields=["trade_date", "stock_code", "open", "high", "low",
                                 "close", "volume", "amount"],
                    )
                    if df is not None and not df.is_empty():
                        import polars as pl
                        stock_df = (
                            df.filter(pl.col("stock_code") == symbol)
                            .sort("trade_date")
                        )
                        for row in stock_df.to_dicts():
                            kline.append({
                                "trade_date": str(row.get("trade_date", ""))[:10],
                                "open": row.get("open"),
                                "high": row.get("high"),
                                "low": row.get("low"),
                                "close": row.get("close"),
                                "volume": row.get("volume"),
                                "amount": row.get("amount"),
                            })
            except Exception:
                logger.debug("LanceDB 获取 K 线失败: %s", symbol, exc_info=True)

        return kline

    def _fetch_announcements(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        """获取公告数据。

        Args:
            symbol: 股票代码
            limit: 返回数量上限

        Returns:
            公告列表
        """
        limit = _safe_limit(limit)
        announcements: List[Dict[str, Any]] = []

        if self.provider_registry is not None:
            try:
                providers = self.provider_registry.get_all()
                for name, provider in providers.items():
                    if not provider.enabled:
                        continue
                    if not hasattr(provider, "fetch_announcements"):
                        continue
                    try:
                        events = provider.fetch_announcements(symbols=[symbol], limit=limit)
                        for ev in events:
                            announcements.append({
                                "title": ev.title,
                                "date": ev.trade_date or ev.event_time,
                                "summary": ev.summary or ev.content[:200] if ev.content else "",
                                "source_url": ev.source_url,
                                "provider": name,
                            })
                        if announcements:
                            break
                    except Exception as exc:
                        logger.debug("Provider %s 获取公告失败: %s", name, exc)
            except Exception as exc:
                logger.debug("ProviderRegistry 获取公告失败: %s", exc)

        return announcements[:limit]

    def _fetch_news(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        """获取新闻数据。

        Args:
            symbol: 股票代码
            limit: 返回数量上限

        Returns:
            新闻列表
        """
        limit = _safe_limit(limit)
        news: List[Dict[str, Any]] = []

        if self.provider_registry is not None:
            try:
                providers = self.provider_registry.get_all()
                for name, provider in providers.items():
                    if not provider.enabled:
                        continue
                    if not hasattr(provider, "fetch_news"):
                        continue
                    try:
                        events = provider.fetch_news(symbols=[symbol], limit=limit)
                        for ev in events:
                            news.append({
                                "title": ev.title,
                                "date": ev.trade_date or ev.event_time,
                                "summary": ev.summary or ev.content[:200] if ev.content else "",
                                "source_url": ev.source_url,
                                "provider": name,
                            })
                        if news:
                            break
                    except Exception as exc:
                        logger.debug("Provider %s 获取新闻失败: %s", name, exc)
            except Exception as exc:
                logger.debug("ProviderRegistry 获取新闻失败: %s", exc)

        return news[:limit]

    def _fetch_reports(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        """获取研报数据。

        Args:
            symbol: 股票代码
            limit: 返回数量上限

        Returns:
            研报列表
        """
        limit = _safe_limit(limit)
        reports: List[Dict[str, Any]] = []

        if self.provider_registry is not None:
            try:
                providers = self.provider_registry.get_all()
                for name, provider in providers.items():
                    if not provider.enabled:
                        continue
                    if not hasattr(provider, "fetch_reports"):
                        continue
                    try:
                        events = provider.fetch_reports(symbols=[symbol], limit=limit)
                        for ev in events:
                            reports.append({
                                "title": ev.title,
                                "date": ev.trade_date or ev.event_time,
                                "summary": ev.summary or ev.content[:200] if ev.content else "",
                                "source_url": ev.source_url,
                                "provider": name,
                            })
                        if reports:
                            break
                    except Exception as exc:
                        logger.debug("Provider %s 获取研报失败: %s", name, exc)
            except Exception as exc:
                logger.debug("ProviderRegistry 获取研报失败: %s", exc)

        return reports[:limit]

    def _fetch_financial(self, symbol: str) -> List[Dict[str, Any]]:
        """获取财务数据。

        Args:
            symbol: 股票代码

        Returns:
            财务数据列表
        """
        financial: List[Dict[str, Any]] = []

        # 1. 尝试从 ProviderRegistry 获取
        if self.provider_registry is not None:
            try:
                providers = self.provider_registry.get_all()
                for name, provider in providers.items():
                    if not provider.enabled:
                        continue
                    if not hasattr(provider, "fetch_finance_snapshot"):
                        continue
                    try:
                        events = provider.fetch_finance_snapshot(symbols=[symbol])
                        for ev in events:
                            financial.append({
                                "title": ev.title,
                                "date": ev.trade_date,
                                "data": ev.payload or {},
                                "provider": name,
                            })
                        if financial:
                            break
                    except Exception as exc:
                        logger.debug("Provider %s 获取财务数据失败: %s", name, exc)
            except Exception as exc:
                logger.debug("ProviderRegistry 获取财务数据失败: %s", exc)

        # 2. 尝试从 LanceDB 获取
        if not financial:
            try:
                from data_svc.query.fast_market_query import get_fast_market_query

                fmq = get_fast_market_query()
                latest_date = fmq.get_latest_trade_date()
                if latest_date:
                    df = fmq._read_table(
                        "daily_ohlcv",
                        start_date=latest_date,
                        end_date=latest_date,
                        fields=["trade_date", "stock_code", "close", "volume", "amount"],
                    )
                    if df is not None and not df.is_empty():
                        import polars as pl
                        row = df.filter(pl.col("stock_code") == symbol)
                        if not row.is_empty():
                            r = row.row(0, named=True)
                            financial.append({
                                "title": f"行情快照 {latest_date}",
                                "date": latest_date,
                                "data": {
                                    "close": r.get("close"),
                                    "volume": r.get("volume"),
                                    "amount": r.get("amount"),
                                },
                                "provider": "lancedb",
                            })
            except Exception:
                logger.debug("LanceDB 获取财务数据失败: %s", symbol, exc_info=True)

        return financial

    # ------------------------------------------------------------------
    # 处理和构建方法
    # ------------------------------------------------------------------

    def _run_extraction(
        self,
        announcements: List[Dict[str, Any]],
        news: List[Dict[str, Any]],
        reports: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """对新闻/研报/公告运行 SmallBatchProcessor。

        Args:
            announcements: 公告列表
            news: 新闻列表
            reports: 研报列表

        Returns:
            抽取结果列表
        """
        announcements = _mapping_list(announcements)
        news = _mapping_list(news)
        reports = _mapping_list(reports)
        if not self.small_batch_processor:
            return []

        # 将原始数据转为 NormalizedEvent
        all_items = []
        for item in announcements:
            all_items.append(NormalizedEvent(
                event_type="announcement",
                symbol=item.get("symbol", ""),
                stock_name=item.get("stock_name", ""),
                title=item.get("title", ""),
                content=item.get("summary", ""),
                data_date=item.get("date", ""),
            ))
        for item in news:
            all_items.append(NormalizedEvent(
                event_type="news",
                symbol=item.get("symbol", ""),
                stock_name=item.get("stock_name", ""),
                title=item.get("title", ""),
                content=item.get("summary", ""),
                data_date=item.get("date", ""),
            ))
        for item in reports:
            all_items.append(NormalizedEvent(
                event_type="report",
                symbol=item.get("symbol", ""),
                stock_name=item.get("stock_name", ""),
                title=item.get("title", ""),
                content=item.get("summary", ""),
                data_date=item.get("date", ""),
            ))

        if not all_items:
            return []

        try:
            results = self.small_batch_processor.process_batch(all_items)
            return [asdict(r) for r in results]
        except Exception as exc:
            logger.error("SmallBatchProcessor 处理失败: %s", exc, exc_info=True)
            return []

    def _collect_evidence(
        self,
        symbol: str,
        company_profile: Dict[str, Any],
        announcements: List[Dict[str, Any]],
        news: List[Dict[str, Any]],
        reports: List[Dict[str, Any]],
        financial: List[Dict[str, Any]],
        extraction_results: List[Dict[str, Any]],
        trade_date: str,
    ) -> None:
        """收集证据到证据中心。

        Args:
            symbol: 股票代码
            company_profile: 公司概况
            announcements: 公告列表
            news: 新闻列表
            reports: 研报列表
            financial: 财务数据
            extraction_results: 抽取结果
            trade_date: 交易日期
        """
        company_profile = company_profile if isinstance(company_profile, dict) else {}
        announcements = _mapping_list(announcements)
        news = _mapping_list(news)
        reports = _mapping_list(reports)
        financial = _mapping_list(financial)
        extraction_results = _mapping_list(extraction_results)
        company_profile = company_profile if isinstance(company_profile, dict) else {}
        extraction_results = _mapping_list(extraction_results)
        stock_name = company_profile.get("stock_name", symbol)

        # 从公告生成证据
        for ann in announcements:
            self.evidence_center.add(Evidence(
                evidence_id=EvidenceCenter._new_id(),
                trade_date=trade_date,
                entity_type="stock",
                entity_id=symbol,
                entity_name=stock_name,
                source_type="announcement",
                source_name="provider_announcement",
                title=ann.get("title", "公告"),
                summary=ann.get("summary", "")[:200],
                symbols=[symbol],
                themes=[],
                evidence_strength="medium",
                data_date=ann.get("date", trade_date),
                created_at=EvidenceCenter._now(),
            ))

        # 从新闻生成证据
        for n in news:
            self.evidence_center.add(Evidence(
                evidence_id=EvidenceCenter._new_id(),
                trade_date=trade_date,
                entity_type="stock",
                entity_id=symbol,
                entity_name=stock_name,
                source_type="news",
                source_name="provider_news",
                title=n.get("title", "新闻"),
                summary=n.get("summary", "")[:200],
                symbols=[symbol],
                themes=[],
                evidence_strength="medium",
                data_date=n.get("date", trade_date),
                created_at=EvidenceCenter._now(),
            ))

        # 从研报生成证据
        for r in reports:
            self.evidence_center.add(Evidence(
                evidence_id=EvidenceCenter._new_id(),
                trade_date=trade_date,
                entity_type="stock",
                entity_id=symbol,
                entity_name=stock_name,
                source_type="report",
                source_name="provider_report",
                title=r.get("title", "研报"),
                summary=r.get("summary", "")[:200],
                symbols=[symbol],
                themes=[],
                evidence_strength="strong",
                data_date=r.get("date", trade_date),
                created_at=EvidenceCenter._now(),
            ))

        # 从财务数据生成证据
        for f in financial:
            self.evidence_center.add(Evidence(
                evidence_id=EvidenceCenter._new_id(),
                trade_date=trade_date,
                entity_type="stock",
                entity_id=symbol,
                entity_name=stock_name,
                source_type="finance_radar",
                source_name="provider_finance",
                title=f.get("title", "财务数据"),
                summary=json.dumps(f.get("data", {}), ensure_ascii=False)[:200],
                symbols=[symbol],
                themes=[],
                evidence_strength="medium",
                data_date=f.get("date", trade_date),
                created_at=EvidenceCenter._now(),
            ))

        # 从抽取结果生成证据
        for ext in extraction_results:
            for item in _mapping_list(ext.get("evidence_items")):
                self.evidence_center.add(Evidence(
                    evidence_id=EvidenceCenter._new_id(),
                    trade_date=trade_date,
                    entity_type="stock",
                    entity_id=symbol,
                    entity_name=stock_name,
                    source_type=ext.get("processor", "rule_based"),
                    source_name=f"extraction_{ext.get('doc_type', 'unknown')}",
                    title=item.get("label", "抽取证据"),
                        summary=str(item.get("evidence_text", "") or "")[:200],
                    symbols=[symbol],
                    themes=ext.get("themes", []),
                    evidence_strength=(
                        "strong" if _safe_float(item.get("confidence", 0)) >= 0.7
                        else "medium" if _safe_float(item.get("confidence", 0)) >= 0.4
                        else "weak"
                    ),
                    data_date=trade_date,
                    created_at=EvidenceCenter._now(),
                ))

    def _build_graph(
        self,
        symbol: str,
        company_profile: Dict[str, Any],
        extraction_results: List[Dict[str, Any]],
    ) -> None:
        """构建实体注册表和证据图谱。

        Args:
            symbol: 股票代码
            company_profile: 公司概况
            extraction_results: 抽取结果
        """
        stock_name = company_profile.get("stock_name", symbol)

        # 确保股票实体存在
        stock_entity = self.entity_registry.get_by_symbol(symbol)
        if not stock_entity:
            stock_entity = Entity(
                entity_type="stock",
                entity_name=stock_name,
                symbols=[symbol],
            )
            self.entity_registry.add(stock_entity)

        # 从抽取结果中提取题材、产品、行业实体
        for ext in extraction_results:
            # 题材实体
            for theme in ext.get("themes", []) if isinstance(ext.get("themes"), list) else []:
                if not isinstance(theme, str):
                    continue
                if not theme:
                    continue
                theme_entity = self.entity_registry.get_by_name(theme)
                if not theme_entity:
                    theme_entity = Entity(
                        entity_type="theme",
                        entity_name=theme,
                        aliases=[theme],
                    )
                    self.entity_registry.add(theme_entity)

                # 建立关系
                self.evidence_graph.add_relation(Relation(
                    source_entity_id=stock_entity.entity_id,
                    target_entity_id=theme_entity.entity_id,
                    relation_type="BELONGS_TO_THEME",
                    weight=0.5,
                    confidence="medium",
                    data_date=ext.get("processed_at", ""),
                ))

            # 产品实体
            for product in ext.get("products", []) if isinstance(ext.get("products"), list) else []:
                if not isinstance(product, str):
                    continue
                if not product:
                    continue
                product_entity = self.entity_registry.get_by_name(product)
                if not product_entity:
                    product_entity = Entity(
                        entity_type="product",
                        entity_name=product,
                    )
                    self.entity_registry.add(product_entity)

                self.evidence_graph.add_relation(Relation(
                    source_entity_id=stock_entity.entity_id,
                    target_entity_id=product_entity.entity_id,
                    relation_type="HAS_PRODUCT",
                    weight=0.5,
                    confidence="medium",
                ))

            # 行业实体
            for industry in ext.get("industries", []) if isinstance(ext.get("industries"), list) else []:
                if not isinstance(industry, str):
                    continue
                if not industry:
                    continue
                industry_entity = self.entity_registry.get_by_name(industry)
                if not industry_entity:
                    industry_entity = Entity(
                        entity_type="industry",
                        entity_name=industry,
                    )
                    self.entity_registry.add(industry_entity)

                self.evidence_graph.add_relation(Relation(
                    source_entity_id=stock_entity.entity_id,
                    target_entity_id=industry_entity.entity_id,
                    relation_type="BELONGS_TO_INDUSTRY",
                    weight=0.5,
                    confidence="medium",
                ))

        # 运行护城河检测
        combined_text = " ".join(
            ext.get("summary", "") for ext in extraction_results if ext.get("summary")
        )
        if combined_text:
            self.moat_detector.detect_from_text(
                combined_text, symbol=symbol, stock_name=stock_name
            )

    def _run_validation(self, symbol: str, trade_date: str) -> Dict[str, Any]:
        """运行 FastValidationLoop。

        Args:
            symbol: 股票代码
            trade_date: 交易日期

        Returns:
            验证结果字典
        """
        if self.fast_validation_loop is None:
            return {
                "validation_score": 0.0,
                "decision": "INSUFFICIENT_EVIDENCE",
                "major_risks": [],
                "note": "FastValidationLoop 未初始化",
            }

        try:
            return self.fast_validation_loop.validate(symbol, trade_date)
        except Exception as exc:
            logger.error("FastValidationLoop 运行失败: %s", exc, exc_info=True)
            return {
                "validation_score": 0.0,
                "decision": "INSUFFICIENT_EVIDENCE",
                "major_risks": [{"type": "validation_error", "detail": str(exc)}],
            }

    # ------------------------------------------------------------------
    # 报告编制
    # ------------------------------------------------------------------

    def _compile_report(self, symbol: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """将所有数据编制为 12 部分研报。

        Args:
            symbol: 股票代码
            data: 收集的数据

        Returns:
            12 部分研报字典
        """
        company_profile = data.get("company_profile", {})
        kline = data.get("kline", [])
        announcements = data.get("announcements", [])
        news = data.get("news", [])
        reports = data.get("reports", [])
        financial = data.get("financial", [])
        extraction_results = data.get("extraction_results", [])
        validation = data.get("validation", {})

        stock_name = company_profile.get("stock_name", symbol)

        # Section 7: 题材归属
        themes = []
        for ext in extraction_results:
            themes.extend(ext.get("themes", []))

        # Section 8: 护城河判断依据
        moat_summary = self.moat_detector.get_stock_moat_summary(symbol)
        moat_evidence_ids = self._get_moat_evidence_ids(symbol)

        # Section 9: 风险点
        risks = []
        risk_evidence_ids: List[str] = []
        for ext in extraction_results:
            for risk in ext.get("risks", []):
                risks.append(risk)
        # 从验证结果中获取重大风险
        for major_risk in validation.get("major_risks", []):
            risks.append(major_risk.get("detail", ""))
            risk_evidence_ids.extend(major_risk.get("evidence_ids", []))

        # Section 10: 量化确认
        quant_confirmation = {
            "validation_score": validation.get("validation_score", 0.0),
            "decision": validation.get("decision", "UNKNOWN"),
            "factor_score": validation.get("factor_score", 0.0),
            "signal_score": validation.get("signal_score", 0.0),
            "trend_score": validation.get("trend_score", 0.0),
        }

        # Section 11: 图谱关系
        graph_relations = []
        stock_entity = self.entity_registry.get_by_symbol(symbol)
        if stock_entity:
            chains = self.evidence_graph.get_evidence_chain(stock_entity.entity_id)
            for chain in chains[:20]:
                graph_relations.append({
                    "relation_type": chain.get("relation_type", ""),
                    "target_name": chain.get("target_name", ""),
                    "weight": chain.get("weight", 0.0),
                    "confidence": chain.get("confidence", ""),
                    "evidence_ids": chain.get("evidence_ids", []),
                })

        # Section 12: 结论等级
        validation_decision = validation.get("decision", "INSUFFICIENT_EVIDENCE")
        conclusion_level = self._map_conclusion_level(validation_decision)

        # K 线摘要
        kline_summary = self._summarize_kline(kline)

        report: Dict[str, Any] = {
            # 元信息
            "symbol": symbol,
            "stock_name": stock_name,
            "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "lookback_days": data.get("lookback_days", 365),

            # Section 1: 公司基本信息
            "company_profile": {
                "symbol": symbol,
                "stock_name": stock_name,
                "industry": company_profile.get("industry", "暂无本地证据"),
                "provider": company_profile.get("provider", "暂无本地证据"),
            },

            # Section 2: 近期行情表现
            "kline_summary": kline_summary,
            "kline_data_count": len(kline),

            # Section 3: 关键公告
            "announcements": announcements if announcements else "暂无本地证据",

            # Section 4: 关键新闻
            "news": news if news else "暂无本地证据",

            # Section 5: 研报观点
            "research_reports": reports if reports else "暂无本地证据",

            # Section 6: 财报信号
            "financial_signals": financial if financial else "暂无本地证据",

            # Section 7: 题材归属
            "themes": list(set(themes)) if themes else "暂无本地证据",

            # Section 8: 护城河判断依据
            "moat": {
                "has_moat": moat_summary.get("has_moat", False),
                "moat_types": moat_summary.get("moat_types", []),
                "evidence_ids": moat_evidence_ids if moat_evidence_ids else "暂无本地证据",
            },

            # Section 9: 风险点
            "risks": list(set(risks)) if risks else "暂无本地证据",
            "risk_evidence_ids": risk_evidence_ids if risk_evidence_ids else "暂无本地证据",

            # Section 10: 量化确认
            "quant_confirmation": quant_confirmation,

            # Section 11: 图谱关系
            "graph_relations": graph_relations if graph_relations else "暂无本地证据",

            # Section 12: 结论等级
            "conclusion_level": conclusion_level,
            "validation_score": validation.get("validation_score", 0.0),
            "major_risks": validation.get("major_risks", []),
        }

        return report

    def _format_md(self, report: Dict[str, Any]) -> str:
        """将研报格式化为 Markdown。

        Args:
            report: 研报字典

        Returns:
            Markdown 格式字符串
        """
        symbol = str(report.get("symbol", "") or "")
        stock_name = report.get("stock_name", "")
        lines = [
            f"# 深度研报: {stock_name}({symbol})",
            "",
            f"- 生成时间: {report.get('generated_at', '')}",
            f"- 回看天数: {report.get('lookback_days', 365)}",
            f"- 结论等级: **{report.get('conclusion_level', 'UNKNOWN')}**",
            f"- 验证得分: {report.get('validation_score', 0.0):.1f}",
            "",
            "---",
            "",
            "## 1. 公司基本信息",
            "",
        ]

        profile = report.get("company_profile", {})
        if isinstance(profile, dict):
            lines.append(f"- 股票代码: {profile.get('symbol', '')}")
            lines.append(f"- 股票名称: {profile.get('stock_name', '')}")
            lines.append(f"- 行业: {profile.get('industry', '暂无本地证据')}")
        else:
            lines.append(str(profile))

        lines.extend(["", "## 2. 近期行情表现", ""])
        kline_summary = report.get("kline_summary", {})
        if isinstance(kline_summary, dict):
            lines.append(f"- 数据天数: {report.get('kline_data_count', 0)}")
            if kline_summary.get("latest_close"):
                lines.append(f"- 最新收盘价: {kline_summary.get('latest_close', 'N/A')}")
            if kline_summary.get("return_5d") is not None:
                lines.append(f"- 5日收益: {kline_summary.get('return_5d', 0):.2%}")
            if kline_summary.get("return_20d") is not None:
                lines.append(f"- 20日收益: {kline_summary.get('return_20d', 0):.2%}")
            if kline_summary.get("trend"):
                lines.append(f"- 趋势判断: {kline_summary.get('trend', '')}")
        else:
            lines.append("暂无本地证据")

        lines.extend(["", "## 3. 关键公告", ""])
        announcements = report.get("announcements", [])
        if isinstance(announcements, list) and announcements:
            for ann in announcements[:5]:
                lines.append(f"- [{ann.get('date', '')}] {ann.get('title', '')}")
        else:
            lines.append("暂无本地证据")

        lines.extend(["", "## 4. 关键新闻", ""])
        news = report.get("news", [])
        if isinstance(news, list) and news:
            for n in news[:5]:
                lines.append(f"- [{n.get('date', '')}] {n.get('title', '')}")
        else:
            lines.append("暂无本地证据")

        lines.extend(["", "## 5. 研报观点", ""])
        research_reports = report.get("research_reports", [])
        if isinstance(research_reports, list) and research_reports:
            for r in research_reports[:5]:
                lines.append(f"- [{r.get('date', '')}] {r.get('title', '')}")
        else:
            lines.append("暂无本地证据")

        lines.extend(["", "## 6. 财报信号", ""])
        financial_signals = report.get("financial_signals", [])
        if isinstance(financial_signals, list) and financial_signals:
            for f in financial_signals[:5]:
                lines.append(f"- {f.get('title', '')}")
        else:
            lines.append("暂无本地证据")

        lines.extend(["", "## 7. 题材归属", ""])
        themes = report.get("themes", [])
        if isinstance(themes, list) and themes:
            for t in themes:
                lines.append(f"- {t}")
        else:
            lines.append("暂无本地证据")

        lines.extend(["", "## 8. 护城河判断依据", ""])
        moat = report.get("moat", {})
        if isinstance(moat, dict):
            lines.append(f"- 是否有护城河: {'是' if moat.get('has_moat') else '否'}")
            moat_types = moat.get("moat_types", [])
            if moat_types:
                lines.append(f"- 护城河类型: {', '.join(moat_types)}")
            ev_ids = moat.get("evidence_ids", [])
            if isinstance(ev_ids, list) and ev_ids:
                lines.append(f"- 证据 ID: {', '.join(ev_ids[:5])}")
            else:
                lines.append("- 证据: 暂无本地证据")
        else:
            lines.append("暂无本地证据")

        lines.extend(["", "## 9. 风险点", ""])
        risks = report.get("risks", [])
        if isinstance(risks, list) and risks:
            for r in risks[:10]:
                lines.append(f"- {r}")
        else:
            lines.append("暂无本地证据")
        risk_ev_ids = report.get("risk_evidence_ids", [])
        if isinstance(risk_ev_ids, list) and risk_ev_ids:
            lines.append(f"- 风险证据 ID: {', '.join(risk_ev_ids[:5])}")

        lines.extend(["", "## 10. 量化确认", ""])
        quant = report.get("quant_confirmation", {})
        if isinstance(quant, dict):
            lines.append(f"- 验证得分: {quant.get('validation_score', 0.0):.1f}")
            lines.append(f"- 决策: {quant.get('decision', 'N/A')}")
            lines.append(f"- 因子得分: {quant.get('factor_score', 0.0):.4f}")
            lines.append(f"- 信号得分: {quant.get('signal_score', 0.0):.4f}")
            lines.append(f"- 趋势得分: {quant.get('trend_score', 0.0):.4f}")
        else:
            lines.append("暂无本地证据")

        lines.extend(["", "## 11. 图谱关系", ""])
        graph_relations = report.get("graph_relations", [])
        if isinstance(graph_relations, list) and graph_relations:
            for rel in _mapping_list(graph_relations)[:10]:
                lines.append(
                    f"- {rel.get('relation_type', '')} -> "
                    f"{rel.get('target_name', '')} "
                    f"(权重={rel.get('weight', 0.0):.2f}, "
                    f"置信度={rel.get('confidence', '')})"
                )
        else:
            lines.append("暂无本地证据")

        lines.extend(["", "## 12. 结论等级", ""])
        lines.append(f"**{report.get('conclusion_level', 'UNKNOWN')}**")
        lines.append(f"- 验证得分: {report.get('validation_score', 0.0):.1f}")
        major_risks = report.get("major_risks", [])
        if major_risks:
            lines.append("- 重大风险:")
            for r in major_risks:
                lines.append(f"  - {r.get('type', '')}: {r.get('detail', '')}")

        lines.append("")
        return "\n".join(lines)

    def export_report(self, symbol: str, report: Dict[str, Any]) -> Dict[str, Any]:
        """导出研报到 data/reports/single_stock/{symbol}_research_latest.json 和 .md。

        Args:
            symbol: 股票代码
            report: 研报字典

        Returns:
            导出摘要
        """
        symbol = Path(str(symbol or "unknown")).name
        report = report if isinstance(report, dict) else {}
        self._report_dir.mkdir(parents=True, exist_ok=True)

        # JSON
        json_path = self._report_dir / f"{symbol}_research_latest.json"
        try:
            json_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str, allow_nan=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("导出 JSON 研报失败: %s", exc)

        # Markdown
        md_path = self._report_dir / f"{symbol}_research_latest.md"
        try:
            md_content = self._format_md(report)
            md_path.write_text(md_content, encoding="utf-8")
        except Exception as exc:
            logger.warning("导出 Markdown 研报失败: %s", exc)

        return {
            "symbol": symbol,
            "json_path": str(json_path),
            "md_path": str(md_path),
            "conclusion_level": report.get("conclusion_level", "UNKNOWN"),
        }

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize_kline(kline: List[Dict[str, Any]]) -> Dict[str, Any]:
        """汇总 K 线数据。

        Args:
            kline: K 线数据列表

        Returns:
            K 线摘要字典
        """
        if not kline:
            return {"trend": "暂无本地证据"}

        kline = _mapping_list(kline)
        if not kline:
            return {"trend": "暂无本地证据"}

        summary: Dict[str, Any] = {
            "data_count": len(kline),
            "latest_date": kline[-1].get("trade_date", ""),
            "earliest_date": kline[0].get("trade_date", ""),
        }

        closes = []
        for item in kline:
            if item.get("close") is None:
                continue
            value = _safe_float(item.get("close"), float("nan"))
            if isfinite(value):
                closes.append(value)
        if closes:
            summary["latest_close"] = closes[-1]
            summary["highest"] = max(closes)
            summary["lowest"] = min(closes)

            # 收益率计算
            if len(closes) >= 6 and abs(closes[-6]) >= 1e-12:
                summary["return_5d"] = closes[-1] / closes[-6] - 1.0
            if len(closes) >= 21 and abs(closes[-21]) >= 1e-12:
                summary["return_20d"] = closes[-1] / closes[-21] - 1.0

            # 趋势判断
            if len(closes) >= 5:
                ret_5d = summary.get("return_5d", 0)
                ret_20d = summary.get("return_20d", 0)
                if ret_5d is not None and ret_5d > 0.03:
                    summary["trend"] = "短期上涨"
                elif ret_5d is not None and ret_5d < -0.03:
                    summary["trend"] = "短期下跌"
                elif ret_20d is not None and ret_20d > 0.05:
                    summary["trend"] = "中期上涨"
                elif ret_20d is not None and ret_20d < -0.05:
                    summary["trend"] = "中期下跌"
                else:
                    summary["trend"] = "震荡"

        return summary

    @staticmethod
    def _map_conclusion_level(validation_decision: str) -> str:
        """将验证决策映射为研报结论等级。

        Args:
            validation_decision: 验证决策

        Returns:
            结论等级: DEEP_RESEARCH_READY / WATCH / INSUFFICIENT_EVIDENCE / BLOCK
        """
        mapping = {
            "PASS": "DEEP_RESEARCH_READY",
            "WATCH": "WATCH",
            "INSUFFICIENT_EVIDENCE": "INSUFFICIENT_EVIDENCE",
            "BLOCK": "BLOCK",
        }
        return mapping.get(validation_decision, "INSUFFICIENT_EVIDENCE")

    def _get_moat_evidence_ids(self, symbol: str) -> List[str]:
        """获取与护城河相关的证据 ID。

        Args:
            symbol: 股票代码

        Returns:
            证据 ID 列表
        """
        entity = self.entity_registry.get_by_symbol(symbol)
        if not entity:
            return []

        moat_relations = self.evidence_graph.get_relations(
            entity.entity_id, direction="outgoing", relation_type="HAS_MOAT"
        )

        evidence_ids: List[str] = []
        for rel in moat_relations:
            evidence_ids.extend(rel.evidence_ids)

        return evidence_ids
