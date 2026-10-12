"""P2.9 评估 SQLite 仓储。

按 P2 计划 §301-323 Task P2.9：
- 持久化 ForecastScorecard（含 FrozenForecastInput + ActualOutcome + ErrorAttribution）
- UNIQUE(scorecard_id) 不可覆盖（不反向修改研究记录）
- 冻结输入哈希校验（防篡改）
- PIT 过滤：actual.reported_at > forecast.as_of（无未来泄漏）
- 线程安全：WAL + RLock
- 幂等迁移
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .contracts import (
    ACTUAL_OUTCOME_STATUSES,
    ActualOutcome,
    ErrorAttribution,
    ForecastScorecard,
    FrozenForecastInput,
    METRICS,
    MARKET_REGIMES,
)


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------

ERR_SCORECARD_EXISTS: str = "scorecard_exists"
ERR_FUTURE_LEAKAGE: str = "future_leakage"
ERR_FROZEN_INPUT_TAMPERED: str = "frozen_input_tampered"


SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------


class EvaluationRepository:
    """评估 SQLite 仓储。

    持久化 ForecastScorecard，确保：
    1. 不可覆盖（UNIQUE scorecard_id）
    2. 冻结输入防篡改
    3. PIT 无未来泄漏
    4. 线程安全（WAL + RLock）
    """

    def __init__(self, db_path: str | Path) -> None:
        """初始化仓储。

        Args:
            db_path: SQLite 数据库路径
        """
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # ------------------------------------------------------------------
    # 连接与迁移
    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """打开连接。"""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def _migrate(self) -> None:
        """幂等迁移。"""
        with self._lock, self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS evaluation_scorecards("
                "scorecard_id TEXT PRIMARY KEY NOT NULL,"
                "forecast_id TEXT NOT NULL,"
                "company_symbol TEXT NOT NULL DEFAULT '',"
                "period TEXT NOT NULL DEFAULT '',"
                "metric TEXT NOT NULL DEFAULT '',"
                "forecast_source TEXT NOT NULL DEFAULT '',"
                "forecast_as_of TEXT NOT NULL DEFAULT '',"
                "reported_at TEXT NOT NULL DEFAULT '',"
                "industry TEXT NOT NULL DEFAULT '',"
                "market_regime TEXT NOT NULL DEFAULT '',"
                "evaluator TEXT NOT NULL DEFAULT '',"
                "evaluation_as_of TEXT NOT NULL DEFAULT '',"
                "input_hash TEXT NOT NULL DEFAULT '',"
                "payload_json TEXT NOT NULL,"
                "created_at TEXT NOT NULL DEFAULT ''"
                ")"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_scorecards_company "
                "ON evaluation_scorecards(company_symbol, period, metric)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_scorecards_forecast "
                "ON evaluation_scorecards(forecast_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_scorecards_regime "
                "ON evaluation_scorecards(industry, market_regime)"
            )

            # 幂等添加列（防御性）
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(evaluation_scorecards)"
                ).fetchall()
            }
            for col, default in (
                ("evaluator", "''"),
                ("evaluation_as_of", "''"),
                ("input_hash", "''"),
            ):
                if col not in cols:
                    conn.execute(
                        f"ALTER TABLE evaluation_scorecards ADD COLUMN {col} "
                        f"TEXT NOT NULL DEFAULT {default}"
                    )

            conn.execute(
                "CREATE TABLE IF NOT EXISTS evaluation_schema_meta("
                "key TEXT PRIMARY KEY NOT NULL,"
                "value TEXT NOT NULL"
                ")"
            )
            conn.execute(
                "INSERT INTO evaluation_schema_meta(key, value) "
                "VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save_scorecard(self, scorecard: ForecastScorecard) -> bool:
        """保存记分卡。

        - 不可覆盖：scorecard_id 已存在 → False
        - 防篡改：input_hash 不匹配 → 抛 ValueError
        - PIT：actual.reported_at <= forecast.as_of → 抛 ValueError

        Args:
            scorecard: 记分卡

        Returns:
            True 表示新增成功；False 表示已存在

        Raises:
            ValueError: 冻结输入被篡改 / PIT 未来泄漏
        """
        if not isinstance(scorecard, ForecastScorecard):
            raise TypeError("scorecard must be a ForecastScorecard")
        if not scorecard.forecast.is_intact():
            raise ValueError(
                "冻结输入已被篡改（input_hash 不匹配）"
            )

        # PIT：actual.reported_at > forecast.as_of
        if scorecard.actual.reported_at <= scorecard.forecast.as_of:
            raise ValueError(
                f"未来泄漏：actual.reported_at({scorecard.actual.reported_at}) "
                f"<= forecast.as_of({scorecard.forecast.as_of})"
            )

        payload = scorecard.to_dict()
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO evaluation_scorecards("
                "scorecard_id, forecast_id, company_symbol, period, metric, "
                "forecast_source, forecast_as_of, reported_at, "
                "industry, market_regime, evaluator, evaluation_as_of, "
                "input_hash, payload_json, created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scorecard_id) DO NOTHING",
                (
                    scorecard.scorecard_id,
                    scorecard.forecast.forecast_id,
                    scorecard.forecast.company_symbol,
                    scorecard.actual.period,
                    scorecard.forecast.metric,
                    scorecard.forecast.forecast_source,
                    scorecard.forecast.as_of,
                    scorecard.actual.reported_at,
                    scorecard.industry,
                    scorecard.market_regime,
                    scorecard.evaluator,
                    scorecard.evaluation_as_of,
                    scorecard.forecast.input_hash,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    scorecard.created_at,
                ),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_scorecard(self, scorecard_id: str) -> ForecastScorecard | None:
        """按 ID 读取记分卡。"""
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM evaluation_scorecards "
                "WHERE scorecard_id = ?",
                (scorecard_id,),
            ).fetchone()
        if not row:
            return None
        return self._try_deserialize(row["payload_json"])

    def list_by_company(
        self,
        company_symbol: str,
        *,
        metric: str | None = None,
    ) -> list[ForecastScorecard]:
        """按公司列出记分卡。

        Args:
            company_symbol: 公司代码
            metric: 可选过滤指标

        Returns:
            记分卡列表（按 reported_at 升序）
        """
        if not company_symbol:
            return []
        sql = (
            "SELECT payload_json FROM evaluation_scorecards "
            "WHERE company_symbol = ?"
        )
        params: list[Any] = [company_symbol]
        if metric:
            sql += " AND metric = ?"
            params.append(metric)
        sql += " ORDER BY reported_at ASC"
        with self._lock, self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [item for r in rows if (item := self._try_deserialize(r["payload_json"])) is not None]

    def list_by_forecast(self, forecast_id: str) -> list[ForecastScorecard]:
        """按预测 ID 列出记分卡。"""
        if not forecast_id:
            return []
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM evaluation_scorecards "
                "WHERE forecast_id = ? ORDER BY reported_at ASC",
                (forecast_id,),
            ).fetchall()
        return [item for r in rows if (item := self._try_deserialize(r["payload_json"])) is not None]

    def list_by_regime(
        self,
        industry: str,
        market_regime: str,
    ) -> list[ForecastScorecard]:
        """按行业 + 市场环境列出记分卡（用于稳定性聚合）。"""
        if not industry or not market_regime:
            return []
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM evaluation_scorecards "
                "WHERE industry = ? AND market_regime = ? "
                "ORDER BY reported_at ASC",
                (industry, market_regime),
            ).fetchall()
        return [item for r in rows if (item := self._try_deserialize(r["payload_json"])) is not None]

    def stats(self) -> dict[str, Any]:
        """仓储统计。"""
        with self._lock, self.connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM evaluation_scorecards"
            ).fetchone()["n"]
            by_metric = {
                row["metric"]: row["n"]
                for row in conn.execute(
                    "SELECT metric, COUNT(*) AS n FROM evaluation_scorecards "
                    "GROUP BY metric"
                ).fetchall()
            }
            by_regime = {
                row["market_regime"]: row["n"]
                for row in conn.execute(
                    "SELECT market_regime, COUNT(*) AS n "
                    "FROM evaluation_scorecards GROUP BY market_regime"
                ).fetchall()
            }
        return {
            "total_scorecards": total,
            "by_metric": by_metric,
            "by_market_regime": by_regime,
            "schema_version": SCHEMA_VERSION,
        }

    # ------------------------------------------------------------------
    # PIT 校验
    # ------------------------------------------------------------------

    def validate_no_future_leakage(self, scorecard: ForecastScorecard) -> bool:
        """校验记分卡无未来泄漏。

        Args:
            scorecard: 待校验记分卡

        Returns:
            True 表示无未来泄漏
        """
        return scorecard.actual.reported_at > scorecard.forecast.as_of

    # ------------------------------------------------------------------
    # 序列化 / 反序列化
    # ------------------------------------------------------------------

    @staticmethod
    def _deserialize(payload_json: str) -> ForecastScorecard:
        """从 JSON 反序列化为 ForecastScorecard。"""
        d = json.loads(payload_json)
        forecast = FrozenForecastInput(
            forecast_id=d["forecast"]["forecast_id"],
            company_symbol=d["forecast"]["company_symbol"],
            as_of=d["forecast"]["as_of"],
            period=d["forecast"]["period"],
            metric=d["forecast"]["metric"],
            forecast_source=d["forecast"]["forecast_source"],
            forecast_value=d["forecast"]["forecast_value"],
            interval_lower=d["forecast"]["interval_lower"],
            interval_upper=d["forecast"]["interval_upper"],
            model_name=d["forecast"]["model_name"],
            model_version=d["forecast"]["model_version"],
            source_refs=list(d["forecast"].get("source_refs", [])),
            assumptions=dict(d["forecast"].get("assumptions", {})),
            input_hash=d["forecast"]["input_hash"],
            frozen_at=d["forecast"]["frozen_at"],
        )
        actual = ActualOutcome(
            company_symbol=d["actual"]["company_symbol"],
            period=d["actual"]["period"],
            metric=d["actual"]["metric"],
            actual_value=d["actual"]["actual_value"],
            reported_at=d["actual"]["reported_at"],
            status=d["actual"]["status"],
            source_refs=list(d["actual"].get("source_refs", [])),
        )
        attribution = ErrorAttribution(
            model_error_pct=d["attribution"]["model_error_pct"],
            mapping_error_pct=d["attribution"]["mapping_error_pct"],
            data_error_pct=d["attribution"]["data_error_pct"],
            timing_error_pct=d["attribution"]["timing_error_pct"],
            assumption_error_pct=d["attribution"]["assumption_error_pct"],
            note=d["attribution"].get("note", ""),
        )
        return ForecastScorecard(
            scorecard_id=d["scorecard_id"],
            forecast=forecast,
            actual=actual,
            signed_error_pct=d["signed_error_pct"],
            abs_error_pct=d["abs_error_pct"],
            interval_covered=d["interval_covered"],
            attribution=attribution,
            industry=d["industry"],
            market_regime=d["market_regime"],
            stability_score=d["stability_score"],
            consensus_error_pct=d.get("consensus_error_pct"),
            evaluation_as_of=d["evaluation_as_of"],
            evaluator=d["evaluator"],
            created_at=d["created_at"],
        )

    @staticmethod
    def _try_deserialize(payload_json: str) -> ForecastScorecard | None:
        try:
            if not isinstance(payload_json, str):
                return None
            return EvaluationRepository._deserialize(payload_json)
        except (
            TypeError,
            ValueError,
            KeyError,
            IndexError,
            AttributeError,
            json.JSONDecodeError,
        ):
            return None


__all__ = [
    "ERR_SCORECARD_EXISTS",
    "ERR_FUTURE_LEAKAGE",
    "ERR_FROZEN_INPUT_TAMPERED",
    "EvaluationRepository",
]
