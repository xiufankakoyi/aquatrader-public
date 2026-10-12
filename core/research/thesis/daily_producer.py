"""每日差异化观点生产入口。

只有真实 P1 经营模型与市场共识能够形成 variant perception。主题成员关系
不能充当内部经营模型，因此数据未就绪时保持显式降级。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .repository import ThesisRepository
    from .variant_perception import VariantPerceptionBuilder


def produce_daily_variant_perceptions(
    trade_date: str,
    *,
    research_repo: Any | None = None,
    expectation_repo: Any | None = None,
    thesis_repo: ThesisRepository | None = None,
    builder: VariantPerceptionBuilder | None = None,
) -> dict[str, Any]:
    """缺少真实 P1 经营模型时不构建或激活 thesis。"""
    del trade_date, research_repo, expectation_repo, thesis_repo, builder
    return {
        "status": "skipped",
        "thesis_count": 0,
        "skipped_no_consensus": 0,
        "skipped_existing": 0,
        "reason": "missing_real_p1_operating_model",
    }
