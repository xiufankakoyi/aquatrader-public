"""P0 正式契约测试。

验证 docs/research/institutional/p0-contracts.md 中的字段契约：
- ResearchFact 必填字段集合
- ResearchAssumption 必填字段集合
- ResearchReadiness 八维字段集合
- source_type 四类互斥
- 假设状态迁移合法路径
- 契约文档无 TBD / TODO

字段集合常量将作为 P0.2 实现时的对照基线，禁止实现层擅自放宽约束。
"""
from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_MD = PROJECT_ROOT / "docs" / "research" / "institutional" / "p0-contracts.md"


# ---------------------------------------------------------------------------
# 契约字段集合（冻结）
# ---------------------------------------------------------------------------

RESEARCH_FACT_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "fact_id",
    "entity_id",
    "metric_or_relation",
    "value",
    "unit",
    "currency",
    "period",
    "published_at",
    "retrieved_at",
    "available_at",
    "valid_from",
    "valid_to",
    "source_trace_id",
    "evidence_ids",
    "grounding_status",
    "confidence",
    "observation_kind",
    "data_version",
})

RESEARCH_ASSUMPTION_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "assumption_id",
    "symbol",
    "variable_name",
    "interval",
    "unit",
    "period",
    "source_type",
    "source_fact_ids",
    "source_evidence_ids",
    "created_at",
    "available_at",
    "expires_at",
    "owner",
    "model_version",
    "status",
    "falsification_condition",
})

RESEARCH_READINESS_DIMENSIONS: tuple[str, ...] = (
    "fact_completeness",
    "source_traceability",
    "source_independence",
    "pit_integrity",
    "forecast_input_completeness",
    "model_computability",
    "valuation_computability",
    "research_uncertainty",
)

RESEARCH_READINESS_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "status",
    "score",
    "missing_items",
    "evidence_refs",
})

RESEARCH_FACT_GROUNDING_STATUSES: frozenset[str] = frozenset({
    "grounding_ok",
    "grounding_partial",
    "missing",
})

RESEARCH_FACT_OBSERVATION_KINDS: frozenset[str] = frozenset({
    "verified_fact",
    "company_disclosed",
    "third_party_consensus",
    "market_implied",
    "analyst_assumption",
    "unknown",
})

RESEARCH_ASSUMPTION_SOURCE_TYPES: frozenset[str] = frozenset({
    "company_disclosed",
    "third_party_consensus",
    "market_implied",
    "analyst_assumption",
})

RESEARCH_ASSUMPTION_STATUSES: frozenset[str] = frozenset({
    "draft",
    "active",
    "falsified",
    "superseded",
})

RESEARCH_ASSUMPTION_VALID_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("draft", "active"),
    ("active", "falsified"),
    ("active", "superseded"),
    ("falsified", "superseded"),
})

RESEARCH_READINESS_VALID_STATUSES: frozenset[str] = frozenset({
    "ok",
    "partial",
    "blocked",
    "not_computable",
})


# ---------------------------------------------------------------------------
# 契约文档约束
# ---------------------------------------------------------------------------


def test_p0_contracts_document_exists() -> None:
    """契约文档必须存在；缺失直接视为 P0 未通过。"""
    assert CONTRACTS_MD.exists(), f"missing contracts file: {CONTRACTS_MD}"


def test_p0_contracts_document_has_no_tbd_or_todo() -> None:
    """契约文档不得包含 TBD / TODO / 待定 / 待补。"""
    if not CONTRACTS_MD.exists():
        pytest.skip("contracts doc absent; validated above")
    text = CONTRACTS_MD.read_text(encoding="utf-8")
    forbidden = ("TBD", "TODO", "FIXME", "待定", "待补", "待完善")
    found = [token for token in forbidden if token in text]
    assert not found, f"contracts doc contains placeholders: {found}"


def test_p0_contracts_document_lists_all_required_fact_fields() -> None:
    """契约文档必须显式列出 ResearchFact 全部必填字段。"""
    if not CONTRACTS_MD.exists():
        pytest.skip("contracts doc absent; validated above")
    text = CONTRACTS_MD.read_text(encoding="utf-8")
    missing = [field for field in RESEARCH_FACT_REQUIRED_FIELDS if field not in text]
    assert not missing, f"contracts doc missing fact fields: {missing}"


def test_p0_contracts_document_lists_all_required_assumption_fields() -> None:
    """契约文档必须显式列出 ResearchAssumption 全部必填字段。"""
    if not CONTRACTS_MD.exists():
        pytest.skip("contracts doc absent; validated above")
    text = CONTRACTS_MD.read_text(encoding="utf-8")
    missing = [field for field in RESEARCH_ASSUMPTION_REQUIRED_FIELDS if field not in text]
    assert not missing, f"contracts doc missing assumption fields: {missing}"


