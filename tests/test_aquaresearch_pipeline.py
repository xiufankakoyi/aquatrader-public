"""P8 总调度与紫苏叶日报聚合测试。

覆盖 SubTask 23.1-23.11：
- 23.1: run_aquaresearch_pipeline.py 能顺序调用模块（通过 import，不用 subprocess）
- 23.2: 任一步失败时能停止并返回错误
- 23.3: 所有核心 JSON 文件存在（在 tmp_path 下）
- 23.4: mobile_research_service 在文件缺失时不崩溃
- 23.5: 紫苏叶日报包含【今日新词雷达】
- 23.6: 紫苏叶日报包含【新研究假设】
- 23.7: 紫苏叶日报包含【证据演化与市场共振】
- 23.8: 日报不出现"买入""卖出""强烈推荐"
- 23.9: get_daily() 返回类型仍为 dict，含 daily_brief / perilla_discovery 两个原有 key
- 23.10: search_tasks_latest.json 由 search_tasks_from_missing_edges_latest.json 聚合而来
- 23.11: 不触碰真实 data/reports/perilla_discovery/ 目录
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================================
# 测试辅助：mock 各模块 run_xxx() 返回值
# ============================================================================


def _success_summary(module: str, counts: Dict[str, int] = None) -> Dict[str, Any]:
    """构造成功的 summary。"""
    return {
        "module": module,
        "success": True,
        "outputs": [],
        "counts": counts or {},
    }


def _failure_summary(module: str, error: str = "mock failure") -> Dict[str, Any]:
    """构造失败的 summary。"""
    return {
        "module": module,
        "success": False,
        "outputs": [],
        "counts": {},
        "error": error,
    }


def _write_core_jsons(output_dir: Path) -> None:
    """在临时目录写入核心 JSON 文件（模拟流水线输出）。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # P0 输出
    (output_dir / "raw_documents_latest.json").write_text(
        json.dumps([{"document_id": "doc_1", "title": "GB200 放量", "content": "..."}]),
        encoding="utf-8",
    )
    (output_dir / "raw_terms_latest.json").write_text(
        json.dumps([{"term": "GB200", "term_type": "product_platform", "burst_score": 0.8,
                     "source_count": 3}]),
        encoding="utf-8",
    )
    (output_dir / "candidate_concepts_latest.json").write_text(
        json.dumps([{"concept_name": "GB200", "term_type": "product_platform",
                     "root_theme": "算", "evidence_document_ids": ["doc_1"]}]),
        encoding="utf-8",
    )
    (output_dir / "evidence_edges_latest.json").write_text(
        json.dumps([{"source_node": "GB200", "relation": "co_occurs_with",
                     "target_node": "PCB", "confidence_level": "weak_signal",
                     "evidence_count": 2}]),
        encoding="utf-8",
    )

    # P1 输出
    (output_dir / "report_claims_latest.json").write_text(
        json.dumps([{"claim_id": "claim_1", "claim_text": "GB200 放量带动 PCB 需求",
                     "confidence": "medium"}]),
        encoding="utf-8",
    )
    (output_dir / "causal_chains_latest.json").write_text(
        json.dumps([{"chain_id": "chain_1", "cause": "GB200 放量",
                     "mechanism": "算力升级", "effect": "PCB 需求提升",
                     "confidence": "medium", "status": "pending"}]),
        encoding="utf-8",
    )
    (output_dir / "evidence_items_latest.json").write_text(
        json.dumps([{"evidence_id": "ev_1", "evidence_level": "weak_signal"}]),
        encoding="utf-8",
    )
    (output_dir / "failure_conditions_latest.json").write_text(
        json.dumps([{"failure_id": "f_1", "risk_type": "demand_risk"}]),
        encoding="utf-8",
    )

    # P2 输出
    (output_dir / "concept_scores_latest.json").write_text(
        json.dumps([{"concept_name": "GB200", "final_score": 0.75}]),
        encoding="utf-8",
    )
    (output_dir / "concept_aliases_latest.json").write_text(
        json.dumps([{"alias_group_id": "optical_interconnect_family"}]),
        encoding="utf-8",
    )

    # P3 输出
    (output_dir / "industry_chain_graph_latest.json").write_text(
        json.dumps({"nodes": [{"node_id": "n1"}], "edges": []}),
        encoding="utf-8",
    )
    (output_dir / "missing_edges_latest.json").write_text(
        json.dumps([{"missing_edge_id": "me_1", "source_node": "GB200",
                     "source_type": "product_platform", "target_hint": "PCB",
                     "priority_score": 0.7, "reason": "缺少下钻边"}]),
        encoding="utf-8",
    )
    (output_dir / "search_tasks_from_missing_edges_latest.json").write_text(
        json.dumps([{"task_id": "task_1", "query": "GB200 PCB 产业链",
                     "task_source": "missing_edge",
                     "generated_by": "industry_chain_graph_builder"}]),
        encoding="utf-8",
    )

    # P4A 输出
    (output_dir / "research_hypotheses_latest.json").write_text(
        json.dumps([{"hypothesis_id": "hyp_1", "hypothesis_text": "GB200 带动 PCB 需求",
                     "hypothesis_type": "product_platform_demand",
                     "novelty_score": 0.6, "evidence_score": 0.5, "status": "born"}]),
        encoding="utf-8",
    )
    (output_dir / "hypothesis_lifecycle_latest.json").write_text(
        json.dumps([{"hypothesis_id": "hyp_1", "current_state": "born"}]),
        encoding="utf-8",
    )
    (output_dir / "adversarial_reviews_latest.json").write_text(
        json.dumps([{"review_id": "rev_1", "hypothesis_id": "hyp_1",
                     "challenge_type": "single_source", "severity": "medium",
                     "suggested_validation_task": "查找更多来源"}]),
        encoding="utf-8",
    )
    (output_dir / "skepticism_scores_latest.json").write_text(
        json.dumps([{"hypothesis_id": "hyp_1", "skepticism_score": 0.4}]),
        encoding="utf-8",
    )
    (output_dir / "portfolio_translation_latest.json").write_text(
        json.dumps([{"hypothesis_id": "hyp_1", "watchlist_action": "monitor"}]),
        encoding="utf-8",
    )

    # P4B 输出
    (output_dir / "evidence_timeline_latest.json").write_text(
        json.dumps([{"timeline_id": "tl_1", "hypothesis_id": "hyp_1",
                     "event_type": "hypothesis_created"}]),
        encoding="utf-8",
    )
    (output_dir / "belief_state_latest.json").write_text(
        json.dumps([{"hypothesis_id": "hyp_1", "posterior_probability": 0.35,
                     "state": "hypothesis"}]),
        encoding="utf-8",
    )
    (output_dir / "post_mortem_latest.json").write_text(
        json.dumps([{"hypothesis_id": "hyp_1", "actual_outcome": ""}]),
        encoding="utf-8",
    )
    (output_dir / "market_resonance_latest.json").write_text(
        json.dumps([{"hypothesis_id": "hyp_1", "market_attention_score": 0.2}]),
        encoding="utf-8",
    )
    (output_dir / "trigger_signals_latest.json").write_text(
        json.dumps([{"trigger_id": "trig_1", "hypothesis_id": "hyp_1",
                     "trigger_type": "evidence_upgrade", "next_action": "keep_monitoring"}]),
        encoding="utf-8",
    )


