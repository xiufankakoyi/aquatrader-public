"""P2.10 严格认证契约、不可变证据和 CLI smoke test。

合成证据只验证认证器的通过/拒绝边界，不替代真实 100 公司生产认证。
"""
from __future__ import annotations

import json
import hashlib
import hmac
from datetime import date, timedelta
from pathlib import Path

import pytest
import sandbox.p2_10_shadow_acceptance as acceptance_module

from sandbox.p2_10_shadow_acceptance import (
    BUSINESS_MODEL_CYCLICAL,
    BUSINESS_MODEL_FINANCIAL,
    BUSINESS_MODEL_GROWTH,
    BUSINESS_MODEL_STABLE_CASHFLOW,
    MIN_COMPANIES,
    MIN_FORMAL_VPS,
    MIN_INDUSTRIES,
    MIN_P1_COMPLETE_MODELS,
    MIN_TRADING_DAYS,
    P2ShadowAcceptanceRunner,
    REQUIRED_BUSINESS_MODELS,
    compute_evidence_fingerprint,
    compute_universe_fingerprint,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_minimal_universe(
    *,
    n_companies: int = 0,
    n_industries: int = 0,
    biz_models: tuple[str, ...] = (),
    p1_complete_count: int = 0,
    trading_days: int = 0,
    topk_budget: dict | None = None,
) -> dict:
    """构造最小化 universe 数据（用于测试脚手架）。"""
    companies = []
    industries = [f"行业{i}" for i in range(max(n_industries, 0))]
    biz_model_list = list(biz_models) or [BUSINESS_MODEL_GROWTH]

    for i in range(max(n_companies, 0)):
        company = {
            "symbol": f"TEST{i:03d}",
            "name": f"测试公司{i:03d}",
            "industry": industries[i % len(industries)] if industries else "未知",
            "business_model": biz_model_list[i % len(biz_model_list)] if biz_model_list else BUSINESS_MODEL_GROWTH,
            "p1_complete": i < p1_complete_count,
        }
        profile = {
            "as_of": "2027-01-01",
            "name": company["name"],
            "industry": company["industry"],
            "business_model": company["business_model"],
            "risk_status": "normal",
            "available_at": "2026-12-31",
            "membership_intervals": [
                {"valid_from": "2020-01-01", "valid_to": "", "status": "listed"}
            ],
            "source_refs": [f"local://pit/{company['symbol']}"],
            "business_model_evidence_refs": [f"local://pit/{company['symbol']}/business-model"],
            "risk_status_evidence_refs": [f"local://pit/{company['symbol']}/risk"],
        }
        profile["profile_hash"] = compute_evidence_fingerprint(profile, "profile_hash")
        company["pit_profile_evidence"] = profile
        companies.append(company)

    return {
        "as_of": "2027-01-01",
        "companies": companies,
        "trading_days_covered": trading_days,
        "topk_budget": topk_budget or {
            "total_budget": 1000,
            "stop_conditions": {"max_cost_per_company": 50.0},
        },
    }


def _add_funnel_evidence(universe: dict) -> None:
    universe["funnel_results"] = [
        {
            "symbol": company["symbol"],
            "decision": "shadow_include",
            "field_explanations": ["quality.revenue: 已按 policy 评估"],
            "policy_version": "p2-shadow-v1",
            "config_hash": "a" * 64,
        }
        for company in universe["companies"]
    ]


def _make_complete_evidence_universe() -> dict:
    universe = _make_minimal_universe(
        n_companies=MIN_COMPANIES,
        n_industries=MIN_INDUSTRIES,
        biz_models=REQUIRED_BUSINESS_MODELS,
        trading_days=MIN_TRADING_DAYS,
    )
    universe["prerequisites"] = {
        "p0_status": "passed",
        "p1_status": "passed",
        "p1_manual_review_count": 10,
        "evidence_refs": ["p0-report#sha256", "p1-report#sha256"],
    }
    trading_dates: list[str] = []
    current = date(2024, 11, 1)
    while len(trading_dates) < MIN_TRADING_DAYS:
        if current.weekday() < 5:
            trading_dates.append(current.isoformat())
        current += timedelta(days=1)
    calendar = {
        "source_ref": "exchange-calendar:sse:2024:v1",
        "dates": trading_dates,
    }
    calendar["calendar_fingerprint"] = compute_evidence_fingerprint(
        calendar, "calendar_fingerprint"
    )
    run_input_fingerprint = "b" * 64
    daily_artifacts = [
        {
            "date": value,
            "run_id": f"daily-{index:02d}",
            "mode": "pit_replay",
            "started_at": f"{value}T09:30:00+08:00",
            "finished_at": f"{value}T15:00:00+08:00",
            "artifact_ref": f"local://runs/daily-{index:02d}.json",
            "content_sha256": f"{index + 1:064x}",
            "input_fingerprint": run_input_fingerprint,
        }
        for index, value in enumerate(trading_dates)
    ]
    for artifact in daily_artifacts:
        artifact["artifact_hash"] = compute_evidence_fingerprint(
            artifact, "artifact_hash"
        )
    universe["experiment"] = {
        "mode": "pit_replay",
        "observation_dates": trading_dates,
        "trading_calendar": calendar,
        "daily_run_artifacts": daily_artifacts,
        "run_input_fingerprint": run_input_fingerprint,
        "policy_version": "p2-shadow-v1",
        "model_versions": {"p1": "1.0", "p2": "2.0"},
        "evaluation_metrics": ["funnel_coverage", "evidence_hit_rate"],
    }
    _add_funnel_evidence(universe)
    universe["scheduler_evidence"] = {
        "executed": True,
        "topk_symbols": ["TEST000", "TEST001"],
        "searched_symbols": ["TEST000"],
        "total_budget": 1000.0,
        "budget_used": 25.0,
        "stop_conditions_exercised": True,
        "stop_events": ["max_documents_per_company"],
        "outside_topk_queries": 0,
    }
    for company in universe["companies"][:MIN_P1_COMPLETE_MODELS]:
        company["p1_model_evidence"] = {
            "statements_complete": True,
            "product_driver_model": True,
            "linked_forecast": True,
            "valuation_methods": ["historical_pe", "historical_pb"],
            "source_refs": [f"statement:{company['symbol']}"],
            "model_versions": {"forecast": "1.0", "valuation": "1.0"},
        }
    universe["formal_vps"] = [
        {
            "thesis_id": f"vp-{index}",
            "committee_status": "approved_for_monitoring",
            "analyst_id": f"analyst-{index}",
            "adversary_id": f"adversary-{index}",
            "adversarial_review_id": f"review-{index}",
            "committee_record_id": f"committee-{index}",
            "market_view": "来源于冻结市场预期记录",
            "internal_view": "来源于 P1 可审计模型",
            "driver_differences": ["revenue_growth"],
            "catalysts": ["下一期经营数据验证"],
            "counter_evidence_ids": [f"counter-{index}"],
            "invalidation_conditions": ["核心经营假设不成立"],
        }
        for index in range(MIN_FORMAL_VPS)
    ]
    for formal_vp in universe["formal_vps"]:
        formal_vp["record_hash"] = compute_evidence_fingerprint(
            formal_vp, "record_hash"
        )
    company_audits = [
        {
            "symbol": company["symbol"],
            "as_of": universe["as_of"],
            "leakage_detected": False,
            "membership_status_checked": True,
            "availability_timestamps_checked": True,
            "artifact_ref": f"local://pit-audit/{company['symbol']}.json",
            "source_refs": [f"local://pit/{company['symbol']}"],
            "content_sha256": f"{index + 1:064x}",
        }
        for index, company in enumerate(universe["companies"])
    ]
    for item in company_audits:
        item["audit_hash"] = compute_evidence_fingerprint(item, "audit_hash")
    universe["pit_audit"] = {
        "checked_count": MIN_COMPANIES,
        "leakage_count": 0,
        "survivorship_bias_checked": True,
        "historical_membership_as_of": universe["as_of"],
        "availability_timestamps_checked": True,
        "membership_populations_checked": ["listed", "delisted"],
        "company_audits": company_audits,
    }
    universe["pit_audit"]["audit_fingerprint"] = compute_evidence_fingerprint(
        universe["pit_audit"], "audit_fingerprint"
    )
    universe["experiment"]["data_fingerprint"] = compute_universe_fingerprint(universe)
    return universe


TEST_ATTESTATION_KEY = b"unit-test-only-p2-attestation-key"


def _attest(universe: dict) -> None:
    fingerprint = compute_universe_fingerprint(universe)
    universe["attestation"] = {
        "algorithm": "hmac-sha256",
        "key_id": "unit-test-key",
        "signature": hmac.new(
            TEST_ATTESTATION_KEY, fingerprint.encode("ascii"), hashlib.sha256
        ).hexdigest(),
    }


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestShadowAcceptanceRunner:
    """P2.10 shadow 认证器测试。"""

    def test_empty_universe_fails_all_gates(self):
        """空 universe → 全部门槛失败。"""
        universe = _make_minimal_universe()
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        assert report.overall_passed is False
        assert len(report.gates) == 7
        # G1 失败（公司数 0 < 100）
        g1 = next(g for g in report.gates if g.gate_id == "G1_funnel_coverage")
        assert not g1.passed
        assert "行业覆盖不足" in g1.note

    def test_full_universe_meets_g1(self):
        """满足 100 公司 + 15 行业 + 4 商业模式 → G1 通过。"""
        universe = _make_minimal_universe(
            n_companies=MIN_COMPANIES,
            n_industries=MIN_INDUSTRIES,
            biz_models=REQUIRED_BUSINESS_MODELS,
        )
        _add_funnel_evidence(universe)
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        g1 = next(g for g in report.gates if g.gate_id == "G1_funnel_coverage")
        assert g1.passed
        assert g1.actual_value >= MIN_COMPANIES

    def test_g1_fails_when_missing_business_model(self):
        """缺商业模式 → G1 失败。"""
        universe = _make_minimal_universe(
            n_companies=MIN_COMPANIES,
            n_industries=MIN_INDUSTRIES,
            biz_models=(
                BUSINESS_MODEL_GROWTH,
                BUSINESS_MODEL_CYCLICAL,
                BUSINESS_MODEL_STABLE_CASHFLOW,
            ),  # 缺 financial
        )
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        g1 = next(g for g in report.gates if g.gate_id == "G1_funnel_coverage")
        assert not g1.passed
        assert "financial" in g1.note

    def test_g2_passes_with_execution_evidence(self):
        """真实执行过 Top-K、预算和停止条件 → G2 通过。"""
        universe = _make_minimal_universe()
        universe["scheduler_evidence"] = {
            "executed": True,
            "topk_symbols": ["TEST000"],
            "searched_symbols": ["TEST000"],
            "total_budget": 1000,
            "budget_used": 10,
            "stop_conditions_exercised": True,
            "stop_events": ["timeout_seconds"],
            "outside_topk_queries": 0,
        }
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        g2 = next(g for g in report.gates if g.gate_id == "G2_topk_budget")
        assert g2.passed

    def test_g3_p1_complete_count(self):
        """P1 完整模型数量校验。"""
        universe = _make_minimal_universe(
            n_companies=50,
            n_industries=15,
            biz_models=REQUIRED_BUSINESS_MODELS,
            p1_complete_count=MIN_P1_COMPLETE_MODELS,
        )
        for company in universe["companies"][:MIN_P1_COMPLETE_MODELS]:
            company["p1_model_evidence"] = {
                "statements_complete": True,
                "product_driver_model": True,
                "linked_forecast": True,
                "valuation_methods": ["pe", "pb"],
                "source_refs": ["statement:test"],
                "model_versions": {"p1": "1"},
            }
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        g3 = next(g for g in report.gates if g.gate_id == "G3_p1_complete_models")
        assert g3.passed
        assert g3.actual_value >= MIN_P1_COMPLETE_MODELS

    def test_g3_fails_when_insufficient(self):
        """P1 完整模型不足 → G3 失败。"""
        universe = _make_minimal_universe(
            n_companies=50,
            n_industries=15,
            biz_models=REQUIRED_BUSINESS_MODELS,
            p1_complete_count=10,  # < 30
        )
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        g3 = next(g for g in report.gates if g.gate_id == "G3_p1_complete_models")
        assert not g3.passed

    def test_g4_no_vps_records_no_sufficient_note(self):
        """无 VP 时 G4 失败并记录『无足够差异化观点』。"""
        universe = _make_minimal_universe()
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        g4 = next(g for g in report.gates if g.gate_id == "G4_formal_vps")
        assert not g4.passed
        # 按 P2 §5 规则不凑数
        assert "不凑数" in g4.note or "无足够差异化观点" in g4.note

    def test_g6_no_repos_returns_zero_checked(self):
        """未注入仓储且无 list_date → G6 失败（checked=0）。"""
        universe = _make_minimal_universe()
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        g6 = next(g for g in report.gates if g.gate_id == "G6_pit_no_leakage")
        assert not g6.passed  # checked=0 → 失败

    def test_g6_complete_pit_audit_passes(self):
        """PIT、历史退市样本和可见时间均审计后 G6 通过。"""
        universe = _make_minimal_universe(n_companies=5, n_industries=5)
        universe["pit_audit"] = {
            "checked_count": 5,
            "leakage_count": 0,
            "survivorship_bias_checked": True,
            "historical_membership_as_of": universe["as_of"],
            "availability_timestamps_checked": True,
            "membership_populations_checked": ["listed", "delisted"],
            "company_audits": [],
        }
        for index, company in enumerate(universe["companies"]):
            item = {
                "symbol": company["symbol"],
                "as_of": universe["as_of"],
                "leakage_detected": False,
                "membership_status_checked": True,
                "availability_timestamps_checked": True,
                "artifact_ref": f"local://pit-audit/{company['symbol']}.json",
                "source_refs": [f"local://pit/{company['symbol']}"],
                "content_sha256": f"{index + 1:064x}",
            }
            item["audit_hash"] = compute_evidence_fingerprint(item, "audit_hash")
            universe["pit_audit"]["company_audits"].append(item)
        universe["pit_audit"]["audit_fingerprint"] = compute_evidence_fingerprint(
            universe["pit_audit"], "audit_fingerprint"
        )
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        g6 = next(g for g in report.gates if g.gate_id == "G6_pit_no_leakage")
        assert g6.passed
        assert g6.actual_value == 0  # leakage_count = 0
        assert any("checked_count=5" in e for e in g6.evidence)

    def test_g6_universe_pit_fallback_fails_when_list_date_future(self):
        """list_date 晚于 as_of → G6 失败（存在未来泄漏）。"""
        universe = _make_minimal_universe(n_companies=3, n_industries=3)
        # 给每家公司加 list_date，全部晚于 as_of=2027-01-01
        universe["pit_audit"] = {
            "checked_count": 3,
            "leakage_count": 3,
            "survivorship_bias_checked": True,
            "historical_membership_as_of": universe["as_of"],
            "availability_timestamps_checked": True,
            "membership_populations_checked": ["listed", "delisted"],
        }
        universe["pit_audit"]["audit_fingerprint"] = compute_evidence_fingerprint(
            universe["pit_audit"], "audit_fingerprint"
        )
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        g6 = next(g for g in report.gates if g.gate_id == "G6_pit_no_leakage")
        assert not g6.passed
        assert g6.actual_value == 3  # 3 家泄漏

    def test_g7_acceptance_report_path_check(self, tmp_path):
        """G7 检查验收报告路径。"""
        universe = _make_minimal_universe()
        report_path = tmp_path / "p2-acceptance-report.md"
        runner = P2ShadowAcceptanceRunner(
            universe_data=universe,
            report_path=report_path,
        )
        report = runner.run()
        g7 = next(g for g in report.gates if g.gate_id == "G7_acceptance_report")
        # 报告路径应在 evidence 中
        assert any("p2-acceptance-report.md" in e for e in g7.evidence)

    def test_complete_evidence_bundle_passes_all_gates(self, tmp_path, monkeypatch):
        universe = _make_complete_evidence_universe()
        _attest(universe)
        trust_store = tmp_path / "trust.json"
        trust_store.write_text(json.dumps({
            "trusted_keys": {
                "unit-test-key": hashlib.sha256(TEST_ATTESTATION_KEY).hexdigest()
            }
        }), encoding="utf-8")
        monkeypatch.setattr(acceptance_module, "TRUST_STORE_PATH", trust_store)
        report = P2ShadowAcceptanceRunner(
            universe_data=universe,
            report_path=tmp_path / "p2-acceptance-report.md",
            attestation_key=TEST_ATTESTATION_KEY,
        ).run()
        assert report.input_contract_passed is True
        assert report.attestation_passed is True
        assert report.prerequisites_passed is True
        assert report.experiment_contract_passed is True
        assert all(gate.passed for gate in report.gates if gate.gate_id != "G7_acceptance_report")
        assert next(g for g in report.gates if g.gate_id == "G7_acceptance_report").passed is False
        assert report.overall_passed is False

    def test_self_declared_evidence_without_trusted_key_cannot_pass(self, tmp_path):
        universe = _make_complete_evidence_universe()
        _attest(universe)
        report = P2ShadowAcceptanceRunner(
            universe_data=universe,
            report_path=tmp_path / "report.md",
        ).run()
        assert report.attestation_passed is False
        assert report.overall_passed is False

    def test_owner_authorized_p1_ai_internal_prerequisite_is_explicitly_accepted(self):
        universe = _make_minimal_universe()
        universe["prerequisites"] = {
            "p0_status": "passed",
            "p1_status": "P1_AI_CERTIFIED",
            "p1_review_mode": "ai_only_internal",
            "p1_ai_review_count": 10,
            "p1_human_independent_review": False,
            "p1_identity_independent": False,
            "evidence_refs": ["p0-report#sha256", "p1-ai-audit#sha256"],
        }

        passed, failures = P2ShadowAcceptanceRunner(
            universe_data=universe
        )._validate_prerequisites()

        assert passed is True
        assert failures == []

    def test_p1_ai_prerequisite_without_disclosed_non_independence_fails(self):
        universe = _make_minimal_universe()
        universe["prerequisites"] = {
            "p0_status": "passed",
            "p1_status": "P1_AI_CERTIFIED",
            "p1_review_mode": "ai_only_internal",
            "p1_ai_review_count": 10,
            "evidence_refs": ["p0-report#sha256", "p1-ai-audit#sha256"],
        }

        passed, failures = P2ShadowAcceptanceRunner(
            universe_data=universe
        )._validate_prerequisites()

        assert passed is False
        assert any("独立性字段" in item for item in failures)

    def test_caller_selected_key_is_not_a_trust_root(self, tmp_path, monkeypatch):
        universe = _make_complete_evidence_universe()
        _attest(universe)
        trust_store = tmp_path / "trust.json"
        trust_store.write_text(
            json.dumps({"trusted_keys": {"production-key": "0" * 64}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(acceptance_module, "TRUST_STORE_PATH", trust_store)
        report = P2ShadowAcceptanceRunner(
            universe_data=universe,
            report_path=tmp_path / "report.md",
            attestation_key=TEST_ATTESTATION_KEY,
        ).run()
        assert report.attestation_passed is False
        assert report.overall_passed is False

    def test_independent_attestation_workflow_uses_fixed_trust_store(
        self, tmp_path, monkeypatch
    ):
        from sandbox.p2_10_attest_universe import attest_universe
        trust_store = tmp_path / "trust.json"
        trust_store.write_text(json.dumps({
            "trusted_keys": {
                "unit-test-key": hashlib.sha256(TEST_ATTESTATION_KEY).hexdigest()
            }
        }), encoding="utf-8")
        monkeypatch.setattr(acceptance_module, "TRUST_STORE_PATH", trust_store)
        signed = attest_universe(
            _make_complete_evidence_universe(),
            key=TEST_ATTESTATION_KEY,
            key_id="unit-test-key",
        )
        report = P2ShadowAcceptanceRunner(
            universe_data=signed,
            attestation_key=TEST_ATTESTATION_KEY,
        ).run()
        assert report.attestation_passed is True

    def test_duplicate_symbols_fail_input_and_do_not_inflate_counts(self):
        universe = _make_complete_evidence_universe()
        universe["companies"][-1] = dict(universe["companies"][0])
        universe["experiment"]["data_fingerprint"] = compute_universe_fingerprint(universe)
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        assert report.input_contract_passed is False
        assert report.universe_size == MIN_COMPANIES - 1

    def test_calendar_and_daily_artifacts_are_required(self):
        universe = _make_complete_evidence_universe()
        universe["experiment"].pop("trading_calendar")
        universe["experiment"].pop("daily_run_artifacts")
        universe["experiment"]["data_fingerprint"] = compute_universe_fingerprint(universe)
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        assert report.experiment_contract_passed is False
        assert any("交易日历" in item for item in report.experiment_contract_failures)

    def test_non_consecutive_trading_dates_fail_experiment_contract(self):
        universe = _make_complete_evidence_universe()
        skipped = universe["experiment"]["observation_dates"].pop(10)
        replacement = "2024-12-02"
        universe["experiment"]["observation_dates"].append(replacement)
        artifact = universe["experiment"]["daily_run_artifacts"].pop(10)
        artifact["date"] = replacement
        artifact["artifact_hash"] = compute_evidence_fingerprint(artifact, "artifact_hash")
        universe["experiment"]["daily_run_artifacts"].append(artifact)
        universe["experiment"]["trading_calendar"]["dates"].append(replacement)
        calendar = universe["experiment"]["trading_calendar"]
        calendar["calendar_fingerprint"] = compute_evidence_fingerprint(
            calendar, "calendar_fingerprint"
        )
        universe["experiment"]["data_fingerprint"] = compute_universe_fingerprint(universe)
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        assert report.experiment_contract_passed is False
        assert skipped not in universe["experiment"]["observation_dates"]

    def test_malformed_nested_evidence_fails_without_crashing(self):
        universe = _make_minimal_universe(n_companies=1)
        universe["prerequisites"] = []
        universe["experiment"] = {"observation_dates": 42}
        universe["scheduler_evidence"] = {"total_budget": "NaN", "stop_events": 42}
        universe["formal_vps"] = [None]
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        assert report.overall_passed is False
        assert len(report.gates) == 7

    def test_unhashable_company_fields_fail_without_crashing(self):
        universe = _make_minimal_universe(n_companies=1)
        universe["companies"][0]["industry"] = []
        universe["companies"][0]["business_model"] = {}
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        assert report.overall_passed is False
        assert report.input_contract_passed is False

    def test_nan_in_universe_fails_close_without_crashing(self):
        """输入含 float('nan')/Infinity → 失败关闭，不崩溃。"""
        universe = _make_complete_evidence_universe()
        # 注入真正的 float('nan')（非字符串 "NaN"）
        universe["companies"][0]["nan_field"] = float("nan")
        # 删除已计算的 data_fingerprint，触发 run() 顶部重新计算
        universe["experiment"].pop("data_fingerprint", None)
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        assert report.overall_passed is False
        assert report.universe_fingerprint == ""
        assert any("NaN" in item or "不可序列化" in item for item in report.input_contract_failures)

    def test_duplicate_formal_vp_is_counted_once(self):
        universe = _make_complete_evidence_universe()
        duplicate = dict(universe["formal_vps"][0])
        universe["formal_vps"] = [duplicate for _ in range(MIN_FORMAL_VPS)]
        universe["experiment"]["data_fingerprint"] = compute_universe_fingerprint(universe)
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        gate = next(g for g in report.gates if g.gate_id == "G4_formal_vps")
        assert gate.actual_value == 1
        assert gate.passed is False

    def test_tampered_vp_record_is_not_counted(self, tmp_path):
        universe = _make_complete_evidence_universe()
        universe["formal_vps"][0]["analyst_id"] = "tampered"
        universe["experiment"]["data_fingerprint"] = compute_universe_fingerprint(universe)
        report = P2ShadowAcceptanceRunner(
            universe_data=universe,
            report_path=tmp_path / "report.md",
        ).run()
        gate = next(g for g in report.gates if g.gate_id == "G4_formal_vps")
        assert gate.passed is False
        assert gate.actual_value == MIN_FORMAL_VPS - 1

    def test_tampered_pit_audit_fingerprint_fails_g6(self):
        universe = _make_complete_evidence_universe()
        universe["pit_audit"]["checked_count"] += 1
        universe["experiment"]["data_fingerprint"] = compute_universe_fingerprint(universe)
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        gate = next(g for g in report.gates if g.gate_id == "G6_pit_no_leakage")
        assert gate.passed is False

    def test_invalid_observation_dates_fail_experiment_contract(self):
        universe = _make_complete_evidence_universe()
        universe["experiment"]["observation_dates"] = [
            f"not-a-date-{index}" for index in range(MIN_TRADING_DAYS)
        ]
        universe["experiment"]["data_fingerprint"] = compute_universe_fingerprint(universe)
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        assert report.experiment_contract_passed is False
        assert any("观察日格式" in item for item in report.experiment_contract_failures)

    def test_report_write_failure_is_reported_as_g7_failure(self, tmp_path):
        blocking_file = tmp_path / "not-a-directory"
        blocking_file.write_text("block", encoding="utf-8")
        report = P2ShadowAcceptanceRunner(
            universe_data=_make_complete_evidence_universe(),
            report_path=blocking_file / "report.md",
        ).run()
        gate = next(g for g in report.gates if g.gate_id == "G7_acceptance_report")
        assert gate.passed is False
        assert report.overall_passed is False

    def test_report_serializable(self):
        """报告可序列化为 JSON。"""
        universe = _make_minimal_universe()
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        d = report.to_dict()
        s = json.dumps(d, ensure_ascii=False)
        assert "gates" in json.loads(s)
        assert len(json.loads(s)["gates"]) == 7

    def test_report_has_run_id_and_timestamps(self):
        """报告含 run_id 和时间戳。"""
        universe = _make_minimal_universe()
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        assert report.run_id.startswith("p2_shadow_")
        assert report.started_at
        assert report.finished_at

    def test_failure_reasons_listed_when_not_passed(self):
        """未通过时 failure_reasons 列出原因。"""
        universe = _make_minimal_universe()
        runner = P2ShadowAcceptanceRunner(universe_data=universe)
        report = runner.run()
        assert not report.overall_passed
        assert len(report.failure_reasons) > 0


# ---------------------------------------------------------------------------
# CLI 测试
# ---------------------------------------------------------------------------


class TestShadowAcceptanceCLI:
    """P2.10 shadow 认证 CLI 测试。"""

    def test_cli_requires_universe_arg(self, capsys):
        """CLI 缺 --universe → 报错。"""
        from sandbox.p2_10_shadow_acceptance import main
        with pytest.raises(SystemExit):
            main([])

    def test_cli_nonexistent_universe_returns_2(self, tmp_path, capsys):
        """CLI universe 文件不存在 → 返回 2。"""
        from sandbox.p2_10_shadow_acceptance import main
        rc = main(["--universe", str(tmp_path / "nonexistent.json")])
        assert rc == 2

    def test_cli_runs_and_outputs_json(self, tmp_path):
        """CLI 可执行并输出 JSON。"""
        from sandbox.p2_10_shadow_acceptance import main
        universe_path = tmp_path / "universe.json"
        universe_path.write_text(
            json.dumps(_make_minimal_universe(n_companies=1, n_industries=1)),
            encoding="utf-8",
        )
        output_path = tmp_path / "report.json"
        markdown_path = tmp_path / "report.md"
        rc = main([
            "--universe", str(universe_path),
            "--output", str(output_path),
            "--markdown-report", str(markdown_path),
        ])
        # 空 universe 不会通过 → rc=1
        assert rc == 1
        # 报告文件已生成
        assert output_path.exists()
        report = json.loads(output_path.read_text(encoding="utf-8"))
        assert "gates" in report
        assert len(report["gates"]) == 7
        assert report["run_id"] in markdown_path.read_text(encoding="utf-8")
        assert (tmp_path / "acceptance_manifest.json").exists()

    def test_cli_trusted_bundle_commits_signed_manifest(
        self, tmp_path, monkeypatch
    ):
        from sandbox.p2_10_shadow_acceptance import main, verify_report_manifest
        universe = _make_complete_evidence_universe()
        _attest(universe)
        trust_store = tmp_path / "trust.json"
        trust_store.write_text(json.dumps({
            "trusted_keys": {
                "unit-test-key": hashlib.sha256(TEST_ATTESTATION_KEY).hexdigest()
            }
        }), encoding="utf-8")
        monkeypatch.setattr(acceptance_module, "TRUST_STORE_PATH", trust_store)
        monkeypatch.setenv(
            "AQUATRADE_P2_ACCEPTANCE_KEY", TEST_ATTESTATION_KEY.decode("ascii")
        )
        universe_path = tmp_path / "universe.json"
        universe_path.write_text(json.dumps(universe), encoding="utf-8")
        output_path = tmp_path / "report.json"
        markdown_path = tmp_path / "report.md"
        manifest_path = tmp_path / "manifest.json"
        rc = main([
            "--universe", str(universe_path),
            "--output", str(output_path),
            "--markdown-report", str(markdown_path),
            "--manifest", str(manifest_path),
        ])
        assert rc == 0
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["overall_passed"] is True
        assert manifest["manifest_signature"]
        assert manifest["json_sha256"] == hashlib.sha256(output_path.read_bytes()).hexdigest()
        assert verify_report_manifest(
            manifest_path, attestation_key=TEST_ATTESTATION_KEY
        ) is True
        output_path.write_text("tampered", encoding="utf-8")
        assert verify_report_manifest(
            manifest_path, attestation_key=TEST_ATTESTATION_KEY
        ) is False

    def test_cli_refuses_to_overwrite_acceptance_evidence(self, tmp_path):
        from sandbox.p2_10_shadow_acceptance import main
        universe_path = tmp_path / "universe.json"
        universe_path.write_text(
            json.dumps(_make_minimal_universe()), encoding="utf-8"
        )
        output_path = tmp_path / "report.json"
        markdown_path = tmp_path / "report.md"
        output_path.write_text("existing", encoding="utf-8")
        rc = main([
            "--universe", str(universe_path),
            "--output", str(output_path),
            "--markdown-report", str(markdown_path),
        ])
        assert rc == 3
        assert output_path.read_text(encoding="utf-8") == "existing"

    def test_cli_malformed_contract_returns_2_and_writes_failed_report(self, tmp_path):
        from sandbox.p2_10_shadow_acceptance import main
        universe_path = tmp_path / "universe.json"
        universe_path.write_text(
            json.dumps({"as_of": "2024-12-31", "companies": "invalid"}),
            encoding="utf-8",
        )
        output_path = tmp_path / "report.json"
        rc = main(["--universe", str(universe_path), "--output", str(output_path)])
        assert rc == 2
        report = json.loads(output_path.read_text(encoding="utf-8"))
        assert report["input_contract_passed"] is False
        assert report["overall_passed"] is False


# ---------------------------------------------------------------------------
# AGENTS.md 合规
# ---------------------------------------------------------------------------


class TestAgentsMdCompliance:
    """AGENTS.md 合规检查。"""

    def test_no_trade_advice_in_source(self):
        """源码中无交易动作建议。"""
        path = (
            Path(__file__).resolve().parent.parent
            / "sandbox"
            / "p2_10_shadow_acceptance.py"
        )
        text = path.read_text(encoding="utf-8")
        # FORBIDDEN_TRADE_ACTION_KEYWORDS 元组中的字符串本身不算违规
        for kw in ("买入", "卖出", "止盈", "止损", "加仓", "减仓", "建仓", "清仓", "调仓"):
            if kw in text and f'"{kw}"' not in text and f"'{kw}'" not in text:
                pytest.fail(f"源码含交易动作词：{kw}")

    def test_no_fabricated_company_data(self):
        """源码中无凭空编造的公司数据。"""
        path = (
            Path(__file__).resolve().parent.parent
            / "sandbox"
            / "p2_10_shadow_acceptance.py"
        )
        text = path.read_text(encoding="utf-8")
        # 不应含真实公司代码（如 600519 茅台）
        # 文档字符串中的 "600519" 作为示例不算违规
        # 仅检查数据构造部分
        assert "600519" not in text.replace('"600519"', "").replace("'600519'", "")


class TestStrictProductionEvidence:
    """回归测试：生产认证不能由自报字段或静态配置冒充。"""

    def test_prerequisites_and_experiment_contract_block_overall_pass(self, tmp_path):
        universe = _make_minimal_universe(
            n_companies=MIN_COMPANIES,
            n_industries=MIN_INDUSTRIES,
            biz_models=REQUIRED_BUSINESS_MODELS,
            p1_complete_count=MIN_P1_COMPLETE_MODELS,
            trading_days=MIN_TRADING_DAYS,
        )
        report = P2ShadowAcceptanceRunner(
            universe_data=universe,
            report_path=tmp_path / "acceptance.md",
        ).run()
        assert report.prerequisites_passed is False
        assert report.experiment_contract_passed is False
        assert report.overall_passed is False

    def test_g1_rejects_company_labels_without_funnel_results(self):
        universe = _make_minimal_universe(
            n_companies=MIN_COMPANIES,
            n_industries=MIN_INDUSTRIES,
            biz_models=REQUIRED_BUSINESS_MODELS,
        )
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        gate = next(g for g in report.gates if g.gate_id == "G1_funnel_coverage")
        assert gate.passed is False
        assert "漏斗执行证据" in gate.note

    def test_g2_rejects_static_budget_config_without_execution_evidence(self):
        universe = _make_minimal_universe(
            topk_budget={
                "total_budget": 1000,
                "stop_conditions": {"max_cost_per_company": 50.0},
            }
        )
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        gate = next(g for g in report.gates if g.gate_id == "G2_topk_budget")
        assert gate.passed is False
        assert "调度执行证据" in gate.note

    def test_g3_rejects_legacy_p1_complete_boolean(self):
        universe = _make_minimal_universe(
            n_companies=MIN_P1_COMPLETE_MODELS,
            p1_complete_count=MIN_P1_COMPLETE_MODELS,
        )
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        gate = next(g for g in report.gates if g.gate_id == "G3_p1_complete_models")
        assert gate.passed is False

    def test_g6_rejects_list_date_only_fallback(self):
        universe = _make_minimal_universe(n_companies=5, n_industries=5)
        for company in universe["companies"]:
            company["list_date"] = "2020-01-01"
        report = P2ShadowAcceptanceRunner(universe_data=universe).run()
        gate = next(g for g in report.gates if g.gate_id == "G6_pit_no_leakage")
        assert gate.passed is False
        assert "PIT 审计证据" in gate.note

    def test_direct_runner_cannot_claim_uncommitted_markdown(self, tmp_path):
        report_path = tmp_path / "p2-acceptance.md"
        report = P2ShadowAcceptanceRunner(
            universe_data=_make_minimal_universe(),
            report_path=report_path,
        ).run()
        gate = next(g for g in report.gates if g.gate_id == "G7_acceptance_report")
        assert gate.passed is False
        assert not report_path.exists()
        assert "manifest" in gate.note
