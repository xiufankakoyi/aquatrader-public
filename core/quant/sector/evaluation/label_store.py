"""未来表现标签 SQLite 持久化存储。

标签只在 evaluation 层生成（事实发生后），不回写事实层（不修改
SectorFeatureRecord 或 SectorAssessment）。标签与 Alpha 反馈分域保存，
不修改 Claim 事实置信度。

生成时机规则（P2.4）：
- horizon_days=5 的标签只能在 as_of + 5 个交易日后生成
- generated_at 为空或无法解析时拒绝生成成熟标签
- generated_at 必须晚于 as_of + horizon_days 个交易日（防止未来函数）
- 未注入 trading_calendar_provider 时回退到日历日（仅用于测试，生产必须注入）
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from .contracts import EvaluationLabel

logger = logging.getLogger(__name__)


# 交易日历提供器类型：返回 start_date 到 end_date 之间的交易日列表（含端点）
# P2.4: horizon 使用交易日历，不得用日历日代替
TradingCalendarProvider = Callable[[str, str], list[str]]


def _normalize_as_of(value: str) -> str:
    """将日期字符串归一化为 YYYYMMDD 格式。"""
    return str(value or "").replace("-", "").replace(":", "").replace("T", "")[:8]


def _parse_date(value: str) -> datetime | None:
    """解析 YYYYMMDD 或 ISO 日期为 datetime 对象。"""
    normalized = _normalize_as_of(value)
    if not normalized or len(normalized) < 8:
        return None
    try:
        return datetime.strptime(normalized[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class EvaluationLabelStore:
    """未来表现标签 SQLite 存储。

    标签只在 evaluation 层生成，不回写事实层。所有写操作通过 RLock 串行化。

    P2.4: generated_at 为空或无法解析时拒绝生成成熟标签；horizon 使用交易日历。

    阶段5 PIT 确认（Task 5.2）：
    - 标签只在事实发生后生成（as_of + horizon_days 个交易日之后），禁止未来函数。
    - 标签只进入 Alpha feedback 通道，不反向修改公司业务事实或 Claim 状态。
    - 标签与 SectorFeatureRecord 分表存储，不回写事实层字段。
    """

    SCHEMA_VERSION = 1
    DEFAULT_DB_PATH = "data/theme_control/sector_evaluation_labels.db"

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        trading_calendar_provider: TradingCalendarProvider | None = None,
    ) -> None:
        """初始化存储并执行 schema 迁移。

        Args:
            db_path: 数据库路径，默认为 data/theme_control/sector_evaluation_labels.db。
            trading_calendar_provider: P2.4 交易日历提供器，返回 start_date 到
                end_date 之间的交易日列表。None 时回退到日历日（仅用于测试，
                生产环境必须注入真实交易日历）。
        """
        self.db_path = Path(db_path or self.DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._trading_calendar_provider = trading_calendar_provider
        self._ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """获取数据库连接上下文，启用 WAL 模式。"""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """创建表与索引（幂等）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sector_evaluation_labels ("
                "label_id TEXT PRIMARY KEY, "
                "as_of TEXT NOT NULL, "
                "sector_id TEXT NOT NULL, "
                "horizon_days INTEGER NOT NULL DEFAULT 5, "
                "forward_return REAL NOT NULL DEFAULT 0.0, "
                "forward_max_drawdown REAL NOT NULL DEFAULT 0.0, "
                "forward_volatility REAL NOT NULL DEFAULT 0.0, "
                "benchmark_return REAL NOT NULL DEFAULT 0.0, "
                "excess_return REAL NOT NULL DEFAULT 0.0, "
                "label_version TEXT NOT NULL DEFAULT 'evaluation_v1', "
                "generated_at TEXT NOT NULL DEFAULT '')"
            )
            # (as_of, sector_id, horizon_days, label_version) 唯一索引
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_label_unique "
                "ON sector_evaluation_labels(as_of, sector_id, horizon_days, label_version)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_label_as_of "
                "ON sector_evaluation_labels(as_of)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_label_sector_id "
                "ON sector_evaluation_labels(sector_id)"
            )

    def _compute_earliest_generate_date(
        self, as_of: str, horizon_days: int,
    ) -> str | None:
        """P2.4: 计算 generated_at 的最早允许日期（基于交易日历）。

        使用交易日历计算 as_of + horizon_days 个交易日后的日期。
        未注入 trading_calendar_provider 时回退到日历日（仅用于测试）。

        Args:
            as_of: PIT 时点（YYYYMMDD）。
            horizon_days: 预测周期（交易日数）。

        Returns:
            最早允许的 generated_at 日期（YYYYMMDD）；计算失败时返回 None。
        """
        as_of_normalized = _normalize_as_of(as_of)
        if not as_of_normalized:
            return None
        as_of_dt = _parse_date(as_of_normalized)
        if as_of_dt is None:
            return None

        if self._trading_calendar_provider is not None:
            # P2.4: 使用交易日历计算 horizon
            # 提供一个足够大的窗口以确保有足够的交易日
            end_date = (as_of_dt + timedelta(days=horizon_days * 3 + 30)).strftime("%Y%m%d")
            try:
                trading_days = self._trading_calendar_provider(as_of_normalized, end_date)
                # 过滤出 as_of 之后的交易日
                future_trading_days = [
                    d for d in trading_days
                    if _normalize_as_of(d) > as_of_normalized
                ]
                # 取第 horizon_days 个交易日
                if len(future_trading_days) >= horizon_days:
                    return _normalize_as_of(future_trading_days[horizon_days - 1])
                # 交易日不足，返回最后一个交易日
                if future_trading_days:
                    return _normalize_as_of(future_trading_days[-1])
            except Exception as exc:
                logger.warning("交易日历查询失败，回退到日历日: %s", exc)

        # 回退：日历日（仅用于测试，生产必须注入交易日历）
        earliest_dt = as_of_dt + timedelta(days=horizon_days)
        return earliest_dt.strftime("%Y%m%d")

    def save(self, label: EvaluationLabel) -> bool:
        """保存未来表现标签。

        P2.4 生成时机规则：
        - generated_at 为空或无法解析时拒绝生成成熟标签
        - generated_at 必须不早于 as_of + horizon_days 个交易日（防止未来函数）
        - 同一 (as_of, sector_id, horizon_days, label_version) 唯一，重复幂等跳过

        Args:
            label: 未来表现标签。

        Returns:
            True 表示新写入；False 表示因 PIT 违规、generated_at 无效或重复而跳过。

        阶段5 确认：标签只进入 Alpha feedback，不反向修改 Claim 事实置信度，
        也不回写 SectorFeatureRecord 事实层字段。
        """
        normalized_as_of = _normalize_as_of(label.as_of)
        if not normalized_as_of:
            logger.warning("保存评价标签失败：as_of 为空")
            return False

        # P2.4: generated_at 为空时拒绝生成成熟标签
        if not label.generated_at or not str(label.generated_at).strip():
            logger.warning("保存评价标签被拒绝：generated_at 为空")
            return False

        # P2.4: generated_at 无法解析时拒绝生成成熟标签
        as_of_dt = _parse_date(label.as_of)
        generated_dt = _parse_date(label.generated_at)
        if as_of_dt is None:
            logger.warning("保存评价标签失败：as_of 解析失败 %s", label.as_of)
            return False
        if generated_dt is None:
            logger.warning(
                "保存评价标签被拒绝：generated_at 无法解析 %s", label.generated_at,
            )
            return False

        # P2.4: generated_at 必须不早于 as_of + horizon_days 个交易日
        earliest_generate_str = self._compute_earliest_generate_date(
            label.as_of, label.horizon_days,
        )
        if earliest_generate_str is not None:
            earliest_generate_dt = _parse_date(earliest_generate_str)
            if earliest_generate_dt is not None and generated_dt < earliest_generate_dt:
                logger.warning(
                    "保存评价标签被拒绝：generated_at=%s 早于 as_of+%d 交易日=%s（未来函数）",
                    label.generated_at, label.horizon_days,
                    earliest_generate_str,
                )
                return False

        try:
            with self._lock, self._connect() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM sector_evaluation_labels "
                    "WHERE as_of=? AND sector_id=? AND horizon_days=? AND label_version=?",
                    (normalized_as_of, label.sector_id, label.horizon_days, label.label_version),
                ).fetchone()
                if existing:
                    return False
                conn.execute(
                    "INSERT INTO sector_evaluation_labels ("
                    "label_id, as_of, sector_id, horizon_days, forward_return, "
                    "forward_max_drawdown, forward_volatility, benchmark_return, "
                    "excess_return, label_version, generated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        label.label_id,
                        normalized_as_of,
                        label.sector_id,
                        label.horizon_days,
                        label.forward_return,
                        label.forward_max_drawdown,
                        label.forward_volatility,
                        label.benchmark_return,
                        label.excess_return,
                        label.label_version,
                        label.generated_at,
                    ),
                )
            return True
        except Exception as exc:
            logger.warning("保存评价标签失败: %s", exc)
            return False

    @staticmethod
    def _row_to_label(row: sqlite3.Row) -> EvaluationLabel:
        """将数据库行转换为 EvaluationLabel 实例。"""
        return EvaluationLabel(
            label_id=row["label_id"],
            as_of=row["as_of"],
            sector_id=row["sector_id"],
            horizon_days=row["horizon_days"],
            forward_return=row["forward_return"],
            forward_max_drawdown=row["forward_max_drawdown"],
            forward_volatility=row["forward_volatility"],
            benchmark_return=row["benchmark_return"],
            excess_return=row["excess_return"],
            label_version=row["label_version"],
            generated_at=row["generated_at"],
        )

    def get_labels_at(
        self,
        as_of: str,
        sector_id: str = "",
        horizon_days: int = 0,
    ) -> list[EvaluationLabel]:
        """查询某 PIT 时点的评价标签。

        Args:
            as_of: 请求的 PIT 时点（YYYYMMDD 或 ISO 日期）。
            sector_id: 可选的板块 ID 过滤。
            horizon_days: 可选的预测周期过滤（0 表示全部）。

        Returns:
            评价标签列表。
        """
        normalized = _normalize_as_of(as_of)
        if not normalized:
            return []
        with self._lock, self._connect() as conn:
            if sector_id and horizon_days > 0:
                rows = conn.execute(
                    "SELECT * FROM sector_evaluation_labels "
                    "WHERE as_of=? AND sector_id=? AND horizon_days=? "
                    "ORDER BY label_version",
                    (normalized, sector_id, horizon_days),
                ).fetchall()
            elif sector_id:
                rows = conn.execute(
                    "SELECT * FROM sector_evaluation_labels "
                    "WHERE as_of=? AND sector_id=? "
                    "ORDER BY horizon_days, label_version",
                    (normalized, sector_id),
                ).fetchall()
            elif horizon_days > 0:
                rows = conn.execute(
                    "SELECT * FROM sector_evaluation_labels "
                    "WHERE as_of=? AND horizon_days=? "
                    "ORDER BY sector_id, label_version",
                    (normalized, horizon_days),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sector_evaluation_labels "
                    "WHERE as_of=? ORDER BY sector_id, horizon_days",
                    (normalized,),
                ).fetchall()
        return [self._row_to_label(row) for row in rows]

    def list_dates(self) -> list[str]:
        """列出所有已持久化的日期（按升序）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT as_of FROM sector_evaluation_labels ORDER BY as_of ASC"
            ).fetchall()
        return [row["as_of"] for row in rows]
