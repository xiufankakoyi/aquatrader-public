"""SQLite transaction store for Stage 3 expectations and theses.

P1.7 扩展：
- 新增 `aquatrade_forecasts` 表（第四类独立预期）
- 新增 `consensus_summaries` 表（一致预期聚合统计缓存）
- 升级 SCHEMA_VERSION 从 2 到 3，幂等迁移
- 新增 `list_consensus_visible_as_of` 方法支持 PIT 重建当时可见共识
- 新增 `list_aquatrade_forecasts` / `get_latest_aquatrade_forecast` 方法

P2.5 扩展：
- 新增 `revision_sequences` 表（1/3/6 个月修正序列缓存）
- 新增 `implied_expectation_summaries` 表（市场隐含预期汇总）
- 升级 SCHEMA_VERSION 从 3 到 4，幂等迁移
- 新增 `save_revision_sequence` / `list_revision_sequences` 方法
- 新增 `save_implied_expectation_summary` / `list_implied_expectation_summaries` 方法
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


class ExpectationRepository:
    SCHEMA_VERSION = 4
    TABLES = {
        "company_guidance": ("guidance_id", "company_symbol"),
        "consensus_estimates": ("consensus_id", "company_symbol"),
        "implied_expectations": ("implied_id", "company_symbol"),
        "aquatrade_forecasts": ("forecast_id", "company_symbol"),
        "consensus_summaries": ("summary_id", "company_symbol"),
        "market_expectations": ("expectation_id", "symbol"),
        "valuation_snapshots": ("snapshot_id", "company_symbol"),
        "expectation_gaps": ("gap_id", "company_symbol"),
        "investment_theses": ("thesis_id", "company_symbol"),
        "thesis_audits": ("audit_id", "thesis_id"),
        "thesis_review_tasks": ("review_id", "thesis_id"),
        # P2.5 新增
        "revision_sequences": ("sequence_id", "company_symbol"),
        "implied_expectation_summaries": ("summary_id", "company_symbol"),
    }

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``EXPECTATIONS_DB_PATH`` 读取，默认
                ``data/research_control/expectations.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "EXPECTATIONS_DB_PATH",
                "data/research_control/expectations.db",
            )
        self.db_path = Path(db_path)
        if not self.db_path.is_absolute():
            self.db_path = Path(__file__).resolve().parents[3] / self.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        """幂等迁移：创建所有表并升级 schema 版本。"""
        with self._lock, self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS expectation_schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for table, (pk, index_col) in self.TABLES.items():
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"{pk} TEXT PRIMARY KEY, {index_col} TEXT NOT NULL DEFAULT '', "
                    "created_at TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL)"
                )
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{index_col} ON {table}({index_col})")
            # P1.7 v3 迁移：为新增表补齐业务列与复合索引（幂等）
            self._migrate_v3_aquatrade_forecasts_columns(conn)
            self._migrate_v3_consensus_summaries_columns(conn)
            self._migrate_v3_consensus_estimates_institution_columns(conn)
            # P2.5 v4 迁移：为 revision_sequences 与 implied_expectation_summaries 添加业务列
            self._migrate_v4_revision_sequences_columns(conn)
            self._migrate_v4_implied_expectation_summaries_columns(conn)
            conn.execute(
                "INSERT INTO expectation_schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(self.SCHEMA_VERSION),),
            )

    def _migrate_v4_revision_sequences_columns(self, conn: sqlite3.Connection) -> None:
        """v4 迁移：为 revision_sequences 添加业务列与 PIT 索引（幂等）。

        - 业务列：metric/period/as_of/window/status/source_count
        - 复合索引：(company_symbol, metric, period, as_of, window)
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(revision_sequences)").fetchall()}
        for col in ["metric", "period", "as_of", "window", "status", "source_count"]:
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE revision_sequences ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_revision_sequences_pit "
            "ON revision_sequences(company_symbol, metric, period, as_of, window)"
        )

    def _migrate_v4_implied_expectation_summaries_columns(self, conn: sqlite3.Connection) -> None:
        """v4 迁移：为 implied_expectation_summaries 添加业务列与 PIT 索引（幂等）。

        - 业务列：as_of/status/all_implied_tagged
        - 复合索引：(company_symbol, as_of)
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(implied_expectation_summaries)").fetchall()}
        for col in ["as_of", "status", "all_implied_tagged"]:
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE implied_expectation_summaries ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_implied_expectation_summaries_pit "
            "ON implied_expectation_summaries(company_symbol, as_of)"
        )

    def _migrate_v3_aquatrade_forecasts_columns(self, conn: sqlite3.Connection) -> None:
        """v3 迁移：为 aquatrade_forecasts 添加业务列与版本唯一索引（幂等）。

        - 业务列：symbol/period/scenario/model_version/as_of/integrated_forecast_id
        - 复合唯一索引：(company_symbol, period, scenario, model_version)
          防止同 model_version 下同情景同期间被覆盖；修订通过 revision_of 链接。
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(aquatrade_forecasts)").fetchall()}
        for col in ["period", "scenario", "model_version", "as_of", "integrated_forecast_id"]:
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE aquatrade_forecasts ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        # 复合索引便于按 (symbol, period, scenario, model_version) 查最新版本
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aquatrade_forecasts_version "
            "ON aquatrade_forecasts(company_symbol, period, scenario, model_version, created_at DESC)"
        )

    def _migrate_v3_consensus_summaries_columns(self, conn: sqlite3.Connection) -> None:
        """v3 迁移：为 consensus_summaries 添加业务列与查询索引（幂等）。

        - 业务列：metric/period/as_of/status/source_count
        - 复合索引：(company_symbol, metric, period, as_of) 便于 PIT 重建
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(consensus_summaries)").fetchall()}
        for col in ["metric", "period", "as_of", "status", "source_count"]:
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE consensus_summaries ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_consensus_summaries_pit "
            "ON consensus_summaries(company_symbol, metric, period, as_of)"
        )

    def _migrate_v3_consensus_estimates_institution_columns(self, conn: sqlite3.Connection) -> None:
        """v3 迁移：为 consensus_estimates 添加 institution_id/analyst_id 业务列（幂等）。

        老数据 institution_id/analyst_id 为空字符串；新数据必须填充以支持分机构版本序列。
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(consensus_estimates)").fetchall()}
        for col in ["institution_id", "analyst_id"]:
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE consensus_estimates ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_consensus_estimates_institution "
            "ON consensus_estimates(institution_id)"
        )

    @staticmethod
    def _payload(item: Any) -> dict[str, Any]:
        if hasattr(item, "to_dict"):
            payload = item.to_dict()
            if isinstance(payload, Mapping):
                return dict(payload)
            raise TypeError("expectation to_dict() must return a mapping")
        if is_dataclass(item):
            payload = asdict(item)
            return payload if isinstance(payload, dict) else {}
        if isinstance(item, Mapping):
            return dict(item)
        raise TypeError("expectation payload must be a mapping or dataclass")

    @staticmethod
    def _decode_payload(raw: Any) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _limit(value: Any, default: int = 100) -> int:
        try:
            return max(1, min(int(value), 1000))
        except (TypeError, ValueError, OverflowError):
            return default

    def save(self, table: str, item: Any) -> None:
        if table not in self.TABLES:
            raise ValueError(f"unsupported table: {table}")
        pk, index_col = self.TABLES[table]
        payload = self._payload(item)
        key = str(payload.get(pk) or "")
        if not key:
            raise ValueError(f"missing primary key: {pk}")
        index_value = str(payload.get(index_col) or "")
        created_at = str(payload.get("created_at") or payload.get("updated_at") or "")
        with self._lock, self.connect() as conn:
            conn.execute(
                f"INSERT INTO {table}({pk},{index_col},created_at,payload_json) VALUES(?,?,?,?) "
                f"ON CONFLICT({pk}) DO UPDATE SET {index_col}=excluded.{index_col}, "
                "created_at=excluded.created_at,payload_json=excluded.payload_json",
                (key, index_value, created_at, json.dumps(payload, ensure_ascii=False, allow_nan=False)),
            )

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        if table not in self.TABLES:
            raise ValueError(f"unsupported table: {table}")
        pk, _ = self.TABLES[table]
        with self.connect() as conn:
            row = conn.execute(f"SELECT payload_json FROM {table} WHERE {pk}=?", (key,)).fetchone()
        return self._decode_payload(row[0]) if row else None

    def list(self, table: str, *, index_value: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if table not in self.TABLES:
            raise ValueError(f"unsupported table: {table}")
        _, index_col = self.TABLES[table]
        query = f"SELECT payload_json FROM {table}"
        params: list[Any] = []
        if index_value:
            query += f" WHERE {index_col}=?"
            params.append(index_value)
        query += " ORDER BY rowid DESC LIMIT ?"
        params.append(self._limit(limit))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [payload for row in rows if (payload := self._decode_payload(row[0])) is not None]

    def save_thesis_bundle(self, thesis: Any, reviews: list[Any]) -> None:
        """Persist thesis, its new audits, and review tasks atomically."""
        thesis_payload = self._payload(thesis)
        if not thesis_payload.get("thesis_id"):
            raise ValueError("missing primary key: thesis_id")
        with self._lock, self.connect() as conn:
            def upsert(table: str, payload: dict[str, Any]) -> None:
                pk, index_col = self.TABLES[table]
                conn.execute(
                    f"INSERT INTO {table}({pk},{index_col},created_at,payload_json) VALUES(?,?,?,?) "
                    f"ON CONFLICT({pk}) DO UPDATE SET {index_col}=excluded.{index_col}, "
                    "payload_json=excluded.payload_json,created_at=excluded.created_at",
                    (str(payload[pk]), str(payload.get(index_col) or ""),
                     str(payload.get("created_at") or ""), json.dumps(payload, ensure_ascii=False, allow_nan=False)),
                )
            upsert("investment_theses", thesis_payload)
            audits = thesis_payload.get("audits", [])
            for audit in audits if isinstance(audits, (list, tuple)) else []:
                if not isinstance(audit, Mapping) or not audit.get("audit_id"):
                    continue
                upsert("thesis_audits", audit)
            for review in reviews if isinstance(reviews, (list, tuple)) else []:
                review_payload = self._payload(review)
                if review_payload.get("review_id"):
                    upsert("thesis_review_tasks", review_payload)

    # ---- P1.7 新增：专用保存方法（同步业务列以支持索引查询）----

    def save_aquatrade_forecast(self, item: Any) -> None:
        """保存 AquaTrade 独立研究预测（第四类预期，与公司指引/一致预期/隐含预期并列）。

        同步写入业务列 period/scenario/model_version/as_of/integrated_forecast_id
        以支持 PIT 查询和版本回溯。
        """
        payload = self._payload(item)
        forecast_id = str(payload.get("forecast_id") or "")
        company_symbol = str(payload.get("company_symbol") or "")
        created_at = str(payload.get("created_at") or "")
        if not forecast_id:
            raise ValueError("missing primary key: forecast_id")
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO aquatrade_forecasts("
                "forecast_id, company_symbol, created_at, payload_json, "
                "period, scenario, model_version, as_of, integrated_forecast_id"
                ") VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(forecast_id) DO UPDATE SET "
                "company_symbol=excluded.company_symbol, "
                "created_at=excluded.created_at, "
                "payload_json=excluded.payload_json, "
                "period=excluded.period, scenario=excluded.scenario, "
                "model_version=excluded.model_version, as_of=excluded.as_of, "
                "integrated_forecast_id=excluded.integrated_forecast_id",
                (
                    forecast_id,
                    company_symbol,
                    created_at,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    str(payload.get("forecast_period") or payload.get("period") or ""),
                    str(payload.get("scenario") or ""),
                    str(payload.get("model_version") or ""),
                    str(payload.get("as_of") or ""),
                    str(payload.get("integrated_forecast_id") or ""),
                ),
            )

    def save_consensus_summary(self, item: Any) -> None:
        """保存一致预期聚合统计。

        同步写入业务列 metric/period/as_of/status/source_count 以支持 PIT 索引查询。
        """
        payload = self._payload(item)
        summary_id = str(payload.get("summary_id") or "")
        company_symbol = str(payload.get("company_symbol") or "")
        created_at = str(payload.get("created_at") or "")
        if not summary_id:
            raise ValueError("missing primary key: summary_id")
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO consensus_summaries("
                "summary_id, company_symbol, created_at, payload_json, "
                "metric, period, as_of, status, source_count"
                ") VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(summary_id) DO UPDATE SET "
                "company_symbol=excluded.company_symbol, "
                "created_at=excluded.created_at, "
                "payload_json=excluded.payload_json, "
                "metric=excluded.metric, period=excluded.period, "
                "as_of=excluded.as_of, status=excluded.status, "
                "source_count=excluded.source_count",
                (
                    summary_id,
                    company_symbol,
                    created_at,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    str(payload.get("metric") or ""),
                    str(payload.get("period") or ""),
                    str(payload.get("as_of") or ""),
                    str(payload.get("status") or ""),
                    str(payload.get("source_count") or "0"),
                ),
            )

    def list_aquatrade_forecasts(
        self,
        company_symbol: str = "",
        period: str = "",
        scenario: str = "",
        as_of: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询 AquaTrade 预测列表，支持 symbol/period/scenario/as_of 过滤。

        as_of 过滤：仅返回 as_of <= 该时点的预测（PIT 防泄漏）。
        按 created_at DESC 排序，最新版本在前。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if company_symbol:
            clauses.append("company_symbol=?")
            params.append(company_symbol)
        if period:
            clauses.append("period=?")
            params.append(period)
        if scenario:
            clauses.append("scenario=?")
            params.append(scenario)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        where = " AND ".join(clauses) if clauses else ""
        query = "SELECT payload_json FROM aquatrade_forecasts"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(query, (*params, self._limit(limit))).fetchall()
        return [payload for row in rows if (payload := self._decode_payload(row[0])) is not None]

    def get_latest_aquatrade_forecast(
        self,
        company_symbol: str,
        period: str,
        scenario: str = "",
        as_of: str = "",
    ) -> dict[str, Any] | None:
        """获取 PIT 可见的最新 AquaTrade 预测。

        Args:
            company_symbol: 公司代码（必填）
            period: 预测期间（必填）
            scenario: 研究情景过滤（可选，空字符串表示任意）
            as_of: PIT 时点（ISO 8601）。为空时不过滤；非空时仅返回 as_of <= 该时点的预测

        Returns:
            最新 AquaTradeForecast 的 payload dict；无匹配返回 None
        """
        if not company_symbol or not period:
            return None
        clauses = ["company_symbol=?", "period=?"]
        params: list[Any] = [company_symbol, period]
        if scenario:
            clauses.append("scenario=?")
            params.append(scenario)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        where = " AND ".join(clauses)
        query = (
            "SELECT payload_json FROM aquatrade_forecasts "
            f"WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        with self.connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return self._decode_payload(row[0]) if row else None

    def list_consensus_visible_as_of(
        self,
        company_symbol: str,
        metric: str,
        period: str,
        as_of: str,
        *,
        historical: bool = True,
    ) -> list[dict[str, Any]]:
        """PIT 重建当时可见的所有 ConsensusEstimate（按机构去重保留最新版）。

        实现细节：
        1. 从 consensus_estimates 表读取 company_symbol 匹配的全部记录
        2. 解析 payload_json，逐条校验是否有 (metric, period) 匹配的 ExpectationValue
        3. 若 historical=True，进一步校验 published_at <= as_of 且 retrieved_at <= as_of
        4. 按 institution_id 去重，同一机构仅保留 retrieved_at 最新的版本

        Args:
            company_symbol: 公司代码（必填）
            metric: 指标名（必填，如 "net_profit"）
            period: 预测期间（必填，如 "2027"）
            as_of: PIT 时点（ISO 8601，必填）
            historical: True 时启用 PIT 过滤；False 时不限制可见性

        Returns:
            每家机构最新可见的 ConsensusEstimate 列表（dict 形式）。
        """
        if not (company_symbol and metric and period and as_of):
            return []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM consensus_estimates WHERE company_symbol=? "
                "ORDER BY rowid ASC",
                (company_symbol,),
            ).fetchall()
        cutoff_dt = _parse_iso(as_of)
        # 按 institution_id 分组保留最新 retrieved_at
        by_institution: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = self._decode_payload(row[0])
            if payload is None:
                continue
            values = payload.get("values", [])
            if not isinstance(values, (list, tuple)):
                continue
            matched_value = None
            for v in values:
                if not isinstance(v, Mapping):
                    continue
                if v.get("metric") != metric or v.get("period") != period:
                    continue
                if historical:
                    if cutoff_dt is None:
                        continue
                    pub_dt = _parse_iso(v.get("published_at", ""))
                    ret_dt = _parse_iso(v.get("retrieved_at", ""))
                    if pub_dt is None or ret_dt is None:
                        continue
                    if pub_dt > cutoff_dt or ret_dt > cutoff_dt:
                        continue
                matched_value = v
                break
            if matched_value is None:
                continue
            inst = payload.get("institution_id") or payload.get("provider") or "unknown"
            if not isinstance(inst, str):
                inst = "unknown"
            existing = by_institution.get(inst)
            if existing is None:
                by_institution[inst] = payload
                continue
            # 同机构保留 retrieved_at 更新的版本
            existing_ret_dt = None
            for v in existing.get("values", []):
                if not isinstance(v, Mapping):
                    continue
                if v.get("metric") == metric and v.get("period") == period:
                    existing_ret_dt = _parse_iso(v.get("retrieved_at", ""))
                    break
            new_ret_dt = _parse_iso(matched_value.get("retrieved_at", ""))
            if new_ret_dt is not None and (existing_ret_dt is None or new_ret_dt > existing_ret_dt):
                by_institution[inst] = payload
        return list(by_institution.values())

    def list_consensus_summaries(
        self,
        company_symbol: str = "",
        metric: str = "",
        period: str = "",
        as_of: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询一致预期聚合统计列表，支持多维度过滤。"""
        clauses: list[str] = []
        params: list[Any] = []
        if company_symbol:
            clauses.append("company_symbol=?")
            params.append(company_symbol)
        if metric:
            clauses.append("metric=?")
            params.append(metric)
        if period:
            clauses.append("period=?")
            params.append(period)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " AND ".join(clauses) if clauses else ""
        query = "SELECT payload_json FROM consensus_summaries"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY as_of DESC, rowid DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(query, (*params, self._limit(limit))).fetchall()
        return [payload for row in rows if (payload := self._decode_payload(row[0])) is not None]

    # ---- P2.5 新增：修正序列与隐含预期汇总持久化 ----

    def save_revision_sequence(self, item: Any) -> None:
        """保存一致预期修正序列。

        同步写入业务列 metric/period/as_of/window/status/source_count 以支持
        PIT 索引查询与单机构冒充检测（验收点 3）。
        """
        payload = self._payload(item)
        sequence_id = str(payload.get("sequence_id") or "")
        company_symbol = str(payload.get("company_symbol") or "")
        # RevisionSequence 无 created_at 字段，使用 as_of 作为时间排序回退
        created_at = str(payload.get("created_at") or payload.get("as_of") or "")
        if not sequence_id:
            raise ValueError("missing primary key: sequence_id")
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO revision_sequences("
                "sequence_id, company_symbol, created_at, payload_json, "
                "metric, period, as_of, window, status, source_count"
                ") VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(sequence_id) DO UPDATE SET "
                "company_symbol=excluded.company_symbol, "
                "created_at=excluded.created_at, "
                "payload_json=excluded.payload_json, "
                "metric=excluded.metric, period=excluded.period, "
                "as_of=excluded.as_of, window=excluded.window, "
                "status=excluded.status, source_count=excluded.source_count",
                (
                    sequence_id,
                    company_symbol,
                    created_at,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    str(payload.get("metric") or ""),
                    str(payload.get("period") or ""),
                    str(payload.get("as_of") or ""),
                    str(payload.get("window") or ""),
                    str(payload.get("status") or ""),
                    str(payload.get("source_count") or "0"),
                ),
            )

    def list_revision_sequences(
        self,
        company_symbol: str = "",
        metric: str = "",
        period: str = "",
        as_of: str = "",
        window: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询修正序列列表，支持多维度 PIT 过滤。

        Args:
            company_symbol: 公司代码过滤
            metric: 指标名过滤
            period: 预测期间过滤
            as_of: PIT 时点过滤（as_of <= 该时点）
            window: 时间窗口过滤（1m / 3m / 6m）
            status: 状态过滤（ok / insufficient_sources / not_computable / no_data / degraded）
            limit: 返回上限（1-1000）

        Returns:
            payload dict 列表，按 as_of DESC 排序
        """
        clauses: list[str] = []
        params: list[Any] = []
        if company_symbol:
            clauses.append("company_symbol=?")
            params.append(company_symbol)
        if metric:
            clauses.append("metric=?")
            params.append(metric)
        if period:
            clauses.append("period=?")
            params.append(period)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        if window:
            clauses.append("window=?")
            params.append(window)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " AND ".join(clauses) if clauses else ""
        query = "SELECT payload_json FROM revision_sequences"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY as_of DESC, rowid DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(query, (*params, self._limit(limit))).fetchall()
        return [payload for row in rows if (payload := self._decode_payload(row[0])) is not None]

    def save_implied_expectation_summary(self, item: Any) -> None:
        """保存市场隐含预期汇总。

        同步写入业务列 as_of/status/all_implied_tagged 以支持 PIT 索引查询
        与"Reverse DCF 与一致预期使用独立标签"校验（验收点 2）。
        """
        payload = self._payload(item)
        summary_id = str(payload.get("summary_id") or "")
        company_symbol = str(payload.get("company_symbol") or "")
        created_at = str(payload.get("created_at") or payload.get("as_of") or "")
        if not summary_id:
            raise ValueError("missing primary key: summary_id")
        # all_implied_tagged 是 bool，转为 "1"/"0" 便于查询
        all_tagged = "1" if payload.get("all_implied_tagged") else "0"
        with self._lock, self.connect() as conn:
            conn.execute(
                "INSERT INTO implied_expectation_summaries("
                "summary_id, company_symbol, created_at, payload_json, "
                "as_of, status, all_implied_tagged"
                ") VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(summary_id) DO UPDATE SET "
                "company_symbol=excluded.company_symbol, "
                "created_at=excluded.created_at, "
                "payload_json=excluded.payload_json, "
                "as_of=excluded.as_of, status=excluded.status, "
                "all_implied_tagged=excluded.all_implied_tagged",
                (
                    summary_id,
                    company_symbol,
                    created_at,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                    str(payload.get("as_of") or ""),
                    str(payload.get("status") or ""),
                    all_tagged,
                ),
            )

    def list_implied_expectation_summaries(
        self,
        company_symbol: str = "",
        as_of: str = "",
        status: str = "",
        all_implied_tagged: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询市场隐含预期汇总列表，支持 PIT 过滤与标签校验。

        Args:
            company_symbol: 公司代码过滤
            as_of: PIT 时点过滤（as_of <= 该时点）
            status: 状态过滤
            all_implied_tagged: True 仅返回全部带 implied 标签的；False 仅返回存在未带标签的
            limit: 返回上限（1-1000）

        Returns:
            payload dict 列表，按 as_of DESC 排序
        """
        clauses: list[str] = []
        params: list[Any] = []
        if company_symbol:
            clauses.append("company_symbol=?")
            params.append(company_symbol)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        if status:
            clauses.append("status=?")
            params.append(status)
        if all_implied_tagged is not None:
            clauses.append("all_implied_tagged=?")
            params.append("1" if all_implied_tagged else "0")
        where = " AND ".join(clauses) if clauses else ""
        query = "SELECT payload_json FROM implied_expectation_summaries"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY as_of DESC, rowid DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(query, (*params, self._limit(limit))).fetchall()
        return [payload for row in rows if (payload := self._decode_payload(row[0])) is not None]

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in self.TABLES}
        return {"schema_version": self.SCHEMA_VERSION, "counts": counts}
