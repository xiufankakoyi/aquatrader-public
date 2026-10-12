from __future__ import annotations

from unittest.mock import MagicMock

from core.research.control_plane.contracts import Claim, EvidenceRecord, ResearchTask, TaskBudget
from core.research.control_plane.orchestrator import ResearchOrchestrator, SearchTier
from core.research.control_plane.repository import ResearchRepository


class _Retriever:
    def __init__(self, documents):
        self.documents = documents

    def search(self, query, *, tier, limit):
        return self.documents


def _claim() -> Claim:
    return Claim(
        claim_id="claim-1",
        subject_id="600000",
        subject_name="测试公司",
        object_name="测试主题",
        support_terms=["测试主题"],
    )


def _task() -> ResearchTask:
    return ResearchTask(
        task_id="task-1",
        target_claim_id="claim-1",
        budget=TaskBudget(max_requests=2, max_documents=4).to_dict(),
    )


def _evidence(evidence_id: str) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        claim_id="claim-1",
        lineage_id=evidence_id,
        source_trace_id=f"source-{evidence_id}",
        stance="support",
        quote="测试主题的正式披露",
        grounding_status="exact",
    )


def _orchestrator(repository, retriever, extractor, *, cache=None):
    return ResearchOrchestrator(
        repository,
        retriever,
        extractor,
        cache=cache,
        tiers=(SearchTier("high", 2, max_queries=1, query_suffixes=("公告",)),),
    )


def test_string_false_snippet_flag_does_not_drop_full_document(tmp_path):
    repository = ResearchRepository(tmp_path / "research.db")
    repository.save_claim(_claim())
    extractor = MagicMock()
    extractor.extract.return_value = [_evidence("ev-1")]
    document = {
        "document_id": "doc-1",
        "source_type": "announcement",
        "content_text": "完整正文",
        "metadata": {"snippet_only": "false"},
    }

    summary = _orchestrator(
        repository, _Retriever([document]), extractor
    ).execute(_task())

    assert extractor.extract.call_count == 1
    assert summary.snippet_documents_ignored == 0


def test_one_extractor_failure_does_not_abort_other_documents(tmp_path):
    repository = ResearchRepository(tmp_path / "research.db")
    repository.save_claim(_claim())
    extractor = MagicMock()
    extractor.extract.side_effect = [RuntimeError("bad document"), [_evidence("ev-2")]]
    documents = [
        {"document_id": "doc-1", "content_text": "正文 1"},
        {"document_id": "doc-2", "content_text": "正文 2"},
    ]

    summary = _orchestrator(
        repository, _Retriever(documents), extractor
    ).execute(_task())

    assert extractor.extract.call_count == 2
    assert summary.evidence_saved == 1
    assert any(error["type"] == "extractor_error" for error in summary.errors)


def test_cache_adapter_failure_falls_back_to_retriever(tmp_path):
    repository = ResearchRepository(tmp_path / "research.db")
    repository.save_claim(_claim())
    cache = MagicMock()
    cache.get.side_effect = RuntimeError("cache down")
    cache.set.side_effect = RuntimeError("cache down")
    retriever = _Retriever([])
    orchestrator = _orchestrator(repository, retriever, MagicMock(), cache=cache)
    task = _task()
    task.params = None

    documents = orchestrator._retrieve_cached(
        _claim(), "测试主题", "support", SearchTier("high", 1), task
    )

    assert documents == []