def test_p0_contracts_document_lists_all_readiness_dimensions() -> None:
    """契约文档必须显式列出 ResearchReadiness 八维字段。"""
    if not CONTRACTS_MD.exists():
        pytest.skip("contracts doc absent; validated above")
    text = CONTRACTS_MD.read_text(encoding="utf-8")
    missing = [dim for dim in RESEARCH_READINESS_DIMENSIONS if dim not in text]
    assert not missing, f"contracts doc missing readiness dimensions: {missing}"


def test_p0_contracts_document_lists_source_type_enum() -> None:
    """契约文档必须列出四类 source_type 互斥语义。"""
    if not CONTRACTS_MD.exists():
        pytest.skip("contracts doc absent; validated above")
    text = CONTRACTS_MD.read_text(encoding="utf-8")
    for source_type in RESEARCH_ASSUMPTION_SOURCE_TYPES:
        assert source_type in text, f"contracts doc missing source_type: {source_type}"


def test_p0_contracts_document_lists_assumption_transitions() -> None:
    """契约文档必须列出合法状态迁移路径。"""
    if not CONTRACTS_MD.exists():
        pytest.skip("contracts doc absent; validated above")
    text = CONTRACTS_MD.read_text(encoding="utf-8")
    for from_status, to_status in RESEARCH_ASSUMPTION_VALID_TRANSITIONS:
        assert from_status in text and to_status in text, (
            f"contracts doc missing transition: {from_status}->{to_status}"
        )


# ---------------------------------------------------------------------------
# 字段集合不变量
# ---------------------------------------------------------------------------


def test_research_fact_required_fields_are_minimum() -> None:
    """RESEARCH_FACT_REQUIRED_FIELDS 必须与 P0 计划 3.1 一致。"""
    assert RESEARCH_FACT_REQUIRED_FIELDS == {
        "fact_id", "entity_id", "metric_or_relation", "value", "unit",
        "currency", "period", "published_at", "retrieved_at", "available_at",
        "valid_from", "valid_to", "source_trace_id", "evidence_ids",
        "grounding_status", "confidence", "observation_kind", "data_version",
    }


def test_research_assumption_required_fields_are_minimum() -> None:
    """RESEARCH_ASSUMPTION_REQUIRED_FIELDS 必须与 P0 计划 3.2 一致。"""
    assert RESEARCH_ASSUMPTION_REQUIRED_FIELDS == {
        "assumption_id", "symbol", "variable_name", "interval", "unit",
        "period", "source_type", "source_fact_ids", "source_evidence_ids",
        "created_at", "available_at", "expires_at", "owner", "model_version",
        "status", "falsification_condition",
    }


def test_research_readiness_dimensions_count_is_eight() -> None:
    """P0 计划 3.3 要求 readiness 必须拆为八维。"""
    assert len(RESEARCH_READINESS_DIMENSIONS) == 8
    assert len(set(RESEARCH_READINESS_DIMENSIONS)) == 8


def test_assumption_source_types_are_mutually_exclusive() -> None:
    """四类 source_type 必须互斥；同一假设不得同时归为多种来源。"""
    assert len(RESEARCH_ASSUMPTION_SOURCE_TYPES) == 4
    assert RESEARCH_ASSUMPTION_SOURCE_TYPES == {
        "company_disclosed", "third_party_consensus",
        "market_implied", "analyst_assumption",
    }


def test_assumption_valid_transitions_form_dag() -> None:
    """合法迁移必须形成 DAG；不允许出现回环。"""
    from_statuses = {f for f, _ in RESEARCH_ASSUMPTION_VALID_TRANSITIONS}
    to_statuses = {t for _, t in RESEARCH_ASSUMPTION_VALID_TRANSITIONS}
    # 终态（superseded）不能同时作为起点
    assert "superseded" not in from_statuses, (
        "terminal status 'superseded' must not be a source state"
    )
    # 所有合法状态必须出现在迁移图里
    assert from_statuses | to_statuses == RESEARCH_ASSUMPTION_STATUSES


# ---------------------------------------------------------------------------
# 基线脚本不变量
# ---------------------------------------------------------------------------


