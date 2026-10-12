from __future__ import annotations

from unittest.mock import MagicMock

from core.research.control_plane.contracts import Claim
from core.research.control_plane.perilla_adapter import (
    GroundedEvidenceExtractor,
    SearchDiscoveryRetriever,
)


def test_discovery_accepts_mapping_results_and_bad_urls_as_snippets() -> None:
    discovery = MagicMock()
    discovery.search.return_value = {
        "documents": [{"url": "http://[bad", "title": "title", "metadata": []}]
    }

    result = SearchDiscoveryRetriever(discovery).search(
        "测试", tier="web", limit=5
    )

    assert len(result) == 1
    assert result[0]["metadata"]["snippet_only"] is True


def test_extractor_malformed_claim_and_document_fields_degrade() -> None:
    repository = MagicMock()
    claim = Claim(
        claim_id="claim-1",
        subject_id="600000.SH",
        subject_name="测试公司",
        object_name="测试主题",
    )
    claim.attributes = []
    document = {
        "title": "测试公司",
        "content_text": "测试公司 600000",
        "url": "http://[bad",
        "metadata": [],
    }

    assert GroundedEvidenceExtractor(repository).extract(
        document, claim, query_purpose="support"
    ) == []


def test_persistence_failure_is_a_degraded_empty_extraction() -> None:
    repository = MagicMock()
    repository.save_source_trace.side_effect = RuntimeError("database down")
    claim = Claim(
        claim_id="claim-1",
        subject_id="600000.SH",
        subject_name="测试公司",
        object_name="测试主题",
    )
    document = {
        "title": "测试公司",
        "content_text": "测试公司 600000 开展测试主题业务",
        "url": "https://example.com/report",
        "metadata": {"source_backend": "official"},
    }

    assert GroundedEvidenceExtractor(repository).extract(
        document, claim, query_purpose="support"
    ) == []
