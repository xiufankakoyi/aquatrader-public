"""AquaTrade research pipelines."""

__all__ = [
    "QuantFlowPipeline",
    "ResearchDataEnginePipeline",
    "SingleStockResearchPipeline",
]


def __getattr__(name: str):
    if name == "QuantFlowPipeline":
        from .quant_flow_pipeline import QuantFlowPipeline

        return QuantFlowPipeline
    if name == "ResearchDataEnginePipeline":
        from .research_data_engine_pipeline import ResearchDataEnginePipeline

        return ResearchDataEnginePipeline
    if name == "SingleStockResearchPipeline":
        from .single_stock_research_pipeline import main as SingleStockResearchPipeline

        return SingleStockResearchPipeline
    raise AttributeError(name)
