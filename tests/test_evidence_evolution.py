"""P4B 证据演化与市场共振测试。

覆盖 SubTask 20.1-20.10：
- 20.1: hypothesis 能生成 evidence_timeline
- 20.2: belief_state 有 posterior_probability
- 20.3: posterior_probability 被限制在 0-1
- 20.4: skepticism_score 高会降低 posterior_probability
- 20.5: abnormal_moves 文件不存在时不崩溃
- 20.6: trigger_signals 不输出买入/卖出
- 20.7: 五个输出 JSON 都能生成（在 tmp_path 下）
- 20.8: LLM 成功路径（mock）
- 20.9: LLM 失败降级路径（mock）
- 20.10: 合规拦截（mock）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from core.llm_processing.llm_cache import LLMCache
from core.research.perilla.evidence.evidence_evolution_tracker import (
    build_evidence_timeline,
    build_post_mortem_stub,
    run_evidence_evolution,
    update_belief_state,
)
from core.research.perilla.discovery.market_resonance_engine import (
    compute_market_resonance,
    generate_trigger_signals,
    load_market_moves,
    run_market_resonance,
)


# ============================================================================
# 测试辅助函数
# ============================================================================


def _make_hypothesis(
    hyp_id: str = "hyp_1",
    text: str = "GB200 升级可能带动 PCB 需求",
    source_nodes: List[str] = None,
    evidence_docs: List[str] = None,
) -> Dict[str, Any]:
    """创建测试用假设。"""
    return {
        "hypothesis_id": hyp_id,
        "hypothesis_text": text,
        "hypothesis_type": "product_platform_demand",
        "source_nodes": source_nodes or ["GB200", "PCB"],
        "logic_chain": "GB200 放量 → PCB 需求提升",
        "evidence_document_ids": evidence_docs or ["doc_1"],
        "evidence_quotes": ["GB200带动PCB需求"],
        "related_missing_edges": [],
        "novelty_score": 0.5,
        "evidence_score": 0.4,
        "crowding_score": 0.3,
        "skepticism_score": 0.0,
        "status": "born",
        "created_at": "2024-01-01T00:00:00",
    }


def _setup_p4b_data(tmp_path: Path) -> Path:
    """在 tmp_path 下创建 P4B 测试输入文件。"""
    hypotheses = [
        _make_hypothesis("hyp_1", "GB200 升级可能带动 PCB 需求", ["GB200", "PCB"]),
        _make_hypothesis("hyp_2", "LPO 可能影响光模块需求", ["LPO", "光模块"]),
    ]
    evidence_items = [
        {
            "evidence_id": "evi_1",
            "document_id": "doc_1",
            "claim_id": "claim_1",
            "chain_id": "chain_1",
            "quote": "GB200带动PCB需求",
            "source_type": "report",
            "evidence_type": "report",
            "evidence_level": "weak_signal",
            "reason": "测试",
            "published_at": "2024-01-01T00:00:00",
        }
    ]
    evidence_edges = [
        {
            "edge_id": "edge_1",
            "source_node": "GB200",
            "source_type": "product_platform",
            "relation": "possible_downstream_of",
            "target_node": "PCB",
            "target_type": "component",
            "confidence_level": "hypothesis",
            "evidence_document_ids": ["doc_1"],
            "evidence_quotes": [],
            "status": "pending",
            "created_at": "2024-01-01T00:00:00",
        }
    ]
    search_tasks = [
        {
            "task_id": "task_1",
            "query": "GB200 PCB 产业链",
            "query_type": "product_downstream",
            "related_node": "GB200",
            "related_missing_edge_id": "missing_1",
            "priority_score": 0.8,
            "reason": "测试",
            "status": "pending",
            "created_at": "2024-01-01T00:00:00",
            "last_run_at": "",
            "task_source": "missing_edge",
            "generated_by": "industry_chain_graph_builder",
        }
    ]
    skepticism_scores = [
        {
            "hypothesis_id": "hyp_1",
            "skepticism_score": 0.4,
            "single_source_risk": 0.5,
            "weak_relation_risk": 0.4,
            "lack_official_evidence_risk": 0.8,
            "crowding_risk": 0.3,
            "missing_company_mapping_risk": 0.3,
            "reason": "测试",
        },
        {
            "hypothesis_id": "hyp_2",
            "skepticism_score": 0.6,
            "single_source_risk": 0.6,
            "weak_relation_risk": 0.5,
            "lack_official_evidence_risk": 0.8,
            "crowding_risk": 0.4,
            "missing_company_mapping_risk": 0.4,
            "reason": "测试",
        },
    ]

    (tmp_path / "research_hypotheses_latest.json").write_text(
        json.dumps(hypotheses, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "evidence_items_latest.json").write_text(
        json.dumps(evidence_items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "evidence_edges_latest.json").write_text(
        json.dumps(evidence_edges, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "search_tasks_from_missing_edges_latest.json").write_text(
        json.dumps(search_tasks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "skepticism_scores_latest.json").write_text(
        json.dumps(skepticism_scores, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path


# ============================================================================
# 测试类
# ============================================================================


class TestEvidenceTimeline:
    """测试证据时间线。"""

    def test_hypothesis_generates_timeline(self):
        """SubTask 20.1: hypothesis 能生成 evidence_timeline。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        timeline = build_evidence_timeline(
            hypotheses, [], [], [], []
        )
        # 至少应有 hypothesis_created 事件
        assert len(timeline) > 0, "应生成时间线事件"
        event_types = [e["event_type"] for e in timeline]
        assert "hypothesis_created" in event_types, (
            f"应包含 hypothesis_created 事件，实际: {event_types}"
        )

    def test_event_type_in_allowed_enum(self):
        """测试 event_type 在允许枚举内。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        timeline = build_evidence_timeline(hypotheses, [], [], [], [])
        allowed = {
            "hypothesis_created", "evidence_added", "edge_added",
            "search_task_created", "skepticism_updated", "market_move_observed",
        }
        for event in timeline:
            assert event["event_type"] in allowed, (
                f"event_type 应在允许枚举内，实际: {event['event_type']}"
            )


class TestBeliefState:
    """测试 belief_state。"""

    def test_belief_state_has_posterior_probability(self):
        """SubTask 20.2: belief_state 有 posterior_probability。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        timeline = build_evidence_timeline(hypotheses, [], [], [], [])
        belief_states = update_belief_state(
            hypotheses, timeline, [], [], use_llm=False
        )
        assert len(belief_states) == 1
        belief = belief_states[0]
        assert "posterior_probability" in belief, "应有 posterior_probability"
        assert "state" in belief, "应有 state"

    def test_posterior_probability_in_range(self):
        """SubTask 20.3: posterior_probability 被限制在 0-1。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        timeline = build_evidence_timeline(hypotheses, [], [], [], [])
        belief_states = update_belief_state(
            hypotheses, timeline, [], [], use_llm=False
        )
        for belief in belief_states:
            posterior = float(belief["posterior_probability"])
            assert 0 <= posterior <= 1, (
                f"posterior_probability 应在 0-1，实际: {posterior}"
            )

    def test_high_skepticism_lowers_posterior(self):
        """SubTask 20.4: skepticism_score 高会降低 posterior_probability。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        timeline = build_evidence_timeline(hypotheses, [], [], [], [])

        # 低 skepticism
        low_skep = [{"hypothesis_id": "hyp_1", "skepticism_score": 0.1}]
        low_belief = update_belief_state(hypotheses, timeline, low_skep, [], use_llm=False)
        low_posterior = float(low_belief[0]["posterior_probability"])

        # 高 skepticism
        high_skep = [{"hypothesis_id": "hyp_1", "skepticism_score": 0.9}]
        high_belief = update_belief_state(hypotheses, timeline, high_skep, [], use_llm=False)
        high_posterior = float(high_belief[0]["posterior_probability"])

        assert high_posterior < low_posterior, (
            f"高 skepticism 应降低 posterior，低={low_posterior}, 高={high_posterior}"
        )

    def test_state_not_confirmed(self):
        """测试 state 不含 confirmed。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        timeline = build_evidence_timeline(hypotheses, [], [], [], [])
        belief_states = update_belief_state(
            hypotheses, timeline, [], [], use_llm=False
        )
        for belief in belief_states:
            assert belief["state"] != "confirmed", (
                f"state 不应为 confirmed，实际: {belief['state']}"
            )

    def test_state_in_allowed_enum(self):
        """测试 state 在允许枚举内。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        timeline = build_evidence_timeline(hypotheses, [], [], [], [])
        belief_states = update_belief_state(
            hypotheses, timeline, [], [], use_llm=False
        )
        allowed = {
            "rejected_or_low_priority", "hypothesis", "weak_signal",
            "watchlist_candidate", "needs_manual_review",
        }
        for belief in belief_states:
            assert belief["state"] in allowed, (
                f"state 应在允许枚举内，实际: {belief['state']}"
            )


