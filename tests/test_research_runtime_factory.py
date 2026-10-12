from pathlib import Path

from core.research.control_plane.runtime_factory import ResearchRuntimeFactory
from core.research.control_plane.repository import ResearchRepository


def test_runtime_factory_degrades_invalid_output_dir_to_default() -> None:
    factory = ResearchRuntimeFactory(repository=object(), output_dir=None)
    assert factory.output_dir == Path("data/research_cache")


def test_runtime_factory_degrades_non_path_output_dir_to_default() -> None:
    factory = ResearchRuntimeFactory(repository=object(), output_dir=object())
    assert factory.output_dir == Path("data/research_cache")


def test_runtime_factory_preserves_falsey_injected_components(tmp_path) -> None:
    class FalseyComponent:
        def __bool__(self) -> bool:
            return False

    components = [FalseyComponent() for _ in range(6)]
    runtime = ResearchRuntimeFactory(
        ResearchRepository(tmp_path / "research.db"),
        local_retriever=components[0],
        first_party_retriever=components[1],
        web_retriever=components[2],
        budget_manager=components[3],
        task_resumer=components[4],
        extractor=components[5],
        crawler=components[0],
    ).build()

    assert runtime.local_retriever is components[0]
    assert runtime.first_party_retriever is components[1]
    assert runtime.web_retriever is components[2]
    assert runtime.budget_manager is components[3]
    assert runtime.task_resumer is components[4]
    assert runtime.orchestrator.extractor is components[5]
