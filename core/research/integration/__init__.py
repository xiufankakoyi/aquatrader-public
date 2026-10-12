"""P1.10 真实数据接入桥接层子包。

从 tushare / akshare 取真实数据并填充到 P1.1-P1.8 已实现的领域仓储，
使 CfaReportBuilder 能在真实数据下生成完整 CFA 报告。

公共导出：
- P1DataLoader：真实数据加载器
- SyntheticDataFactory：合成数据工厂（用于 50 公司认证冒烟测试）
- LoadResult：加载结果统计
"""
from __future__ import annotations

from .p1_data_loader import (
    P1DataLoader,
    SyntheticDataFactory,
    LoadResult,
    COMPANY_SAMPLE,
    INDUSTRY_DISTRIBUTION,
)
from .annual_report_operating_model import AnnualReportOperatingModelStore

__all__ = [
    "P1DataLoader",
    "SyntheticDataFactory",
    "LoadResult",
    "COMPANY_SAMPLE",
    "INDUSTRY_DISTRIBUTION",
    "AnnualReportOperatingModelStore",
]
