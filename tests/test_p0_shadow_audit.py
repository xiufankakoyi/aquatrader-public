from __future__ import annotations


def _formal_fact(**overrides):
    fact = {
        "fact_id": "fact-1",
        "entity_id": "300308.SZ",
        "metric_or_relation": "PRODUCES",
        "value": "光模块",
        "unit": "relation_target",
        "currency": "",
        "period": "2025",
        "published_at": "2026-03-31",
        "retrieved_at": "2026-07-18T00:00:00+00:00",
        "available_at": "2026-03-31",
        "valid_from": "",
        "valid_to": "",
        "source_trace_id": "src-1",
        "evidence_ids": ["ev-1"],
        "grounding_status": "grounding_ok",
        "confidence": {"low": 0.9, "high": 0.9},
        "observation_kind": "verified_fact",
        "data_version": "research_fact_v1",
    }
    fact.update(overrides)
    return fact


def test_fact_audit_checks_formal_contract_traceability_and_pit() -> None:
    from scripts.run_value_chain_shadow import _collect_fact_audit

    audit = _collect_fact_audit({
        "as_of": "2026-07-18",
        "research_facts": [
            _formal_fact(),
            _formal_fact(
                fact_id="fact-2",
                source_trace_id="",
                evidence_ids=[],
                grounding_status="missing",
                available_at="2026-07-19",
            ),
        ],
        "research_assumptions": [],
        "research_readiness": {},
        "source_snapshots": {},
        "profit_bridge": {},
    })

    assert audit["formal_facts_total"] == 2
    assert audit["formal_facts_traceable"] == 1
    assert audit["formal_fact_traceability_rate"] == 0.5
    assert audit["formal_fact_contract_violations"] == 0
    assert audit["formal_fact_future_violations"] == 1


def test_fact_audit_detects_missing_contract_fields_and_unsafe_defaults() -> None:
    from scripts.run_value_chain_shadow import _collect_fact_audit

    broken = _formal_fact()
    broken.pop("source_trace_id")
    audit = _collect_fact_audit({
        "as_of": "2026-07-18",
        "research_facts": [broken],
        "research_assumptions": [],
        "research_readiness": {
            "model_computability": {"status": "blocked", "score": 0.5},
        },
        "source_snapshots": {},
        "profit_bridge": {
            "baseline": {"period": "20251231", "net_profit": 100.0},
            "market_implied_scenarios": [{"historical_product_revenue_exposure": 1.0}],
        },
    })

    assert audit["formal_fact_contract_violations"] == 1
    assert audit["blocked_dims_score_is_none"] is False
    assert audit["unsafe_default_violations"] == 1


def test_shadow_hard_gates_return_explicit_failures() -> None:
    from scripts.run_value_chain_shadow import _evaluate_hard_gates

    failures = _evaluate_hard_gates({
        "symbol_count": 20,
        "completed_count": 20,
        "failed_count": 0,
        "industry_count": 15,
        "aggregates": {
            "fact_audit": {
                "formal_facts_total": 10,
                "formal_fact_traceability_rate": 0.9,
                "formal_fact_contract_violations": 0,
                "formal_fact_future_violations": 0,
                "visible_source_future_violations": 0,
                "unsafe_default_violations": 0,
                "blocked_score_none_companies": 20,
            },
        },
    })

    assert failures == ["formal_fact_traceability_below_100pct"]
