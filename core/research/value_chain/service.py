from __future__ import annotations

import json
import math
from collections.abc import Mapping
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .contracts import CompanyDossier, ValuationState
from .causal_context import ResearchCausalContext
from .fundamentals import compute_financial_quality
from .source_gateway import ResearchSourceGateway
from .profit_bridge import build_profit_bridge
from .expectation_context import ExpectationContext
from .consensus import summarize_consensus
from .profit_discovery import (
    load_forward_disclosed_indicators,
    load_observed_operating_facts,
)
from .readiness import build_research_readiness
from .records import build_formal_research_records
from .repository import ResearchRunRepository
from .valuation_context import summarize_valuation_history
from .research_assessment import build_research_assessment
from core.research.control_plane.repository import ResearchRepository


class CompanyValueResearchService:
    """Build an auditable dossier without manufacturing missing fundamentals."""

    def __init__(
        self,
        registry: Any | None = None,
        causal_context: ResearchCausalContext | None = None,
        expectation_context: ExpectationContext | None = None,
        research_repository: ResearchRepository | None = None,
        runs_repository: ResearchRunRepository | None = None,
    ) -> None:
        self.gateway = ResearchSourceGateway(registry=registry)
        self.causal_context = causal_context or ResearchCausalContext()
        self.expectation_context = expectation_context or ExpectationContext()
        self.research_repository = research_repository or ResearchRepository()
        # P0.3: 研究运行仓储，惰性创建以避免单测中无关路径写入
        self.runs_repository = runs_repository

    @staticmethod
    def _technical_context(records: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
        records = [dict(item) for item in records if isinstance(item, Mapping)] if isinstance(records, (list, tuple)) else []
        if not records:
            return {"technical_state": "UNKNOWN", "status": "not_computable"}
        frame = pd.DataFrame(records).copy()
        if "stock_code" not in frame.columns and "symbol" in frame.columns:
            frame["stock_code"] = frame["symbol"]
        required = {"stock_code", "trade_date", "close"}
        if not required.issubset(frame.columns):
            return {"technical_state": "UNKNOWN", "status": "not_computable"}
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        for column in ("close", "volume", "amount"):
            frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
        frame = frame.dropna(subset=["trade_date", "close"]).sort_values(["stock_code", "trade_date"])
        from core.quant.theme.market_feature_enricher import ThemeMarketFeatureEnricher

        features = ThemeMarketFeatureEnricher._symbol_features(frame)
        row = features.get(symbol) or features.get(symbol.split(".")[0])
        if row is None and len(features) == 1:
            row = next(iter(features.values()))
        return {"status": "ok", **row} if row else {"technical_state": "UNKNOWN", "status": "not_computable"}

    @staticmethod
    def _valuation(records: list[dict[str, Any]], *, as_of: str, provider: str) -> ValuationState:
        records = [item for item in records if isinstance(item, Mapping)] if isinstance(records, (list, tuple)) else []
        if not records:
            return ValuationState(as_of=as_of, provider=provider)
        row = records[0]
        metrics: dict[str, float] = {}
        aliases = {
            "pe": ("pe", "pe_ttm"), "pb": ("pb", "pb_mrq"),
            "market_cap": ("market_cap", "total_market_cap", "total_mv"),
        }
        for output, names in aliases.items():
            for name in names:
                try:
                    value = float(row.get(name))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    metrics[output] = value
                    break
        return ValuationState(
            status="partial" if metrics else "not_computable",
            metrics=metrics,
            as_of=as_of,
            provider=provider,
        )

    def build_dossier(self, symbol: str, *, as_of: str, lookback_days: int = 120) -> CompanyDossier:
        snapshots = self.gateway.collect(symbol, as_of=as_of, lookback_days=lookback_days)
        profile = snapshots["profile"].records[0] if snapshots["profile"].records else {}
        financial_history = snapshots["finance"].records
        financial_quality = compute_financial_quality(financial_history)
        technical = self._technical_context(snapshots["daily_bars"].records, symbol)
        industry_context = self.causal_context.for_company(
            symbol=symbol,
            company_name=str(profile.get("stock_name") or profile.get("name") or symbol),
            as_of=str(as_of)[:10],
        )
        observed_operating_facts = load_observed_operating_facts(
            self.research_repository, symbol=symbol, as_of=str(as_of)[:10]
        )
        forward_disclosed_indicators = load_forward_disclosed_indicators(
            self.research_repository, symbol=symbol, as_of=str(as_of)[:10]
        )
        expectation_context = self.expectation_context.for_company(symbol, str(as_of)[:10])
        structured_consensus = summarize_consensus(
            snapshots["consensus"].records, as_of=str(as_of)[:10]
        )
        valuation_context = summarize_valuation_history(
            snapshots["valuation_history"].records,
            structured_consensus=structured_consensus,
            as_of=str(as_of)[:10],
        )
        if valuation_context["status"] in {"ok", "partial"}:
            current_valuation = valuation_context.get("current", {})
            if not isinstance(current_valuation, Mapping):
                current_valuation = {}
            valuation = ValuationState(
                status=valuation_context["status"],
                metrics={
                    output: float(current_valuation[source])
                    for output, source in (
                        ("pe", "pe_ttm"), ("pb", "pb"),
                        ("market_cap", "total_market_cap"),
                    )
                    if _finite_positive(current_valuation.get(source)) is not None
                },
                as_of=str(current_valuation.get("trade_date") or str(as_of)[:10]),
                provider=snapshots["valuation_history"].provider,
            )
        else:
            valuation = self._valuation(
                snapshots["valuation"].records,
                as_of=str(as_of)[:10],
                provider=snapshots["valuation"].provider,
            )
        profit_bridge = build_profit_bridge(
            financial_history,
            snapshots["earnings_forecast"].records,
            observed_operating_facts=observed_operating_facts,
            structured_consensus=structured_consensus,
            forward_disclosed_indicators=forward_disclosed_indicators,
        )
        expectation_context["structured_consensus"] = structured_consensus
        if structured_consensus["status"] == "verified":
            expectation_context["gaps"] = [
                gap for gap in expectation_context.get("gaps", [])
                if gap != "verified_market_expectation_missing"
            ]
        research_dimensions = {
            "industry_logic": industry_context.get("chain_status", "missing"),
            "fundamental_quality": financial_quality.status,
            "profit_forecast": profit_bridge["status"],
            "market_expectation": (
                "verified" if expectation_context.get("verified_expectations")
                or structured_consensus["status"] == "verified" else "missing"
            ),
            "valuation": valuation.status,
            "technical_context": technical.get("status", "not_computable"),
        }
        degradation: list[str] = []
        if financial_quality.status == "not_computable":
            degradation.append("financial_history_missing")
        if valuation.status == "not_computable":
            degradation.append("valuation_missing")
        if technical.get("status") != "ok":
            degradation.append("technical_context_missing")
        degradation.extend(industry_context.get("gaps", []))
        if profit_bridge["status"] == "market_implied":
            degradation.append("company_forward_driver_evidence_missing")
        elif profit_bridge["status"] != "partial":
            degradation.append("forward_profit_bridge_missing")
        degradation.extend(expectation_context.get("gaps", []))
        for name, snapshot in snapshots.items():
            if snapshot.status != "ok" and f"source_{name}_{snapshot.status}" not in degradation:
                degradation.append(f"source_{name}_{snapshot.status}")
        research_assessment = build_research_assessment(
            industry_context=industry_context,
            financial_quality=financial_quality.to_dict(),
            profit_bridge=profit_bridge,
            structured_consensus=structured_consensus,
            valuation_context=valuation_context,
            technical_context=technical,
            degradation_reasons=degradation,
            expectation_context=expectation_context,
            source_snapshots={name: snap.to_dict() for name, snap in snapshots.items()},
            valuation_state=valuation.to_dict(),
            as_of=str(as_of)[:10],
        )
        readiness = build_research_readiness(
            industry_context=industry_context,
            financial_quality=financial_quality.to_dict(),
            profit_bridge=profit_bridge,
            expectation_context=expectation_context,
            valuation_context=valuation_context,
            valuation_state=valuation.to_dict(),
            source_snapshots={name: snap.to_dict() for name, snap in snapshots.items()},
            as_of=str(as_of)[:10],
        )
        research_facts, research_assumptions = build_formal_research_records(
            symbol=symbol,
            industry_context=industry_context,
            profit_bridge=profit_bridge,
            source_snapshots={name: snap.to_dict() for name, snap in snapshots.items()},
        )
        return CompanyDossier(
            symbol=symbol,
            company_name=str(profile.get("stock_name") or profile.get("name") or symbol),
            industry=str(profile.get("industry") or ""),
            as_of=str(as_of)[:10],
            company_profile=profile,
            financial_history=financial_history,
            financial_quality=financial_quality,
            valuation=valuation,
            valuation_context=valuation_context,
            technical_context=technical,
            earnings_forecasts=snapshots["earnings_forecast"].records,
            announcements=snapshots["announcements"].records,
            industry_context=industry_context,
            profit_bridge=profit_bridge,
            expectation_context=expectation_context,
            research_dimensions=research_dimensions,
            research_assessment=research_assessment,
            source_snapshots=snapshots,
            degradation_reasons=degradation,
            research_readiness=readiness,
            research_facts=research_facts,
            research_assumptions=research_assumptions,
        )

    def run(
        self,
        symbol: str,
        *,
        as_of: str,
        lookback_days: int = 120,
        output_root: str | Path = "data/research_value_runs",
        enqueue_gaps: bool = False,
    ) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        dossier = self.build_dossier(symbol, as_of=as_of, lookback_days=lookback_days)
        run_id = f"value_{started.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
        output_path = Path(output_root)
        if not output_path.is_absolute() and output_path == Path("data/research_value_runs"):
            output_path = Path(__file__).resolve().parents[3] / output_path
        run_dir = output_path / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        payload = dossier.to_dict()
        gap_tasks = {"created_task_ids": [], "existing_task_ids": []}
        if enqueue_gaps:
            from .gap_tasks import GapTaskPlanner

            gap_tasks = GapTaskPlanner().enqueue(
                symbol=dossier.symbol,
                company_name=dossier.company_name,
                as_of=dossier.as_of,
                gaps=dossier.degradation_reasons,
            )
        acceptance = {
            "run_id": run_id,
            "symbol": symbol,
            "as_of": str(as_of)[:10],
            "status": "ok" if not dossier.degradation_reasons else "partial",
            "degradation_reasons": dossier.degradation_reasons,
            "source_statuses": {name: item.status for name, item in dossier.source_snapshots.items()},
            "financial_quality_status": dossier.financial_quality.status,
            "valuation_status": dossier.valuation.status,
            "technical_status": dossier.technical_context.get("status", "not_computable"),
            "gap_tasks": gap_tasks,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "notice": "研究数据产物，不包含交易动作建议。",
        }
        baseline = {
            "generated_at": started.isoformat(),
            "schema_version": dossier.schema_version,
            "source_record_counts": {
                name: len(item.records) for name, item in dossier.source_snapshots.items()
            },
        }
        for name, value in (("baseline.json", baseline), ("dossier.json", payload), ("acceptance.json", acceptance)):
            (run_dir / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
                encoding="utf-8",
            )
        # P0.3: 将本次运行写入不可覆盖仓储索引，便于列表/分页/diff。
        # 同输入重复运行因 run_id 含 uuid 后缀天然生成新版本，不覆盖旧版本。
        # 未显式传入 runs_repository 时，索引库与产物目录对齐，避免单测污染默认 DB。
        if self.runs_repository is not None:
            repository = self.runs_repository
        elif output_path == Path(__file__).resolve().parents[3] / "data/research_value_runs":
            # 正式默认运行与 Research API 共用统一 control 索引。
            repository = ResearchRunRepository(runs_root=output_path)
        else:
            # 临时/测试输出保持自包含，避免污染正式索引。
            repository = ResearchRunRepository(
                db_path=output_path / "research_value_runs.db",
                runs_root=output_path,
            )
        repository.index_dossier_run(
            run_id=run_id,
            dossier=payload,
            acceptance=acceptance,
            artifact_path=str(run_dir.resolve()),
            created_at=acceptance.get("finished_at") or started.isoformat(),
        )
        return {"run_id": run_id, "run_dir": str(run_dir.resolve()), "dossier": payload, "acceptance": acceptance}


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None
