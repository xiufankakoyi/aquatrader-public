"""Fast Validation Loop v2 — 综合多维度评分验证。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.research.evidence.evidence_center import Evidence, EvidenceCenter
from core.research.engine.models import EntityRegistry, EvidenceGraph, MoatDetector

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if isfinite(number) else default

# 各维度权重（总和 = 1.0）
_WEIGHTS = {
    "evidence_score": 0.15,
    "graph_score": 0.10,
    "factor_score": 0.10,
    "signal_score": 0.10,
    "trend_score": 0.10,
    "news_score": 0.10,
    "report_score": 0.10,
    "financial_score": 0.10,
    "moat_score": 0.10,
    "risk_penalty": 0.05,  # 风险扣分权重（从总分中扣除）
}

# 决策阈值
_PASS_THRESHOLD = 80.0
_WATCH_THRESHOLD = 60.0
_INSUFFICIENT_THRESHOLD = 40.0


class FastValidationLoop:
    """综合多维度评分，对股票进行快速验证。

    评分维度:
    - evidence_score: 证据数量和质量
    - graph_score: 图谱关系丰富度
    - factor_score: 量化因子得分
    - signal_score: 策略信号得分
    - trend_score: 趋势得分
    - news_score: 新闻情绪得分
    - report_score: 研报覆盖得分
    - financial_score: 财务信号得分
    - moat_score: 护城河得分
    - risk_penalty: 风险扣分

    决策等级:
    - >= 80: PASS
    - >= 60: WATCH
    - >= 40: INSUFFICIENT_EVIDENCE
    - < 40 或重大风险: BLOCK
    """

    def __init__(
        self,
        evidence_center: Optional[EvidenceCenter] = None,
        entity_registry: Optional[EntityRegistry] = None,
        evidence_graph: Optional[EvidenceGraph] = None,
        event_bus: Optional[Any] = None,
    ) -> None:
        """初始化快速验证循环。

        Args:
            evidence_center: 证据中心实例
            entity_registry: 实体注册表实例
            evidence_graph: 证据图谱实例
            event_bus: 事件总线实例
        """
        self.evidence_center = evidence_center or EvidenceCenter()
        self.entity_registry = entity_registry or EntityRegistry()
        self.evidence_graph = evidence_graph or EvidenceGraph(self.entity_registry)
        self.event_bus = event_bus
        self.moat_detector = MoatDetector(self.evidence_graph)
        self._report_dir = PROJECT_ROOT / "data" / "reports"

    def validate(self, symbol: str, trade_date: Optional[str] = None) -> Dict[str, Any]:
        """验证单只股票。

        Args:
            symbol: 股票代码
            trade_date: 交易日期，默认今天

        Returns:
            验证结果字典，包含各维度得分、综合得分、决策等级、重大风险列表、详情
        """
        if not trade_date:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        # 获取股票名称
        stock_name = self._get_stock_name(symbol)

        # 各维度评分
        evidence_score = self._calc_evidence_score(symbol)
        graph_score = self._calc_graph_score(symbol)
        factor_score = self._calc_factor_score(symbol)
        signal_score = self._calc_signal_score(symbol)
        trend_score = self._calc_trend_score(symbol)
        news_score = self._calc_news_score(symbol)
        report_score = self._calc_report_score(symbol)
        financial_score = self._calc_financial_score(symbol)
        moat_score = self._calc_moat_score(symbol)
        risk_penalty = self._calc_risk_penalty(symbol)

        # 重大风险检测
        major_risks = self._detect_major_risks(symbol)

        # 加权综合得分
        raw_score = (
            _WEIGHTS["evidence_score"] * evidence_score
            + _WEIGHTS["graph_score"] * graph_score
            + _WEIGHTS["factor_score"] * factor_score
            + _WEIGHTS["signal_score"] * signal_score
            + _WEIGHTS["trend_score"] * trend_score
            + _WEIGHTS["news_score"] * news_score
            + _WEIGHTS["report_score"] * report_score
            + _WEIGHTS["financial_score"] * financial_score
            + _WEIGHTS["moat_score"] * moat_score
        )
        # 风险扣分从原始分中扣除
        validation_score = max(0.0, raw_score * 100 - risk_penalty * 100)

        # 决策
        decision = self._make_decision(validation_score, major_risks)

        # 详情
        details = {
            "evidence": {
                "score": evidence_score,
                "evidence_count": self._count_evidence(symbol),
                "strong_count": self._count_evidence(symbol, strength="strong"),
                "medium_count": self._count_evidence(symbol, strength="medium"),
                "weak_count": self._count_evidence(symbol, strength="weak"),
            },
            "graph": {
                "score": graph_score,
                "relation_count": self._count_graph_relations(symbol),
                "neighbor_count": self._count_graph_neighbors(symbol),
            },
            "factor": {
                "score": factor_score,
                "source": "lancedb_factors" if factor_score > 0 else "暂无本地证据",
            },
            "signal": {
                "score": signal_score,
                "source": "signal_engine" if signal_score > 0 else "暂无本地证据",
            },
            "trend": {
                "score": trend_score,
                "source": "kline_analysis" if trend_score > 0 else "暂无本地证据",
            },
            "news": {
                "score": news_score,
                "news_count": self._count_evidence_by_source(symbol, "news"),
            },
            "report": {
                "score": report_score,
                "report_count": self._count_evidence_by_source(symbol, "report"),
            },
            "financial": {
                "score": financial_score,
                "source": "finance_radar" if financial_score > 0 else "暂无本地证据",
            },
            "moat": {
                "score": moat_score,
                "moat_summary": self.moat_detector.get_stock_moat_summary(symbol),
            },
            "risk": {
                "penalty_score": risk_penalty,
                "major_risks": major_risks,
            },
        }

        result: Dict[str, Any] = {
            "symbol": symbol,
            "stock_name": stock_name,
            "trade_date": trade_date,
            "evidence_score": round(evidence_score, 4),
            "graph_score": round(graph_score, 4),
            "factor_score": round(factor_score, 4),
            "signal_score": round(signal_score, 4),
            "trend_score": round(trend_score, 4),
            "news_score": round(news_score, 4),
            "report_score": round(report_score, 4),
            "financial_score": round(financial_score, 4),
            "moat_score": round(moat_score, 4),
            "risk_penalty": round(risk_penalty, 4),
            "validation_score": round(validation_score, 2),
            "decision": decision,
            "major_risks": major_risks,
            "details": details,
            "validated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }

        logger.info(
            "验证完成: %s(%s) score=%.1f decision=%s risks=%d",
            stock_name, symbol, validation_score, decision, len(major_risks),
        )

        return result

    def validate_batch(
        self,
        symbols: List[str],
        trade_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """批量验证多只股票。

        Args:
            symbols: 股票代码列表
            trade_date: 交易日期

        Returns:
            验证结果列表
        """
        results: List[Dict[str, Any]] = []
        if not isinstance(symbols, list):
            return []
        for symbol in symbols:
            if not isinstance(symbol, str) or not symbol:
                continue
            try:
                result = self.validate(symbol, trade_date)
                results.append(result)
            except Exception as exc:
                logger.error("验证失败: %s, %s", symbol, exc, exc_info=True)
                results.append({
                    "symbol": symbol,
                    "stock_name": self._get_stock_name(symbol),
                    "trade_date": trade_date or datetime.now().strftime("%Y-%m-%d"),
                    "validation_score": 0.0,
                    "decision": "BLOCK",
                    "major_risks": [{"type": "validation_error", "detail": str(exc)}],
                    "error": str(exc),
                })
        return results

    # ------------------------------------------------------------------
    # 各维度评分方法
    # ------------------------------------------------------------------

    def _calc_evidence_score(self, symbol: str) -> float:
        """计算证据得分，基于证据数量和质量。

        评分规则:
        - 0 条证据: 0.0
        - 1-2 条: 0.2
        - 3-5 条: 0.4
        - 6-10 条: 0.6
        - 11-20 条: 0.8
        - >20 条: 1.0
        - strong 证据加权 +0.1（上限 1.0）
        - stale 证据 -0.1（下限 0.0）

        Args:
            symbol: 股票代码

        Returns:
            证据得分 [0.0, 1.0]
        """
        evidences = self._get_evidences_for_symbol(symbol)
        if not evidences:
            return 0.0

        count = len(evidences)
        if count <= 2:
            score = 0.2
        elif count <= 5:
            score = 0.4
        elif count <= 10:
            score = 0.6
        elif count <= 20:
            score = 0.8
        else:
            score = 1.0

        # strong 证据加分
        strong_count = sum(1 for ev in evidences if ev.evidence_strength == "strong")
        score += min(strong_count * 0.05, 0.2)

        # stale 证据减分
        stale_count = sum(1 for ev in evidences if ev.is_stale)
        score -= min(stale_count * 0.05, 0.3)

        return max(0.0, min(1.0, score))

    def _calc_graph_score(self, symbol: str) -> float:
        """计算图谱得分，基于实体关系丰富度。

        评分规则:
        - 无实体: 0.0
        - 有实体但无关系: 0.1
        - 1-3 条关系: 0.3
        - 4-8 条关系: 0.5
        - 9-15 条关系: 0.7
        - >15 条关系: 0.9
        - HAS_MOAT 关系额外 +0.1

        Args:
            symbol: 股票代码

        Returns:
            图谱得分 [0.0, 1.0]
        """
        entity = self.entity_registry.get_by_symbol(symbol)
        if not entity:
            return 0.0

        relations = self.evidence_graph.get_relations(entity.entity_id, direction="both")
        rel_count = len(relations)

        if rel_count == 0:
            score = 0.1
        elif rel_count <= 3:
            score = 0.3
        elif rel_count <= 8:
            score = 0.5
        elif rel_count <= 15:
            score = 0.7
        else:
            score = 0.9

        # HAS_MOAT 关系加分
        moat_rels = [r for r in relations if r.relation_type == "HAS_MOAT"]
        if moat_rels:
            score += 0.1

        # high confidence 关系加分
        high_conf = sum(1 for r in relations if r.confidence == "high")
        score += min(high_conf * 0.02, 0.1)

        return max(0.0, min(1.0, score))

    def _calc_factor_score(self, symbol: str) -> float:
        """计算量化因子得分。

        从 LanceDB factors 表读取因子数据，基于:
        - MA 排列（ma5 >= ma10 >= ma20）
        - RSI 区间
        - 收益率方向

        Args:
            symbol: 股票代码

        Returns:
            因子得分 [0.0, 1.0]
        """
        try:
            from data_svc.query.fast_market_query import get_fast_market_query

            fmq = get_fast_market_query()
            latest_date = fmq.get_latest_trade_date()
            if not latest_date:
                return 0.0

            df = fmq._read_table(
                "factors",
                start_date=latest_date,
                end_date=latest_date,
                fields=["trade_date", "stock_code", "ma5", "ma10", "ma20", "rsi_12",
                         "return_5d", "return_20d", "volatility_20d"],
            )
            if df is None or df.is_empty():
                return 0.0

            import polars as pl
            row = df.filter(pl.col("stock_code") == symbol)
            if row.is_empty():
                return 0.0

            score = 0.0
            r = row.row(0, named=True)

            # MA 排列
            ma5 = r.get("ma5")
            ma10 = r.get("ma10")
            ma20 = r.get("ma20")
            if ma5 and ma10 and ma20:
                if ma5 >= ma10 >= ma20:
                    score += 0.3
                elif ma5 >= ma10:
                    score += 0.15

            # RSI 区间
            rsi = r.get("rsi_12")
            if rsi is not None:
                if 40 <= rsi <= 70:
                    score += 0.3
                elif 30 <= rsi <= 80:
                    score += 0.15

            # 收益率方向
            ret_5d = r.get("return_5d")
            ret_20d = r.get("return_20d")
            if ret_5d is not None and ret_5d > 0:
                score += 0.2
            if ret_20d is not None and ret_20d > 0:
                score += 0.2

            return max(0.0, min(1.0, score))

        except Exception:
            logger.debug("因子得分计算失败: %s", symbol, exc_info=True)
            return 0.0

    def _calc_signal_score(self, symbol: str) -> float:
        """计算策略信号得分。

        从证据中心查找策略信号相关证据。

        Args:
            symbol: 股票代码

        Returns:
            信号得分 [0.0, 1.0]
        """
        evidences = self._get_evidences_for_symbol(symbol)
        signal_evs = [
            ev for ev in evidences
            if ev.source_type in ("quant_flow", "dragon_eye")
            and ("信号" in ev.title or "signal" in ev.title.lower())
        ]

        if not signal_evs:
            return 0.0

        # 基础分
        score = min(len(signal_evs) * 0.2, 0.6)

        # strong 证据加分
        for ev in signal_evs:
            if ev.evidence_strength == "strong":
                score += 0.2
            elif ev.evidence_strength == "medium":
                score += 0.1

        return max(0.0, min(1.0, score))

    def _calc_trend_score(self, symbol: str) -> float:
        """计算趋势得分。

        从 LanceDB 读取 K 线数据，判断短期和中期趋势。

        Args:
            symbol: 股票代码

        Returns:
            趋势得分 [0.0, 1.0]
        """
        try:
            from data_svc.query.fast_market_query import get_fast_market_query

            fmq = get_fast_market_query()
            latest_date = fmq.get_latest_trade_date()
            if not latest_date:
                return 0.0

            from datetime import timedelta
            start = (
                datetime.strptime(latest_date, "%Y-%m-%d") - timedelta(days=60)
            ).strftime("%Y-%m-%d")

            df = fmq._read_table(
                "daily_ohlcv",
                start_date=start,
                end_date=latest_date,
                fields=["trade_date", "stock_code", "close"],
            )
            if df is None or df.is_empty():
                return 0.0

            import polars as pl
            stock_df = (
                df.filter(pl.col("stock_code") == symbol)
                .drop_nulls("close")
                .sort("trade_date")
            )
            if stock_df.height < 5:
                return 0.0

            closes = stock_df.get_column("close").to_list()
            score = 0.0

            # 短期趋势（5 日）
            if len(closes) >= 6:
                ret_5d = closes[-1] / closes[-6] - 1.0
                if ret_5d > 0.03:
                    score += 0.3
                elif ret_5d > 0:
                    score += 0.15
                elif ret_5d < -0.03:
                    score -= 0.1

            # 中期趋势（20 日）
            if len(closes) >= 21:
                ret_20d = closes[-1] / closes[-21] - 1.0
                if ret_20d > 0.05:
                    score += 0.4
                elif ret_20d > 0:
                    score += 0.2
                elif ret_20d < -0.05:
                    score -= 0.15

            # 价格位置（相对近期高点）
            if len(closes) >= 10:
                recent_high = max(closes[-10:])
                position = closes[-1] / recent_high
                if position > 0.95:
                    score += 0.3
                elif position > 0.85:
                    score += 0.15

            return max(0.0, min(1.0, score))

        except Exception:
            logger.debug("趋势得分计算失败: %s", symbol, exc_info=True)
            return 0.0

    def _calc_news_score(self, symbol: str) -> float:
        """计算新闻情绪得分。

        基于证据中心中新闻类证据的数量和情绪倾向。

        Args:
            symbol: 股票代码

        Returns:
            新闻得分 [0.0, 1.0]
        """
        evidences = self._get_evidences_for_symbol(symbol)
        news_evs = [ev for ev in evidences if ev.source_type in ("news", "dragon_eye")]

        if not news_evs:
            return 0.0

        # 基础分：有新闻覆盖
        score = min(len(news_evs) * 0.15, 0.5)

        # 情绪加分：标题中包含正面关键词
        positive_keywords = ["增长", "突破", "中标", "获批", "增持", "利好", "新高"]
        negative_keywords = ["风险", "亏损", "处罚", "减持", "暴跌", "ST", "退市"]

        for ev in news_evs:
            title_lower = ev.title.lower()
            if any(kw in title_lower for kw in positive_keywords):
                score += 0.1
            if any(kw in title_lower for kw in negative_keywords):
                score -= 0.1

        return max(0.0, min(1.0, score))

    def _calc_report_score(self, symbol: str) -> float:
        """计算研报覆盖得分。

        基于证据中心中研报类证据的数量。

        Args:
            symbol: 股票代码

        Returns:
            研报得分 [0.0, 1.0]
        """
        evidences = self._get_evidences_for_symbol(symbol)
        report_evs = [ev for ev in evidences if ev.source_type in ("report", "lancedb")]

        if not report_evs:
            return 0.0

        # 基础分
        count = len(report_evs)
        if count == 1:
            score = 0.2
        elif count <= 3:
            score = 0.4
        elif count <= 6:
            score = 0.6
        elif count <= 10:
            score = 0.8
        else:
            score = 1.0

        # strong 证据加分
        strong_count = sum(1 for ev in report_evs if ev.evidence_strength == "strong")
        score += min(strong_count * 0.05, 0.2)

        return max(0.0, min(1.0, score))

    def _calc_financial_score(self, symbol: str) -> float:
        """计算财务信号得分。

        从证据中心查找财务类证据，以及从 LanceDB 读取估值数据。

        Args:
            symbol: 股票代码

        Returns:
            财务得分 [0.0, 1.0]
        """
        score = 0.0

        # 从证据中心查找财务信号
        evidences = self._get_evidences_for_symbol(symbol)
        financial_evs = [
            ev for ev in evidences
            if ev.source_type in ("finance_radar", "industry_chain")
        ]

        if financial_evs:
            score += min(len(financial_evs) * 0.15, 0.4)
            # strong 证据加分
            strong_count = sum(1 for ev in financial_evs if ev.evidence_strength == "strong")
            score += min(strong_count * 0.1, 0.3)

        # 尝试从 LanceDB 读取估值分位
        try:
            from data_svc.query.fast_market_query import get_fast_market_query

            fmq = get_fast_market_query()
            latest_date = fmq.get_latest_trade_date()
            if latest_date:
                df = fmq._read_table(
                    "daily_ohlcv",
                    start_date=latest_date,
                    end_date=latest_date,
                    fields=["trade_date", "stock_code", "close"],
                )
                if df is not None and not df.is_empty():
                    import polars as pl
                    row = df.filter(pl.col("stock_code") == symbol)
                    if not row.is_empty():
                        score += 0.3  # 有价格数据即加分
        except Exception:
            logger.debug("财务得分 LanceDB 读取失败: %s", symbol, exc_info=True)

        return max(0.0, min(1.0, score))

    def _calc_moat_score(self, symbol: str) -> float:
        """计算护城河得分。

        从证据图谱中获取护城河关系，转换为 0-1 得分。

        Args:
            symbol: 股票代码

        Returns:
            护城河得分 [0.0, 1.0]
        """
        summary = self.moat_detector.get_stock_moat_summary(symbol)

        if not summary.get("has_moat", False):
            return 0.0

        # 基础分：有护城河
        score = 0.3

        # 护城河类型数量加分
        moat_types = summary.get("moat_types", [])
        score += min(len(moat_types) * 0.15, 0.5)

        # 关系数量加分
        rel_count = summary.get("relation_count", 0)
        score += min(rel_count * 0.05, 0.2)

        return max(0.0, min(1.0, score))

    def _calc_risk_penalty(self, symbol: str) -> float:
        """计算风险扣分。

        基于证据中的风险信号。

        Args:
            symbol: 股票代码

        Returns:
            风险扣分 [0.0, 1.0]
        """
        penalty = 0.0

        # 从证据中查找风险信号
        evidences = self._get_evidences_for_symbol(symbol)
        risk_keywords = [
            "风险", "亏损", "处罚", "减持", "暴跌", "ST", "退市",
            "违规", "立案", "冻结", "债务危机", "信用降级",
            "竞争加剧", "价格战", "市场份额下降", "政策风险",
        ]

        for ev in evidences:
            title_lower = ev.title.lower()
            content_lower = ev.summary.lower()
            for kw in risk_keywords:
                if kw.lower() in title_lower or kw.lower() in content_lower:
                    penalty += 0.05
                    break  # 每条证据最多计一次风险

        # stale 证据扣分
        stale_count = sum(1 for ev in evidences if ev.is_stale)
        penalty += min(stale_count * 0.02, 0.1)

        return max(0.0, min(1.0, penalty))

    def _detect_major_risks(self, symbol: str) -> List[Dict[str, Any]]:
        """检测重大风险，触发 BLOCK。

        重大风险:
        - ST 股票
        - 停牌
        - 涨跌停不可交易
        - 重大财务风险（来自证据）
        - 重大负面公告
        - 数据过时

        Args:
            symbol: 股票代码

        Returns:
            重大风险列表
        """
        risks: List[Dict[str, Any]] = []

        # 1. 检查 ST 标记
        stock_name = self._get_stock_name(symbol)
        if "ST" in stock_name or "st" in stock_name.lower():
            risks.append({
                "type": "st_stock",
                "detail": f"股票名称含 ST: {stock_name}",
                "evidence_ids": self._find_evidence_ids(symbol, "ST"),
            })

        # 2. 检查停牌（从 LanceDB 判断）
        try:
            from data_svc.query.fast_market_query import get_fast_market_query

            fmq = get_fast_market_query()
            latest_date = fmq.get_latest_trade_date()
            if latest_date:
                df = fmq._read_table(
                    "daily_ohlcv",
                    start_date=latest_date,
                    end_date=latest_date,
                    fields=["trade_date", "stock_code", "close", "volume"],
                )
                if df is not None and not df.is_empty():
                    import polars as pl
                    row = df.filter(pl.col("stock_code") == symbol)
                    if not row.is_empty():
                        r = row.row(0, named=True)
                        volume = r.get("volume", 0)
                        volume_value = _safe_float(volume, float("nan"))
                        if isfinite(volume_value) and volume_value == 0:
                            risks.append({
                                "type": "suspended",
                                "detail": f"成交量为 0，可能停牌: {latest_date}",
                                "evidence_ids": [],
                            })
                    else:
                        # 当日无数据，可能停牌
                        risks.append({
                            "type": "suspended",
                            "detail": f"当日无行情数据，可能停牌: {latest_date}",
                            "evidence_ids": [],
                        })
        except Exception:
            logger.debug("停牌检测失败: %s", symbol, exc_info=True)

        # 3. 检查涨跌停
        try:
            from data_svc.query.fast_market_query import get_fast_market_query

            fmq = get_fast_market_query()
            latest_date = fmq.get_latest_trade_date()
            if latest_date:
                df = fmq._read_table(
                    "factors",
                    start_date=latest_date,
                    end_date=latest_date,
                    fields=["trade_date", "stock_code", "ret_1d"],
                )
                if df is not None and not df.is_empty():
                    import polars as pl
                    row = df.filter(pl.col("stock_code") == symbol)
                    if not row.is_empty():
                        r = row.row(0, named=True)
                        ret_1d = r.get("ret_1d")
                        if ret_1d is not None:
                            ret_1d = _safe_float(ret_1d)
                            if ret_1d >= 0.095:
                                risks.append({
                                    "type": "limit_up",
                                    "detail": f"涨停 {ret_1d:.2%}，可能无法买入",
                                    "evidence_ids": [],
                                })
                            elif ret_1d <= -0.095:
                                risks.append({
                                    "type": "limit_down",
                                    "detail": f"跌停 {ret_1d:.2%}，可能无法卖出",
                                    "evidence_ids": [],
                                })
        except Exception:
            logger.debug("涨跌停检测失败: %s", symbol, exc_info=True)

        # 4. 重大财务风险（来自证据）
        evidences = self._get_evidences_for_symbol(symbol)
        severe_risk_keywords = ["退市", "立案", "冻结", "债务危机", "信用降级"]
        for ev in evidences:
            text = f"{ev.title} {ev.summary}".lower()
            for kw in severe_risk_keywords:
                if kw in text:
                    risks.append({
                        "type": "major_financial_risk",
                        "detail": f"证据含重大风险关键词 '{kw}': {ev.title[:50]}",
                        "evidence_ids": [ev.evidence_id],
                    })
                    break  # 每条证据最多计一次

        # 5. 重大负面公告
        announcement_evs = [
            ev for ev in evidences
            if "公告" in ev.title and ev.evidence_strength == "strong"
        ]
        negative_ann_keywords = ["处罚", "违规", "立案", "诉讼"]
        for ev in announcement_evs:
            text = f"{ev.title} {ev.summary}".lower()
            for kw in negative_ann_keywords:
                if kw in text:
                    risks.append({
                        "type": "major_negative_announcement",
                        "detail": f"负面公告含 '{kw}': {ev.title[:50]}",
                        "evidence_ids": [ev.evidence_id],
                    })
                    break

        # 6. 数据过时
        stale_evs = [ev for ev in evidences if ev.is_stale]
        if len(stale_evs) == len(evidences) and len(evidences) > 0:
            risks.append({
                "type": "stale_data",
                "detail": f"全部 {len(evidences)} 条证据均已过时",
                "evidence_ids": [ev.evidence_id for ev in stale_evs[:5]],
            })

        return risks

    def _make_decision(self, score: float, major_risks: List[Dict[str, Any]]) -> str:
        """根据得分和风险做出验证决策。

        Args:
            score: 综合得分
            major_risks: 重大风险列表

        Returns:
            决策等级: PASS / WATCH / INSUFFICIENT_EVIDENCE / BLOCK
        """
        # 有重大风险直接 BLOCK
        if major_risks:
            # ST、停牌、退市风险直接 BLOCK
            block_types = {"st_stock", "suspended", "major_financial_risk"}
            if any(r.get("type") in block_types for r in major_risks):
                return "BLOCK"
            # 涨跌停不直接 BLOCK，但降级
            if any(r.get("type") in ("limit_up", "limit_down") for r in major_risks):
                if score < _WATCH_THRESHOLD:
                    return "BLOCK"

        if score >= _PASS_THRESHOLD:
            return "PASS"
        elif score >= _WATCH_THRESHOLD:
            return "WATCH"
        elif score >= _INSUFFICIENT_THRESHOLD:
            return "INSUFFICIENT_EVIDENCE"
        else:
            return "BLOCK"

    def export_report(
        self,
        results: List[Dict[str, Any]],
        trade_date: str,
    ) -> Dict[str, Any]:
        """导出验证结果到 data/reports/fast_validation_loop_latest.json 和 .md。

        Args:
            results: 验证结果列表
            trade_date: 交易日期

        Returns:
            导出摘要
        """
        self._report_dir.mkdir(parents=True, exist_ok=True)

        # 统计
        decision_counts: Dict[str, int] = {}
        for r in results:
            d = r.get("decision", "UNKNOWN")
            decision_counts[d] = decision_counts.get(d, 0) + 1

        payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "trade_date": trade_date,
            "total_validated": len(results),
            "decision_counts": decision_counts,
            "results": results,
        }

        # JSON
        json_path = self._report_dir / "fast_validation_loop_latest.json"
        try:
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str, allow_nan=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("导出 JSON 报告失败: %s", exc)

        # Markdown
        md_path = self._report_dir / "fast_validation_loop_latest.md"
        try:
            lines = [
                f"# Fast Validation Loop 报告 — {trade_date}",
                "",
                f"- 生成时间: {payload['generated_at']}",
                f"- 验证总数: {payload['total_validated']}",
                f"- PASS: {decision_counts.get('PASS', 0)}",
                f"- WATCH: {decision_counts.get('WATCH', 0)}",
                f"- INSUFFICIENT_EVIDENCE: {decision_counts.get('INSUFFICIENT_EVIDENCE', 0)}",
                f"- BLOCK: {decision_counts.get('BLOCK', 0)}",
                "",
                "## 验证结果",
                "",
                "| 代码 | 名称 | 综合得分 | 决策 | 重大风险 |",
                "| --- | --- | --- | --- | --- |",
            ]
            for r in results:
                risk_count = len(r.get("major_risks", []))
                lines.append(
                    f"| {r.get('symbol', '')} | {r.get('stock_name', '')} | "
                    f"{r.get('validation_score', 0):.1f} | {r.get('decision', '')} | "
                    f"{risk_count} |"
                )
            lines.append("")
            md_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception as exc:
            logger.warning("导出 Markdown 报告失败: %s", exc)

        return {
            "total": len(results),
            "decision_counts": decision_counts,
            "json_path": str(json_path),
            "md_path": str(md_path),
        }

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _get_evidences_for_symbol(self, symbol: str) -> List[Evidence]:
        """获取与指定股票相关的所有证据。

        Args:
            symbol: 股票代码

        Returns:
            证据列表
        """
        return [
            ev for ev in self.evidence_center.evidences
            if symbol in ev.symbols or ev.entity_id == symbol
        ]

    def _count_evidence(
        self,
        symbol: str,
        strength: Optional[str] = None,
    ) -> int:
        """统计证据数量。

        Args:
            symbol: 股票代码
            strength: 可选，按证据强度过滤

        Returns:
            证据数量
        """
        evidences = self._get_evidences_for_symbol(symbol)
        if strength:
            return sum(1 for ev in evidences if ev.evidence_strength == strength)
        return len(evidences)

    def _count_graph_relations(self, symbol: str) -> int:
        """统计图谱关系数量。

        Args:
            symbol: 股票代码

        Returns:
            关系数量
        """
        entity = self.entity_registry.get_by_symbol(symbol)
        if not entity:
            return 0
        return len(self.evidence_graph.get_relations(entity.entity_id, direction="both"))

    def _count_graph_neighbors(self, symbol: str) -> int:
        """统计图谱邻居数量。

        Args:
            symbol: 股票代码

        Returns:
            邻居数量
        """
        entity = self.entity_registry.get_by_symbol(symbol)
        if not entity:
            return 0
        neighbors = self.evidence_graph.get_neighbors(entity.entity_id, max_depth=1)
        return len(neighbors) - 1  # 排除自身

    def _count_evidence_by_source(self, symbol: str, source_type: str) -> int:
        """按来源类型统计证据数量。

        Args:
            symbol: 股票代码
            source_type: 来源类型

        Returns:
            证据数量
        """
        return sum(
            1 for ev in self._get_evidences_for_symbol(symbol)
            if ev.source_type == source_type
        )

    def _find_evidence_ids(self, symbol: str, keyword: str) -> List[str]:
        """查找包含关键词的证据 ID。

        Args:
            symbol: 股票代码
            keyword: 关键词

        Returns:
            证据 ID 列表
        """
        return [
            ev.evidence_id
            for ev in self._get_evidences_for_symbol(symbol)
            if keyword.lower() in f"{ev.title} {ev.summary}".lower()
        ]

    def _get_stock_name(self, symbol: str) -> str:
        """获取股票名称。

        优先从实体注册表获取，否则从 LanceDB 读取。

        Args:
            symbol: 股票代码

        Returns:
            股票名称
        """
        # 1. 从实体注册表获取
        entity = self.entity_registry.get_by_symbol(symbol)
        if entity and entity.entity_name:
            return entity.entity_name

        # 2. 从证据中心获取
        for ev in self.evidence_center.evidences:
            if symbol in ev.symbols and ev.entity_name:
                return ev.entity_name

        # 3. 尝试从 LanceDB 获取
        try:
            from data_svc.query.fast_market_query import get_fast_market_query

            fmq = get_fast_market_query()
            df = fmq._read_table(
                "stock_info",
                fields=["stock_code", "name"],
            )
            if df is not None and not df.is_empty():
                import polars as pl
                row = df.filter(pl.col("stock_code") == symbol)
                if not row.is_empty():
                    return str(row.row(0, named=True).get("name", symbol))
        except Exception:
            pass

        return symbol