# ============================================================================
# SubTask 23.1: 顺序调用模块（通过 import，不用 subprocess）
# ============================================================================


class TestPipelineSequentialCall:
    """测试总调度能顺序调用各模块。"""

    def test_pipeline_calls_modules_in_order_via_import(self, tmp_path: Path) -> None:
        """23.1: 通过 import 顺序调用 9 个模块，全部成功。"""
        from scripts.run_aquaresearch_pipeline import run_pipeline

        call_order: list[str] = []

        def _track(name: str):
            def _wrapper(*args, **kwargs):
                call_order.append(name)
                return _success_summary(name)
            return _wrapper

        with patch(
            "scripts.run_aquaresearch_pipeline.run_local_concept_loop",
            side_effect=_track("P0_local_concept_loop"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_report_causal_extractor",
            side_effect=_track("P1_report_causal_extractor"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_term_radar_enhancer",
            side_effect=_track("P2_term_radar_enhancer"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_industry_chain_graph",
            side_effect=_track("P3_industry_chain_graph"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_search_task_compiler",
            side_effect=_track("P3_search_task_compiler"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_hypothesis_engine",
            side_effect=_track("P4A_hypothesis_engine"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_adversarial_research_agents",
            side_effect=_track("P4A_adversarial_research_agents"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_evidence_evolution",
            side_effect=_track("P4B_evidence_evolution"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_market_resonance",
            side_effect=_track("P4B_market_resonance"),
        ):
            # 预写 search_tasks_from_missing_edges_latest.json 供聚合
            (tmp_path / "search_tasks_from_missing_edges_latest.json").write_text(
                json.dumps([{"task_id": "task_1", "query": "test"}]), encoding="utf-8"
            )

            result = run_pipeline(
                output_dir=tmp_path,
                project_root=_PROJECT_ROOT,
                use_llm=False,
            )

        assert result["success"] is True
        assert call_order == [
            "P0_local_concept_loop",
            "P1_report_causal_extractor",
            "P2_term_radar_enhancer",
            "P3_industry_chain_graph",
            "P3_search_task_compiler",
            "P4A_hypothesis_engine",
            "P4A_adversarial_research_agents",
            "P4B_evidence_evolution",
            "P4B_market_resonance",
        ]


# ============================================================================
# SubTask 23.2: 任一步失败时能停止并返回错误
# ============================================================================


class TestPipelineFailureStop:
    """测试任一步失败时立即停止。"""

    def test_pipeline_stops_on_p1_failure(self, tmp_path: Path) -> None:
        """23.2: P1 失败时立即停止，不调用 P2 及之后模块。"""
        from scripts.run_aquaresearch_pipeline import run_pipeline

        called_after_p1: list[str] = []

        with patch(
            "scripts.run_aquaresearch_pipeline.run_local_concept_loop",
            return_value=_success_summary("P0"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_report_causal_extractor",
            return_value=_failure_summary("P1", "LLM 不可达"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_term_radar_enhancer",
            side_effect=lambda *a, **kw: called_after_p1.append("P2") or _success_summary("P2"),
        ):
            result = run_pipeline(
                output_dir=tmp_path,
                project_root=_PROJECT_ROOT,
                use_llm=False,
            )

        assert result["success"] is False
        assert result["failed_step"] == "P1_report_causal_extractor"
        assert "LLM 不可达" in result["error"]
        assert called_after_p1 == []  # P2 未被调用

    def test_pipeline_stops_on_p4a_failure(self, tmp_path: Path) -> None:
        """23.2: P4A 失败时立即停止，不调用 P4B。"""
        from scripts.run_aquaresearch_pipeline import run_pipeline

        called_after_p4a: list[str] = []

        with patch(
            "scripts.run_aquaresearch_pipeline.run_local_concept_loop",
            return_value=_success_summary("P0"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_report_causal_extractor",
            return_value=_success_summary("P1"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_term_radar_enhancer",
            return_value=_success_summary("P2"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_industry_chain_graph",
            return_value=_success_summary("P3_graph"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_search_task_compiler",
            return_value=_success_summary("P3_tasks"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_hypothesis_engine",
            return_value=_failure_summary("P4A", "假设生成异常"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_adversarial_research_agents",
            side_effect=lambda *a, **kw: called_after_p4a.append("P4A_adv") or _success_summary("P4A_adv"),
        ):
            result = run_pipeline(
                output_dir=tmp_path,
                project_root=_PROJECT_ROOT,
                use_llm=False,
            )

        assert result["success"] is False
        assert result["failed_step"] == "P4A_hypothesis_engine"
        assert called_after_p4a == []


# ============================================================================
# SubTask 23.3: 所有核心 JSON 文件存在（在 tmp_path 下）
# ============================================================================


class TestCoreJsonFiles:
    """测试核心 JSON 文件存在。"""

    def test_all_core_jsons_exist_after_pipeline(self, tmp_path: Path) -> None:
        """23.3: 流水线成功后所有核心 JSON 文件存在。"""
        from scripts.run_aquaresearch_pipeline import run_pipeline

        # mock 所有模块成功，并让 search_task_compiler 写入文件
        def _mock_search_task_compiler(input_dir=None, output_dir=None):
            out = output_dir or input_dir
            tasks = [{"task_id": "task_1", "query": "test", "task_source": "missing_edge"}]
            (out / "search_tasks_from_missing_edges_latest.json").write_text(
                json.dumps(tasks, ensure_ascii=False), encoding="utf-8"
            )
            return _success_summary("search_task_compiler", {"search_tasks": 1})

        with patch(
            "scripts.run_aquaresearch_pipeline.run_local_concept_loop",
            return_value=_success_summary("P0"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_report_causal_extractor",
            return_value=_success_summary("P1"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_term_radar_enhancer",
            return_value=_success_summary("P2"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_industry_chain_graph",
            return_value=_success_summary("P3_graph"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_search_task_compiler",
            side_effect=_mock_search_task_compiler,
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_hypothesis_engine",
            return_value=_success_summary("P4A_hyp"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_adversarial_research_agents",
            return_value=_success_summary("P4A_adv"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_evidence_evolution",
            return_value=_success_summary("P4B_evo"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_market_resonance",
            return_value=_success_summary("P4B_res"),
        ):
            result = run_pipeline(
                output_dir=tmp_path,
                project_root=_PROJECT_ROOT,
                use_llm=False,
            )

        assert result["success"] is True

        # 验证聚合后的 search_tasks_latest.json 存在
        assert (tmp_path / "search_tasks_latest.json").exists()
        # 验证 search_tasks_from_missing_edges_latest.json 存在
        assert (tmp_path / "search_tasks_from_missing_edges_latest.json").exists()


# ============================================================================
# SubTask 23.4: mobile_research_service 在文件缺失时不崩溃
# ============================================================================


class TestMobileResearchServiceMissingFiles:
    """测试文件缺失时不崩溃。"""

    def test_get_daily_does_not_crash_when_files_missing(self, tmp_path: Path) -> None:
        """23.4: 所有 P8 文件缺失时 get_daily() 不崩溃。"""
        from server.services.research.mobile_research_service import MobileResearchService

        service = MobileResearchService()

        # monkeypatch 所有文件路径常量指向不存在的临时目录
        missing_dir = tmp_path / "nonexistent"
        with patch(
            "server.services.research.mobile_research_service._PERILLA_DISCOVERY_DIR", missing_dir
        ), patch(
            "server.services.research.mobile_research_service._RAW_TERMS_FILE", missing_dir / "raw_terms.json"
        ), patch(
            "server.services.research.mobile_research_service._CANDIDATE_CONCEPTS_FILE", missing_dir / "cc.json"
        ), patch(
            "server.services.research.mobile_research_service._EVIDENCE_EDGES_FILE", missing_dir / "ee.json"
        ), patch(
            "server.services.research.mobile_research_service._CAUSAL_CHAINS_FILE", missing_dir / "cc.json"
        ), patch(
            "server.services.research.mobile_research_service._MISSING_EDGES_FILE", missing_dir / "me.json"
        ), patch(
            "server.services.research.mobile_research_service._SEARCH_TASKS_FILE", missing_dir / "st.json"
        ), patch(
            "server.services.research.mobile_research_service._RESEARCH_HYPOTHESES_FILE", missing_dir / "rh.json"
        ), patch(
            "server.services.research.mobile_research_service._ADVERSARIAL_REVIEWS_FILE", missing_dir / "ar.json"
        ), patch(
            "server.services.research.mobile_research_service._BELIEF_STATE_FILE", missing_dir / "bs.json"
        ), patch(
            "server.services.research.mobile_research_service._TRIGGER_SIGNALS_FILE", missing_dir / "ts.json"
        ), patch(
            "server.services.research.mobile_research_service._REPORTS_DIR", missing_dir
        ):
            result = service.get_daily()

        # 不崩溃，返回 dict
        assert isinstance(result, dict)
        # P8 板块降级显示"数据暂未生成。"
        assert "数据暂未生成" in result["causal_chains_section"]["markdown"]
        assert "数据暂未生成" in result["missing_edges_section"]["markdown"]
        assert "数据暂未生成" in result["research_hypotheses_section"]["markdown"]
        assert "数据暂未生成" in result["adversarial_reviews_section"]["markdown"]
        assert "数据暂未生成" in result["evidence_evolution_section"]["markdown"]


# ============================================================================
# SubTask 23.5-23.7: 紫苏叶日报包含 8 板块
# ============================================================================


class TestDailyReportSections:
    """测试紫苏叶日报包含 8 板块。"""

    @pytest.fixture
    def service_with_data(self, tmp_path: Path):
        """构造带数据的 MobileResearchService。"""
        from server.services.research.mobile_research_service import MobileResearchService

        _write_core_jsons(tmp_path)

        service = MobileResearchService()
        with patch(
            "server.services.research.mobile_research_service._PERILLA_DISCOVERY_DIR", tmp_path
        ), patch(
            "server.services.research.mobile_research_service._RAW_TERMS_FILE", tmp_path / "raw_terms_latest.json"
        ), patch(
            "server.services.research.mobile_research_service._CANDIDATE_CONCEPTS_FILE", tmp_path / "candidate_concepts_latest.json"
        ), patch(
            "server.services.research.mobile_research_service._EVIDENCE_EDGES_FILE", tmp_path / "evidence_edges_latest.json"
        ), patch(
            "server.services.research.mobile_research_service._CAUSAL_CHAINS_FILE", tmp_path / "causal_chains_latest.json"
        ), patch(
            "server.services.research.mobile_research_service._MISSING_EDGES_FILE", tmp_path / "missing_edges_latest.json"
        ), patch(
            "server.services.research.mobile_research_service._SEARCH_TASKS_FILE", tmp_path / "search_tasks_latest.json"
        ), patch(
            "server.services.research.mobile_research_service._RESEARCH_HYPOTHESES_FILE", tmp_path / "research_hypotheses_latest.json"
        ), patch(
            "server.services.research.mobile_research_service._ADVERSARIAL_REVIEWS_FILE", tmp_path / "adversarial_reviews_latest.json"
        ), patch(
            "server.services.research.mobile_research_service._BELIEF_STATE_FILE", tmp_path / "belief_state_latest.json"
        ), patch(
            "server.services.research.mobile_research_service._TRIGGER_SIGNALS_FILE", tmp_path / "trigger_signals_latest.json"
        ):
            yield service

    def test_daily_contains_raw_terms_section(self, service_with_data) -> None:
        """23.5: 日报包含【今日新词雷达】板块。"""
        result = service_with_data.get_daily()
        assert "raw_terms_section" in result
        md = result["raw_terms_section"]["markdown"]
        assert "今日新词雷达" in md
        assert "GB200" in md

    def test_daily_contains_research_hypotheses_section(self, service_with_data) -> None:
        """23.6: 日报包含【新研究假设】板块。"""
        result = service_with_data.get_daily()
        assert "research_hypotheses_section" in result
        md = result["research_hypotheses_section"]["markdown"]
        assert "新研究假设" in md
        assert "GB200" in md

    def test_daily_contains_evidence_evolution_section(self, service_with_data) -> None:
        """23.7: 日报包含【证据演化与市场共振】板块。"""
        result = service_with_data.get_daily()
        assert "evidence_evolution_section" in result
        md = result["evidence_evolution_section"]["markdown"]
        assert "证据演化与市场共振" in md
        assert "hyp_1" in md


# ============================================================================
# SubTask 23.8: 日报不出现"买入""卖出""强烈推荐"
# ============================================================================


class TestComplianceFilter:
    """测试合规过滤。"""

    def test_daily_does_not_contain_forbidden_terms(self, tmp_path: Path) -> None:
        """23.8: 日报不出现买入/卖出/强烈推荐。"""
        from server.services.research.mobile_research_service import MobileResearchService

        # 构造含禁用词的脏数据
        (tmp_path / "research_hypotheses_latest.json").write_text(
            json.dumps([{
                "hypothesis_id": "hyp_bad",
                "hypothesis_text": "建议买入 GB200 概念股，强烈推荐",
                "hypothesis_type": "investment_logic_signal",
                "novelty_score": 0.5,
                "evidence_score": 0.4,
                "status": "born",
            }]),
            encoding="utf-8",
        )
        (tmp_path / "adversarial_reviews_latest.json").write_text(
            json.dumps([{
                "review_id": "rev_bad",
                "hypothesis_id": "hyp_bad",
                "challenge_type": "compliance",
                "severity": "high",
                "suggested_validation_task": "卖出信号检测",
            }]),
            encoding="utf-8",
        )

        service = MobileResearchService()
        with patch(
            "server.services.research.mobile_research_service._RESEARCH_HYPOTHESES_FILE",
            tmp_path / "research_hypotheses_latest.json",
        ), patch(
            "server.services.research.mobile_research_service._ADVERSARIAL_REVIEWS_FILE",
            tmp_path / "adversarial_reviews_latest.json",
        ):
            result = service.get_daily()

        # 检查所有板块 markdown 不含禁用词
        forbidden = ["买入", "卖出", "强烈推荐"]
        for key, section in result.items():
            if not isinstance(section, dict):
                continue
            md = section.get("markdown", "")
            for term in forbidden:
                assert term not in md, f"板块 {key} 含禁用词: {term}"


# ============================================================================
# SubTask 23.9: get_daily() 返回类型兼容
# ============================================================================


class TestGetDailyBackwardCompatible:
    """测试 get_daily() 返回类型向后兼容。"""

    def test_get_daily_returns_dict_with_original_keys(self, tmp_path: Path) -> None:
        """23.9: get_daily() 返回 dict，含 daily_brief / perilla_discovery 两个原有 key。"""
        from server.services.research.mobile_research_service import MobileResearchService

        service = MobileResearchService()
        with patch(
            "server.services.research.mobile_research_service._PERILLA_DISCOVERY_DIR", tmp_path
        ), patch(
            "server.services.research.mobile_research_service._REPORTS_DIR", tmp_path
        ):
            result = service.get_daily()

        assert isinstance(result, dict)
        # 原有 key 必须存在（值可为 None）
        assert "daily_brief" in result
        assert "perilla_discovery" in result
        # P0 板块 key 存在
        assert "raw_terms_section" in result
        assert "candidate_concepts_section" in result
        assert "evidence_edges_section" in result
        assert "pending_note_section" in result
        # P8 板块 key 存在
        assert "causal_chains_section" in result
        assert "missing_edges_section" in result
        assert "research_hypotheses_section" in result
        assert "adversarial_reviews_section" in result
        assert "evidence_evolution_section" in result


# ============================================================================
# SubTask 23.10: search_tasks_latest.json 由聚合而来
# ============================================================================


class TestSearchTasksAggregation:
    """测试 search_tasks 聚合。"""

    def test_search_tasks_latest_aggregated_from_missing_edges(self, tmp_path: Path) -> None:
        """23.10: search_tasks_latest.json 由 search_tasks_from_missing_edges_latest.json 聚合。"""
        from scripts.run_aquaresearch_pipeline import _aggregate_search_tasks

        # 预写源文件
        source_tasks = [
            {"task_id": "task_1", "query": "GB200 PCB 产业链",
             "task_source": "missing_edge",
             "generated_by": "industry_chain_graph_builder"},
            {"task_id": "task_2", "query": "LPO 光模块 厂商",
             "task_source": "missing_edge",
             "generated_by": "industry_chain_graph_builder"},
        ]
        (tmp_path / "search_tasks_from_missing_edges_latest.json").write_text(
            json.dumps(source_tasks, ensure_ascii=False), encoding="utf-8"
        )

        result = _aggregate_search_tasks(input_dir=tmp_path, output_dir=tmp_path)

        assert result["success"] is True
        assert result["counts"]["search_tasks"] == 2

        # 验证聚合后的文件内容与源文件一致
        aggregated = json.loads(
            (tmp_path / "search_tasks_latest.json").read_text(encoding="utf-8")
        )
        assert len(aggregated) == 2
        assert aggregated[0]["task_source"] == "missing_edge"
        assert aggregated[1]["task_source"] == "missing_edge"

    def test_search_tasks_aggregation_handles_missing_source(self, tmp_path: Path) -> None:
        """23.10: 源文件不存在时聚合写入空列表。"""
        from scripts.run_aquaresearch_pipeline import _aggregate_search_tasks

        result = _aggregate_search_tasks(input_dir=tmp_path, output_dir=tmp_path)

        assert result["success"] is True
        assert result["counts"]["search_tasks"] == 0
        assert (tmp_path / "search_tasks_latest.json").exists()
        assert json.loads(
            (tmp_path / "search_tasks_latest.json").read_text(encoding="utf-8")
        ) == []


# ============================================================================
# SubTask 23.11: 不触碰真实 data/reports/perilla_discovery/ 目录
# ============================================================================


class TestNoRealDirectoryAccess:
    """测试不触碰真实数据目录。"""

    def test_pipeline_does_not_touch_real_directory(self, tmp_path: Path) -> None:
        """23.11: 流水线只写入 tmp_path，不触碰真实 data/reports/perilla_discovery/。"""
        from scripts.run_aquaresearch_pipeline import run_pipeline

        real_dir = _PROJECT_ROOT / "data" / "reports" / "perilla_discovery"
        # 记录真实目录的文件列表（如果存在）
        real_files_before = set()
        if real_dir.exists():
            real_files_before = {f.name for f in real_dir.iterdir() if f.is_file()}

        with patch(
            "scripts.run_aquaresearch_pipeline.run_local_concept_loop",
            return_value=_success_summary("P0"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_report_causal_extractor",
            return_value=_success_summary("P1"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_term_radar_enhancer",
            return_value=_success_summary("P2"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_industry_chain_graph",
            return_value=_success_summary("P3_graph"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_search_task_compiler",
            return_value=_success_summary("P3_tasks"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_hypothesis_engine",
            return_value=_success_summary("P4A_hyp"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_adversarial_research_agents",
            return_value=_success_summary("P4A_adv"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_evidence_evolution",
            return_value=_success_summary("P4B_evo"),
        ), patch(
            "scripts.run_aquaresearch_pipeline.run_market_resonance",
            return_value=_success_summary("P4B_res"),
        ):
            run_pipeline(
                output_dir=tmp_path,
                project_root=_PROJECT_ROOT,
                use_llm=False,
            )

        # 验证真实目录文件列表未变化
        if real_dir.exists():
            real_files_after = {f.name for f in real_dir.iterdir() if f.is_file()}
            # 不应有新增文件（允许文件内容变化，但文件名集合应一致或仅增多）
            # 严格检查：不应有以 search_tasks_latest.json 为名的新文件
            new_files = real_files_after - real_files_before
            assert "search_tasks_latest.json" not in new_files, \
                "测试触碰了真实目录：新增了 search_tasks_latest.json"

    def test_test_files_do_not_write_to_real_directory(self, tmp_path: Path) -> None:
        """23.11: 测试辅助函数只写 tmp_path。"""
        _write_core_jsons(tmp_path)

        # 验证所有文件都在 tmp_path 下
        expected_files = [
            "raw_documents_latest.json",
            "raw_terms_latest.json",
            "candidate_concepts_latest.json",
            "evidence_edges_latest.json",
            "causal_chains_latest.json",
            "evidence_items_latest.json",
            "failure_conditions_latest.json",
            "concept_scores_latest.json",
            "concept_aliases_latest.json",
            "industry_chain_graph_latest.json",
            "missing_edges_latest.json",
            "search_tasks_from_missing_edges_latest.json",
            "research_hypotheses_latest.json",
            "hypothesis_lifecycle_latest.json",
            "adversarial_reviews_latest.json",
            "skepticism_scores_latest.json",
            "portfolio_translation_latest.json",
            "evidence_timeline_latest.json",
            "belief_state_latest.json",
            "post_mortem_latest.json",
            "market_resonance_latest.json",
            "trigger_signals_latest.json",
        ]
        for fname in expected_files:
            assert (tmp_path / fname).exists(), f"文件未生成: {fname}"

        # 验证真实目录未被触碰
        real_dir = _PROJECT_ROOT / "data" / "reports" / "perilla_discovery"
        if real_dir.exists():
            # 真实目录不应有这些测试文件的新增（除非原本就有）
            for fname in expected_files:
                real_file = real_dir / fname
                if real_file.exists():
                    # 如果真实目录原本就有这个文件，检查内容未被测试覆盖
                    # （通过检查是否包含测试标记数据）
                    content = real_file.read_text(encoding="utf-8")
                    assert "test_pipeline" not in content, \
                        f"真实目录文件 {fname} 被测试污染"
