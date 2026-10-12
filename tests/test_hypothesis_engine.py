"""P4A 多智能体假设生成与反方审查测试。

覆盖 SubTask 16.1-16.10：
- 16.1: GB200 与 PTFE 共现时能生成 hypothesis
- 16.2: hypothesis 初始 state 是 born 或 pending
- 16.3: SkepticAgent 能指出"只有共现没有实质关系"（规则降级路径）
- 16.4: EvidenceReviewAgent 能生成 skepticism_score
- 16.5: PortfolioTranslator 不输出买入/卖出
- 16.6: watchlist_action 只能在允许枚举内
- 16.7: 五个输出 JSON 都能生成（在 tmp_path 下）
- 16.8: LLM 成功路径（mock）
- 16.9: LLM 失败降级路径（mock）
- 16.10: 合规拦截（mock）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from core.llm_processing.llm_cache import LLMCache
from core.research.perilla.discovery.adversarial_research_agents import (
    EvidenceReviewAgent,
    PortfolioTranslator,
    SkepticAgent,
    run_adversarial_research_agents,
)
from core.research.perilla.discovery.hypothesis_engine import (
    generate_hypotheses,
    initialize_lifecycle,
    load_inputs,
    run_hypothesis_engine,
)


# ============================================================================
# 测试辅助函数
# ============================================================================


def _make_concept(
    name: str, term_type: str, root_theme: str = "",
    final_score: float = 0.5, doc_ids: List[str] = None,
) -> Dict[str, Any]:
    """创建测试用 candidate_concept。"""
    return {
        "concept_id": f"concept_{name}",
        "concept_name": name,
        "normalized_name": name,
        "term_type": term_type,
        "root_theme": root_theme,
        "evidence_document_ids": doc_ids or ["doc_1"],
        "final_score": final_score,
    }


def _make_causal_chain(
    cause: str, beneficiary: str, doc_id: str = "doc_1",
) -> Dict[str, Any]:
    """创建测试用 causal_chain。"""
    return {
        "chain_id": f"chain_{cause}_{beneficiary}",
        "claim_id": "claim_1",
        "document_id": doc_id,
        "cause": cause,
        "mechanism": "带动",
        "effect": "需求提升",
        "beneficiary_node": beneficiary,
        "evidence_quote": f"{cause}带动{beneficiary}需求",
        "confidence": "low",
        "status": "pending",
    }


def _setup_p4a_data(tmp_path: Path) -> Path:
    """在 tmp_path 下创建 P4A 测试输入文件。"""
    concepts = [
        _make_concept("GB200", "product_platform", "算", 0.8, ["doc_1", "doc_2"]),
        _make_concept("PTFE", "material", "光", 0.6, ["doc_1"]),
        _make_concept("LPO", "technology_route", "光", 0.7, ["doc_1"]),
        _make_concept("PCB", "component", "电", 0.5, ["doc_1"]),
    ]
    causal_chains = [
        _make_causal_chain("GB200", "PCB"),
        _make_causal_chain("LPO", "光模块"),
    ]
    missing_edges = [
        {
            "missing_edge_id": "missing_1",
            "source_node": "GB200",
            "source_type": "product_platform",
            "target_hint": "PCB",
            "priority_score": 0.8,
            "status": "pending",
        }
    ]
    concept_scores = [
        {"concept_name": "GB200", "final_score": 0.8},
        {"concept_name": "PTFE", "final_score": 0.6},
    ]
    industry_chain_graph = {
        "nodes": [{"node_name": "GB200", "node_type": "product_platform"}],
        "edges": [],
    }

    (tmp_path / "candidate_concepts_latest.json").write_text(
        json.dumps(concepts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "causal_chains_latest.json").write_text(
        json.dumps(causal_chains, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "missing_edges_latest.json").write_text(
        json.dumps(missing_edges, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "concept_scores_latest.json").write_text(
        json.dumps(concept_scores, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "industry_chain_graph_latest.json").write_text(
        json.dumps(industry_chain_graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path


# ============================================================================
# 测试类
# ============================================================================


class TestHypothesisGeneration:
    """测试假设生成。"""

    def test_gb200_ptfe_hypothesis_generated(self):
        """SubTask 16.1: GB200 与 PTFE 共现时能生成 hypothesis。"""
        inputs = {
            "candidate_concepts": [
                _make_concept("GB200", "product_platform", "算", 0.8),
                _make_concept("PTFE", "material", "光", 0.6),
            ],
            "causal_chains": [],
            "missing_edges": [],
            "concept_scores": [
                {"concept_name": "GB200", "final_score": 0.8},
                {"concept_name": "PTFE", "final_score": 0.6},
            ],
        }
        hypotheses = generate_hypotheses(inputs, use_llm=False)
        # GB200（product_platform）与 PTFE（material）应生成假设
        texts = [h["hypothesis_text"] for h in hypotheses]
        assert any("GB200" in t and "PTFE" in t for t in texts), (
            f"应生成 GB200 与 PTFE 的假设，实际: {texts}"
        )

    def test_initial_state_born_or_pending(self):
        """SubTask 16.2: hypothesis 初始 state 是 born 或 pending。"""
        inputs = {
            "candidate_concepts": [
                _make_concept("GB200", "product_platform", "算", 0.8),
                _make_concept("PCB", "component", "电", 0.5),
            ],
            "causal_chains": [],
            "missing_edges": [],
            "concept_scores": [],
        }
        hypotheses = generate_hypotheses(inputs, use_llm=False)
        lifecycle = initialize_lifecycle(hypotheses)
        for item in lifecycle:
            assert item["current_state"] in ("born", "pending"), (
                f"初始 state 应为 born 或 pending，实际: {item['current_state']}"
            )

    def test_no_supported_or_tradeable_watch_state(self):
        """测试 state 不含 supported / tradeable_watch。"""
        inputs = {
            "candidate_concepts": [
                _make_concept("GB200", "product_platform", "算", 0.8),
                _make_concept("PCB", "component", "电", 0.5),
            ],
            "causal_chains": [],
            "missing_edges": [],
            "concept_scores": [],
        }
        hypotheses = generate_hypotheses(inputs, use_llm=False)
        lifecycle = initialize_lifecycle(hypotheses)
        for item in lifecycle:
            assert item["current_state"] not in ("supported", "tradeable_watch"), (
                f"state 不应为 supported/tradeable_watch，实际: {item['current_state']}"
            )


class TestSkepticAgent:
    """测试反方审查 Agent。"""

    def test_skeptic_points_out_weak_relation(self):
        """SubTask 16.3: SkepticAgent 能指出"只有共现没有实质关系"。"""
        hypotheses = [{
            "hypothesis_id": "hyp_1",
            "hypothesis_text": "GB200 升级可能带动 PTFE 需求。",
            "evidence_quotes": [],
            "evidence_document_ids": ["doc_1"],
            "source_nodes": ["GB200", "PTFE"],
            "crowding_score": 0.3,
        }]
        skeptic = SkepticAgent(use_llm=False)
        reviews = skeptic.review(hypotheses)
        # 应包含 weak_relation 类型的审查
        challenge_types = [r["challenge_type"] for r in reviews]
        assert "weak_relation" in challenge_types, (
            f"应包含 weak_relation 审查，实际: {challenge_types}"
        )

    def test_skeptic_six_risk_types(self):
        """测试 SkepticAgent 生成六类风险攻击。"""
        hypotheses = [{
            "hypothesis_id": "hyp_1",
            "hypothesis_text": "测试假设",
            "evidence_quotes": [],
            "evidence_document_ids": ["doc_1"],
            "source_nodes": ["GB200"],
            "crowding_score": 0.3,
        }]
        skeptic = SkepticAgent(use_llm=False)
        reviews = skeptic.review(hypotheses)
        challenge_types = {r["challenge_type"] for r in reviews}
        expected_types = {
            "single_source_risk", "weak_relation", "lack_official_evidence",
            "crowding", "tech_substitution", "missing_company_mapping",
        }
        assert expected_types.issubset(challenge_types), (
            f"应包含六类风险，实际: {challenge_types}"
        )


class TestEvidenceReviewAgent:
    """测试证据审查 Agent。"""

    def test_skepticism_score_generated(self):
        """SubTask 16.4: EvidenceReviewAgent 能生成 skepticism_score。"""
        hypotheses = [{
            "hypothesis_id": "hyp_1",
            "hypothesis_text": "测试假设",
            "evidence_quotes": ["证据1"],
            "evidence_document_ids": ["doc_1", "doc_2"],
            "source_nodes": ["GB200"],
            "crowding_score": 0.3,
        }]
        reviewer = EvidenceReviewAgent()
        scores = reviewer.review(hypotheses)
        assert len(scores) == 1
        score = scores[0]
        assert "skepticism_score" in score
        assert 0 <= score["skepticism_score"] <= 1
        # 验证公式各项存在
        assert "single_source_risk" in score
        assert "weak_relation_risk" in score
        assert "lack_official_evidence_risk" in score
        assert "crowding_risk" in score
        assert "missing_company_mapping_risk" in score


class TestPortfolioTranslator:
    """测试组合翻译 Agent。"""

    def test_no_buy_sell_output(self):
        """SubTask 16.5: PortfolioTranslator 不输出买入/卖出。"""
        hypotheses = [{
            "hypothesis_id": "hyp_1",
            "hypothesis_text": "测试假设",
            "evidence_quotes": ["证据1"],
            "evidence_document_ids": ["doc_1"],
            "source_nodes": ["GB200"],
            "evidence_score": 0.7,
            "novelty_score": 0.6,
            "crowding_score": 0.3,
        }]
        skepticism_scores = [{
            "hypothesis_id": "hyp_1",
            "skepticism_score": 0.3,
        }]
        translator = PortfolioTranslator(use_llm=False)
        translations = translator.translate(hypotheses, skepticism_scores)
        for t in translations:
            text = json.dumps(t, ensure_ascii=False)
            assert "买入" not in text, f"不应包含'买入': {text}"
            assert "卖出" not in text, f"不应包含'卖出': {text}"
            assert "强烈推荐" not in text, f"不应包含'强烈推荐': {text}"

    def test_watchlist_action_in_allowed_enum(self):
        """SubTask 16.6: watchlist_action 只能在允许枚举内。"""
        hypotheses = [
            {
                "hypothesis_id": f"hyp_{i}",
                "hypothesis_text": f"测试假设{i}",
                "evidence_quotes": [],
                "evidence_document_ids": [],
                "source_nodes": [],
                "evidence_score": 0.5,
                "novelty_score": 0.5,
                "crowding_score": 0.5,
            }
            for i in range(5)
        ]
        skepticism_scores = [
            {"hypothesis_id": f"hyp_{i}", "skepticism_score": 0.1 * i}
            for i in range(5)
        ]
        translator = PortfolioTranslator(use_llm=False)
        translations = translator.translate(hypotheses, skepticism_scores)
        allowed = {"ignore", "monitor", "research_required", "watchlist_candidate"}
        for t in translations:
            assert t["watchlist_action"] in allowed, (
                f"watchlist_action 应在允许枚举内，实际: {t['watchlist_action']}"
            )


class TestJsonGeneration:
    """测试 JSON 文件生成。"""

    def test_five_jsons_generated(self, tmp_path: Path):
        """SubTask 16.7: 五个输出 JSON 都能生成（在 tmp_path 下）。"""
        _setup_p4a_data(tmp_path)
        # 1. 假设生成
        hyp_summary = run_hypothesis_engine(
            input_dir=tmp_path, output_dir=tmp_path, use_llm=False
        )
        assert hyp_summary["success"], f"假设生成失败: {hyp_summary.get('error')}"
        assert (tmp_path / "research_hypotheses_latest.json").exists()
        assert (tmp_path / "hypothesis_lifecycle_latest.json").exists()

        # 2. 对抗式审查
        adv_summary = run_adversarial_research_agents(
            input_dir=tmp_path, output_dir=tmp_path, use_llm=False
        )
        assert adv_summary["success"], f"对抗式审查失败: {adv_summary.get('error')}"
        assert (tmp_path / "adversarial_reviews_latest.json").exists()
        assert (tmp_path / "skepticism_scores_latest.json").exists()
        assert (tmp_path / "portfolio_translation_latest.json").exists()

    def test_does_not_touch_real_data_dir(self, tmp_path: Path):
        """测试不触碰真实 data/reports/perilla_discovery/ 目录。"""
        _setup_p4a_data(tmp_path)
        summary = run_hypothesis_engine(
            input_dir=tmp_path, output_dir=tmp_path, use_llm=False
        )
        assert summary["success"]
        for path in summary["outputs"]:
            assert str(tmp_path) in path, f"输出应指向 tmp_path: {path}"


class TestLLMPaths:
    """测试 LLM 路径（mock）。"""

    def test_llm_success_path_generates_hypothesis(self, tmp_path: Path):
        """SubTask 16.8: mock ConceptLLMExplainer 成功时使用 LLM 假设。"""
        _setup_p4a_data(tmp_path)
        inputs = load_inputs(tmp_path)

        class MockExplainer:
            available = True
            model_name = "mock-p4a"
            cache_dir = tmp_path / "llm_cache"

            def generate_json(self, **kwargs):
                return {
                    "hypotheses": [
                        {
                            "hypothesis_text": "GB200 LLM 假设可能带动 PCB 需求。",
                            "hypothesis_type": "product_platform_demand",
                            "source_nodes": ["GB200", "PCB"],
                            "logic_chain": "GB200 放量 -> PCB 需求提升",
                            "evidence_quotes": ["GB200带动PCB需求"],
                            "novelty_score": 0.8,
                            "evidence_score": 0.7,
                            "crowding_score": 0.2,
                            "status": "pending",
                        }
                    ]
                }

        hyps = generate_hypotheses(
            inputs,
            use_llm=True,
            llm_explainer_factory=MockExplainer,
        )

        assert hyps
        assert hyps[0]["hypothesis_text"] == "GB200 LLM 假设可能带动 PCB 需求。"
        assert hyps[0]["llm_status"] == "llm"
        assert hyps[0]["novelty_score"] == 0.8

    def test_llm_compliance_block_falls_back_to_rules(self, tmp_path: Path):
        """SubTask 16.10: mock LLM 命中合规禁用词时回退规则。"""
        _setup_p4a_data(tmp_path)
        inputs = load_inputs(tmp_path)

        class MockExplainer:
            available = True
            model_name = "mock-p4a"
            cache_dir = tmp_path / "llm_cache_block"

            def generate_json(self, **kwargs):
                return {
                    "hypotheses": [
                        {
                            "hypothesis_text": "建议买入 GB200，强烈推荐。",
                            "hypothesis_type": "product_platform_demand",
                            "source_nodes": ["GB200", "PCB"],
                            "evidence_quotes": ["GB200带动PCB需求"],
                            "novelty_score": 0.9,
                            "evidence_score": 0.9,
                            "crowding_score": 0.1,
                            "status": "pending",
                        }
                    ]
                }

        hyps = generate_hypotheses(
            inputs,
            use_llm=True,
            llm_explainer_factory=MockExplainer,
        )

        assert hyps
        dumped = json.dumps(hyps, ensure_ascii=False)
        assert "建议买入" not in dumped
        assert "强烈推荐" not in dumped
        assert any(h.get("llm_status") == "rule_fallback" for h in hyps)

    def test_adversarial_agents_llm_success_path(self, tmp_path: Path):
        """SubTask 16.8: mock AquaLLM 成功时使用 LLM 审查和组合翻译。"""
        hypotheses = [{
            "hypothesis_id": "hyp_1",
            "hypothesis_text": "GB200 可能带动 PCB 需求",
            "hypothesis_type": "product_platform_demand",
            "source_nodes": ["GB200", "PCB"],
            "logic_chain": "GB200 放量 -> PCB 需求提升",
            "evidence_document_ids": ["doc_1"],
            "evidence_quotes": ["GB200带动PCB需求"],
            "related_missing_edges": [],
            "novelty_score": 0.5,
            "evidence_score": 0.4,
            "crowding_score": 0.2,
            "skepticism_score": 0.0,
            "status": "born",
            "created_at": "2024-01-01T00:00:00",
        }]

        class MockAquaLLM:
            model_name = "mock-aqua"

            def generate_json(self, user_prompt: str, system_prompt: str):
                if "反方审查" in system_prompt:
                    return {
                        "reviews": [
                            {
                                "hypothesis_id": "hyp_1",
                                "challenge_type": "single_source_risk",
                                "challenge_text": "LLM 指出该假设证据来源单一。",
                                "severity": "medium",
                                "suggested_validation_task": "补充公告和新闻交叉验证",
                            }
                        ]
                    }
                return {
                    "portfolio_translation": {
                        "watchlist_action": "research_required",
                        "trade_meaning": "LLM 翻译为需要继续研究的观察对象",
                        "trigger_conditions": "补充官方证据",
                        "risk_notes": "证据仍偏弱",
                        "next_research_tasks": "验证产业链关系",
                    }
                }

        cache = LLMCache(str(tmp_path / "llm_cache"))
        reviews = SkepticAgent(use_llm=True, llm=MockAquaLLM(), cache=cache).review(hypotheses)
        scores = EvidenceReviewAgent().review(hypotheses)
        translations = PortfolioTranslator(use_llm=True, llm=MockAquaLLM(), cache=cache).translate(
            hypotheses, scores
        )

        assert any(r.get("llm_status") == "llm" for r in reviews)
        assert any("LLM 指出" in r.get("challenge_text", "") for r in reviews)
        assert translations[0]["llm_status"] == "llm"
        assert translations[0]["watchlist_action"] == "research_required"

    def test_llm_not_available_pure_rules(self, tmp_path: Path):
        """SubTask 16.9: LLM 不可用时降级为纯规则模式。"""
        _setup_p4a_data(tmp_path)
        # use_llm=False 模拟 LLM 不可用
        summary = run_hypothesis_engine(
            input_dir=tmp_path, output_dir=tmp_path, use_llm=False
        )
        assert summary["success"], "LLM 不可用时应降级到规则模式"
        assert summary["counts"]["hypotheses"] > 0

    def test_llm_failure_fallback_to_rules(self, tmp_path: Path):
        """SubTask 16.9: LLM 失败时降级到规则模式。"""
        _setup_p4a_data(tmp_path)
        inputs = load_inputs(tmp_path)

        class FailingExplainer:
            available = True
            model_name = "mock-p4a"
            cache_dir = tmp_path / "llm_cache_fail"

            def generate_json(self, **kwargs):
                raise RuntimeError("mock LLM failure")

        hyps = generate_hypotheses(
            inputs,
            use_llm=True,
            llm_explainer_factory=FailingExplainer,
        )
        assert hyps, "LLM 失败时应降级到规则模式"
        assert any(h.get("llm_status") == "rule_fallback" for h in hyps)

    def test_compliance_block(self, tmp_path: Path):
        """SubTask 16.10: 合规拦截测试。"""
        _setup_p4a_data(tmp_path)
        # 运行假设生成
        summary = run_hypothesis_engine(
            input_dir=tmp_path, output_dir=tmp_path, use_llm=False
        )
        assert summary["success"]
        # 读取生成的假设，验证不含禁用词
        hyps = json.loads(
            (tmp_path / "research_hypotheses_latest.json").read_text(encoding="utf-8")
        )
        for hyp in hyps:
            text = json.dumps(hyp, ensure_ascii=False)
            assert "买入" not in text, f"假设不应包含'买入': {text}"
            assert "卖出" not in text, f"假设不应包含'卖出': {text}"
            assert "强烈推荐" not in text, f"假设不应包含'强烈推荐': {text}"