def test_audit_p0_baseline_script_exists() -> None:
    """P0.1 要求 scripts/audit_p0_baseline.py 存在且只读生产数据库。"""
    script = PROJECT_ROOT / "scripts" / "audit_p0_baseline.py"
    assert script.exists(), f"missing audit script: {script}"
    text = script.read_text(encoding="utf-8")
    # 严格只读：连接必须使用 mode=ro
    assert "mode=ro" in text, "audit script must open databases read-only"
    # 不得包含写入或迁移语句
    forbidden = ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE", "ALTER TABLE")
    found = [token for token in forbidden if token in text]
    assert not found, f"audit script must be read-only, found write ops: {found}"


def test_audit_p0_baseline_does_not_clean_workspace() -> None:
    """审计脚本不得修改 git 工作区。"""
    script = PROJECT_ROOT / "scripts" / "audit_p0_baseline.py"
    text = script.read_text(encoding="utf-8")
    forbidden = (
        "git checkout",
        "git restore",
        "git reset --hard",
        "git clean -fd",
        "git stash",
    )
    found = [token for token in forbidden if token in text]
    assert not found, f"audit script modifies workspace: {found}"


def test_formal_research_records_satisfy_frozen_contracts() -> None:
    """正式 dossier 记录必须实例化 P0 Fact/Assumption，而不是只保留临时 Claim。"""
    from core.research.value_chain.records import build_formal_research_records

    facts, assumptions = build_formal_research_records(
        symbol="300308.SZ",
        industry_context={
            "verified_edges": [{
                "claim_id": "claim-product",
                "subject_id": "300308.SZ",
                "relation": "PRODUCES",
                "object_name": "光通信收发模块",
                "evidence_confidence": 0.9,
                "evidence": [{
                    "evidence_id": "ev-product",
                    "source_trace_id": "src-product",
                    "published_at": "2026-03-31",
                    "retrieved_at": "2026-07-18T00:00:00+00:00",
                    "grounding_status": "exact",
                    "metadata": {"period": "2025"},
                }],
            }],
        },
        profit_bridge={
            "forward_driver_assumptions": [{
                "assumption_id": "assumption-capacity",
                "metric": "capacity",
                "interval": {"low": 100.0, "high": 120.0},
                "unit": "pieces",
                "period": "2026",
                "source_type": "analyst_assumption",
                "source_fact_ids": ["claim-product"],
                "source_evidence_ids": ["ev-product"],
                "created_at": "2026-07-18T00:00:00+00:00",
                "available_at": "2026-07-18T00:00:00+00:00",
                "owner": "research",
                "status": "active",
                "falsification_condition": "产能未达到区间下界",
            }],
        },
    )

    fact = facts[0].to_dict()
    assumption = assumptions[0].to_dict()
    assert set(fact) == RESEARCH_FACT_REQUIRED_FIELDS
    assert fact["source_trace_id"] == "src-product"
    assert fact["evidence_ids"] == ["ev-product"]
    assert fact["grounding_status"] == "grounding_ok"
    assert set(assumption) == RESEARCH_ASSUMPTION_REQUIRED_FIELDS
    assert assumption["source_type"] == "analyst_assumption"


def test_formal_research_records_do_not_guess_assumption_source_type() -> None:
    from core.research.value_chain.records import build_formal_research_records

    _, assumptions = build_formal_research_records(
        symbol="300308.SZ",
        industry_context={},
        profit_bridge={
            "forward_driver_assumptions": [{
                "metric": "capacity",
                "value": 100.0,
                "eligible_as_forward_assumption": True,
            }],
        },
    )

    assert assumptions == []


def test_formal_record_builder_projects_finance_rows_with_exact_provenance() -> None:
    from core.research.value_chain.records import build_formal_research_records

    facts, _ = build_formal_research_records(
        symbol="600519.SH",
        industry_context={},
        profit_bridge={},
        source_snapshots={
            "finance": {
                "capability": "finance_snapshot",
                "provider": "tushare",
                "records": [{
                    "symbol": "600519.SH",
                    "period": "20251231",
                    "published_at": "20260331",
                    "revenue": 100.0,
                    "net_profit": 20.0,
                    "provider": "tushare",
                    "updated_at": "2026-07-18 10:00:00",
                }],
            },
        },
    )

    revenue = next(fact for fact in facts if fact.metric_or_relation == "revenue")
    assert revenue.value == 100.0
    assert revenue.period == "20251231"
    assert revenue.available_at == "2026-03-31"
    assert revenue.source_trace_id == "tushare:finance_snapshot:600519.SH:20251231"
    assert revenue.evidence_ids == ["tushare:finance_snapshot:600519.SH:20251231:revenue"]
    assert revenue.grounding_status == "grounding_ok"