class TestMarketResonance:
    """测试市场共振。"""

    def test_abnormal_moves_not_exist_not_crash(self, tmp_path: Path):
        """SubTask 20.5: abnormal_moves 文件不存在时不崩溃。"""
        moves = load_market_moves(tmp_path)
        assert moves == [], "文件不存在时应返回空列表"

    def test_trigger_signals_no_buy_sell(self):
        """SubTask 20.6: trigger_signals 不输出买入/卖出。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        belief_states = [{
            "hypothesis_id": "hyp_1",
            "posterior_probability": 0.5,
            "skepticism_score": 0.3,
            "state": "weak_signal",
        }]
        resonances = [{
            "hypothesis_id": "hyp_1",
            "market_attention_score": 0.6,
        }]
        signals = generate_trigger_signals(hypotheses, belief_states, resonances)
        for signal in signals:
            text = json.dumps(signal, ensure_ascii=False)
            assert "买入" not in text, f"不应包含'买入': {text}"
            assert "卖出" not in text, f"不应包含'卖出': {text}"

    def test_trigger_type_in_allowed_enum(self):
        """测试 trigger_type 在允许枚举内。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        belief_states = [{
            "hypothesis_id": "hyp_1",
            "posterior_probability": 0.5,
            "skepticism_score": 0.3,
            "state": "weak_signal",
        }]
        resonances = [{
            "hypothesis_id": "hyp_1",
            "market_attention_score": 0.6,
        }]
        signals = generate_trigger_signals(hypotheses, belief_states, resonances)
        allowed = {
            "evidence_upgrade", "market_resonance",
            "skepticism_too_high", "needs_manual_review",
        }
        for signal in signals:
            assert signal["trigger_type"] in allowed, (
                f"trigger_type 应在允许枚举内，实际: {signal['trigger_type']}"
            )

    def test_next_action_in_allowed_enum(self):
        """测试 next_action 在允许枚举内。"""
        hypotheses = [_make_hypothesis("hyp_1")]
        belief_states = [{
            "hypothesis_id": "hyp_1",
            "posterior_probability": 0.5,
            "skepticism_score": 0.3,
            "state": "weak_signal",
        }]
        resonances = [{
            "hypothesis_id": "hyp_1",
            "market_attention_score": 0.6,
        }]
        signals = generate_trigger_signals(hypotheses, belief_states, resonances)
        allowed = {
            "keep_monitoring", "run_search_tasks", "manual_review", "downgrade",
        }
        for signal in signals:
            assert signal["next_action"] in allowed, (
                f"next_action 应在允许枚举内，实际: {signal['next_action']}"
            )


