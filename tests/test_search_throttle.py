from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import yaml

from core.research.perilla.discovery.drill_down_engine import DrillDownEngine
from core.research.perilla.discovery.search_discovery import SearchDiscovery
from core.utils.search_throttle import SearchThrottlePool


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_search_throttle_pool_bounds_peak_concurrency() -> None:
    pool = SearchThrottlePool(max_concurrency=2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def work() -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return 1

    threads = [threading.Thread(target=pool.run, args=(work,)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 2
    stats = pool.snapshot()
    assert stats["completed"] == 8
    assert stats["peak_active"] == 2


def test_search_throttle_pool_spaces_request_starts() -> None:
    pool = SearchThrottlePool(max_concurrency=3, min_interval=0.03)
    starts: list[float] = []
    lock = threading.Lock()

    def work() -> None:
        with lock:
            starts.append(time.monotonic())

    threads = [threading.Thread(target=pool.run, args=(work,)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    starts.sort()
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    assert all(gap >= 0.025 for gap in gaps)


def test_search_throttle_pool_allows_configured_burst() -> None:
    pool = SearchThrottlePool(
        max_concurrency=3,
        min_interval=0.04,
        burst_capacity=3,
    )
    starts: list[float] = []
    lock = threading.Lock()

    def work() -> None:
        with lock:
            starts.append(time.monotonic())

    threads = [threading.Thread(target=pool.run, args=(work,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    starts.sort()
    assert starts[2] - starts[0] < 0.02
    assert starts[3] - starts[2] >= 0.03


def test_search_discovery_search_many_uses_json_api(monkeypatch) -> None:
    response = Mock()
    response.status_code = 200
    response.headers = {}
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "results": [
            {
                "url": "https://example.com/a",
                "title": "示例",
                "content": "光模块由若干部件组成",
                "engine": "duckduckgo",
                "engines": ["duckduckgo"],
                "score": 1,
            }
        ],
        "unresponsive_engines": [],
    }
    get_mock = Mock(return_value=response)
    monkeypatch.setattr(
        "core.research.perilla.discovery.search_discovery.requests.get",
        get_mock,
    )

    discovery = SearchDiscovery(
        searxng_base_url="http://localhost:8888",
        engines="duckduckgo",
        max_concurrency=2,
        min_interval=0,
    )
    results = discovery.search_many(["光模块 组成", "光模块 材料"])

    assert set(results) == {"光模块 组成", "光模块 材料"}
    assert all(len(items) == 1 for items in results.values())
    assert get_mock.call_count == 2
    for call in get_mock.call_args_list:
        assert call.kwargs["params"]["engines"] == "duckduckgo"


def test_searxng_config_keeps_expected_research_engines() -> None:
    settings = yaml.safe_load(
        (PROJECT_ROOT / "deploy" / "searxng" / "settings.yml").read_text(
            encoding="utf-8"
        )
    )
    engines = set(settings["use_default_settings"]["engines"]["keep_only"])

    assert {
        "duckduckgo",
        "bing",
        "brave",
        "wikipedia",
        "wikidata",
        "baidu",
        "baidu kaifa",
    } <= engines
    assert "json" in settings["search"]["formats"]


def test_drill_down_prefers_non_empty_searxng_results(monkeypatch) -> None:
    engine = DrillDownEngine(max_depth=1)
    registry = Mock()
    search = Mock()
    fast_query = engine._query_plan("光模块")[0]
    search.search_many.return_value = {
        fast_query: [
            SimpleNamespace(
                title="光模块结构",
                content_text="光模块包含发射组件和接收组件",
                site_name="duckduckgo",
                url="https://example.com/optical",
                metadata={"engines": ["duckduckgo"]},
            )
        ]
    }
    monkeypatch.setattr(engine, "_ensure_search_discovery", lambda: search)
    monkeypatch.setattr(engine, "_ensure_registry", lambda: registry)

    docs = engine._drill_search("光模块")

    assert len(docs) == 1
    assert docs[0]["source"] == "searxng:duckduckgo"
    search.search_many.assert_called_once_with([fast_query])
    registry.semantic_search.assert_not_called()


def test_adaptive_query_budget_stops_after_fast_path(monkeypatch) -> None:
    engine = DrillDownEngine(extraction_confidence_threshold=0.65)
    calls: list[str] = []

    def search_queries(_concept: str, queries: list[str]) -> list[dict]:
        calls.extend(queries)
        return [{"source": "test", "text": "证据"}]

    monkeypatch.setattr(engine, "_search_queries", search_queries)
    monkeypatch.setattr(
        engine,
        "_rule_extract",
        lambda _concept, _docs: [
            {"item": "冷板", "type": "sub_component", "evidence": "包含冷板", "source": "test"},
            {"item": "冷却液", "type": "used_material", "evidence": "使用冷却液", "source": "test"},
        ],
    )

    outcome = engine._adaptive_search("液冷服务器", important=True)

    assert len(calls) == 1
    assert len(outcome["queries"]) == 1
    assert not outcome["needs_llm"]


def test_adaptive_query_budget_uses_targeted_retry(monkeypatch) -> None:
    engine = DrillDownEngine(extraction_confidence_threshold=0.65)
    calls: list[str] = []

    def search_queries(_concept: str, queries: list[str]) -> list[dict]:
        calls.extend(queries)
        return [{"source": "test", "text": queries[0]}]

    monkeypatch.setattr(engine, "_search_queries", search_queries)

    def rule_extract(_concept: str, docs: list[dict]) -> list[dict]:
        items = [
            {"item": "冷却液", "type": "used_material", "evidence": "使用冷却液", "source": "test"},
        ]
        if len(docs) >= 2:
            items.append(
                {"item": "冷板", "type": "sub_component", "evidence": "包含冷板", "source": "test"}
            )
        return items

    monkeypatch.setattr(engine, "_rule_extract", rule_extract)
    outcome = engine._adaptive_search("液冷服务器", important=False)

    assert len(calls) == 2
    assert calls[1] == engine._query_plan("液冷服务器")[2]
    assert not outcome["needs_llm"]


def test_adaptive_query_budget_uses_hard_path_only_for_important_node(
    monkeypatch,
) -> None:
    engine = DrillDownEngine(extraction_confidence_threshold=0.9)
    calls: list[str] = []
    monkeypatch.setattr(
        engine,
        "_search_queries",
        lambda _concept, queries: (
            calls.extend(queries)
            or [{"source": "test", "text": queries[0]}]
        ),
    )
    monkeypatch.setattr(
        engine,
        "_rule_extract",
        lambda _concept, _docs: [
            {"item": "冷板", "type": "sub_component", "evidence": "包含冷板", "source": "test"},
        ],
    )

    normal = engine._adaptive_search("普通节点", important=False)
    important = engine._adaptive_search("重要节点", important=True)

    assert len(normal["queries"]) == 2
    assert not normal["needs_llm"]
    assert len(important["queries"]) == 3
    assert important["needs_llm"]


def test_llm_batch_requires_exact_evidence(monkeypatch) -> None:
    engine = DrillDownEngine(max_breadth=5)
    monkeypatch.setattr(
        engine,
        "_llm_settings",
        lambda: ("http://llm.example/v1", "test-model", "token"),
    )
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"results":['
                        '{"source_concept":"液冷服务器","extracted_items":['
                        '{"item":"冷板","relation_type":"contains",'
                        '"evidence_id":"C1-E1","evidence_snippet":"液冷服务器包含冷板"},'
                        '{"item":"虚构部件","relation_type":"contains",'
                        '"evidence_id":"C1-E1","evidence_snippet":"原文不存在"}]}'
                        "]}"
                    )
                }
            }
        ]
    }
    post = Mock(return_value=response)
    monkeypatch.setattr("requests.post", post)

    result = engine._llm_extract_batch(
        [
            {
                "concept_name": "液冷服务器",
                "docs": [
                    {
                        "source": "searxng:wikipedia",
                        "text": "液冷服务器包含冷板，并使用冷却液传递热量。",
                    }
                ],
            }
        ]
    )

    assert [item["item"] for item in result["液冷服务器"]] == ["冷板"]
    assert result["液冷服务器"][0]["evidence_id"] == "C1-E1"
    assert post.call_count == 1


def test_drill_batches_low_confidence_nodes_into_one_llm_call(monkeypatch) -> None:
    engine = DrillDownEngine(max_depth=2, llm_batch_size=8)

    def adaptive(concept: str, *, important: bool) -> dict:
        if concept == "根概念":
            return {
                "concept_name": concept,
                "docs": [],
                "rule_items": [
                    {"item": "部件甲", "type": "sub_component", "evidence": "包含部件甲", "source": "test"},
                    {"item": "部件乙", "type": "sub_component", "evidence": "包含部件乙", "source": "test"},
                ],
                "score": 0.8,
                "queries": ["fast"],
                "needs_llm": False,
            }
        return {
            "concept_name": concept,
            "docs": [{"source": "test", "text": f"{concept}包含子项"}],
            "rule_items": [],
            "score": 0.1,
            "queries": ["fast", "retry", "hard"],
            "needs_llm": important,
        }

    calls: list[list[str]] = []

    def llm_batch(cases: list[dict]) -> dict[str, list[dict]]:
        names = [case["concept_name"] for case in cases]
        calls.append(names)
        return {
            name: [
                {
                    "item": f"{name}子项",
                    "type": "sub_component",
                    "evidence": f"{name}包含子项",
                    "source": "llm_batch:test",
                }
            ]
            for name in names
        }

    monkeypatch.setattr(engine, "_adaptive_search", adaptive)
    monkeypatch.setattr(engine, "_llm_extract_batch", llm_batch)

    tree = engine.drill("根概念", max_depth=2)

    assert calls == [["部件甲", "部件乙"]]
    assert tree["runtime_stats"]["search_query_count"] == 0
