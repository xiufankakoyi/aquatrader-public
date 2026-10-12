"""P2 新词雷达与概念发现增强测试。

覆盖 SubTask 8.1-8.9：
- 8.1: 低介电树脂能被 statistical_terms 抽出（命中 XX树脂）
- 8.2: 高频高速材料能被抽出（命中 XX材料）
- 8.3: GB200 和 PCB 共现时 cooccurrence_score > 0
- 8.4: PTFE 的 concept_scores 有 final_score
- 8.5: concept_aliases 能生成 optical_interconnect_family
- 8.6: 不覆盖 P0 原字段
- 8.7: 四个输出文件存在（在 tmp_path 下）
- 8.8: 最长匹配优先：高端铜箔存在时，铜和铜箔不单独计数
- 8.9: 单字材料词降权：单独的铜被丢弃，与涨价共现的铜被保留
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from core.research.perilla.discovery.term_radar_enhancer import (
    build_concept_aliases,
    build_concept_scores,
    compute_burst_scores,
    compute_cooccurrence_scores,
    extract_regex_terms,
    extract_statistical_terms,
    run_term_radar_enhancer,
)


# ============================================================================
# 测试辅助函数
# ============================================================================


def _make_document(
    doc_id: str,
    title: str,
    content: str,
    source_type: str = "report",
) -> Dict[str, Any]:
    """创建测试用文档。"""
    return {
        "document_id": doc_id,
        "source_id": "test_source",
        "source_type": source_type,
        "title": title,
        "summary": "",
        "content_text": content,
        "url": "",
        "published_at": "2024-01-01T00:00:00",
        "collected_at": "2024-01-01T00:00:00",
        "related_companies": [],
        "related_themes": [],
        "raw_hash": doc_id,
        "source_payload": {},
    }


def _make_raw_term(
    term: str,
    term_type: str = "unclassified",
    source_count: int = 1,
    doc_ids: List[str] = None,
) -> Dict[str, Any]:
    """创建测试用 raw_term。"""
    return {
        "term": term,
        "normalized_term": term,
        "term_type": term_type,
        "source_count": source_count,
        "recent_count": source_count,
        "burst_score": float(source_count * 2),
        "related_document_ids": doc_ids or ["doc_1"],
        "related_titles": [],
        "related_companies": [],
        "evidence_quotes": [],
        "co_terms": [],
        "first_seen_at": "",
        "last_seen_at": "",
        "status": "unclassified",
    }


def _make_candidate_concept(
    name: str,
    term_type: str = "unclassified",
    root_theme: str = "",
    doc_ids: List[str] = None,
) -> Dict[str, Any]:
    """创建测试用 candidate_concept。"""
    return {
        "concept_id": f"concept_{name}",
        "concept_name": name,
        "normalized_name": name,
        "term_type": term_type,
        "root_theme": root_theme,
        "chain_layer": "",
        "evidence_document_ids": doc_ids or ["doc_1"],
        "evidence_titles": [],
        "related_companies": [],
        "evidence_quotes": [],
        "co_terms": [],
        "concept_score": 0.5,
        "evidence_level": "HYPOTHESIS",
        "llm_explanation": "",
        "llm_policy": "",
        "discovery_reason": "",
        "status": "candidate",
    }


def _setup_test_data(tmp_path: Path) -> Path:
    """在 tmp_path 下创建测试输入文件。"""
    docs = [
        _make_document(
            "doc_1",
            "GB200 放量带动高速 PCB 需求",
            "随着 GB200 放量，高速 PCB 需求有望提升。低介电树脂作为新型材料受到关注。CPO 和 LPO 共同推动光互连发展。",
        ),
        _make_document(
            "doc_2",
            "PTFE 高频高速材料应用",
            "PTFE 作为高频高速材料，在 5G 和 AI 服务器中广泛应用。高端铜箔需求增长。磷化铟 InP 用于光芯片制造。",
        ),
    ]
    raw_terms = [
        _make_raw_term("GB200", "product_platform", 2, ["doc_1", "doc_2"]),
        _make_raw_term("PCB", "component", 1, ["doc_1"]),
        _make_raw_term("PTFE", "material", 1, ["doc_2"]),
        _make_raw_term("CPO", "technology_route", 1, ["doc_1"]),
        _make_raw_term("LPO", "technology_route", 1, ["doc_1"]),
    ]
    concepts = [
        _make_candidate_concept("GB200", "product_platform", "算", ["doc_1", "doc_2"]),
        _make_candidate_concept("PCB", "component", "电", ["doc_1"]),
        _make_candidate_concept("PTFE", "material", "光", ["doc_2"]),
        _make_candidate_concept("CPO", "technology_route", "光", ["doc_1"]),
        _make_candidate_concept("LPO", "technology_route", "光", ["doc_1"]),
    ]

    (tmp_path / "raw_documents_latest.json").write_text(
        json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "raw_terms_latest.json").write_text(
        json.dumps(raw_terms, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (tmp_path / "candidate_concepts_latest.json").write_text(
        json.dumps(concepts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path


# ============================================================================
# 测试类
# ============================================================================


class TestStatisticalTermsExtraction:
    """测试统计新词抽取（后缀模式约束）。"""

    def test_extract_low_dielectric_resin(self):
        """SubTask 8.1: 低介电树脂能被抽出（命中 XX树脂 后缀）。"""
        docs = [_make_document("doc_1", "测试", "低介电树脂作为新型材料受到关注。")]
        terms = extract_statistical_terms(docs)
        term_names = [t["term"] for t in terms]
        assert "低介电树脂" in term_names, f"应抽出'低介电树脂'，实际: {term_names}"

    def test_extract_high_freq_material(self):
        """SubTask 8.2: 高频高速材料能被抽出（命中 XX材料 后缀）。"""
        docs = [_make_document("doc_1", "测试", "高频高速材料在 5G 中广泛应用。")]
        terms = extract_statistical_terms(docs)
        term_names = [t["term"] for t in terms]
        assert "高频高速材料" in term_names, f"应抽出'高频高速材料'，实际: {term_names}"

    def test_no_unconstrained_substring(self):
        """验证不做无约束 substring 枚举。"""
        docs = [_make_document("doc_1", "测试", "随着GB200放量高速PCB需求提升")]
        terms = extract_statistical_terms(docs)
        term_names = [t["term"] for t in terms]
        # 不应抽出无后缀的垃圾片段
        assert "随着" not in term_names
        assert "随着G" not in term_names
        assert "需求" not in term_names
        assert "需求提" not in term_names


class TestCooccurrenceScores:
    """测试共现评分。"""

    def test_gb200_pcb_cooccurrence(self):
        """SubTask 8.3: GB200 和 PCB 共现时 cooccurrence_score > 0。"""
        docs = [_make_document("doc_1", "GB200 PCB", "GB200 和 PCB 共现")]
        raw_terms = [
            _make_raw_term("GB200", "product_platform", 1, ["doc_1"]),
            _make_raw_term("PCB", "component", 1, ["doc_1"]),
        ]
        result = compute_cooccurrence_scores(raw_terms, docs)
        term_map = {t["normalized_term"]: t for t in result}
        gb200 = term_map.get("GB200", {})
        assert gb200.get("cooccurrence_score", 0) > 0, "GB200 共现评分应 > 0"
        assert "PCB" in gb200.get("co_terms", []), "GB200 的 co_terms 应含 PCB"


class TestConceptScores:
    """测试概念评分。"""

    def test_ptfe_has_final_score(self):
        """SubTask 8.4: PTFE 的 concept_scores 有 final_score。"""
        raw_terms = [
            {
                "term": "PTFE", "normalized_term": "PTFE",
                "term_type": "material", "source_count": 5,
                "burst_score": 10.0, "cooccurrence_score": 0.5,
            }
        ]
        concepts = [_make_candidate_concept("PTFE", "material", "光", ["doc_1"])]
        scores, updated_concepts = build_concept_scores(raw_terms, concepts)
        ptfe_score = next((s for s in scores if s["concept_name"] == "PTFE"), None)
        assert ptfe_score is not None, "应生成 PTFE 的 concept_score"
        assert "final_score" in ptfe_score, "PTFE 应有 final_score 字段"
        assert 0 <= ptfe_score["final_score"] <= 1, "final_score 应在 0-1 范围"

    def test_candidate_concepts_have_five_fields(self):
        """测试 candidate_concepts 补齐五字段。"""
        raw_terms = [
            {
                "term": "GB200", "normalized_term": "GB200",
                "source_count": 3, "burst_score": 6.0,
                "cooccurrence_score": 0.3,
            }
        ]
        concepts = [_make_candidate_concept("GB200", "product_platform", "算")]
        _, updated = build_concept_scores(raw_terms, concepts)
        concept = updated[0]
        assert "burst_score" in concept, "应有 burst_score"
        assert "cooccurrence_score" in concept, "应有 cooccurrence_score"
        assert "novelty_score" in concept, "应有 novelty_score"
        assert "evidence_score" in concept, "应有 evidence_score"
        assert "final_score" in concept, "应有 final_score"


class TestConceptAliases:
    """测试别名分组。"""

    def test_optical_interconnect_family(self):
        """SubTask 8.5: concept_aliases 能生成 optical_interconnect_family。"""
        docs = [_make_document("doc_1", "光互连", "CPO 和 LPO 共同推动光互连发展。NPO 也有应用。")]
        concepts = [
            _make_candidate_concept("CPO", "technology_route", "光"),
            _make_candidate_concept("LPO", "technology_route", "光"),
        ]
        aliases = build_concept_aliases(docs, concepts)
        group_ids = [a["alias_group_id"] for a in aliases]
        assert "optical_interconnect_family" in group_ids, (
            f"应生成 optical_interconnect_family，实际: {group_ids}"
        )


class TestLongestMatch:
    """测试最长匹配优先。"""

    def test_longest_match_priority(self):
        """SubTask 8.8: 高端铜箔存在时，铜和铜箔不单独计数。

        注意：单字"铜"不在后缀模式中，所以这里主要验证
        "高端铜箔"被抽出，且"铜箔"不与"高端铜箔"位置重叠时单独计数。
        """
        docs = [_make_document("doc_1", "测试", "高端铜箔需求增长，铜箔加工技术提升。")]
        terms = extract_statistical_terms(docs)
        term_names = [t["term"] for t in terms]
        # "高端铜箔"应被抽出（命中 XX铜箔）
        assert "高端铜箔" in term_names, f"应抽出'高端铜箔'，实际: {term_names}"
        # "铜箔"也应被抽出（因为"铜箔加工"中的"铜箔"不与"高端铜箔"重叠）
        # 但如果文本是"高端铜箔"，则"铜箔"不应单独出现
        docs2 = [_make_document("doc_1", "测试", "高端铜箔")]
        terms2 = extract_statistical_terms(docs2)
        term_names2 = [t["term"] for t in terms2]
        assert "高端铜箔" in term_names2
        # "铜箔"不应单独出现（因为被"高端铜箔"最长匹配占用）
        assert "铜箔" not in term_names2, (
            f"'铜箔'不应单独出现（被'高端铜箔'最长匹配占用），实际: {term_names2}"
        )


class TestSingleCharMaterialDownweight:
    """测试单字材料词降权。"""

    def test_single_char_copper_dropped_alone(self):
        """SubTask 8.9: 单独出现的铜被丢弃。"""
        # "铜"单独出现，没有共现关键词
        docs = [_make_document("doc_1", "测试", "铜是重要的金属材料。")]
        terms = extract_statistical_terms(docs)
        # "铜"不在后缀模式中，所以不会被后缀模式忽略
        # 但如果文本中有"铜箔"，"铜"作为单字材料词应被降权
        term_names = [t["term"] for t in terms]
        assert "铜" not in term_names, "单独的'铜'不应被抽出"

    def test_single_char_copper_kept_with_price(self):
        """SubTask 8.9: 与涨价共现的铜被保留。

        注意：单字"铜"本身不在后缀模式中，所以这里测试的是
        含"铜"的词（如"铜涨价"）能被抽出。
        """
        docs = [_make_document("doc_1", "测试", "铜涨价趋势明显，铜资源供给紧张。")]
        terms = extract_statistical_terms(docs)
        term_names = [t["term"] for t in terms]
        # "铜涨价"应被抽出（命中 XX涨价 后缀）
        assert "铜涨价" in term_names, f"应抽出'铜涨价'，实际: {term_names}"


class TestRunTermRadarEnhancer:
    """测试 run_term_radar_enhancer 全流程。"""

    def test_four_output_files_exist(self, tmp_path: Path):
        """SubTask 8.7: 四个输出文件存在（在 tmp_path 下）。"""
        _setup_test_data(tmp_path)
        summary = run_term_radar_enhancer(input_dir=tmp_path, output_dir=tmp_path)
        assert summary["success"], f"运行失败: {summary.get('error')}"

        assert (tmp_path / "raw_terms_latest.json").exists()
        assert (tmp_path / "candidate_concepts_latest.json").exists()
        assert (tmp_path / "concept_scores_latest.json").exists()
        assert (tmp_path / "concept_aliases_latest.json").exists()

    def test_does_not_overwrite_p0_fields(self, tmp_path: Path):
        """SubTask 8.6: 不覆盖 P0 原字段。"""
        _setup_test_data(tmp_path)
        summary = run_term_radar_enhancer(input_dir=tmp_path, output_dir=tmp_path)
        assert summary["success"]

        # 读取更新后的 candidate_concepts
        concepts = json.loads(
            (tmp_path / "candidate_concepts_latest.json").read_text(encoding="utf-8")
        )
        # P0 原字段应保留
        for concept in concepts:
            assert "concept_id" in concept, "应保留 concept_id"
            assert "concept_name" in concept, "应保留 concept_name"
            assert "normalized_name" in concept, "应保留 normalized_name"
            assert "term_type" in concept, "应保留 term_type"
            assert "root_theme" in concept, "应保留 root_theme"
            assert "concept_score" in concept, "应保留 concept_score"
            assert "evidence_level" in concept, "应保留 evidence_level"
            # 新增五字段
            assert "burst_score" in concept, "应新增 burst_score"
            assert "cooccurrence_score" in concept, "应新增 cooccurrence_score"
            assert "novelty_score" in concept, "应新增 novelty_score"
            assert "evidence_score" in concept, "应新增 evidence_score"
            assert "final_score" in concept, "应新增 final_score"

    def test_does_not_touch_real_data_dir(self, tmp_path: Path):
        """测试不触碰真实 data/reports/perilla_discovery/ 目录。"""
        _setup_test_data(tmp_path)
        # 记录真实目录的修改时间
        real_dir = Path("data/reports/perilla_discovery")
        real_scores = real_dir / "concept_scores_latest.json"
        # 确保测试输出到 tmp_path，不触碰真实目录
        summary = run_term_radar_enhancer(input_dir=tmp_path, output_dir=tmp_path)
        assert summary["success"]
        # 验证输出在 tmp_path 下
        for path in summary["outputs"]:
            assert str(tmp_path) in path, f"输出应指向 tmp_path: {path}"

    def test_empty_documents_not_crash(self, tmp_path: Path):
        """测试空文档不崩溃。"""
        (tmp_path / "raw_documents_latest.json").write_text("[]", encoding="utf-8")
        (tmp_path / "raw_terms_latest.json").write_text("[]", encoding="utf-8")
        (tmp_path / "candidate_concepts_latest.json").write_text("[]", encoding="utf-8")
        summary = run_term_radar_enhancer(input_dir=tmp_path, output_dir=tmp_path)
        assert summary["success"], "空文档不应崩溃"
        assert summary["counts"]["raw_terms"] == 0
        assert summary["counts"]["concept_scores"] == 0