class TestJsonGeneration:
    """测试 JSON 文件生成。"""

    def test_five_jsons_generated(self, tmp_path: Path):
        """SubTask 20.7: 五个输出 JSON 都能生成（在 tmp_path 下）。"""
        _setup_p4b_data(tmp_path)
        # 1. 证据演化
        evo_summary = run_evidence_evolution(
            input_dir=tmp_path, output_dir=tmp_path, use_llm=False
        )
        assert evo_summary["success"], f"证据演化失败: {evo_summary.get('error')}"
        assert (tmp_path / "evidence_timeline_latest.json").exists()
        assert (tmp_path / "belief_state_latest.json").exists()
        assert (tmp_path / "post_mortem_latest.json").exists()

        # 2. 市场共振
        res_summary = run_market_resonance(input_dir=tmp_path, output_dir=tmp_path)
        assert res_summary["success"], f"市场共振失败: {res_summary.get('error')}"
        assert (tmp_path / "market_resonance_latest.json").exists()
        assert (tmp_path / "trigger_signals_latest.json").exists()

    def test_does_not_touch_real_data_dir(self, tmp_path: Path):
        """测试不触碰真实 data/reports/perilla_discovery/ 目录。"""
        _setup_p4b_data(tmp_path)
        summary = run_evidence_evolution(
            input_dir=tmp_path, output_dir=tmp_path, use_llm=False
        )
        assert summary["success"]
        for path in summary["outputs"]:
            assert str(tmp_path) in path, f"输出应指向 tmp_path: {path}"


