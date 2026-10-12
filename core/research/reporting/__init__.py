"""P1.9 CFA 风格研究报告子包。

导出契约、构建器与仓储：
- 契约: ReportSection / ReportSectionContent / CfaReport /
        SECTION_TITLES / SECTION_STATUSES / OVERALL_STATUSES /
        NOTICE / compute_overall_status / empty_section
- 构建器: CfaReportBuilder
- 仓储: ReportRepository
"""
from .contracts import (
    DEGRADED_MISSING_RATIO_THRESHOLD,
    NOTICE,
    OVERALL_STATUS_DEGRADED,
    OVERALL_STATUS_OK,
    OVERALL_STATUS_PARTIAL,
    OVERALL_STATUSES,
    ReportSection,
    ReportSectionContent,
    SECTION_STATUS_MISSING,
    SECTION_STATUS_NOT_APPLICABLE,
    SECTION_STATUS_OK,
    SECTION_STATUS_PARTIAL,
    SECTION_STATUSES,
    SECTION_TITLES,
    CfaReport,
    compute_overall_status,
    empty_section,
    new_id,
    utc_now,
)
from .cfa_report_builder import CfaReportBuilder, ReportRepository

__all__ = [
    # 契约
    "CfaReport",
    "DEGRADED_MISSING_RATIO_THRESHOLD",
    "NOTICE",
    "OVERALL_STATUS_DEGRADED",
    "OVERALL_STATUS_OK",
    "OVERALL_STATUS_PARTIAL",
    "OVERALL_STATUSES",
    "ReportSection",
    "ReportSectionContent",
    "SECTION_STATUS_MISSING",
    "SECTION_STATUS_NOT_APPLICABLE",
    "SECTION_STATUS_OK",
    "SECTION_STATUS_PARTIAL",
    "SECTION_STATUSES",
    "SECTION_TITLES",
    "compute_overall_status",
    "empty_section",
    "new_id",
    "utc_now",
    # 构建器与仓储
    "CfaReportBuilder",
    "ReportRepository",
]
