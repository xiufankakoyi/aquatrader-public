"""P3.9 大规模历史 PIT 认证工具包。

按 P3 计划 Task P3.9：
- 至少 300 家公司、20 个行业、5 年 PIT 可见历史
- 覆盖上行、下行和震荡市场状态
- 硬门槛：无未来泄漏、无选择性删除、使用当时有效版本、
  同时显示成功与失败、样本不足时明确标注、独立复核人签署

本包提供 PIT 认证工具框架，支持未来真实运行。
"""
from __future__ import annotations

from core.research.pit_certification.contracts import (
    CertificationStatus,
    CertificationUniverse,
    CompanyCertificationMember,
    IndependentSignOff,
    MarketState,
    PITCertificationReport,
    PITCertificationResult,
    PITCertificationScope,
    PITErrorAttribution,
    PITLeakageCheck,
    compute_results_hash,
    utc_now,
)
from core.research.pit_certification.error_attribution import (
    ErrorAttributionResult,
    attribute_error,
)
from core.research.pit_certification.leakage_checker import check_future_leakage
from core.research.pit_certification.market_state_classifier import (
    classify_market_state,
)
from core.research.pit_certification.pit_runner import (
    run_pit_certification,
)
from core.research.pit_certification.report_generator import (
    generate_certification_report,
    sign_certification_report,
)
from core.research.pit_certification.universe_builder import (
    build_certification_universe,
)

__all__ = [
    "CertificationStatus",
    "CertificationUniverse",
    "CompanyCertificationMember",
    "ErrorAttributionResult",
    "IndependentSignOff",
    "MarketState",
    "PITCertificationReport",
    "PITCertificationResult",
    "PITCertificationScope",
    "PITErrorAttribution",
    "PITLeakageCheck",
    "attribute_error",
    "build_certification_universe",
    "check_future_leakage",
    "classify_market_state",
    "compute_results_hash",
    "generate_certification_report",
    "run_pit_certification",
    "sign_certification_report",
    "utc_now",
]
