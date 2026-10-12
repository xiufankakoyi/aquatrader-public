"""每日截面构建编排：UniverseSnapshotBuilder.build → repo.save。

P2.1 调度层缺口修复：UniverseSnapshotBuilder 与 UniverseSnapshotRepository
均已实现，但生产路径上没有调用方执行 repo.save(snapshot)，导致
discovery-funnel 端点三张业务表全为 0。

本模块提供：
- AllAShareUniverseProvider：生产用 UniverseProvider，基于 akshare
  stock_zh_a_spot_em() 获取全 A 股成分股列表
- build_and_save_universe_snapshot：幂等编排 build → save，同 as_of
  已存在时跳过（除非 force=True）

设计原则：
- KISS：仅做 build → save 编排，不引入额外抽象
- 防御性：akshare 不可用时返回空列表，编排层拒绝保存空截面
- 幂等：同 snapshot_id 已存在时 repo.save 抛 ValueError，本模块捕获并跳过
- 不输出投资建议（AGENTS.md）
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from .contracts import (
    MEMBERSHIP_LISTED,
    _normalize_as_of,
)
from .repository import UniverseSnapshotRepository
from .universe_snapshot import (
    BuilderConfig,
    EmptyFieldGroupAssembler,
    P1BackedFieldGroupAssembler,
    UniverseEntry,
    UniverseSnapshotBuilder,
)

logger = logging.getLogger(__name__)


class AllAShareUniverseProvider:
    """生产用 UniverseProvider：基于 akshare 获取全 A 股成分股。

    通过 ak.stock_zh_a_spot_em() 拉取全市场实时行情，提取 symbol 与
    company_name 作为 UniverseEntry 列表。akshare 不可用时返回空列表，
    由 builder 决定是否排除（防御性降级）。

    不做 PIT 过滤：当前 akshare 接口返回的是实时快照，对历史 as_of 不
    可重放。生产场景下 as_of 应为当日，由调用方保证。
    """

    def __init__(
        self,
        *,
        exclude_ke: bool = True,
        local_query: Any | None = None,
    ) -> None:
        """初始化。

        Args:
            exclude_ke: 是否排除北交所（KE/BJ 开头），默认 True。
        """
        self.exclude_ke = bool(exclude_ke)
        self._local_query = local_query
        self.last_provider = ""
        self.last_source_as_of = ""

    def list_universe(self, as_of: str) -> list[UniverseEntry]:
        """返回全 A 股成分股列表。

        Args:
            as_of: 时点 YYYY-MM-DD（仅用于标注，实际返回实时数据）。

        Returns:
            UniverseEntry 列表；akshare 不可用时返回空列表。
        """
        frame = None
        try:
            import akshare as _ak  # type: ignore[import-not-found]
        except ImportError:
            logger.warning("akshare 未安装，回退本地 PIT 日线股票池")
        else:
            try:
                frame = _ak.stock_zh_a_spot_em()
            except Exception as exc:  # noqa: BLE001 — 外部源失败降级
                logger.warning(
                    "akshare stock_zh_a_spot_em 调用失败: %s",
                    exc,
                )

        if _frame_has_rows(frame):
            entries = self._entries_from_frame(frame)
            if entries:
                self.last_provider = "akshare_stock_zh_a_spot_em"
                self.last_source_as_of = _normalize_as_of(as_of)
                return entries
        logger.warning("实时证券主表不可用，回退本地 PIT 日线股票池")
        return self._local_entries(as_of)

    def _entries_from_frame(self, df: Any) -> list[UniverseEntry]:
        """Normalize a provider frame without inferring missing names."""

        entries: list[UniverseEntry] = []
        if not hasattr(df, "columns"):
            return entries
        # 列名兼容：akshare 返回中文列名，部分版本可能返回英文
        code_col = _pick_column(
            df,
            ["代码", "code", "symbol", "stock_code", "ts_code"],
        )
        name_col = _pick_column(df, ["名称", "name", "stock_name"])
        if code_col is None:
            logger.warning("证券主表缺少代码列")
            return []

        for row in _iter_frame_rows(df):
            try:
                raw_value = row.get(code_col) if isinstance(row, Mapping) else row[code_col]
            except (IndexError, KeyError, TypeError):
                continue
            raw_code = "" if raw_value is None else str(raw_value).strip()
            if not raw_code:
                continue
            # 提取纯数字代码（akshare 通常返回 6 位数字字符串）
            symbol_digits = "".join(ch for ch in raw_code if ch.isdigit())
            if len(symbol_digits) != 6:
                continue
            # 拼接市场后缀（SH/SZ/BJ）
            symbol = _normalize_symbol_with_suffix(symbol_digits)
            if self.exclude_ke and symbol.endswith(".BJ"):
                continue
            company_name = ""
            if name_col:
                try:
                    name_value = row.get(name_col) if isinstance(row, Mapping) else row[name_col]
                except (IndexError, KeyError, TypeError):
                    name_value = None
                if name_value is not None and str(name_value).lower() != "nan":
                    company_name = str(name_value).strip()
            entries.append(UniverseEntry(
                symbol=symbol,
                company_name=company_name,
                membership_status=MEMBERSHIP_LISTED,
                listing_date="",
                delisting_date="",
            ))

        return entries

    def _local_entries(self, as_of: str) -> list[UniverseEntry]:
        """Read the latest local daily pool no later than ``as_of``."""
        try:
            query = self._local_query
            if query is None:
                from data_svc.unified_data_query import (
                    get_unified_data_query,
                )

                query = get_unified_data_query()
                self._local_query = query
            cutoff = _normalize_as_of(as_of)
            start = (
                date.fromisoformat(cutoff) - timedelta(days=14)
            ).isoformat()
            trade_dates = [
                str(item)[:10]
                for item in query.get_trade_dates(start, cutoff)
                if str(item)[:10] <= cutoff
            ]
            if not trade_dates:
                return []
            source_as_of = max(trade_dates)
            frame = query.get_stock_pool(source_as_of)
            if not _frame_has_rows(frame):
                return []
            entries = self._entries_from_frame(frame)
            if entries:
                self.last_provider = "local_daily_ohlcv"
                self.last_source_as_of = source_as_of
                logger.info(
                    "AllAShareUniverseProvider 使用本地 PIT 日线池: "
                    "as_of=%s source_as_of=%s members=%d",
                    cutoff,
                    source_as_of,
                    len(entries),
                )
            return entries
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.warning("本地 PIT 日线股票池读取失败: %s", exc)
            return []


def _pick_column(df: Any, candidates: list[str]) -> str | None:
    """从 DataFrame 中按候选列名顺序找到第一个存在的列。

    Args:
        df: pandas DataFrame。
        candidates: 候选列名列表。

    Returns:
        匹配的列名；无匹配返回 None。
    """
    columns = set(df.columns)
    for name in candidates:
        if name in columns:
            return name
    return None


def _frame_has_rows(frame: Any) -> bool:
    """Return whether a pandas/Polars-like frame contains rows."""
    if frame is None:
        return False
    height = getattr(frame, "height", None)
    if height is not None:
        try:
            return int(height) > 0
        except (TypeError, ValueError, OverflowError):
            return False
    empty = getattr(frame, "empty", None)
    if empty is not None:
        try:
            return not bool(empty)
        except (TypeError, ValueError):
            return False
    try:
        return len(frame) > 0
    except (TypeError, ValueError):
        return False


def _iter_frame_rows(frame: Any) -> Any:
    """Iterate rows without pandas ``iterrows`` when a faster API exists."""
    iter_rows = getattr(frame, "iter_rows", None)
    if callable(iter_rows):
        try:
            return iter_rows(named=True)
        except TypeError:
            return iter_rows()

    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict(orient="records")
        except TypeError:
            pass

    iterrows = getattr(frame, "iterrows", None)
    if callable(iterrows):
        return (row for _, row in iterrows())
    return ()


def _normalize_symbol_with_suffix(digits: str) -> str:
    """根据 6 位数字代码推断市场后缀。

    Args:
        digits: 6 位股票代码（纯数字）。

    Returns:
        带 .SH/.SZ/.BJ 后缀的代码。
    """
    if digits.startswith(("60", "68", "90", "11", "13", "50", "51", "56", "58")):
        return f"{digits}.SH"
    if digits.startswith(("00", "30", "20", "12", "14", "15", "16", "17", "18")):
        return f"{digits}.SZ"
    if digits.startswith(("43", "83", "87", "88")):
        return f"{digits}.BJ"
    # 默认归深交所（A 股绝大多数以 0/3 开头）
    return f"{digits}.SZ"


def build_and_save_universe_snapshot(
    as_of: str,
    *,
    force: bool = False,
    provider: Any | None = None,
    assembler: Any | None = None,
    repo: UniverseSnapshotRepository | None = None,
) -> dict[str, Any]:
    """幂等构建并保存 universe snapshot。

    流程：
    1. 检查同 as_of 是否已存在 snapshot；存在且 force=False 时跳过
    2. 调用 UniverseSnapshotBuilder.build(as_of) 构建截面
    3. 调用 repo.save(snapshot) 持久化（同 snapshot_id 已存在时捕获 ValueError）

    Args:
        as_of: 截面时点 YYYY-MM-DD。
        force: True 时即使 as_of 已有 snapshot 也重新构建（仍受 repo.save
            不可覆盖约束，需先手动删除旧 snapshot）。
        provider: 自定义 UniverseProvider；None 使用 AllAShareUniverseProvider。
        assembler: 自定义 FieldGroupAssembler；None 使用 P1BackedFieldGroupAssembler
            （P1 仓储不可用时降级为 EmptyFieldGroupAssembler）。
        repo: 自定义 UniverseSnapshotRepository；None 使用默认实例。

    Returns:
        dict: {"status": "ok"|"skipped"|"failed", "snapshot_id": str,
               "member_count": int, "exclusion_count": int, "reason": str}
    """
    cutoff = _normalize_as_of(as_of)
    repository = repo or UniverseSnapshotRepository()

    # 幂等检查：同 as_of 已存在则跳过（除非 force=True）
    if not force:
        try:
            existing = repository.get_by_as_of(cutoff, universe_name="all_a_share")
            if (
                existing is not None
                and _normalize_as_of(getattr(existing, "as_of", "")) == cutoff
            ):
                return {
                    "status": "skipped",
                    "snapshot_id": existing.snapshot_id,
                    "member_count": len(existing.members),
                    "exclusion_count": len(existing.exclusions),
                    "reason": f"snapshot already exists for as_of={cutoff}",
                }
        except Exception as exc:  # noqa: BLE001 — 防御性：查询失败继续构建
            logger.warning("检查已有 snapshot 失败，继续构建: %s", exc)

    # 选择 provider
    universe_provider = provider or AllAShareUniverseProvider()

    # 选择 assembler：优先 P1BackedFieldGroupAssembler，降级为 Empty
    field_assembler = assembler
    if field_assembler is None:
        try:
            field_assembler = P1BackedFieldGroupAssembler()
        except Exception as exc:  # noqa: BLE001 — P1 仓储不可用时降级
            logger.warning("P1BackedFieldGroupAssembler 初始化失败，降级为 Empty: %s", exc)
            field_assembler = EmptyFieldGroupAssembler()

    config = BuilderConfig(
        universe_name="all_a_share",
        universe_version="v1",
        policy_version="p2.1.0",
        provider="resilient_a_share_universe",
        provider_version="akshare_then_local_daily_ohlcv_v1",
        built_by="scheduled_job",
    )

    builder = UniverseSnapshotBuilder(
        provider=universe_provider,
        assembler=field_assembler,
        config=config,
    )

    try:
        snapshot = builder.build(cutoff)
    except Exception as exc:  # noqa: BLE001 — 防御性：构建失败不抛异常
        logger.error("universe snapshot 构建失败: %s", exc)
        return {
            "status": "failed",
            "snapshot_id": "",
            "member_count": 0,
            "exclusion_count": 0,
            "reason": f"build_failed: {type(exc).__name__}: {exc}",
        }

    if not snapshot.members:
        return {
            "status": "failed",
            "snapshot_id": snapshot.snapshot_id,
            "member_count": 0,
            "exclusion_count": len(snapshot.exclusions),
            "reason": "empty_universe",
        }

    try:
        repository.save(snapshot)
    except ValueError as exc:
        # 同 snapshot_id 已存在（不可覆盖约束）
        logger.info("snapshot 已存在，跳过保存: %s", exc)
        return {
            "status": "skipped",
            "snapshot_id": snapshot.snapshot_id,
            "member_count": len(snapshot.members),
            "exclusion_count": len(snapshot.exclusions),
            "reason": f"snapshot_id already exists: {snapshot.snapshot_id}",
        }
    except Exception as exc:  # noqa: BLE001 — 防御性：保存失败不抛异常
        logger.error("universe snapshot 保存失败: %s", exc)
        return {
            "status": "failed",
            "snapshot_id": snapshot.snapshot_id,
            "member_count": len(snapshot.members),
            "exclusion_count": len(snapshot.exclusions),
            "reason": f"save_failed: {type(exc).__name__}: {exc}",
        }

    logger.info(
        "universe snapshot 构建保存成功: snapshot_id=%s members=%d exclusions=%d",
        snapshot.snapshot_id, len(snapshot.members), len(snapshot.exclusions),
    )
    return {
        "status": "ok",
        "snapshot_id": snapshot.snapshot_id,
        "member_count": len(snapshot.members),
        "exclusion_count": len(snapshot.exclusions),
        "reason": "ok",
    }
