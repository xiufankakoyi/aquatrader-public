from __future__ import annotations

import hashlib
from typing import Any

from core.research.control_plane.contracts import ResearchTask, TaskStatus
from core.research.control_plane.repository import ResearchRepository


# ---------------------------------------------------------------------------
# P2.4：阻断盈利模型的缺口类型
# 按 P2.4 §170「优先补阻断盈利模型的证据，而不是泛搜更多新闻」：
# 以下缺口直接阻断 P1.3 经营模型与 P1.7 估值引擎运行，
# 在 enqueue 时标记 model_blocking=True，由 TopKResearchScheduler 优先调度。
# ---------------------------------------------------------------------------

MODEL_BLOCKING_GAPS: frozenset[str] = frozenset(
    {
        "verified_causal_chain_missing",
        "forward_profit_bridge_missing",
        "company_forward_driver_evidence_missing",
    }
)


class GapTaskPlanner:
    """Create deterministic idle-research tasks from validated dossier gaps."""

    TASK_SPECS = {
        "verified_causal_chain_missing": (
            "ontology_missing", "验证公司产品、上下游对象与有向产业因果关系",
        ),
        "causal_chain_depth_insufficient": (
            "evidence_missing", "补充产品、上游约束、下游客户/场景与需求催化的完整因果链",
        ),
        "forward_profit_bridge_missing": (
            "evidence_missing", "补充产品、产能、价格、份额到利润的前瞻驱动证据",
        ),
        "company_forward_driver_evidence_missing": (
            "evidence_missing", "补充公司披露的未来产能、价格、销量、份额与成本驱动证据",
        ),
        "verified_market_expectation_missing": (
            "evidence_missing", "补充可追溯的一致预期、公司指引与隐含预期证据",
        ),
    }

    def __init__(self, repository: ResearchRepository | None = None) -> None:
        self.repository = repository or ResearchRepository()

    @staticmethod
    def _task_id(symbol: str, gap: str, as_of: str) -> str:
        digest = hashlib.sha256(f"{symbol}|{gap}|{as_of}".encode("utf-8")).hexdigest()[:20]
        return f"rt_value_{digest}"

    @staticmethod
    def is_model_blocking(gap: str) -> bool:
        """判断缺口是否阻断盈利模型（P2.4 §170）。

        Args:
            gap: 缺口类型

        Returns:
            True 表示该缺口阻断 P1.3/P1.7 模型运行，需优先补证
        """
        return gap in MODEL_BLOCKING_GAPS

    def enqueue(
        self,
        *,
        symbol: str,
        company_name: str,
        as_of: str,
        gaps: list[str],
    ) -> dict[str, list[str]]:
        created: list[str] = []
        existing: list[str] = []
        raw_gaps = gaps if isinstance(gaps, (list, tuple, set)) else ()
        for gap in dict.fromkeys(raw_gaps):
            if not isinstance(gap, str):
                continue
            spec = self.TASK_SPECS.get(gap)
            if spec is None:
                continue
            task_id = self._task_id(symbol, gap, as_of)
            if self.repository.get_task(task_id) is not None:
                existing.append(task_id)
                continue
            task_type, objective = spec
            model_blocking = self.is_model_blocking(gap)
            task = ResearchTask(
                task_id=task_id,
                task_type=task_type,
                trigger_source="company_value_chain",
                priority=0.8 if gap == "verified_causal_chain_missing" else 0.7,
                status=TaskStatus.PARTIAL.value,
                failure={
                    "code": "GAP_DISCOVERY_DISPATCHER_PENDING",
                    "message": "等待专用认知缺口发现执行器；不得交给 Claim 补证执行器",
                },
                progress={"percent": 0, "message": "等待认知缺口发现执行器"},
                params={
                    "research_mode": "idle_learning",
                    "target_entity": symbol,
                    "company_name": company_name,
                    "as_of": as_of,
                    "gap": gap,
                    "objective": objective,
                    "required_output": "grounded_claims_with_source_trace",
                    "allow_theme_membership_as_causal": False,
                    "required_dispatcher": "gap_discovery",
                    # P2.4：标记是否阻断盈利模型，供 TopKResearchScheduler 优先调度
                    "model_blocking": model_blocking,
                },
            )
            self.repository.save_task(task)
            created.append(task_id)
        return {"created_task_ids": created, "existing_task_ids": existing}