class TestLLMPaths:
    """测试 LLM 路径。"""

    def test_llm_success_updates_strength_and_velocity(self, tmp_path: Path):
        """SubTask 20.8: mock AquaLLM 成功时更新 evidence_strength/evidence_velocity。"""
        _setup_p4b_data(tmp_path)
        hypotheses = json.loads(
            (tmp_path / "research_hypotheses_latest.json").read_text(encoding="utf-8")
        )
        evidence_items = json.loads(
            (tmp_path / "evidence_items_latest.json").read_text(encoding="utf-8")
        )
        evidence_edges = json.loads(
            (tmp_path / "evidence_edges_latest.json").read_text(encoding="utf-8")
        )
        search_tasks = json.loads(
            (tmp_path / "search_tasks_from_missing_edges_latest.json").read_text(encoding="utf-8")
        )
        skepticism_scores = json.loads(
            (tmp_path / "skepticism_scores_latest.json").read_text(encoding="utf-8")
        )
        timeline = build_evidence_timeline(
            hypotheses, evidence_items, evidence_edges, search_tasks, skepticism_scores
        )

        class MockAquaLLM:
            model_name = "mock-p4b"

            def generate_json(self, **kwargs):
                return {
                    "evidence_strength": 0.8,
                    "evidence_velocity": 0.6,
                    "reason": "mock 证据强度较高",
                }

        beliefs = update_belief_state(
            hypotheses,
            timeline,
            skepticism_scores,
            market_moves=[],
            use_llm=True,
            llm=MockAquaLLM(),
            cache=LLMCache(str(tmp_path / "llm_cache")),
        )

        assert beliefs
        assert beliefs[0]["llm_status"] == "llm"
        assert beliefs[0]["evidence_strength"] == 0.8
        assert beliefs[0]["evidence_velocity"] == 0.6
        assert 0 <= beliefs[0]["posterior_probability"] <= 1

    def test_llm_compliance_block_falls_back_to_rules(self, tmp_path: Path):
        """SubTask 20.10: mock AquaLLM 命中合规禁用词时回退规则。"""
        _setup_p4b_data(tmp_path)
        hypotheses = json.loads(
            (tmp_path / "research_hypotheses_latest.json").read_text(encoding="utf-8")
        )
        evidence_items = json.loads(
            (tmp_path / "evidence_items_latest.json").read_text(encoding="utf-8")
        )
        evidence_edges = json.loads(
            (tmp_path / "evidence_edges_latest.json").read_text(encoding="utf-8")
        )
        search_tasks = json.loads(
            (tmp_path / "search_tasks_from_missing_edges_latest.json").read_text(encoding="utf-8")
        )
        skepticism_scores = json.loads(
            (tmp_path / "skepticism_scores_latest.json").read_text(encoding="utf-8")
        )
        timeline = build_evidence_timeline(
            hypotheses, evidence_items, evidence_edges, search_tasks, skepticism_scores
        )

        class MockAquaLLM:
            model_name = "mock-p4b"

            def generate_json(self, **kwargs):
                return {
                    "evidence_strength": 0.9,
                    "evidence_velocity": 0.9,
                    "reason": "建议买入后再验证",
                }

        beliefs = update_belief_state(
            hypotheses,
            timeline,
            skepticism_scores,
            market_moves=[],
            use_llm=True,
            llm=MockAquaLLM(),
            cache=LLMCache(str(tmp_path / "llm_cache_block")),
        )

        assert beliefs
        assert beliefs[0]["llm_status"] == "rule_fallback"
        dumped = json.dumps(beliefs, ensure_ascii=False)
        assert "建议买入" not in dumped

    def test_llm_not_available_pure_rules(self, tmp_path: Path):
        """SubTask 20.9: LLM 不可用时降级为纯规则归一化。"""
        _setup_p4b_data(tmp_path)
        summary = run_evidence_evolution(
            input_dir=tmp_path, output_dir=tmp_path, use_llm=False
        )
        assert summary["success"], "LLM 不可用时应降级到规则模式"
        assert summary["counts"]["belief_state"] > 0

    def test_llm_failure_fallback_to_rules(self, tmp_path: Path):
        """SubTask 20.9: LLM 失败时降级到规则归一化。"""
        _setup_p4b_data(tmp_path)
        hypotheses = json.loads(
            (tmp_path / "research_hypotheses_latest.json").read_text(encoding="utf-8")
        )
        evidence_items = json.loads(
            (tmp_path / "evidence_items_latest.json").read_text(encoding="utf-8")
        )
        evidence_edges = json.loads(
            (tmp_path / "evidence_edges_latest.json").read_text(encoding="utf-8")
        )
        search_tasks = json.loads(
            (tmp_path / "search_tasks_from_missing_edges_latest.json").read_text(encoding="utf-8")
        )
        skepticism_scores = json.loads(
            (tmp_path / "skepticism_scores_latest.json").read_text(encoding="utf-8")
        )
        timeline = build_evidence_timeline(
            hypotheses, evidence_items, evidence_edges, search_tasks, skepticism_scores
        )

        class FailingAquaLLM:
            model_name = "mock-p4b"

            def generate_json(self, **kwargs):
                raise RuntimeError("mock LLM failure")

        beliefs = update_belief_state(
            hypotheses,
            timeline,
            skepticism_scores,
            market_moves=[],
            use_llm=True,
            llm=FailingAquaLLM(),
            cache=LLMCache(str(tmp_path / "llm_cache_fail")),
        )
        assert beliefs, "LLM 失败时应降级到规则模式"
        assert beliefs[0]["llm_status"] == "rule_fallback"

    def test_compliance_block(self, tmp_path: Path):
        """SubTask 20.10: 合规拦截测试。"""
        _setup_p4b_data(tmp_path)
        summary = run_evidence_evolution(
            input_dir=tmp_path, output_dir=tmp_path, use_llm=False
        )
        assert summary["success"]
        # 读取 belief_state，验证不含禁用词
        beliefs = json.loads(
            (tmp_path / "belief_state_latest.json").read_text(encoding="utf-8")
        )
        for belief in beliefs:
            text = json.dumps(belief, ensure_ascii=False)
            assert "买入" not in text, f"belief_state 不应包含'买入': {text}"
            assert "卖出" not in text, f"belief_state 不应包含'卖出': {text}"
            assert "强烈推荐" not in text, f"belief_state 不应包含'强烈推荐': {text}"
