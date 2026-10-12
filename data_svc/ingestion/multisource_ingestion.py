"""多数据源摄入组合器。"""

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from data_svc.ingestion.crawler_etiquette import CrawlerEtiquette
from data_svc.ingestion.document_store import DocumentStore
from data_svc.ingestion.event_deduper import EventDeduper
from data_svc.ingestion.normalizer import Normalizer
from data_svc.ingestion.watermark_manager import WatermarkManager
from data_svc.unified_data_query import UnifiedDataQuery


@dataclass
class GatewayIngestionConfig:
    """网关摄入配置。"""

    symbols: List[str] = field(default_factory=list)
    include_capabilities: List[str] = field(
        default_factory=lambda: ["market_snapshot", "news_feed", "recent_events"]
    )
    max_items_per_capability: int = 100


@dataclass
class GatewayIngestionResult:
    """网关摄入结果。"""

    success: bool = True
    batch_trace_id: str = ""
    raw_events: List[Any] = field(default_factory=list)
    raw_documents: List[Any] = field(default_factory=list)
    published_event_count: int = 0
    saved_document_count: int = 0
    elapsed_ms: int = 0
    warnings: List[str] = field(default_factory=list)


class MultiSourceIngestion:
    """
    组合多数据源同步能力。

    当前实现优先使用本地 ``UnifiedDataQuery`` 兜底，再按需接入外部 provider。
    这样即使外部数据源不可用，管线也不会崩溃。
    """

    def __init__(
        self,
        registry: Any = None,
        event_bus: Any = None,
        source_router: Any = None,
        gateway: Any = None,
        gateway_config: Optional[GatewayIngestionConfig] = None,
    ):
        """
        初始化多源摄入器。

        Args:
            registry: 数据源注册中心（可选）。
            event_bus: 事件总线（可选）。
            source_router: 数据源路由器（可选）。
            gateway: 数据网关实例（可选）。
            gateway_config: 网关摄入配置（可选）。
        """
        self.watermarks = WatermarkManager()
        self.normalizer = Normalizer()
        self.deduper = EventDeduper()
        self.store = DocumentStore()
        self.etiquette = CrawlerEtiquette()
        self.query = UnifiedDataQuery()
        self.registry = registry
        self.event_bus = event_bus
        self.source_router = source_router
        self.gateway = gateway
        self.gateway_config = gateway_config or GatewayIngestionConfig()

    def sync_all_sources(
        self,
        symbols: Optional[List[str]] = None,
        sources: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        同步所有指定数据源，将新增数据以事件形式存入文档存储。

        Args:
            symbols: 需要同步的股票代码列表。
            sources: 数据源名称列表，None 表示仅使用本地查询层。

        Returns:
            每个数据源同步结果的字典。
        """
        results: Dict[str, Any] = {}
        symbols = symbols or []

        # 1. 本地兜底：把已有本地数据作为事件写入
        if symbols:
            try:
                local_count = 0
                for sym in symbols:
                    try:
                        df = self.query.get_stock_history(
                            sym,
                            "2020-01-01",
                            datetime.now().strftime("%Y-%m-%d"),
                        )
                        if df is not None and not df.empty:
                            normalized = self.normalizer.normalize_daily(df, sym, "local")
                            for row in normalized.to_dict(orient="records"):
                                h = self.deduper.event_hash({"source": "local", "symbol": sym, "row": row})
                                self.store.insert_event("daily_data", row, h)
                                local_count += 1
                    except Exception as e:
                        results[f"local_{sym}"] = f"error: {e}"
                results["local"] = {"status": "ok", "count": local_count}
            except Exception as e:
                results["local"] = f"error: {e}"

        # 2. 外部数据源：按需接入，失败时降级记录
        if sources:
            for src in sources:
                try:
                    self.etiquette.wait_if_needed(src)
                    records = self._sync_external(src, symbols)
                    self.watermarks.set_watermark(src)
                    results[src] = {"status": "ok", "count": records}
                except Exception as e:
                    results[src] = {"status": "error", "detail": str(e)}

        return results

    def _sync_external(self, source: str, symbols: List[str]) -> int:
        """
        同步单个外部数据源，子类或后续实现可覆盖此方法。

        默认实现返回 0，表示尚未接入该源。避免对未统一接口的外部 provider 强依赖。
        """
        return 0

    def ingest_events(
        self,
        event_type: str,
        events: List[dict],
        source: str = "manual",
    ) -> Dict[str, int]:
        """
        批量摄入外部事件，自动去重后写入文档存储。

        Args:
            event_type: 事件类型。
            events: 事件列表。
            source: 来源标识。

        Returns:
            {"inserted": int, "duplicated": int}
        """
        new_events = self.deduper.deduplicate(events)
        inserted = 0
        for ev in new_events:
            h = self.deduper.event_hash({"source": source, **ev})
            if self.store.insert_event(event_type, ev, h):
                inserted += 1
        return {"inserted": inserted, "duplicated": len(events) - inserted}

    def collect_from_gateway(
        self,
        symbols: Optional[List[str]] = None,
        include_capabilities: Optional[List[str]] = None,
        max_items_per_capability: int = 100,
    ) -> GatewayIngestionResult:
        """
        从数据网关收集原始事件与文档。

        Args:
            symbols: 股票代码列表，默认使用 gateway_config.symbols。
            include_capabilities: 需要采集的能力列表。
            max_items_per_capability: 每种能力最大采集条数。

        Returns:
            网关摄入结果。
        """
        t0 = time.time()
        symbols = symbols or self.gateway_config.symbols or []
        capabilities = include_capabilities or self.gateway_config.include_capabilities
        raw_events: List[Any] = []
        raw_documents: List[Any] = []
        published = 0
        warnings: List[str] = []

        if self.gateway is None:
            warnings.append("gateway not provided, returning empty result")
            return GatewayIngestionResult(
                success=True,
                batch_trace_id=str(uuid.uuid4()),
                raw_events=raw_events,
                raw_documents=raw_documents,
                elapsed_ms=int((time.time() - t0) * 1000),
                warnings=warnings,
            )

        try:
            if "market_snapshot" in capabilities:
                try:
                    snap = self.gateway.get_market_snapshot(symbols)
                    for sym, payload in (snap or {}).items():
                        raw_events.append({
                            "event_type": "market_snapshot",
                            "symbol": sym,
                            "payload": payload,
                        })
                except Exception as e:
                    warnings.append(f"market_snapshot failed: {e}")

            if "news_feed" in capabilities:
                try:
                    news = self.gateway.get_news_feed(symbols, limit=max_items_per_capability)
                    for item in news or []:
                        raw_documents.append(item)
                except Exception as e:
                    warnings.append(f"news_feed failed: {e}")

            if "recent_events" in capabilities:
                try:
                    ev_df = self.gateway.get_recent_events(limit=max_items_per_capability)
                    for _, row in (ev_df or pd.DataFrame()).iterrows():
                        raw_events.append(row.to_dict())
                except Exception as e:
                    warnings.append(f"recent_events failed: {e}")
        except Exception as e:
            warnings.append(f"gateway collection failed: {e}")

        if self.event_bus is not None:
            for ev in raw_events:
                try:
                    self.event_bus.publish(ev)
                    published += 1
                except Exception as e:
                    warnings.append(f"event publish failed: {e}")

        return GatewayIngestionResult(
            success=True,
            batch_trace_id=str(uuid.uuid4()),
            raw_events=raw_events,
            raw_documents=raw_documents,
            published_event_count=published,
            saved_document_count=0,
            elapsed_ms=int((time.time() - t0) * 1000),
            warnings=warnings,
        )
