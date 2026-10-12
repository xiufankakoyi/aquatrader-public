"""Perilla Discovery Engine public API.

The public names are loaded lazily so importing one Perilla submodule does not
eagerly import the discovery, evidence, alpha, and ontology stacks.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS: dict[str, tuple[str, str]] = {
    "BottleneckHypothesis": ("core.research.perilla.discovery.models", "BottleneckHypothesis"),
    "CompanyCandidate": ("core.research.perilla.discovery.models", "CompanyCandidate"),
    "ConceptSignal": ("core.research.perilla.discovery.models", "ConceptSignal"),
    "DemandAnchor": ("core.research.perilla.discovery.models", "DemandAnchor"),
    "DynamicSupplyChain": ("core.research.perilla.discovery.models", "DynamicSupplyChain"),
    "EarlySignal": ("core.research.perilla.discovery.models", "EarlySignal"),
    "PerillaDiscoveryItem": ("core.research.perilla.discovery.models", "PerillaDiscoveryItem"),
    "PerillaDiscoveryReport": ("core.research.perilla.discovery.models", "PerillaDiscoveryReport"),
    "SourceDocument": ("core.research.perilla.discovery.models", "SourceDocument"),
    "SupplyChainEdge": ("core.research.perilla.discovery.models", "SupplyChainEdge"),
    "SupplyChainNode": ("core.research.perilla.discovery.models", "SupplyChainNode"),
    "ConceptRadar": ("core.research.perilla.discovery.concept_radar", "ConceptRadar"),
    "ConceptScoutEngine": ("core.research.perilla.discovery.concept_scout_engine", "ConceptScoutEngine"),
    "ConceptDiscoveryEngine": ("core.research.perilla.discovery.concept_discovery_engine", "ConceptDiscoveryEngine"),
    "EvidenceSearchTaskGenerator": ("core.research.perilla.discovery.evidence_search_task_generator", "EvidenceSearchTaskGenerator"),
    "PerillaKnowledgeLoopEngine": ("core.research.perilla.discovery.knowledge_loop_engine", "PerillaKnowledgeLoopEngine"),
    "ResearchQuestionEngine": ("core.research.perilla.discovery.research_question_engine", "ResearchQuestionEngine"),
    "ConceptSignalLoader": ("core.research.perilla.discovery.concept_signal_loader", "ConceptSignalLoader"),
    "ConceptTaxonomy": ("core.research.perilla.discovery.concept_taxonomy", "ConceptTaxonomy"),
    "WeakSignalTracker": ("core.research.perilla.discovery.weak_signal_tracker", "WeakSignalTracker"),
    "SourceRegistry": ("core.research.perilla.evidence.source_registry", "SourceRegistry"),
    "SourceEntry": ("core.research.perilla.evidence.source_registry", "SourceEntry"),
    "SearchDiscovery": ("core.research.perilla.discovery.search_discovery", "SearchDiscovery"),
    "DynamicChainConstructor": ("core.research.perilla.discovery.chain_constructor", "DynamicChainConstructor"),
    "BottleneckMiner": ("core.research.perilla.discovery.bottleneck_miner", "BottleneckMiner"),
    "CompanyMapper": ("core.research.perilla.discovery.company_mapper", "CompanyMapper"),
    "EvidenceGrader": ("core.research.perilla.evidence.evidence_grader", "EvidenceGrader"),
    "PerillaClassifier": ("core.research.perilla.discovery.perilla_classifier", "PerillaClassifier"),
    "ReviewQueue": ("core.research.perilla.discovery.review_queue", "ReviewQueue"),
    "AutonomousPerillaEngine": ("core.research.perilla.discovery.autonomous_perilla_engine", "AutonomousPerillaEngine"),
    "OntologyRegistry": ("core.research.perilla.shared.ontology.ontology_registry", "OntologyRegistry"),
    "SchemaProposal": ("core.research.perilla.shared.ontology.schema_proposal", "SchemaProposal"),
    "SchemaPromotionGate": ("core.research.perilla.shared.ontology.schema_promotion_gate", "SchemaPromotionGate"),
    "SchemaEvolutionEngine": ("core.research.perilla.shared.ontology.schema_evolution_engine", "SchemaEvolutionEngine"),
    "OntologyVersion": ("core.research.perilla.shared.ontology.ontology_versioning", "OntologyVersion"),
    "OntologyVersioning": ("core.research.perilla.shared.ontology.ontology_versioning", "OntologyVersioning"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
