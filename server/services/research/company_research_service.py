"""公司研究档案服务层。

P0.4 实现：统一封装 ResearchRunRepository + CompanyValueResearchService，
对外提供只读查询、POST 创建 run、diff 与 latest 等能力。

- 列表分页与详情懒加载：避免默认返回大体积 source_snapshots。
- 错误统一中文摘要 + 稳定错误码 + trace_id。
- 缺数据返回 200 + degraded 字段；仅无效输入才抛 ValueError 由路由转 4xx。

按 docs/superpowers/plans/2026-07-18-institutional-research-p0-trusted-foundation.md
Task P0.4 实施。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.research.value_chain.repository import ResearchRunRepository
from core.research.value_chain.service import CompanyValueResearchService


# A 股代码白名单：6 位数字 + .SH/.SZ/.BJ 后缀；用于严格校验，杜绝路径注入
_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)

# 详情懒加载支持的 section 名
VALID_SECTIONS: tuple[str, ...] = (
    "company_profile", "financial_history", "financial_quality",
    "valuation", "valuation_context", "technical_context",
    "earnings_forecasts", "announcements", "industry_context",
    "profit_bridge", "expectation_context", "research_dimensions",
    "research_assessment", "source_snapshots", "research_readiness",
    "research_facts", "research_assumptions", "degradation_reasons",
)


class CompanyResearchServiceError(Exception):
    """公司研究服务错误；携带中文 message + 稳定 code。"""

    def __init__(self, message: str, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_trace_id() -> str:
    """生成短 trace_id，用于错误追踪。"""
    return "tr_" + uuid.uuid4().hex[:12]


def validate_symbol(symbol: str) -> str:
    """严格校验 A 股代码格式；非法时抛 CompanyResearchServiceError。"""
    if not symbol or not isinstance(symbol, str):
        raise CompanyResearchServiceError("symbol 不能为空", "INVALID_SYMBOL")
    cleaned = symbol.strip().upper()
    if not _SYMBOL_PATTERN.match(cleaned):
        raise CompanyResearchServiceError(
            f"symbol 格式非法：{symbol}；必须为 6 位数字 + .SH/.SZ/.BJ",
            "INVALID_SYMBOL",
        )
    return cleaned


def validate_as_of(as_of: str) -> str:
    """校验 PIT 时点格式（YYYY-MM-DD）。"""
    if not as_of:
        raise CompanyResearchServiceError("as_of 不能为空", "INVALID_AS_OF")
    cleaned = str(as_of).strip()[:10]
    try:
        datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise CompanyResearchServiceError(
            f"as_of 格式非法：{as_of}；必须为 YYYY-MM-DD",
            "INVALID_AS_OF",
        ) from exc
    return cleaned


def validate_lookback_days(lookback_days: int) -> int:
    """校验 lookback_days 范围（30-730 天）。"""
    try:
        value = int(lookback_days)
    except (TypeError, ValueError) as exc:
        raise CompanyResearchServiceError(
            f"lookback_days 必须为整数：{lookback_days}",
            "INVALID_LOOKBACK_DAYS",
        ) from exc
    if value < 30 or value > 730:
        raise CompanyResearchServiceError(
            f"lookback_days 必须在 30-730 之间：{value}",
            "INVALID_LOOKBACK_DAYS",
        )
    return value


def validate_pagination(page: int, page_size: int) -> tuple[int, int]:
    """校验分页参数。"""
    try:
        page_int = max(1, int(page))
    except (TypeError, ValueError) as exc:
        raise CompanyResearchServiceError(
            f"page 必须为正整数：{page}", "INVALID_PAGINATION",
        ) from exc
    try:
        page_size_int = max(1, min(100, int(page_size)))
    except (TypeError, ValueError) as exc:
        raise CompanyResearchServiceError(
            f"page_size 必须为 1-100 之间的整数：{page_size}",
            "INVALID_PAGINATION",
        ) from exc
    return page_int, page_size_int


# API 层禁止暴露的内部字段（包含本机绝对路径或敏感索引元数据）。
# artifact_path 由 ResearchRunRepository 索引保留以供审计，但不应通过 HTTP 响应外泄。
_INTERNAL_FIELDS: tuple[str, ...] = (
    "artifact_path",
    "run_dir",
    "payload",
)


def _strip_internal_paths(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """移除响应记录中的内部路径字段；不修改原始字典。

    Args:
        record: 待返回给客户端的运行索引字典。

    Returns:
        副本，剔除 artifact_path / run_dir / payload 等内部字段后的字典。
    """
    if not record or not isinstance(record, dict):
        return record
    return {k: v for k, v in record.items() if k not in _INTERNAL_FIELDS}


def _parse_bool(value: Any, *, field: str, default: bool = False) -> bool:
    """严格解析布尔值：接受 True/False/"true"/"false"（大小写不敏感）。

    其他类型（含 1/0 数字、任意字符串）一律拒绝，避免 bool("false")==True 的隐式陷阱。
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise CompanyResearchServiceError(
        f"{field} 必须为布尔值 true/false：{value!r}",
        "INVALID_BOOL",
    )


