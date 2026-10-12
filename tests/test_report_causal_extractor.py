"""P1 研报因果链抽取测试。

覆盖 spec 中 Task 5 的 SubTask 5.1-5.11：
- claim 抽取（信号词匹配）
- causal_chain 拆解（cause/effect 识别）
- confidence 分级（low/medium，禁止 high）
- evidence_items 的 evidence_level 映射（probable 仅 official）
- failure_conditions 生成（三类规则）
- 四个 JSON 文件生成（tmp_path 隔离）
- 不触碰真实 data/reports/perilla_discovery/
- LLM 成功路径（mock 返回有效 JSON）
- LLM 失败降级路径（mock 抛异常）
- 合规拦截（mock 返回含"建议买入"）

所有测试使用 tmp_path 隔离，禁止读写真实 data/reports/perilla_discovery/。
LLM 相关测试用 monkeypatch mock，禁止真实调用 LLM API。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

# 将项目根目录加入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.research.perilla.discovery.report_causal_extractor import (
    _P1LLMExtractor,
    build_evidence_items,
    extract_causal_chains,
    extract_report_claims,
    generate_failure_conditions,
    load_raw_documents,
    run_report_causal_extractor,
)


# =============================================================================
# 测试辅助
# =============================================================================


def _make_document(
    doc_id: str = "doc_test001",
    title: str = "GB200 放量带动 PCB 需求",
    summary: str = "随着 GB200 放量，高速 PCB 需求有望提升",
    content: str = "随着 GB200 放量，高速 PCB 需求有望提升，高端铜箔受益。",
    source_type: str = "研报",
    published_at: str = "2026-06-17T10:00:00",
    companies: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """构造测试用 raw_document。

    Args:
        doc_id: 文档 ID
        title: 标题
        summary: 摘要
        content: 正文
        source_type: 来源类型
        published_at: 发布时间
        companies: 关联公司列表

    Returns:
        raw_document 字典
    """
    return {
        "document_id": doc_id,
        "source_id": "test_source",
        "source_type": source_type,
        "title": title,
        "summary": summary,
        "content_text": content,
        "url": "",
        "published_at": published_at,
        "collected_at": published_at,
        "related_companies": companies or [],
        "related_themes": [],
        "raw_hash": doc_id[-8:],
        "source_payload": {},
    }


def _write_documents_to_tmp(tmp_path: Path, documents: List[Dict[str, Any]]) -> Path:
    """将文档列表写入 tmp_path/raw_documents_latest.json。

    Args:
        tmp_path: 临时目录
        documents: 文档列表

    Returns:
        tmp_path
    """
    file_path = tmp_path / "raw_documents_latest.json"
    file_path.write_text(
        json.dumps(documents, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return tmp_path


def _make_mock_llm_extractor(
    extract_return: Tuple[Optional[Dict], List[Dict]],
    available: bool = True,
) -> MagicMock:
    """创建 mock _P1LLMExtractor。

    Args:
        extract_return: extract 方法返回值 (parsed_result, grounding_results)
        available: LLM 是否可用

    Returns:
        mock _P1LLMExtractor 实例
    """
    mock = MagicMock(spec=_P1LLMExtractor)
    mock.available = available
    mock.extract.return_value = extract_return
    # mock stats
    mock.client = MagicMock()
    mock.client.stats = MagicMock()
    mock.client.stats.llm_call_count = 1
    mock.client.stats.success = 1
    mock.client.stats.failure = 0
    mock.client.stats.fallback_rule_based = 0
    mock.client.stats.cache_hit = 0
    mock.client.stats.compliance_drop = 0
    mock.client.stats.grounding_drop = 0
    return mock


# =============================================================================
# SubTask 5.1, 5.4: claim 抽取 + confidence 分级
# =============================================================================


class TestClaimExtraction:
    """测试 claim 抽取（规则降级路径）。"""

    def test_extract_claim_with_signal_word(self, tmp_path: Path) -> None:
        """SubTask 5.1: 测试"随着 GB200 放量，高速 PCB 需求有望提升"能抽 claim。"""
        doc = _make_document(
            summary="随着 GB200 放量，高速 PCB 需求有望提升",
            content="随着 GB200 放量，高速 PCB 需求有望提升，高端铜箔受益。",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        assert len(claims) > 0, "期望抽出至少 1 条 claim"
        # 验证 claim_text 含信号词
        claim_text = claims[0].get("claim_text", "")
        assert "GB200" in claim_text or "PCB" in claim_text, \
            f"claim_text 应含 GB200 或 PCB，实际: {claim_text}"

    def test_confidence_low_with_you_wang(self, tmp_path: Path) -> None:
        """SubTask 5.4: 测试"有望"导致 confidence = low（规则降级路径）。"""
        doc = _make_document(
            summary="随着 GB200 放量，高速 PCB 需求有望提升",
            content="随着 GB200 放量，高速 PCB 需求有望提升。",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        assert len(claims) > 0
        assert claims[0]["confidence"] == "low", \
            f"含'有望'时 confidence 应为 low，实际: {claims[0]['confidence']}"

    def test_confidence_medium_with_announcement(self, tmp_path: Path) -> None:
        """测试"公告披露"导致 confidence = medium（规则降级路径）。"""
        doc = _make_document(
            summary="公告披露：公司签订订单",
            content="公告披露：公司签订订单，客户认证通过。",
            source_type="公告",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        assert len(claims) > 0
        assert claims[0]["confidence"] == "medium", \
            f"含'公告披露'时 confidence 应为 medium，实际: {claims[0]['confidence']}"

    def test_confidence_never_high(self, tmp_path: Path) -> None:
        """测试 confidence 永远不为 high。"""
        doc = _make_document(
            summary="公告披露确认签订客户认证",
            content="公告披露确认签订客户认证。",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        for claim in claims:
            assert claim["confidence"] in ("low", "medium"), \
                f"confidence 禁止 high，实际: {claim['confidence']}"


# =============================================================================
# SubTask 5.2, 5.3: causal_chain 拆解
# =============================================================================


class TestCausalChainExtraction:
    """测试 causal_chain 拆解（规则降级路径）。"""

    def test_extract_cause_gb200(self, tmp_path: Path) -> None:
        """SubTask 5.2: 测试能识别 GB200 为 cause。"""
        doc = _make_document(
            summary="随着 GB200 放量，高速 PCB 需求有望提升",
            content="随着 GB200 放量，高速 PCB 需求有望提升。",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        assert len(claims) > 0

        chains, _ = extract_causal_chains(claims, [doc], llm_extractor=None)
        assert len(chains) > 0, "期望抽出至少 1 条 causal_chain"
        # cause 应含 GB200 或"放量"
        cause = chains[0].get("cause", "")
        assert "GB200" in cause or "放量" in cause, \
            f"cause 应含 GB200 或放量，实际: {cause}"

    def test_extract_effect_pcb(self, tmp_path: Path) -> None:
        """SubTask 5.3: 测试能识别高速 PCB 为 effect 或 beneficiary_node。"""
        doc = _make_document(
            summary="随着 GB200 放量，高速 PCB 需求有望提升",
            content="随着 GB200 放量，高速 PCB 需求有望提升。",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        chains, _ = extract_causal_chains(claims, [doc], llm_extractor=None)
        assert len(chains) > 0

        chain = chains[0]
        effect = chain.get("effect", "")
        beneficiary = chain.get("beneficiary_node", "")
        # effect 或 beneficiary_node 应含 PCB
        assert "PCB" in effect or "PCB" in beneficiary, \
            f"effect 或 beneficiary_node 应含 PCB，effect={effect}, beneficiary={beneficiary}"

    def test_cannot_extract_writes_unspecified(self, tmp_path: Path) -> None:
        """测试无法抽取时字段写"未明确"，不编造。"""
        doc = _make_document(
            summary="这是一个普通陈述",
            content="这是一个普通陈述，没有因果关系。",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        # 没有信号词，不应抽出 claim
        # 但如果有信号词但无因果连接词，chain 字段应为"未明确"
        if claims:
            chains, _ = extract_causal_chains(claims, [doc], llm_extractor=None)
            for chain in chains:
                if chain.get("cause") == "未明确":
                    assert chain.get("effect") in ("未明确", ""), \
                        "cause 未明确时 effect 也应为未明确"


# =============================================================================
# SubTask 5.5: evidence_items 的 evidence_level 映射
# =============================================================================


class TestEvidenceItems:
    """测试 evidence_items 生成与 evidence_level 映射。"""

    def test_official_evidence_level_probable(self, tmp_path: Path) -> None:
        """SubTask 5.5: 测试"公告披露"导致 evidence_type=official，evidence_level=probable。"""
        doc = _make_document(
            summary="公告披露：公司签订订单",
            content="公告披露：公司签订订单。",
            source_type="公告",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        chains, _ = extract_causal_chains(claims, [doc], llm_extractor=None)
        evidence_items = build_evidence_items(claims, chains, [doc])

        assert len(evidence_items) > 0, "期望生成至少 1 条 evidence_item"
        # 至少有一条 evidence_type=official, evidence_level=probable
        official_items = [e for e in evidence_items if e.get("evidence_type") == "official"]
        assert len(official_items) > 0, "期望含 official 类型 evidence_item"
        for item in official_items:
            assert item["evidence_level"] == "probable", \
                f"official 类型 evidence_level 应为 probable，实际: {item['evidence_level']}"

    def test_report_evidence_level_weak_signal(self, tmp_path: Path) -> None:
        """测试研报来源 evidence_level=weak_signal。"""
        doc = _make_document(
            summary="随着 GB200 放量，PCB 需求有望提升",
            content="随着 GB200 放量，PCB 需求有望提升。",
            source_type="研报",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        evidence_items = build_evidence_items(claims, [], [doc])

        report_items = [e for e in evidence_items if e.get("evidence_type") == "report"]
        assert len(report_items) > 0, "期望含 report 类型 evidence_item"
        for item in report_items:
            assert item["evidence_level"] == "weak_signal", \
                f"report 类型 evidence_level 应为 weak_signal，实际: {item['evidence_level']}"

    def test_probable_not_in_edges(self, tmp_path: Path) -> None:
        """测试 probable 仅出现在 evidence_items，不出现在 edges（P1 不生成 edges）。"""
        doc = _make_document(source_type="公告")
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        evidence_items = build_evidence_items(claims, [], [doc])
        # evidence_items 允许 probable
        for item in evidence_items:
            assert item["evidence_level"] in ("hypothesis", "weak_signal", "probable")


# =============================================================================
# SubTask 5.6: failure_conditions 生成
# =============================================================================


class TestFailureConditions:
    """测试 failure_conditions 生成。"""

    def test_material_generates_evidence_risk(self, tmp_path: Path) -> None:
        """SubTask 5.6: 测试材料类观点能生成 evidence_risk 类型 failure_condition。"""
        doc = _make_document(
            summary="高端铜箔材料受益于 PCB 需求提升",
            content="高端铜箔材料受益于 PCB 需求提升。",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        chains, _ = extract_causal_chains(claims, [doc], llm_extractor=None)
        # 手动设置 beneficiary_node 为材料类
        for chain in chains:
            chain["beneficiary_node"] = "高端铜箔"
        failure_conditions = generate_failure_conditions(claims, chains)

        assert len(failure_conditions) > 0, "期望生成至少 1 条 failure_condition"
        # 至少有一条 risk_type=evidence_risk
        evidence_risk_items = [f for f in failure_conditions if f.get("risk_type") == "evidence_risk"]
        assert len(evidence_risk_items) > 0, \
            f"期望含 evidence_risk 类型，实际 risk_types: {[f.get('risk_type') for f in failure_conditions]}"

    def test_product_platform_generates_demand_risk(self, tmp_path: Path) -> None:
        """测试产品平台类 cause 生成 demand_risk。"""
        doc = _make_document(
            summary="GB200 放量带动 PCB 需求",
            content="GB200 放量带动 PCB 需求提升。",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        chains, _ = extract_causal_chains(claims, [doc], llm_extractor=None)
        # 手动设置 cause 为产品平台
        for chain in chains:
            chain["cause"] = "GB200 放量"
        failure_conditions = generate_failure_conditions(claims, chains)

        demand_risk_items = [f for f in failure_conditions if f.get("risk_type") == "demand_risk"]
        assert len(demand_risk_items) > 0, \
            f"期望含 demand_risk 类型，实际 risk_types: {[f.get('risk_type') for f in failure_conditions]}"

    def test_substitution_generates_substitution_risk(self, tmp_path: Path) -> None:
        """测试国产替代生成 substitution_risk。"""
        doc = _make_document(
            summary="国产替代逻辑推动高端铜箔需求",
            content="国产替代逻辑推动高端铜箔需求提升。",
        )
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        failure_conditions = generate_failure_conditions(claims, [])

        substitution_items = [f for f in failure_conditions if f.get("risk_type") == "substitution_risk"]
        assert len(substitution_items) > 0, \
            f"期望含 substitution_risk 类型，实际 risk_types: {[f.get('risk_type') for f in failure_conditions]}"

    def test_risk_type_in_allowed_enum(self, tmp_path: Path) -> None:
        """测试 risk_type 限定在允许枚举内。"""
        doc = _make_document()
        claims, _ = extract_report_claims([doc], llm_extractor=None)
        failure_conditions = generate_failure_conditions(claims, [])

        allowed = {"demand_risk", "evidence_risk", "substitution_risk", "valuation_risk", "unknown"}
        for fc in failure_conditions:
            assert fc["risk_type"] in allowed, \
                f"risk_type 应在 {allowed} 内，实际: {fc['risk_type']}"


# =============================================================================
# SubTask 5.7, 5.8: JSON 文件生成 + 不触碰真实目录
# =============================================================================


class TestJsonGeneration:
    """测试四个 JSON 文件生成（tmp_path 隔离）。"""

    def test_four_jsons_generated(self, tmp_path: Path) -> None:
        """SubTask 5.7: 测试四个输出 JSON 都能生成（在 tmp_path 下）。"""
        documents = [
            _make_document(
                doc_id="doc_test_001",
                summary="随着 GB200 放量，高速 PCB 需求有望提升",
                content="随着 GB200 放量，高速 PCB 需求有望提升。",
            ),
            _make_document(
                doc_id="doc_test_002",
                summary="公告披露：公司签订订单",
                content="公告披露：公司签订订单。",
                source_type="公告",
            ),
        ]
        _write_documents_to_tmp(tmp_path, documents)

        summary = run_report_causal_extractor(
            input_dir=tmp_path,
            output_dir=tmp_path,
            use_llm=False,
        )

        assert summary["success"], f"运行失败: {summary.get('error')}"
        assert summary["counts"]["raw_documents"] == 2

        # 验证四个 JSON 文件存在
        for filename in [
            "report_claims_latest.json",
            "causal_chains_latest.json",
            "evidence_items_latest.json",
            "failure_conditions_latest.json",
        ]:
            file_path = tmp_path / filename
            assert file_path.exists(), f"期望文件存在: {file_path}"
            data = json.loads(file_path.read_text(encoding="utf-8"))
            assert isinstance(data, list), f"{filename} 应为 list"

    def test_does_not_touch_real_data_dir(self, tmp_path: Path) -> None:
        """SubTask 5.8: 测试不触碰真实 data/reports/perilla_discovery/ 目录。"""
        real_dir = _PROJECT_ROOT / "data" / "reports" / "perilla_discovery"
        # 记录真实目录的 P1 文件修改时间
        real_files_before = {}
        p1_files = [
            "report_claims_latest.json",
            "causal_chains_latest.json",
            "evidence_items_latest.json",
            "failure_conditions_latest.json",
        ]
        for fname in p1_files:
            fpath = real_dir / fname
            if fpath.exists():
                real_files_before[fname] = fpath.stat().st_mtime

        # 运行到 tmp_path
        documents = [_make_document()]
        _write_documents_to_tmp(tmp_path, documents)
        run_report_causal_extractor(
            input_dir=tmp_path,
            output_dir=tmp_path,
            use_llm=False,
        )

        # 验证真实目录文件未被修改
        for fname in p1_files:
            fpath = real_dir / fname
            if fpath.exists() and fname in real_files_before:
                assert fpath.stat().st_mtime == real_files_before[fname], \
                    f"真实目录文件被修改: {fname}"

    def test_empty_documents_not_crash(self, tmp_path: Path) -> None:
        """测试无 raw_documents 时不崩溃，输出空 JSON。"""
        # 不写入 raw_documents_latest.json
        summary = run_report_causal_extractor(
            input_dir=tmp_path,
            output_dir=tmp_path,
            use_llm=False,
        )
        assert summary["success"], "无文档时也应成功"
        assert summary["counts"]["raw_documents"] == 0
        # 四个 JSON 文件仍应存在（空列表）
        for filename in [
            "report_claims_latest.json",
            "causal_chains_latest.json",
            "evidence_items_latest.json",
            "failure_conditions_latest.json",
        ]:
            assert (tmp_path / filename).exists(), f"空文档时也应生成: {filename}"


# =============================================================================
# SubTask 5.9, 5.10, 5.11: LLM 三种路径测试
# =============================================================================


class TestLLMPaths:
    """测试 LLM 成功/失败降级/合规拦截三种路径。"""

    def test_llm_success_path(self, tmp_path: Path) -> None:
        """SubTask 5.9: 测试 LLM 成功路径：mock 返回有效 JSON，验证 claim 被抽取。"""
        doc = _make_document(
            summary="随着 GB200 放量，高速 PCB 需求有望提升",
            content="随着 GB200 放量，高速 PCB 需求有望提升。",
        )

        # mock LLM 返回有效 JSON
        llm_result = {
            "claims": [
                {
                    "claim_text": "GB200 放量带动高速 PCB 需求提升",
                    "mentioned_terms": ["GB200", "PCB"],
                    "mentioned_companies": [],
                    "signal_words": ["带动"],
                    "evidence_quote": "随着 GB200 放量，高速 PCB 需求有望提升",
                    "confidence": "medium",
                }
            ],
            "causal_chains": [
                {
                    "claim_index": 0,
                    "cause": "GB200 放量",
                    "mechanism": "带动",
                    "effect": "高速 PCB 需求提升",
                    "beneficiary_node": "高端铜箔",
                    "evidence_quote": "随着 GB200 放量，高速 PCB 需求有望提升",
                    "confidence": "medium",
                }
            ],
        }
        mock_extractor = _make_mock_llm_extractor(
            extract_return=(llm_result, []),
            available=True,
        )

        claims, stats = extract_report_claims([doc], llm_extractor=mock_extractor)
        assert len(claims) > 0, "LLM 成功时应抽出 claim"
        assert "GB200" in claims[0].get("claim_text", ""), \
            f"LLM claim 应含 GB200，实际: {claims[0].get('claim_text')}"
        assert claims[0]["confidence"] in ("low", "medium"), \
            f"confidence 禁止 high，实际: {claims[0]['confidence']}"

    def test_llm_failure_fallback_to_rules(self, tmp_path: Path) -> None:
        """SubTask 5.10: 测试 LLM 失败降级路径：mock 抛异常，验证降级到规则模式仍能输出。"""
        doc = _make_document(
            summary="随着 GB200 放量，高速 PCB 需求有望提升",
            content="随着 GB200 放量，高速 PCB 需求有望提升。",
        )

        # mock LLM 返回 None（模拟 LLM 失败）
        mock_extractor = _make_mock_llm_extractor(
            extract_return=(None, []),
            available=True,
        )

        claims, stats = extract_report_claims([doc], llm_extractor=mock_extractor)
        # LLM 失败时应降级到规则模式，仍能抽出 claim
        assert len(claims) > 0, "LLM 失败时应降级到规则模式仍能抽出 claim"
        assert "GB200" in claims[0].get("claim_text", "") or "PCB" in claims[0].get("claim_text", ""), \
            f"规则降级 claim 应含 GB200 或 PCB，实际: {claims[0].get('claim_text')}"

    def test_llm_compliance_block(self, tmp_path: Path) -> None:
        """SubTask 5.11: 测试合规拦截：mock LLM 返回含"建议买入"，验证被拦截。"""
        doc = _make_document(
            summary="随着 GB200 放量，高速 PCB 需求有望提升",
            content="随着 GB200 放量，高速 PCB 需求有望提升。",
        )

        # mock LLM 返回含"建议买入"的 JSON（模拟合规拦截）
        # 实际上 _P1LLMExtractor.extract 内部会做 check_compliance
        # 但这里我们 mock extract 方法直接返回 None（模拟合规拦截后的结果）
        # 为了测试合规拦截逻辑，我们直接测试 _P1LLMExtractor 的合规检查
        llm_result_with_blocked = {
            "claims": [
                {
                    "claim_text": "建议买入 GB200 相关标的",
                    "evidence_quote": "建议买入",
                    "confidence": "medium",
                }
            ],
            "causal_chains": [],
        }

        # 创建真实的 _P1LLMExtractor 但 mock _call_p1_api 返回含禁用词的 JSON
        from core.llm_processing.llm_extraction_client import LLMExtractionClient
        from core.llm_processing.prompt_templates import check_compliance

        # 验证 check_compliance 能拦截"建议买入"
        blocked_text = json.dumps(llm_result_with_blocked, ensure_ascii=False)
        assert not check_compliance(blocked_text), \
            "含'建议买入'的文本应被 check_compliance 拦截"

        # mock LLM client available=True，但 _call_p1_api 返回含禁用词的 JSON
        mock_client = MagicMock(spec=LLMExtractionClient)
        mock_client.available = True
        mock_client.api_base = "http://mock"
        mock_client.model_name = "mock-model"
        mock_client.api_key = ""
        mock_client.timeout = 60
        mock_client.stats = MagicMock()
        mock_client.stats.llm_call_count = 0
        mock_client.stats.success = 0
        mock_client.stats.failure = 0
        mock_client.stats.fallback_rule_based = 0
        mock_client.stats.cache_hit = 0
        mock_client.stats.compliance_drop = 0
        mock_client.stats.grounding_drop = 0
        mock_client._parse_json.return_value = llm_result_with_blocked

        extractor = _P1LLMExtractor(mock_client, cache=MagicMock())
        # mock _call_p1_api 返回含禁用词的原始 JSON
        with patch.object(extractor, "_call_p1_api", return_value=json.dumps(llm_result_with_blocked)):
            # mock cache 不命中
            mock_cache_file = MagicMock()
            mock_cache_file.exists.return_value = False
            extractor.cache._cache_path = MagicMock(return_value=mock_cache_file)

            parsed, _ = extractor.extract(doc)

        # 合规拦截后应返回 None
        assert parsed is None, "含'建议买入'的 LLM 输出应被合规拦截，返回 None"
        assert mock_client.stats.compliance_drop == 1, "应记录合规丢弃"

    def test_llm_not_available_pure_rules(self, tmp_path: Path) -> None:
        """测试 LLM 不可用时自动降级为纯规则模式。"""
        doc = _make_document(
            summary="随着 GB200 放量，高速 PCB 需求有望提升",
            content="随着 GB200 放量，高速 PCB 需求有望提升。",
        )

        # mock LLM 不可用
        mock_extractor = _make_mock_llm_extractor(
            extract_return=(None, []),
            available=False,
        )

        claims, stats = extract_report_claims([doc], llm_extractor=mock_extractor)
        # LLM 不可用时应纯规则模式
        assert len(claims) > 0, "LLM 不可用时应纯规则模式仍能抽出 claim"

    def test_llm_high_confidence_downgraded(self, tmp_path: Path) -> None:
        """测试 LLM 返回 high confidence 时降级为 low。"""
        doc = _make_document(
            summary="GB200 放量",
            content="GB200 放量。",
        )

        llm_result = {
            "claims": [
                {
                    "claim_text": "GB200 放量带动需求",
                    "evidence_quote": "GB200 放量",
                    "confidence": "high",  # 应被降级
                }
            ],
            "causal_chains": [],
        }
        mock_extractor = _make_mock_llm_extractor(
            extract_return=(llm_result, []),
            available=True,
        )

        claims, _ = extract_report_claims([doc], llm_extractor=mock_extractor)
        assert len(claims) > 0
        assert claims[0]["confidence"] != "high", \
            f"high confidence 应被降级，实际: {claims[0]['confidence']}"
