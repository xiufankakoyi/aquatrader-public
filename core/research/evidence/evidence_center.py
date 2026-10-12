"""Evidence Center v0.1 — 最小证据中心，为 QuantFlow 和后续 LLM 行研提供结构化证据。"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, asdict
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.events.models import EventTopic, NormalizedEvent, ExtractionResult

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return number if isfinite(number) else default


def _safe_int(value: Any, default: int = 0) -> int:
    number = _safe_float(value, float(default))
    return int(number) if isfinite(number) else default

# 行情类事件类型集合
_MARKET_DATA_EVENT_TYPES = frozenset({"quote", "kline", "orderbook", "finance_snapshot"})
_VALUATION_EVENT_TYPES = frozenset({"valuation"})

# 不需要文本锚定的证据类型（结构化数据）
_STRUCTURED_EVIDENCE_TYPES = frozenset({
    "market_data", "quote_depth", "kline", "valuation",
})

# 分类标签类证据类型（板块/题材标签，不需要逐字锚定）
_TAXONOMY_EVIDENCE_TYPES = frozenset({"sector", "theme"})

# NormalizedEvent.event_type → Evidence.evidence_type 映射
_EVENT_TYPE_TO_EVIDENCE_TYPE: Dict[str, str] = {
    "quote": "market_data",
    "kline": "kline",
    "orderbook": "quote_depth",
    "valuation": "valuation",
    "finance_snapshot": "market_data",
    "report": "report",
    "announcement": "announcement",
    "news": "catalyst",
    "cls_flash": "catalyst",
    "global_news": "catalyst",
    "financial": "report",
    "company_profile": "report",
    "sector_snapshot": "sector",
    "theme_snapshot": "theme",
}

# ExtractionResult.doc_type → Evidence.evidence_type 映射
_DOC_TYPE_TO_EVIDENCE_TYPE: Dict[str, str] = {
    "report": "report",
    "announcement": "announcement",
    "news": "catalyst",
    "financial": "report",
    "research": "report",
    "catalyst": "catalyst",
    "risk": "risk",
    "moat": "moat",
    "theme": "theme",
    "sector": "sector",
    "stock": "market_data",
}


def _infer_grounding_type(
    evidence_type: str,
    grounding_type_from_item: str = "",
    processor: str = "",
    evidence_text: str = "",
) -> str:
    """推断证据的锚定类型。

    优先使用 LLM/grounding validator 返回的 grounding_type，
    否则根据 evidence_type 和 processor 推断。

    Args:
        evidence_type: 证据类型
        grounding_type_from_item: 从 evidence_item 传入的 grounding_type
        processor: 处理器类型
        evidence_text: 证据文本

    Returns:
        锚定类型字符串
    """
    # 优先使用已有的 grounding_type（来自 grounding validator）
    if grounding_type_from_item:
        return grounding_type_from_item

    # 结构化数据类型 → structured_field
    if evidence_type in _STRUCTURED_EVIDENCE_TYPES:
        return "structured_field"

    # 板块/题材标签 → taxonomy
    if evidence_type in _TAXONOMY_EVIDENCE_TYPES:
        return "taxonomy"

    # 规则引擎抽取（未经过 grounding validator）→ not_applicable
    if processor in ("rule_based", "rule_based_fallback", "rule_based_chunked"):
        return "not_applicable"

    # 默认：未做锚定校验
    return "not_applicable"


@dataclass
class Evidence:
    """单条结构化证据。"""

    evidence_id: str
    trade_date: str
    entity_type: str  # stock / theme / industry / event
    entity_id: str
    entity_name: str
    source_type: str  # dragon_eye / lancedb / news / report / manual / demo
    source_name: str
    title: str
    summary: str
    symbols: List[str] = field(default_factory=list)
    symbol: str = ""
    themes: List[str] = field(default_factory=list)
    evidence_strength: str = "medium"  # strong / medium / weak
    data_date: str = ""
    is_stale: bool = False
    provider: str = ""
    event_id: str = ""
    raw_event_id: str = ""
    document_id: str = ""
    trace_id: str = ""
    source_url: str = ""
    file_path: str = ""
    extraction_method: str = ""  # rule_based / local_llm / manual
    confidence: str = "medium"  # high / medium / low
    evidence_text: str = ""
    is_derived: bool = False
    evidence_type: str = ""  # market_data / valuation / quote_depth / kline / report / announcement / catalyst / risk / moat / theme / sector
    raw_hash: str = ""
    command_trace: str = ""
    industries: List[str] = field(default_factory=list)
    # v1.2 新增字段
    processor: str = ""  # rule_based / local_llm / hybrid / rule_based_fallback
    prompt_version: str = ""
    model_name: str = ""
    grounding_status: str = ""  # exact / partial / drop
    grounding_type: str = ""  # text_exact / text_partial / text_missing_drop / taxonomy / structured_field / not_applicable
    extraction_confidence: str = ""  # high / medium / low（抽取阶段置信度）
    cross_validation_status: str = ""  # cross_validated / rule_only / llm_only
    review_status: str = ""  # pending_human_review / reviewed / not_required
    quality_score: float = 0.0  # 六维评分总分
    quality_strength: str = ""  # strong / medium / weak（评分后强度）
    created_at: str = ""

    def __post_init__(self) -> None:
        self.symbols = [item for item in _as_list(self.symbols) if isinstance(item, str) and item]
        self.themes = [item for item in _as_list(self.themes) if isinstance(item, str) and item]
        self.industries = [item for item in _as_list(self.industries) if isinstance(item, str) and item]
        self.quality_score = max(0.0, min(1.0, _safe_float(self.quality_score)))


class EvidenceCenter:
    """证据中心：收集、标记、导出结构化证据。"""

    def __init__(self, report_dir: Optional[str] = None):
        self.evidences: List[Evidence] = []
        self.report_dir = Path(report_dir) if report_dir else PROJECT_ROOT / "data" / "reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._subscribed_event_buses: set[int] = set()

    @staticmethod
    def _new_id() -> str:
        """生成唯一证据 ID。"""
        return f"ev_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _now() -> str:
        """返回当前时间的 ISO 格式字符串。"""
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    def add(self, evidence: Evidence) -> None:
        """添加一条证据。

        Args:
            evidence: Evidence 实例
        """
        if isinstance(evidence, Evidence):
            self.evidences.append(evidence)

    def _output_path(self, filename: str) -> Path:
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise ValueError("filename must be a single file name")
        return self.report_dir / filename

    def collect_from_dragon_eye(self, dragon_data: Dict[str, Any], trade_date: str) -> None:
        """从 DragonEye 数据生成证据条目。

        Args:
            dragon_data: DragonEye summary stage 的 data 字典
            trade_date: 交易日
        """
        dragon_data = _as_mapping(dragon_data)
        evidence_date = dragon_data.get("evidence_date", trade_date)
        evidence = _as_mapping(dragon_data.get("evidence"))

        # 情绪证据 — 从 evidence.market_sentiment_cycle 提取
        sentiment = _as_mapping(evidence.get("market_sentiment_cycle"))
        if not sentiment:
            sentiment = _as_mapping(dragon_data.get("sentiment"))
        if sentiment:
            phase = sentiment.get("phase") or sentiment.get("sentiment_phase", "unknown")
            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="theme",
                entity_id="market_sentiment",
                entity_name="市场情绪",
                source_type="dragon_eye",
                source_name="dragon_eye_sentiment",
                title=f"市场情绪周期: {phase}",
                 summary=str(sentiment.get("description", sentiment.get("summary", "")) or "")[:200],
                themes=sentiment.get("themes", []),
                evidence_strength="medium",
                data_date=evidence_date,
                created_at=self._now(),
            ))

        # 涨停证据 — 从 evidence.limit_up_stats 提取
        limit_up = _as_mapping(evidence.get("limit_up_stats"))
        if not limit_up:
            limit_up = _as_mapping(dragon_data.get("limit_up_stats"))
        limit_up_count = limit_up.get("count", 0) if isinstance(limit_up, dict) else 0
        if not limit_up_count:
            limit_up_count = dragon_data.get("limit_up_count", 0)
        limit_up_count = _safe_int(limit_up_count)
        if limit_up_count > 0:
            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="event",
                entity_id="limit_up_pool",
                entity_name="涨停池",
                source_type="dragon_eye",
                source_name="dragon_eye_limit_up",
                title=f"涨停 {limit_up_count} 只",
                summary=f"当日涨停股票 {limit_up_count} 只",
                evidence_strength="strong",
                data_date=evidence_date,
                created_at=self._now(),
            ))

        # 题材流向证据 — 从 evidence.sector_heat_stats 提取
        sector_heat = evidence.get("sector_heat_stats", {}) if isinstance(evidence, dict) else {}
        theme_flow = dragon_data.get("theme_flow")
        if not theme_flow:
            # 从 sector_heat_stats 数据推断
            sector_data = sector_heat.get("data", []) if isinstance(sector_heat, dict) else []
            if sector_data:
                theme_flow = "有主线"
        if theme_flow and theme_flow not in ("unavailable", "无主线"):
            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="theme",
                entity_id="theme_flow",
                entity_name="题材流向",
                source_type="dragon_eye",
                source_name="dragon_eye_theme_flow",
                title=f"题材流向: {theme_flow}",
                summary=f"当日题材流向判断: {theme_flow}",
                evidence_strength="medium",
                data_date=evidence_date,
                created_at=self._now(),
            ))

    def collect_from_quant_flow(self, brief_data: Dict[str, Any], trade_date: str) -> None:
        """从 QuantFlow final brief 关键证据生成条目。

        Args:
            brief_data: final_research_brief 的 data 字典
            trade_date: 交易日
        """
        brief_data = _as_mapping(brief_data)
        # 市场强弱证据
        strength = _as_mapping(brief_data.get("market_strength_conclusion"))
        if strength:
            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="theme",
                entity_id="market_strength",
                entity_name="市场强弱",
                source_type="lancedb",
                source_name="quant_flow_market_regime",
                title=f"市场强弱: {strength.get('label', 'unknown')}",
                 summary=str(strength.get("summary", "") or "")[:200],
                evidence_strength="medium",
                data_date=strength.get("data_date", trade_date),
                created_at=self._now(),
            ))

        # 策略信号证据
        signals = _as_mapping(brief_data.get("signal_conclusion"))
        if signals:
            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="event",
                entity_id="strategy_signals",
                entity_name="策略信号",
                source_type="lancedb",
                source_name="quant_flow_strategy_scan",
                title=f"策略信号: {signals.get('status', 'unknown')}",
                 summary=str(signals.get("no_signal_reason", "") or "")[:200],
                evidence_strength="weak",
                data_date=signals.get("data_date", trade_date),
                created_at=self._now(),
            ))

    def collect_from_stock_screener(self, candidates_data: Dict[str, Any], trade_date: str) -> None:
        """从 stock_screener_candidates 阶段数据生成候选股票证据。

        Args:
            candidates_data: 候选股票数据，包含 candidates 列表及元信息
            trade_date: 交易日
        """
        candidates_data = _as_mapping(candidates_data)
        if candidates_data.get("no_candidates", False):
            return

        candidates = _as_list(candidates_data.get("candidates"))
        if not candidates:
            return

        # 候选数量超过 20 只时只取前 20 只
        candidates = candidates[:20]
        data_date = candidates_data.get("data_date", trade_date)

        for item in candidates:
            if not isinstance(item, dict):
                continue
            symbol = item.get("symbol", "")
            name = item.get("name", "")
            if not symbol:
                continue

            return_5d = _safe_float(item.get("return_5d", 0))
            return_20d = _safe_float(item.get("return_20d", 0))
            rsi_12 = _safe_float(item.get("rsi_12", 0))

            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="stock",
                entity_id=symbol,
                entity_name=name,
                source_type="quant_flow",
                source_name="quant_flow_stock_screener",
                title=f"因子筛选候选: {name}({symbol})",
                summary=f"5日收益{return_5d:.2%}, 20日收益{return_20d:.2%}, RSI{rsi_12:.0f}",
                symbols=[symbol],
                themes=[],
                evidence_strength="medium",
                data_date=data_date,
                is_stale=False,
                created_at=self._now(),
            ))

    def collect_from_strategy_signals(self, signals_data: Dict[str, Any], trade_date: str) -> None:
        """从 strategy_signal_scan 阶段数据生成策略信号证据。

        Args:
            signals_data: 策略信号数据，包含 buy/sell/watch 列表
            trade_date: 交易日
        """
        signals_data = _as_mapping(signals_data)
        buy_signals = _as_list(signals_data.get("buy"))
        if not buy_signals:
            return

        # 买入信号超过 20 个时只取前 20 个
        buy_signals = buy_signals[:20]
        data_date = signals_data.get("data_date", trade_date)

        for signal in buy_signals:
            if not isinstance(signal, dict):
                continue
            stock_code = signal.get("stock_code", "")
            stock_name = signal.get("stock_name", "")
            signal_name = signal.get("signal_name", "")
            signal_strength = _safe_float(signal.get("signal_strength", 0))

            if not stock_code:
                continue

            # 根据信号强度确定证据强度
            if signal_strength >= 0.7:
                strength = "strong"
            elif signal_strength >= 0.4:
                strength = "medium"
            else:
                strength = "weak"

            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="event",
                entity_id=stock_code,
                entity_name=stock_name,
                source_type="quant_flow",
                source_name="quant_flow_strategy_signal",
                title=f"策略买入信号: {stock_name}({stock_code})",
                summary=f"信号类型: {signal_name}, 信号强度: {signal_strength}",
                symbols=[stock_code],
                themes=[],
                evidence_strength=strength,
                data_date=data_date,
                is_stale=False,
                created_at=self._now(),
            ))

    def collect_from_market_regime(self, market_data: Dict[str, Any], trade_date: str) -> None:
        """从 market_regime_detect 阶段数据生成市场状态证据。

        生成 1 条市场状态总览证据 + 每个核心指数的趋势证据。

        Args:
            market_data: 市场状态数据，包含 regime、risk_state、breadth、index_trends
            trade_date: 交易日
        """
        market_data = _as_mapping(market_data)
        regime = str(market_data.get("regime", "") or "")
        risk_state = str(market_data.get("risk_state", "") or "")
        breadth = _as_mapping(market_data.get("breadth"))
        component_dates = _as_mapping(market_data.get("component_dates"))

        # 市场状态总览证据
        if regime:
            # 根据风险状态和市场强弱确定证据强度
            if risk_state == "风险偏高":
                strength = "weak"
            elif "偏强" in regime:
                strength = "strong"
            else:
                strength = "medium"

            # 拼接 summary：风险状态 + 资金面宽度信息
            summary_parts = [f"风险状态: {risk_state}"]
            if breadth:
                rise_count = breadth.get("rise_count", 0)
                fall_count = breadth.get("fall_count", 0)
                sample_count = breadth.get("sample_count", 0)
                summary_parts.append(
                    f"上涨{rise_count}/下跌{fall_count}/样本{sample_count}"
                )

            data_date = component_dates.get("breadth", trade_date)

            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="theme",
                entity_id="market_regime",
                entity_name="市场状态",
                source_type="lancedb",
                source_name="quant_flow_market_regime",
                title=f"市场状态: {regime}",
                summary=", ".join(summary_parts),
                symbols=[],
                themes=[],
                evidence_strength=strength,
                data_date=data_date,
                is_stale=False,
                created_at=self._now(),
            ))

        # 核心指数趋势证据
        index_trends = _as_list(market_data.get("index_trends"))
        for idx_item in index_trends:
            if not isinstance(idx_item, dict):
                continue
            symbol = idx_item.get("symbol", "")
            name = idx_item.get("name", "")
            return_5d = _safe_float(idx_item.get("return_5d", 0))
            return_20d = _safe_float(idx_item.get("return_20d", 0))
            idx_data_date = idx_item.get("data_date", trade_date)

            if not symbol:
                continue

            # 根据近 20 日收益确定证据强度
            if return_20d > 0.03:
                idx_strength = "strong"
            elif return_20d < -0.03:
                idx_strength = "weak"
            else:
                idx_strength = "medium"

            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="theme",
                entity_id=f"index_trend_{symbol}",
                entity_name=f"{name}趋势",
                source_type="lancedb",
                source_name="quant_flow_index_trend",
                title=f"{name}: 5日{return_5d:.2%}, 20日{return_20d:.2%}",
                summary=f"{name}近5日收益{return_5d:.2%}, 近20日收益{return_20d:.2%}",
                symbols=[symbol],
                themes=[],
                evidence_strength=idx_strength,
                data_date=idx_data_date,
                is_stale=False,
                created_at=self._now(),
            ))

    def collect_from_portfolio_risk(self, portfolio_data: Dict[str, Any], trade_date: str) -> None:
        """从 portfolio_risk_check 阶段数据生成持仓风险证据。

        Args:
            portfolio_data: 持仓风险数据，包含 active_position_count、mode、positions 等
            trade_date: 交易日
        """
        portfolio_data = _as_mapping(portfolio_data)
        active_position_count = _safe_int(portfolio_data.get("active_position_count", 0))
        mode = portfolio_data.get("mode", "demo")
        valuation_complete = portfolio_data.get("valuation_complete", False)

        # 生成持仓风险总览证据
        strength = "medium" if valuation_complete else "weak"

        self.add(Evidence(
            evidence_id=self._new_id(),
            trade_date=trade_date,
            entity_type="event",
            entity_id="portfolio_risk",
            entity_name="持仓风险",
            source_type="local_portfolio",
            source_name="quant_flow_portfolio_risk",
            title=f"持仓风险检查: {mode}模式, {active_position_count}只持仓",
            summary=f"模式: {mode}, 持仓数: {active_position_count}, 估值完成: {valuation_complete}",
            symbols=[],
            themes=[],
            evidence_strength=strength,
            data_date=trade_date,
            is_stale=False,
            created_at=self._now(),
        ))

        # 为每个持仓生成一条证据（最多 10 条）
        positions = _as_list(portfolio_data.get("positions"))
        if not positions:
            return

        for pos in positions[:10]:
            if not isinstance(pos, dict):
                continue
            stock_code = pos.get("stock_code") or pos.get("normalized_symbol", "")
            if not stock_code:
                continue

            market_value = _safe_float(pos.get("market_value", 0))
            weight = _safe_float(pos.get("weight", 0))

            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="stock",
                entity_id=stock_code,
                entity_name=stock_code,
                source_type="local_portfolio",
                source_name="quant_flow_portfolio_risk",
                title=f"持仓: {stock_code}, 市值{market_value:.0f}, 权重{weight:.2%}",
                summary=f"持仓股票{stock_code}, 市值{market_value:.0f}, 权重{weight:.2%}",
                symbols=[stock_code],
                themes=[],
                evidence_strength="medium",
                data_date=trade_date,
                is_stale=False,
                created_at=self._now(),
            ))

    def collect_from_industry_chain_if_available(self, trade_date: str) -> None:
        """尝试从本地产业链数据生成产业链证据。

        数据源: data/industry/company_evidence.parquet 或 knowledge/data_sources/company_evidence.csv。
        文件不存在或读取失败时静默返回，不抛异常。

        Args:
            trade_date: 交易日
        """
        # 尝试 parquet 和 csv 两个路径
        parquet_path = PROJECT_ROOT / "data" / "industry" / "company_evidence.parquet"
        csv_path = PROJECT_ROOT / "knowledge" / "data_sources" / "company_evidence.csv"

        try:
            import polars as pl

            if parquet_path.exists():
                df = pl.read_parquet(str(parquet_path))
            elif csv_path.exists():
                df = pl.read_csv(str(csv_path))
            else:
                return

            if df.is_empty():
                return

            # 最多生成 10 条证据
            row_count = min(df.height, 10)
            for i in range(row_count):
                row = df.row(i, named=True)

                stock_code = str(row.get("stock_code", ""))
                company_name = str(row.get("company_name", ""))
                evidence_type = str(row.get("evidence_type", ""))
                evidence_content = str(row.get("evidence_content", ""))
                concept_name = str(row.get("concept_name", ""))

                if not stock_code:
                    continue

                # 拼接标题和摘要
                title = f"产业链: {company_name or stock_code}"
                if concept_name:
                    title += f" - {concept_name}"

                summary_parts = []
                if evidence_type:
                    summary_parts.append(f"类型: {evidence_type}")
                if evidence_content:
                    summary_parts.append(evidence_content[:150])
                summary = ", ".join(summary_parts) if summary_parts else "产业链关联证据"

                self.add(Evidence(
                    evidence_id=self._new_id(),
                    trade_date=trade_date,
                    entity_type="industry",
                    entity_id=stock_code,
                    entity_name=company_name or stock_code,
                    source_type="industry_chain",
                    source_name="industry_chain_evidence",
                    title=title,
                    summary=summary,
                    symbols=[stock_code],
                    themes=[concept_name] if concept_name else [],
                    evidence_strength="medium",
                    data_date=trade_date,
                    is_stale=False,
                    created_at=self._now(),
                ))

        except Exception:
            logger.debug("产业链数据读取失败，静默跳过", exc_info=True)

    def collect_from_finance_radar_if_available(self, trade_date: str) -> None:
        """尝试从本地财务数据生成财务雷达估值分位证据。

        数据源: data/parquet_data/stock_daily.parquet。
        为估值分位极端的股票（PE 分位 < 20% 或 > 80%）生成证据。
        文件不存在或读取失败时静默返回，不抛异常。

        Args:
            trade_date: 交易日
        """
        parquet_path = PROJECT_ROOT / "data" / "parquet_data" / "stock_daily.parquet"
        if not parquet_path.exists():
            return

        try:
            import polars as pl

            df = pl.read_parquet(str(parquet_path))
            if df.is_empty():
                return

            # 检查必要列是否存在
            required_cols = {"symbol", "trade_date"}
            if not required_cols.issubset(set(df.columns)):
                return

            # 获取最新交易日的数据
            latest_date = df.select(pl.col("trade_date").max()).item()
            if latest_date is None:
                return
            latest_df = df.filter(pl.col("trade_date") == latest_date)

            # 如果有 PE 列，计算 PE 分位
            pe_col = None
            for col_name in ("pe_ttm", "pe", "PE"):
                if col_name in latest_df.columns:
                    pe_col = col_name
                    break

            if pe_col is None:
                return

            # 过滤有效 PE 值
            valid_df = latest_df.with_columns(
                pl.col(pe_col).cast(pl.Float64, strict=False).alias("_pe_value")
            ).filter(
                pl.col("_pe_value").is_not_null()
                & pl.col("_pe_value").is_finite()
                & (pl.col("_pe_value") > 0)
            )
            if valid_df.is_empty():
                return

            # 计算 PE 分位（百分位排名）
            valid_df = valid_df.with_columns(
                (pl.col("_pe_value").rank(method="average") / pl.len() * 100)
                .alias("pe_percentile")
            )

            # 筛选极端分位：PE 分位 < 20% 或 > 80%
            extreme_df = valid_df.filter(
                (pl.col("pe_percentile") < 20) | (pl.col("pe_percentile") > 80)
            ).sort("pe_percentile")

            if extreme_df.is_empty():
                return

            # 最多生成 10 条证据
            row_count = min(extreme_df.height, 10)
            for i in range(row_count):
                row = extreme_df.row(i, named=True)

                symbol = str(row.get("symbol", ""))
                if not symbol:
                    continue

                pe_value = _safe_float(row.get("_pe_value"))
                pe_percentile = _safe_float(row.get("pe_percentile"), 50.0)

                # 确定证据强度和方向
                if pe_percentile < 20:
                    direction = "低估"
                    strength = "strong"
                else:
                    direction = "高估"
                    strength = "weak"

                name_col = None
                for nc in ("name", "stock_name"):
                    if nc in row:
                        name_col = nc
                        break
                stock_name = str(row.get(name_col, "")) if name_col else symbol

                self.add(Evidence(
                    evidence_id=self._new_id(),
                    trade_date=trade_date,
                    entity_type="stock",
                    entity_id=symbol,
                    entity_name=stock_name,
                    source_type="finance_radar",
                    source_name="finance_radar_valuation",
                    title=f"估值分位{direction}: {stock_name}({symbol})",
                    summary=f"PE={pe_value:.1f}, 分位{pe_percentile:.1f}%, {direction}",
                    symbols=[symbol],
                    themes=[],
                    evidence_strength=strength,
                    data_date=str(latest_date),
                    is_stale=False,
                    created_at=self._now(),
                ))

        except Exception:
            logger.debug("财务雷达数据读取失败，静默跳过", exc_info=True)

    def mark_stale(self, global_latest_trade_date: str) -> int:
        """标记过时证据。

        Args:
            global_latest_trade_date: 全局最新交易日

        Returns:
            标记为 stale 的证据数量
        """
        stale_count = 0
        for ev in self.evidences:
            if ev.data_date and ev.data_date < global_latest_trade_date:
                ev.is_stale = True
                stale_count += 1
        return stale_count

    def export(self, filename: str = "evidence_center_latest.json") -> Dict[str, Any]:
        """导出证据到 JSON 文件。

        Args:
            filename: 输出文件名

        Returns:
            导出摘要 {"total": ..., "stale": ..., "by_source": {...}}
        """
        output_path = self._output_path(filename)
        items = [asdict(ev) for ev in self.evidences]

        # 按数据源统计
        by_source: Dict[str, int] = {}
        for ev in self.evidences:
            by_source[ev.source_type] = by_source.get(ev.source_type, 0) + 1

        stale_count = sum(1 for ev in self.evidences if ev.is_stale)

        payload = {
            "generated_at": self._now(),
            "total_evidence": len(items),
            "stale_evidence": stale_count,
            "by_source": by_source,
            "evidences": items,
        }

        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )

        return {
            "total": len(items),
            "stale": stale_count,
            "by_source": by_source,
            "path": str(output_path),
        }

    def collect_from_normalized_events(self, normalized_events: list, trade_date: str) -> None:
        """从 NormalizedEvent 列表生成证据条目。

        对每个 NormalizedEvent，将其核心字段映射为 Evidence 并添加到证据中心。
        使用 getattr/hasattr 防御性处理可能缺失的属性。

        Args:
            normalized_events: NormalizedEvent 对象列表
            trade_date: 交易日
        """
        for ne in _as_list(normalized_events):
            if ne is None:
                continue
            entity_type = getattr(ne, "entity_type", "event") or "event"
            entity_id = getattr(ne, "entity_id", "") or ""
            stock_name = getattr(ne, "stock_name", "") or ""
            title = getattr(ne, "title", "") or ""
            entity_name = stock_name or title or entity_id

            provider = getattr(ne, "provider", "") or ""
            event_type = getattr(ne, "event_type", "") or ""
            source_name = f"{provider}_{event_type}" if provider else event_type

            symbol = getattr(ne, "symbol", "") or ""
            symbols = [symbol] if symbol else []

            # 从 payload 提取 themes、trace_id、command_trace
            payload = getattr(ne, "payload", None)
            if isinstance(payload, dict):
                themes = payload.get("themes", [])
                trace_id = payload.get("trace_id", "")
                command_trace = payload.get("command_trace", "")
            else:
                themes = []
                trace_id = ""
                command_trace = ""

            # 根据 quality 映射 evidence_strength
            quality = getattr(ne, "quality", "ok") or "ok"
            strength_map = {"ok": "medium", "partial": "weak", "failed": "weak"}
            evidence_strength = strength_map.get(quality, "medium")

            # 根据 event_type 映射 evidence_type
            event_type = getattr(ne, "event_type", "") or ""
            evidence_type = _EVENT_TYPE_TO_EVIDENCE_TYPE.get(event_type, "")

            # 行情类数据 is_derived=False，其余为 True
            is_derived = event_type not in _MARKET_DATA_EVENT_TYPES and event_type not in _VALUATION_EVENT_TYPES

            # evidence_text: 优先 content，其次 summary
            evidence_text = getattr(ne, "content", "") or getattr(ne, "summary", "") or ""

            # 推断 grounding_type: 结构化数据/分类标签
            ne_grounding_type = _infer_grounding_type(evidence_type=evidence_type)

            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type=entity_type,
                entity_id=entity_id,
                entity_name=entity_name,
                source_type=provider,
                source_name=source_name,
                title=title,
                summary=getattr(ne, "summary", "") or "",
                symbols=symbols,
                symbol=symbol,
                themes=themes,
                evidence_strength=evidence_strength,
                data_date=getattr(ne, "data_date", "") or trade_date,
                is_stale=False,
                provider=provider,
                event_id=getattr(ne, "event_id", "") or "",
                raw_event_id=getattr(ne, "raw_event_id", "") or "",
                trace_id=trace_id,
                source_url=getattr(ne, "source_url", "") or "",
                file_path=getattr(ne, "file_path", "") or "",
                evidence_text=evidence_text,
                is_derived=is_derived,
                evidence_type=evidence_type,
                raw_hash=getattr(ne, "raw_hash", "") or "",
                command_trace=command_trace,
                grounding_type=ne_grounding_type,
                created_at=self._now(),
            ))

    def collect_from_extraction_results(self, results: list, trade_date: str) -> None:
        """从 ExtractionResult 列表生成证据条目。

        对每个 ExtractionResult，生成一条主证据 + evidence_items 中的子证据。
        所有提取生成的证据标记 is_derived=True。

        Args:
            results: ExtractionResult 对象列表
            trade_date: 交易日
        """
        for result in _as_list(results):
            if result is None:
                continue
            symbol = getattr(result, "symbol", "") or ""
            stock_name = getattr(result, "stock_name", "") or ""
            doc_type = getattr(result, "doc_type", "") or ""
            processor = getattr(result, "processor", "rule_based") or "rule_based"
            confidence = getattr(result, "confidence", "medium") or "medium"
            summary = str(getattr(result, "summary", "") or "")
            themes = getattr(result, "themes", []) or []
            event_id = getattr(result, "event_id", "") or ""
            document_id = getattr(result, "document_id", "") or ""
            industries = getattr(result, "industries", []) or []

            # 推断血缘字段
            provider = getattr(result, "provider", "") or "china_stock_data_gateway"
            raw_event_id = event_id
            trace_id = getattr(result, "trace_id", "") or (f"trace_{event_id}" if event_id else "")
            raw_hash_content = f"extraction:{event_id}:{document_id}:{symbol}"
            raw_hash = hashlib.md5(raw_hash_content.encode()).hexdigest()[:16]
            evidence_type = _DOC_TYPE_TO_EVIDENCE_TYPE.get(doc_type, "")

            # evidence_text: 优先从 evidence_items 获取，回退到 summary
            evidence_items = getattr(result, "evidence_items", []) or []
            if evidence_items:
                item_texts = []
                for ei in evidence_items[:5]:
                    if isinstance(ei, dict):
                        t = ei.get("evidence_text", ei.get("text", ei.get("content", "")))
                        if t:
                            item_texts.append(str(t)[:200])
                evidence_text = "; ".join(item_texts) if item_texts else (summary or "")
            else:
                evidence_text = summary or ""

            # 主证据
            # v1.2: 提取新字段
            item_grounding_status = ""
            item_cross_validation = ""
            main_grounding_type = _infer_grounding_type(
                evidence_type=evidence_type,
                processor=processor,
            )

            self.add(Evidence(
                evidence_id=self._new_id(),
                trade_date=trade_date,
                entity_type="stock" if symbol else "event",
                entity_id=symbol or event_id,
                entity_name=stock_name or event_id,
                source_type="extraction",
                source_name=f"extraction_{processor}",
                title=f"提取结果: {stock_name or event_id}",
                summary=summary[:200] if summary else "",
                symbols=[symbol] if symbol else [],
                symbol=symbol,
                themes=themes,
                evidence_strength="medium",
                data_date=trade_date,
                is_stale=False,
                provider=provider,
                event_id=event_id,
                raw_event_id=raw_event_id,
                trace_id=trace_id,
                document_id=document_id,
                extraction_method=processor,
                confidence=confidence,
                evidence_text=evidence_text,
                is_derived=True,
                evidence_type=evidence_type,
                raw_hash=raw_hash,
                industries=industries,
                processor=processor,
                grounding_type=main_grounding_type,
                extraction_confidence=confidence,
                created_at=self._now(),
            ))

            # evidence_items 子证据
            # category → evidence_type 映射
            _CATEGORY_TO_EVIDENCE_TYPE = {
                "theme": "theme",
                "risk": "risk",
                "catalyst": "catalyst",
                "moat": "moat",
                "financial_signal": "report",
            }
            for item in evidence_items:
                if not isinstance(item, dict):
                    continue

                item_text = str(item.get("evidence_text", item.get("text", item.get("content", ""))) or "")
                item_entity = item.get("entity_id", symbol or event_id)
                item_entity_name = item.get("entity_name", stock_name or item_entity)
                item_entity_type = item.get("entity_type", "stock" if symbol else "event")
                item_themes = item.get("themes", [])
                # 优先用 item.evidence_type，回退到 category 映射，最后用主证据 evidence_type
                item_category = item.get("category", "")
                item_evidence_type = item.get("evidence_type", "") or _CATEGORY_TO_EVIDENCE_TYPE.get(item_category, evidence_type)
                # v1.2 新字段
                item_grounding = item.get("grounding_status", "")
                item_grounding_type = item.get("grounding_type", "")
                item_cross_val = item.get("cross_validation_status", "")
                item_confidence = item.get("confidence", confidence)
                # 归一化 confidence 为字符串
                if isinstance(item_confidence, (int, float)):
                    item_confidence = "high" if item_confidence >= 0.8 else ("medium" if item_confidence >= 0.5 else "low")

                # 推断 grounding_type
                inferred_grounding_type = _infer_grounding_type(
                    evidence_type=item_evidence_type,
                    grounding_type_from_item=item_grounding_type,
                    processor=processor,
                    evidence_text=item_text,
                )

                # 推断 review_status: llm_only → pending_human_review
                review_status = "pending_human_review" if item_cross_val == "llm_only" else ""

                self.add(Evidence(
                    evidence_id=self._new_id(),
                    trade_date=trade_date,
                    entity_type=item_entity_type,
                    entity_id=item_entity,
                    entity_name=item_entity_name,
                    source_type="extraction",
                    source_name=f"extraction_{processor}_item",
                    title=item_text[:80] if item_text else "提取子证据",
                    summary=item_text[:200] if item_text else "",
                    symbols=[symbol] if symbol else [],
                    symbol=symbol,
                    themes=item_themes if isinstance(item_themes, list) else [],
                    evidence_strength="medium",
                    data_date=trade_date,
                    is_stale=False,
                    provider=provider,
                    event_id=event_id,
                    raw_event_id=raw_event_id,
                    trace_id=trace_id,
                    document_id=document_id,
                    extraction_method=processor,
                    confidence=item_confidence,
                    evidence_text=item_text,
                    is_derived=True,
                    evidence_type=item_evidence_type,
                    raw_hash=raw_hash,
                    industries=industries,
                    processor=processor,
                    grounding_status=item_grounding,
                    grounding_type=inferred_grounding_type,
                    extraction_confidence=item_confidence,
                    cross_validation_status=item_cross_val,
                    review_status=review_status,
                    created_at=self._now(),
                ))

    def collect_from_event_bus(self, event_bus: Any) -> None:
        """订阅 EventBus 上的相关主题，自动收集证据。

        订阅 normalized_event 主题 -> 调用 collect_from_normalized_events
        订阅 extraction_done 主题 -> 调用 collect_from_extraction_results

        Args:
            event_bus: EventBus 实例，需提供 subscribe(topic, handler) 方法
        """

        subscribe = getattr(event_bus, "subscribe", None)
        if not callable(subscribe):
            logger.warning("EvidenceCenter event bus does not expose subscribe()")
            return
        bus_key = id(event_bus)
        if bus_key in self._subscribed_event_buses:
            return

        def _on_normalized_event(envelope: Any) -> None:
            """处理 normalized_event 主题的事件信封。"""
            payload = getattr(envelope, "payload", {})
            trade_date = payload.get("trade_date", "") if isinstance(payload, dict) else ""
            if not trade_date:
                trade_date = getattr(envelope, "created_at", "")[:10] if getattr(envelope, "created_at", "") else ""

            # 尝试从 payload 中恢复 NormalizedEvent 列表
            raw_events = payload.get("events", []) if isinstance(payload, dict) else []
            if not raw_events:
                # 单条事件场景
                single = payload.get("event") if isinstance(payload, dict) else None
                if single:
                    raw_events = [single]

            if raw_events:
                # 如果已经是 NormalizedEvent 实例则直接使用，否则尝试构造
                events: list = []
                for raw in raw_events:
                    if isinstance(raw, NormalizedEvent):
                        events.append(raw)
                    elif isinstance(raw, dict):
                        try:
                            events.append(NormalizedEvent(**raw))
                        except Exception:
                            logger.debug("无法构造 NormalizedEvent，跳过: %s", raw, exc_info=True)
                    else:
                        logger.debug("未知的 normalized_event 数据类型: %s", type(raw))
                if events:
                    self.collect_from_normalized_events(events, trade_date)

        def _on_extraction_done(envelope: Any) -> None:
            """处理 extraction_done 主题的事件信封。"""
            payload = getattr(envelope, "payload", {})
            trade_date = payload.get("trade_date", "") if isinstance(payload, dict) else ""
            if not trade_date:
                trade_date = getattr(envelope, "created_at", "")[:10] if getattr(envelope, "created_at", "") else ""

            raw_results = payload.get("results", []) if isinstance(payload, dict) else []
            if not raw_results:
                single = payload.get("result") if isinstance(payload, dict) else None
                if single:
                    raw_results = [single]

            if raw_results:
                results: list = []
                for raw in raw_results:
                    if isinstance(raw, ExtractionResult):
                        results.append(raw)
                    elif isinstance(raw, dict):
                        try:
                            results.append(ExtractionResult(**raw))
                        except Exception:
                            logger.debug("无法构造 ExtractionResult，跳过: %s", raw, exc_info=True)
                    else:
                        logger.debug("未知的 extraction_done 数据类型: %s", type(raw))
                if results:
                    self.collect_from_extraction_results(results, trade_date)

        # 订阅两个核心主题
        subscribe(EventTopic.NORMALIZED_EVENT, _on_normalized_event)
        subscribe(EventTopic.EXTRACTION_DONE, _on_extraction_done)
        self._subscribed_event_buses.add(bus_key)
        logger.info("EvidenceCenter 已订阅 EventBus: %s, %s", EventTopic.NORMALIZED_EVENT, EventTopic.EXTRACTION_DONE)

    def export_with_graph_seed(self, filename: str = "evidence_graph_seed.json") -> dict:
        """导出证据为图谱种子数据。

        生成去重的实体列表和关系列表，用于知识图谱初始化。
        实体类型: stock, report, announcement, theme, sector, risk, moat, catalyst, provider
        关系类型: STOCK_HAS_REPORT, STOCK_HAS_ANNOUNCEMENT, STOCK_BELONGS_TO_THEME,
                  STOCK_BELONGS_TO_SECTOR, STOCK_HAS_RISK, STOCK_HAS_MOAT_CLAIM,
                  STOCK_HAS_CATALYST, EVIDENCE_FROM_PROVIDER
        非基础 relation 必须带 evidence_ids。

        Args:
            filename: 输出文件名，默认 evidence_graph_seed.json

        Returns:
            包含 entities、relations、evidence_count、entity_count 的字典
        """
        seen_entities: dict[str, dict] = {}
        relations: list[dict[str, Any]] = []

        def _ensure_entity(entity_id: str, entity_type: str, entity_name: str, **extra: Any) -> None:
            """确保实体在集合中，已存在则跳过。"""
            if entity_id and entity_id not in seen_entities:
                entry = {"entity_id": entity_id, "entity_type": entity_type, "entity_name": entity_name}
                entry.update(extra)
                seen_entities[entity_id] = entry

        for ev in self.evidences:
            # --- 实体收集 ---
            # 股票实体
            if ev.symbol:
                _ensure_entity(ev.symbol, "stock", ev.entity_name or ev.symbol)

            # Provider 实体
            if ev.provider:
                _ensure_entity(ev.provider, "provider", ev.provider)

            # 根据证据类型创建对应实体
            ev_type = ev.evidence_type
            if ev_type in ("report", "announcement", "catalyst", "risk", "moat", "theme"):
                target_entity_id = f"{ev_type}_{ev.entity_id}" if ev.entity_id else ""
                if target_entity_id:
                    _ensure_entity(target_entity_id, ev_type, ev.title or ev.entity_name)

            # 题材实体
            for theme_name in ev.themes:
                if theme_name:
                    _ensure_entity(theme_name, "theme", theme_name)

            # 板块实体（从 industries 字段）
            for ind_name in ev.industries:
                if ind_name:
                    _ensure_entity(ind_name, "sector", ind_name)

            # --- 关系收集 ---
            stock_id = ev.symbol
            evidence_id = ev.evidence_id

            # STOCK_HAS_REPORT
            if stock_id and ev_type == "report":
                target_id = f"report_{ev.entity_id}"
                relations.append({
                    "relation_type": "STOCK_HAS_REPORT",
                    "from": stock_id,
                    "to": target_id,
                    "evidence_ids": [evidence_id],
                })

            # STOCK_HAS_ANNOUNCEMENT
            if stock_id and ev_type == "announcement":
                target_id = f"announcement_{ev.entity_id}"
                relations.append({
                    "relation_type": "STOCK_HAS_ANNOUNCEMENT",
                    "from": stock_id,
                    "to": target_id,
                    "evidence_ids": [evidence_id],
                })

            # STOCK_HAS_CATALYST
            if stock_id and ev_type == "catalyst":
                target_id = f"catalyst_{ev.entity_id}"
                relations.append({
                    "relation_type": "STOCK_HAS_CATALYST",
                    "from": stock_id,
                    "to": target_id,
                    "evidence_ids": [evidence_id],
                })

            # STOCK_HAS_RISK
            if stock_id and ev_type == "risk":
                target_id = f"risk_{ev.entity_id}"
                relations.append({
                    "relation_type": "STOCK_HAS_RISK",
                    "from": stock_id,
                    "to": target_id,
                    "evidence_ids": [evidence_id],
                })

            # STOCK_HAS_MOAT_CLAIM
            if stock_id and ev_type == "moat":
                target_id = f"moat_{ev.entity_id}"
                relations.append({
                    "relation_type": "STOCK_HAS_MOAT_CLAIM",
                    "from": stock_id,
                    "to": target_id,
                    "evidence_ids": [evidence_id],
                })

            # STOCK_BELONGS_TO_THEME
            if stock_id and ev.themes:
                for theme_name in ev.themes:
                    if theme_name:
                        relations.append({
                            "relation_type": "STOCK_BELONGS_TO_THEME",
                            "from": stock_id,
                            "to": theme_name,
                            "evidence_ids": [evidence_id],
                        })

            # STOCK_BELONGS_TO_SECTOR
            if stock_id and ev.industries:
                for ind_name in ev.industries:
                    if ind_name:
                        relations.append({
                            "relation_type": "STOCK_BELONGS_TO_SECTOR",
                            "from": stock_id,
                            "to": ind_name,
                            "evidence_ids": [evidence_id],
                        })

            # EVIDENCE_FROM_PROVIDER（基础关系，不带 evidence_ids）
            if ev.provider and evidence_id:
                relations.append({
                    "relation_type": "EVIDENCE_FROM_PROVIDER",
                    "from": evidence_id,
                    "to": ev.provider,
                })

        entities = list(seen_entities.values())

        result = {
            "entities": entities,
            "relations": relations,
            "evidence_count": len(self.evidences),
            "entity_count": len(entities),
        }

        output_path = self._output_path(filename)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
        logger.info("图谱种子数据已导出: %s (%d 实体, %d 证据)", output_path, len(entities), len(self.evidences))

        return result