def _parse_int(value: Any, *, field: str, default: int) -> int:
    """解析整数；字符串必须能严格转 int，否则抛错（不返回 500）。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        # bool 是 int 的子类，但此处语义上应拒绝
        raise CompanyResearchServiceError(
            f"{field} 必须为整数：{value!r}", "INVALID_INT",
        )
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        lowered = value.strip()
        if not lowered:
            return default
        try:
            return int(lowered)
        except ValueError as exc:
            raise CompanyResearchServiceError(
                f"{field} 必须为整数：{value!r}", "INVALID_INT",
            ) from exc
    raise CompanyResearchServiceError(
        f"{field} 必须为整数：{value!r}", "INVALID_INT",
    )


def _filter_dossier_sections(
    dossier: dict[str, Any] | None,
    include: list[str] | None,
) -> dict[str, Any] | None:
    """按 include 列表过滤 dossier section；include=None 返回完整 dossier。

    默认剔除 source_snapshots 以避免大体积响应；显式 include 时按需返回。
    """
    if dossier is None:
        return None
    if not include:
        # 默认剔除大体积字段，保留元数据 + readiness
        filtered = dict(dossier)
        filtered.pop("source_snapshots", None)
        filtered.pop("financial_history", None)
        filtered.pop("earnings_forecasts", None)
        filtered.pop("announcements", None)
        return filtered
    invalid = [name for name in include if name not in VALID_SECTIONS]
    if invalid:
        raise CompanyResearchServiceError(
            f"不支持的 section：{','.join(invalid)}；合法 section：{','.join(VALID_SECTIONS)}",
            "INVALID_SECTION",
        )
    return {name: dossier.get(name) for name in include}


class CompanyResearchService:
    """公司研究档案服务层：封装运行创建、查询、diff。"""

    def __init__(
        self,
        *,
        runs_repository: ResearchRunRepository | None = None,
        research_service: CompanyValueResearchService | None = None,
        runs_root: str | Path = "data/research_value_runs",
    ) -> None:
        """初始化服务。

        Args:
            runs_repository: 可选注入 ResearchRunRepository，便于单测替换。
            research_service: 可选注入 CompanyValueResearchService。
            runs_root: 运行产物根目录，用于未注入 repository 时构造默认实例。
        """
        self.runs_root = Path(runs_root)
        self._runs_repository = runs_repository
        self._research_service = research_service

    @property
    def runs_repository(self) -> ResearchRunRepository:
        """惰性创建 ResearchRunRepository。"""
        if self._runs_repository is None:
            self._runs_repository = ResearchRunRepository(runs_root=self.runs_root)
        return self._runs_repository

    @property
    def research_service(self) -> CompanyValueResearchService:
        """惰性创建 CompanyValueResearchService。"""
        if self._research_service is None:
            self._research_service = CompanyValueResearchService(
                runs_repository=self.runs_repository,
            )
        return self._research_service

    # ------------------------------------------------------------------
    # 创建 run
    # ------------------------------------------------------------------

    def create_run(
        self,
        symbol: str,
        *,
        as_of: str,
        lookback_days: int = 120,
        enqueue_gaps: bool = False,
    ) -> dict[str, Any]:
        """创建一次公司研究运行并写入仓储索引。

        Args:
            symbol: A 股代码（6 位数字 + .SH/.SZ/.BJ）。
            as_of: PIT 时点（YYYY-MM-DD）。
            lookback_days: 回看天数，30-730。
            enqueue_gaps: 是否将缺口写入 gap task 队列。

        Returns:
            运行摘要：run_id / status / degradation_reasons。

        Raises:
            CompanyResearchServiceError: 输入非法时抛出。
        """
        cleaned_symbol = validate_symbol(symbol)
        cleaned_as_of = validate_as_of(as_of)
        cleaned_lookback = validate_lookback_days(lookback_days)
        try:
            result = self.research_service.run(
                cleaned_symbol,
                as_of=cleaned_as_of,
                lookback_days=cleaned_lookback,
                output_root=self.runs_root,
                enqueue_gaps=enqueue_gaps,
            )
        except CompanyResearchServiceError:
            raise
        except Exception as exc:
            raise CompanyResearchServiceError(
                f"研究运行执行失败：{type(exc).__name__}: {exc}",
                "RUN_EXECUTION_FAILED",
            ) from exc
        acceptance = result.get("acceptance") or {}
        return {
            "run_id": result["run_id"],
            "symbol": cleaned_symbol,
            "as_of": cleaned_as_of,
            "status": str(acceptance.get("status") or "partial"),
            "degradation_reasons": acceptance.get("degradation_reasons") or [],
            "notice": "研究数据产物，不包含交易动作建议。",
            "trace_id": _new_trace_id(),
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_runs(
        self,
        symbol: str,
        *,
        as_of: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分页列出公司的运行索引。

        Args:
            symbol: A 股代码。
            as_of: 可选 PIT 时点过滤。
            status: 可选状态过滤。
            page: 页码，1-based。
            page_size: 每页大小，1-100。

        Returns:
            分页结果：items / page / page_size / total / total_pages。
        """
        cleaned_symbol = validate_symbol(symbol)
        page_int, page_size_int = validate_pagination(page, page_size)
        result = self.runs_repository.list_runs(
            symbol=cleaned_symbol, as_of=as_of, status=status,
            page=page_int, page_size=page_size_int,
        )
        # 移除每条索引记录中的 artifact_path 等内部字段
        result["items"] = [_strip_internal_paths(item) for item in result.get("items", [])]
        return result

    def get_run(
        self,
        run_id: str,
        *,
        include: list[str] | None = None,
    ) -> dict[str, Any]:
        """读取单个 run 的索引元数据 + dossier + acceptance。

        Args:
            run_id: 运行 ID。
            include: 可选 section 过滤列表；None 时返回精简 dossier。

        Returns:
            bundle 字典 + filtered_dossier + trace_id。

        Raises:
            CompanyResearchServiceError: run 不存在时抛 RUN_NOT_FOUND。
        """
        if not run_id or not isinstance(run_id, str):
            raise CompanyResearchServiceError("run_id 不能为空", "INVALID_RUN_ID")
        bundle = self.runs_repository.read_run_bundle(run_id)
        if bundle["run"] is None:
            raise CompanyResearchServiceError(
                f"运行不存在：{run_id}", "RUN_NOT_FOUND",
            )
        return {
            "run": _strip_internal_paths(bundle["run"]),
            "dossier": _filter_dossier_sections(bundle.get("dossier"), include),
            "acceptance": bundle.get("acceptance"),
            "degraded": bundle["degraded"],
            "degradation_reason": bundle.get("degradation_reason") or "",
            "include": include or [],
            "trace_id": _new_trace_id(),
        }

    def diff_runs(
        self,
        run_id: str,
        *,
        base_run_id: str,
    ) -> dict[str, Any]:
        """比较两个 run 的事实/假设/预期/估值/缺口。

        Args:
            run_id: 当前 run ID（compare）。
            base_run_id: 基线 run ID（base）。

        Returns:
            diff 结果 + trace_id。

        Raises:
            CompanyResearchServiceError: run 不存在时抛 RUN_NOT_FOUND。
        """
        if not run_id or not base_run_id:
            raise CompanyResearchServiceError(
                "run_id 与 base_run_id 都不能为空", "INVALID_RUN_ID",
            )
        base_record = self.runs_repository.get_run(base_run_id)
        if base_record is None:
            raise CompanyResearchServiceError(
                f"基线运行不存在：{base_run_id}", "RUN_NOT_FOUND",
            )
        compare_record = self.runs_repository.get_run(run_id)
        if compare_record is None:
            raise CompanyResearchServiceError(
                f"对比运行不存在：{run_id}", "RUN_NOT_FOUND",
            )
        diff = self.runs_repository.diff_runs(base_run_id, run_id)
        diff["trace_id"] = _new_trace_id()
        return diff

    def get_latest(self, symbol: str) -> dict[str, Any]:
        """读取公司最新 run 的索引元数据 + 精简 dossier。

        不存在时返回 200 + degraded=true，便于前端降级展示。

        Args:
            symbol: A 股代码。

        Returns:
            latest bundle 或 degraded 状态。
        """
        cleaned_symbol = validate_symbol(symbol)
        record = self.runs_repository.latest_run(cleaned_symbol)
        if record is None:
            return {
                "run": None,
                "dossier": None,
                "degraded": True,
                "degradation_reason": "no_runs_for_symbol",
                "symbol": cleaned_symbol,
                "trace_id": _new_trace_id(),
            }
        bundle = self.get_run(record["run_id"])
        # get_run 已剥离 run 内部路径，但 latest 仍可能携带顶层 artifact_path
        bundle["run"] = _strip_internal_paths(bundle.get("run"))
        return bundle

    def stats(self) -> dict[str, Any]:
        """返回仓储统计摘要 + trace_id。"""
        stats = self.runs_repository.stats()
        stats["trace_id"] = _new_trace_id()
        return stats
