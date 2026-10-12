"""研究复盘与自我进化：把判断和市场表现对比，调整规则权重。

解决差距 8：没有"研究复盘"和"自我进化"。

核心能力：
  1. 记录各 agent 的研究判断（主题/股票/决策/置信度/理由）
  2. 对比后续市场表现，计算实际收益与基准收益
  3. 按 agent 维度统计胜率、平均收益，动态调整权重
  4. 从命中/失误中提取经验规则，用于后续 prompt/规则优化
"""
from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_filename(value: str, default: str) -> str:
    name = Path(str(value or default)).name
    return name if re.fullmatch(r"[A-Za-z0-9_.-]+", name) else default


class PriceProvider(Protocol):
    """价格数据提供者接口。"""

    def get_return(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> float:
        """返回指定区间收益率，失败返回 0.0。"""
        ...


class FastMarketPriceProvider:
    """基于 FastMarketQuery 的真实行情价格提供者。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化。

        Args:
            db_path: LanceDB 路径，None 时使用默认路径。
        """
        # 延迟导入避免循环依赖
        from data_svc.query.fast_market_query import FastMarketQuery

        self._query = FastMarketQuery(db_path=str(db_path) if db_path else None)

    def get_return(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> float:
        """返回指定区间收益率。

        使用收盘价计算：(end_close - start_close) / start_close。
        失败或无数据返回 0.0。

        Args:
            symbol: 股票代码。
            start_date: 开始日期 YYYY-MM-DD。
            end_date: 结束日期 YYYY-MM-DD。

        Returns:
            区间收益率，失败返回 0.0。
        """
        try:
            df = self._query.get_stock_history(symbol, start_date, end_date, fields=["close", "trade_date"])
            if df.is_empty() or "close" not in df.columns or len(df) < 1:
                return 0.0

            # 按日期排序，取最早和最晚的收盘价
            df = df.sort("trade_date")
            start_close = float(df["close"].head(1)[0])
            end_close = float(df["close"].tail(1)[0])
            if start_close <= 0:
                return 0.0
            return (end_close - start_close) / start_close
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
            return 0.0


@dataclass
class JudgmentRecord:
    """Agent 研究判断记录。"""

    judgment_id: str
    timestamp: str
    trade_date: str
    agent: str
    target_type: str  # theme / stock
    target_key: str
    decision: str  # BUY_CANDIDATE / WATCH / BLOCK / INSUFFICIENT_EVIDENCE
    confidence: str  # high / medium / low
    reasoning: str
    evidence_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为 dict。"""
        return {
            "judgment_id": self.judgment_id,
            "timestamp": self.timestamp,
            "trade_date": self.trade_date,
            "agent": self.agent,
            "target_type": self.target_type,
            "target_key": self.target_key,
            "decision": self.decision,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "evidence_summary": self.evidence_summary,
            "metadata": dict(self.metadata),
        }


@dataclass
class PerformanceRecord:
    """判断的市场表现记录。"""

    judgment_id: str
    symbol: str
    start_date: str
    end_date: str
    realized_return: float
    benchmark_return: float
    hit: bool
    miss_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为 dict。"""
        return {
            "judgment_id": self.judgment_id,
            "symbol": self.symbol,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "realized_return": round(self.realized_return, 4),
            "benchmark_return": round(self.benchmark_return, 4),
            "hit": self.hit,
            "miss_reason": self.miss_reason,
        }


class ResearchReviewLog:
    """判断记录持久化。"""

    def __init__(self, storage_path: str | Path | None = None) -> None:
        """初始化。

        Args:
            storage_path: 存储路径，默认 data/research_runs/research_review。
        """
        if storage_path is None:
            project_root = Path(__file__).resolve().parents[3]
            storage_path = project_root / "data" / "research_runs" / "research_review"
        self._storage_path = Path(storage_path)
        if not self._storage_path.is_absolute():
            self._storage_path = Path(__file__).resolve().parents[3] / self._storage_path
        self._storage_path.mkdir(parents=True, exist_ok=True)
        self._judgments: list[JudgmentRecord] = []

    def record(self, judgment: JudgmentRecord) -> None:
        """记录一条判断。"""
        if not isinstance(judgment, JudgmentRecord):
            raise TypeError("judgment must be a JudgmentRecord")
        self._judgments.append(judgment)

    def save(self, filename: str = "judgments.jsonl") -> Path:
        """以 JSONL 形式持久化。"""
        path = self._storage_path / _safe_filename(filename, "judgments.jsonl")
        with path.open("w", encoding="utf-8") as f:
            for j in self._judgments:
                f.write(json.dumps(j.to_dict(), ensure_ascii=False, allow_nan=False) + "\n")
        return path

    def load(self, filename: str = "judgments.jsonl") -> list[JudgmentRecord]:
        """加载 JSONL 判断记录。"""
        path = self._storage_path / _safe_filename(filename, "judgments.jsonl")
        records: list[JudgmentRecord] = []
        if not path.exists():
            return records
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            try:
                records.append(JudgmentRecord(**data))
            except (TypeError, ValueError):
                continue
        self._judgments = records
        return records

    def all_judgments(self) -> list[JudgmentRecord]:
        """返回所有判断记录。"""
        return list(self._judgments)


class ReviewEngine:
    """复盘引擎：评估判断表现并生成调整建议。"""

    def __init__(
        self,
        review_log: ResearchReviewLog | None = None,
        price_provider: PriceProvider | None = None,
        benchmark_return: float = 0.0,
        hit_threshold: float = 0.05,
        auto_persist: bool = True,
    ) -> None:
        """初始化。

        Args:
            review_log: 判断记录日志。
            price_provider: 价格数据提供者。
            benchmark_return: 基准收益率，默认 0（无风险/空仓）。
            hit_threshold: 超过该收益率视为命中。
            auto_persist: 是否在 record_judgment 后自动持久化到 JSONL，
                          仅当 auto_persist=True 时才从磁盘加载历史数据，
                          避免单元测试间数据污染。
        """
        self.review_log = review_log or ResearchReviewLog()
        self.price_provider = price_provider
        self.benchmark_return = _safe_float(benchmark_return)
        self.hit_threshold = _safe_float(hit_threshold, 0.05)
        self.auto_persist = auto_persist
        self._persist_filename = "judgments.jsonl"
        if self.auto_persist:
            try:
                self.review_log.load(self._persist_filename)
            except Exception:
                pass

    def record_judgment(
        self,
        agent: str,
        target_type: str,
        target_key: str,
        decision: str,
        confidence: str,
        reasoning: str,
        trade_date: str = "",
        evidence_summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> JudgmentRecord:
        """记录一次 agent 判断。

        Returns:
            生成的 JudgmentRecord。
        """
        record = JudgmentRecord(
            judgment_id=f"judg_{uuid.uuid4().hex[:12]}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            trade_date=trade_date,
            agent=agent,
            target_type=target_type,
            target_key=target_key,
            decision=decision,
            confidence=confidence,
            reasoning=reasoning,
            evidence_summary=evidence_summary,
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )
        self.review_log.record(record)
        if self.auto_persist:
            try:
                self.review_log.save(self._persist_filename)
            except Exception:
                pass
        return record

    def evaluate_performance(
        self,
        judgments: list[JudgmentRecord] | None = None,
        horizon_days: int = 20,
    ) -> list[PerformanceRecord]:
        """评估判断记录的市场表现。

        Args:
            judgments: 判断记录列表，默认使用 review_log 中全部。
            horizon_days: 持有期交易日数。

        Returns:
            PerformanceRecord 列表。
        """
        judgments = judgments or self.review_log.all_judgments()
        results: list[PerformanceRecord] = []

        for judgment in judgments:
            if not isinstance(judgment, JudgmentRecord):
                continue
            symbol = self._symbol_from_judgment(judgment)
            if not symbol or not judgment.trade_date:
                continue

            start_date = judgment.trade_date
            end_date = self._add_trading_days(judgment.trade_date, horizon_days)

            realized = 0.0
            if self.price_provider is not None:
                try:
                    realized = _safe_float(
                        self.price_provider.get_return(symbol, start_date, end_date)
                    )
                except Exception:
                    realized = 0.0

            hit = realized >= self.hit_threshold
            miss_reason = ""
            if not hit:
                if realized < 0:
                    miss_reason = "股价下跌"
                elif realized < self.hit_threshold:
                    miss_reason = "涨幅未达预期"

            results.append(
                PerformanceRecord(
                    judgment_id=judgment.judgment_id,
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    realized_return=realized,
                    benchmark_return=self.benchmark_return,
                    hit=hit,
                    miss_reason=miss_reason,
                )
            )

        return results

    def adjust_agent_weights(
        self,
        performance_records: list[PerformanceRecord],
        judgments: list[JudgmentRecord] | None = None,
    ) -> dict[str, float]:
        """基于命中表现调整各 agent 权重。

        规则：
          - 初始权重 1.0
          - 命中率 > 0.6：+0.2
          - 命中率 < 0.3：-0.2
          - 平均收益 < -0.05：额外 -0.1
          - 权重限制在 [0.2, 2.0]

        Args:
            performance_records: 表现记录。
            judgments: 原始判断记录，用于关联 agent。

        Returns:
            agent -> weight 字典。
        """
        judgments = judgments or self.review_log.all_judgments()
        judgment_to_agent = {j.judgment_id: j.agent for j in judgments}

        stats: dict[str, dict[str, Any]] = {}
        for perf in performance_records:
            agent = judgment_to_agent.get(perf.judgment_id)
            if not agent:
                continue
            if agent not in stats:
                stats[agent] = {"hits": 0, "total": 0, "avg_return": 0.0}
            stats[agent]["total"] += 1
            if perf.hit:
                stats[agent]["hits"] += 1
            stats[agent]["avg_return"] += perf.realized_return

        weights: dict[str, float] = {}
        for agent, s in stats.items():
            total = s["total"]
            if total == 0:
                weights[agent] = 1.0
                continue
            hit_rate = s["hits"] / total
            avg_return = s["avg_return"] / total

            weight = 1.0
            if hit_rate > 0.6:
                weight += 0.2
            elif hit_rate < 0.3:
                weight -= 0.2
            if avg_return < -0.05:
                weight -= 0.1

            weights[agent] = round(max(0.2, min(2.0, weight)), 2)

        return weights

    def generate_lessons(
        self,
        performance_records: list[PerformanceRecord],
        judgments: list[JudgmentRecord] | None = None,
    ) -> list[str]:
        """从命中/失误中提取经验规则。

        Args:
            performance_records: 表现记录。
            judgments: 原始判断记录。

        Returns:
            经验规则文本列表。
        """
        judgments = judgments or self.review_log.all_judgments()
        judgment_map = {j.judgment_id: j for j in judgments}

        hit_reasons: list[str] = []
        miss_reasons: list[str] = []
        confidence_hit: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
        confidence_total: dict[str, int] = {"high": 0, "medium": 0, "low": 0}

        for perf in performance_records:
            judgment = judgment_map.get(perf.judgment_id)
            if judgment is None:
                continue
            confidence_total[judgment.confidence] = (
                confidence_total.get(judgment.confidence, 0) + 1
            )
            if perf.hit:
                confidence_hit[judgment.confidence] = (
                    confidence_hit.get(judgment.confidence, 0) + 1
                )
                hit_reasons.append(judgment.reasoning)
            else:
                miss_reasons.append(judgment.reasoning)

        lessons: list[str] = []
        for conf in ("high", "medium", "low"):
            total = confidence_total.get(conf, 0)
            if total >= 3:
                hit_rate = confidence_hit.get(conf, 0) / total
                if hit_rate > 0.6:
                    lessons.append(f"{conf} 置信度判断胜率 {hit_rate:.0%}，未来可保持该档位决策权重")
                elif hit_rate < 0.3:
                    lessons.append(f"{conf} 置信度判断胜率 {hit_rate:.0%}，需降低该档位权重并增加反证检查")

        # 简单规则：如果失误记录里频繁出现风险忽略，则生成规则
        risk_ignored = sum(1 for r in miss_reasons if "风险" not in r and "减持" not in r)
        if risk_ignored >= 2 and len(miss_reasons) >= 3:
            lessons.append("失误判断中常忽略风险因素，今后 Quant/Risk agent 反对时应强制降档")

        # 命中记录若常出现多源证据，则强化该规则
        multi_source_hits = sum(
            1
            for j_id in [p.judgment_id for p in performance_records if p.hit]
            if judgment_map.get(j_id) and "多源" in judgment_map[j_id].reasoning
        )
        if multi_source_hits >= 2:
            lessons.append("命中判断多伴随多源证据，今后 evidence 数量 < 2 的主题应要求补充证据")

        return lessons

    def save_review_report(
        self,
        performance_records: list[PerformanceRecord],
        weights: dict[str, float],
        lessons: list[str],
        filename: str = "review_report_latest.json",
    ) -> Path:
        """保存复盘报告。"""
        path = self.review_log._storage_path / _safe_filename(filename, "review_report_latest.json")
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "performance_count": len(performance_records),
            "performance": [p.to_dict() for p in performance_records],
            "agent_weights": weights,
            "lessons": lessons,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _symbol_from_judgment(judgment: JudgmentRecord) -> str:
        """从判断记录提取股票代码。"""
        if judgment.target_type == "stock":
            return judgment.target_key
        # theme 类型尝试从 metadata 取代表股票
        return judgment.metadata.get("proxy_symbol", "") if isinstance(judgment.metadata, dict) else ""

    @staticmethod
    def _add_trading_days(start_date: str, days: int) -> str:
        """简单按自然日估算交易日，忽略节假日。"""
        try:
            dt = datetime.strptime(start_date, "%Y-%m-%d")
        except (TypeError, ValueError):
            return ""
        dt = dt + timedelta(days=days * 7 // 5)
        return dt.strftime("%Y-%m-%d")
