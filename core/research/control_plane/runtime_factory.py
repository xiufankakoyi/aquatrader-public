"""Build the durable research runtime used by the monitor.

The factory keeps production wiring in one place while allowing tests and
older callers to inject individual providers.  It deliberately does not
invent company URLs: the task context is passed to the first-party retriever
unchanged and only explicit IR URLs are consumed by the company-site provider.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .budget_manager import GlobalBudgetManager
from .first_party_retriever import (
    CompanyIRSiteProvider,
    FirstPartyRetriever,
    SseInteractiveProvider,
    SzseInteractiveProvider,
)
from .local_retrieval import LocalEvidenceRetriever
from .orchestrator import ResearchOrchestrator, SearchTier
from .perilla_adapter import GroundedEvidenceExtractor, SearchDiscoveryRetriever
from .production_retrieval import CninfoPdfRetriever
from .repository import ResearchRepository
from .task_resumer import TaskResumer


@dataclass
class ResearchRuntime:
    """All components required for one dynamic-theme research execution."""

    local_retriever: Any
    first_party_retriever: Any
    web_retriever: Any
    budget_manager: Any
    task_resumer: TaskResumer
    orchestrator: ResearchOrchestrator


class ResearchRuntimeFactory:
    """Create a monitor-safe, persistent research runtime.

    Optional component arguments are intentionally narrow dependency seams for
    tests and controlled deployments.  Defaults use the already verified
    CNINFO, AKShare SSE/CNINFO IRM, explicit company IR and SearchDiscovery
    adapters.
    """

    def __init__(
        self,
        repository: ResearchRepository,
        *,
        output_dir: str | Path = "data/research_cache",
        local_retriever: Any | None = None,
        first_party_retriever: Any | None = None,
        web_retriever: Any | None = None,
        budget_manager: Any | None = None,
        task_resumer: TaskResumer | None = None,
        extractor: Any | None = None,
        company_ir_resolver: Callable[..., Any] | None = None,
        crawler: Any | None = None,
        interaction_provider: Any | None = None,
        search_discovery: Any | None = None,
    ) -> None:
        self.repository = repository
        try:
            self.output_dir = Path(output_dir) if output_dir else Path(
                "data/research_cache"
            )
        except (TypeError, ValueError):
            self.output_dir = Path("data/research_cache")
        self._local_retriever = local_retriever
        self._first_party_retriever = first_party_retriever
        self._web_retriever = web_retriever
        self._budget_manager = budget_manager
        self._task_resumer = task_resumer
        self._extractor = extractor
        self._company_ir_resolver = company_ir_resolver
        self._crawler = crawler
        self._interaction_provider = interaction_provider
        self._search_discovery = search_discovery

    def build(self, *, as_of: str = "", task_context: dict[str, Any] | None = None) -> ResearchRuntime:
        """Build one runtime; ``task_context`` is reserved for future seams."""
        _ = task_context
        safe_as_of = as_of if isinstance(as_of, str) else ""
        local = (
            self._local_retriever
            if self._local_retriever is not None
            else LocalEvidenceRetriever(self.repository)
        )
        resumer = (
            self._task_resumer
            if self._task_resumer is not None
            else TaskResumer(self.repository)
        )
        budget = (
            self._budget_manager
            if self._budget_manager is not None
            else GlobalBudgetManager(repository=self.repository)
        )

        crawler = self._crawler
        if crawler is None:
            from core.research.perilla.discovery.focused_crawler import FocusedCrawler

            crawler = FocusedCrawler(
                cache_dir=str(self.output_dir / "web"),
                timeout=12,
            )

        first_party = self._first_party_retriever
        if first_party is None:
            from infrastructure.data_providers.market.akshare_provider import AkshareProvider

            cninfo = CninfoPdfRetriever(self.repository, as_of=safe_as_of)
            interaction_provider = self._interaction_provider
            if interaction_provider is None:
                interaction_provider = AkshareProvider()
            first_party = FirstPartyRetriever(
                cninfo_pdf_retriever=cninfo,
                interactive_providers=[
                    SseInteractiveProvider(market_provider=interaction_provider),
                    SzseInteractiveProvider(market_provider=interaction_provider),
                ],
                company_site_provider=CompanyIRSiteProvider(
                    crawler=crawler,
                    resolver=self._company_ir_resolver,
                ),
            )

        web = self._web_retriever
        if web is None:
            if self._search_discovery is None:
                from config.setting import Setting
                from core.research.perilla.discovery.search_discovery import SearchDiscovery

                self._search_discovery = SearchDiscovery(
                    searxng_base_url=Setting.SEARXNG_BASE_URL,
                    engines=Setting.SEARXNG_ENGINES,
                    max_results=5,
                    timeout=8,
                    enabled=True,
                    max_concurrency=1,
                    min_interval=0.1,
                )
            web = SearchDiscoveryRetriever(
                self._search_discovery,
                body_crawler=crawler,
            )

        extractor = (
            self._extractor
            if self._extractor is not None
            else GroundedEvidenceExtractor(self.repository)
        )

        # P4.2 修复：注入 FeedbackController 和 FactOutcomeStore，
        # 让 Claim 晋升路径和检索反馈回路在生产中真正接线。
        from .feedback import FeedbackController
        from .fact_outcome import FactOutcomeStore

        feedback_controller = FeedbackController(self.repository)
        fact_outcome_store = FactOutcomeStore(self.repository, feedback_controller)

        orchestrator = ResearchOrchestrator(
            self.repository,
            web,
            extractor,
            local_retriever=local,
            first_party_retriever=first_party,
            global_budget_manager=budget,
            task_resumer=resumer,
            feedback_controller=feedback_controller,
            fact_outcome_store=fact_outcome_store,
            tiers=(
                SearchTier(
                    "second_hand",
                    4,
                    max_queries=3,
                    query_suffixes=("新闻", "报道", "券商研报"),
                ),
                SearchTier(
                    "web",
                    4,
                    max_queries=3,
                    query_suffixes=("官网", "公告", "新闻", "报道"),
                ),
            ),
        )
        return ResearchRuntime(
            local_retriever=local,
            first_party_retriever=first_party,
            web_retriever=web,
            budget_manager=budget,
            task_resumer=resumer,
            orchestrator=orchestrator,
        )
