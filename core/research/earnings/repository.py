"""SQLite 版本化盈利映射存储。

新增版本化假设、模型、情景、结果和状态审计表；迁移幂等。
SQLite 是任务、状态、模型输入、结果、反馈和审计的事务源；LanceDB 仅保存向量。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TypeVar

from .contracts import (
    CapacityModel,
    DemandModel,
    EarningsDriver,
    EarningsImpact,
    EarningsScenario,
    ForecastFinancials,
    IntegratedForecast,
    ImpactAssumption,
    InputGap,
    MarginModel,
    PricingModel,
    ReconciliationCheck,
    RevenueExposure,
    ShareCapitalVersion,
    SensitivityItem,
    utc_now,
)

T = TypeVar("T")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class EarningsRepository:
    """盈利映射 SQLite 存储，带版本化和状态审计。"""

    SCHEMA_VERSION = 4

    # 表结构: 表名 -> (主键, 状态列, 时间列)
    _SCHEMA: dict[str, tuple[str, str, str]] = {
        "earnings_drivers": ("driver_id", "driver_type", "created_at"),
        "earnings_assumptions": ("assumption_id", "variable_name", "as_of"),
        "earnings_exposures": ("exposure_id", "source_kind", "as_of"),
        "earnings_capital_versions": ("capital_id", "company_symbol", "as_of"),
        "earnings_models": ("model_id", "driver_id", "created_at"),
        "earnings_scenarios": ("scenario_id", "kind", "created_at"),
        "earnings_impacts": ("impact_id", "status", "created_at"),
        "earnings_gaps": ("gap_id", "severity", "created_at"),
        "earnings_state_audit": ("audit_id", "entity_type", "created_at"),
        "earnings_forecasts": ("forecast_id", "scenario", "created_at"),
    }

    # 模型类型 -> 对应 dataclass，用于 from_dict 反序列化
    _MODEL_CLASSES: dict[str, type] = {
        "demand": DemandModel,
        "capacity": CapacityModel,
        "pricing": PricingModel,
        "margin": MarginModel,
    }

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``EARNINGS_DB_PATH`` 读取，默认
                ``data/research_control/earnings.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "EARNINGS_DB_PATH",
                "data/research_control/earnings.db",
            )
        path = Path(db_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        self.db_path = path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        """创建/升级 schema；可重复执行（幂等）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            # v3 迁移： earnings_inputs -> earnings_assumptions, earnings_results -> earnings_impacts
            self._migrate_v3_rename_tables(conn)
            # 兼容修复：旧表列名差异，必须在创建索引前完成
            self._migrate_assumptions_name_column(conn)
            self._migrate_impacts_result_id_column(conn)
            for table, (pk, state_col, time_col) in self._SCHEMA.items():
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"{pk} TEXT PRIMARY KEY, {state_col} TEXT NOT NULL DEFAULT '', "
                    f"{time_col} TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{state_col} ON {table}({state_col})"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{time_col} ON {table}({time_col})"
                )
            # v2 迁移：earnings_models 新增 model_type 列
            self._migrate_add_model_type(conn)
            # v4 迁移：earnings_forecasts 新增业务列（symbol/period/model_version/forecast_version）
            self._migrate_v4_forecasts_business_columns(conn)
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )

    def _migrate_v4_forecasts_business_columns(self, conn: sqlite3.Connection) -> None:
        """v4 迁移：为 earnings_forecasts 添加业务列与版本唯一索引（幂等）。

        - forecast_id 主键在 _SCHEMA 创建时已建立
        - 业务列 symbol/period/scenario/horizon/model_version/forecast_version/as_of
        - 复合唯一索引：(symbol, period, scenario, model_version, forecast_version) 防止覆盖
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(earnings_forecasts)").fetchall()}
        for col in [
            "symbol", "period", "horizon", "model_version", "forecast_version", "as_of",
        ]:
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE earnings_forecasts ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        # 复合唯一索引：防止 (symbol, period, scenario, model_version, forecast_version) 被覆盖
        # 但允许同 (symbol, period, scenario, model_version) 下不同 forecast_version 并存
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_earnings_forecasts_version_unique "
            "ON earnings_forecasts(symbol, period, scenario, model_version, forecast_version)"
        )

    def _migrate_v3_rename_tables(self, conn: sqlite3.Connection) -> None:
        """v3 迁移： earnings_inputs -> earnings_assumptions, earnings_results -> earnings_impacts。"""
        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "earnings_inputs" in existing:
            conn.execute("ALTER TABLE earnings_inputs RENAME TO earnings_assumptions")
            # v3 字段重命名：旧 QuantitativeInput 用 name，新 ImpactAssumption 用 variable_name
            cols = {row[1] for row in conn.execute("PRAGMA table_info(earnings_assumptions)").fetchall()}
            if "name" in cols and "variable_name" not in cols:
                conn.execute("ALTER TABLE earnings_assumptions RENAME COLUMN name TO variable_name")
            # 索引名仍指向旧表名，清理后由 migrate 统一重建
            conn.execute("DROP INDEX IF EXISTS idx_earnings_inputs_name")
            conn.execute("DROP INDEX IF EXISTS idx_earnings_inputs_as_of")
        if "earnings_results" in existing:
            conn.execute("ALTER TABLE earnings_results RENAME TO earnings_impacts")
            conn.execute("DROP INDEX IF EXISTS idx_earnings_results_status")
            conn.execute("DROP INDEX IF EXISTS idx_earnings_results_created_at")

    def _migrate_assumptions_name_column(self, conn: sqlite3.Connection) -> None:
        """将 earnings_assumptions 表的旧列重命名为新 schema（幂等）。"""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(earnings_assumptions)").fetchall()}
        if "input_id" in cols and "assumption_id" not in cols:
            conn.execute("ALTER TABLE earnings_assumptions RENAME COLUMN input_id TO assumption_id")
        if "name" in cols and "variable_name" not in cols:
            conn.execute("ALTER TABLE earnings_assumptions RENAME COLUMN name TO variable_name")

    def _migrate_impacts_result_id_column(self, conn: sqlite3.Connection) -> None:
        """将 earnings_impacts 表的旧 result_id 列重命名为 impact_id（幂等）。"""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(earnings_impacts)").fetchall()}
        if "result_id" in cols and "impact_id" not in cols:
            conn.execute("ALTER TABLE earnings_impacts RENAME COLUMN result_id TO impact_id")

    def _migrate_add_model_type(self, conn: sqlite3.Connection) -> None:
        """为 earnings_models 表添加 model_type 列（v2 迁移，幂等）。"""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(earnings_models)").fetchall()}
        if "model_type" not in cols:
            conn.execute(
                "ALTER TABLE earnings_models ADD COLUMN model_type TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_earnings_models_model_type "
                "ON earnings_models(model_type)"
            )

    def _upsert(
        self,
        table: str,
        pk: str,
        key: str,
        index_values: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        columns = [pk, *index_values.keys(), "payload_json"]
        values = [
            key,
            *index_values.values(),
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
        ]
        updates = ",".join(f"{col}=excluded.{col}" for col in columns[1:])
        placeholders = ",".join("?" for _ in columns)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders}) "
                f"ON CONFLICT({pk}) DO UPDATE SET {updates}",
                values,
            )

    def _get(self, table: str, pk: str, key: str, cls: type[T]) -> T | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM {table} WHERE {pk}=?", (key,)
            ).fetchone()
        return self._decode_item(row[0], cls) if row else None

    @staticmethod
    def _limit(limit: Any, default: int = 100) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError, OverflowError):
            value = default
        return max(1, min(value, 1000))

    @staticmethod
    def _decode_payload(raw: Any) -> Mapping[str, Any] | None:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, Mapping) else None

    @classmethod
    def _decode_item(cls, raw: Any, item_cls: type[T]) -> T | None:
        payload = cls._decode_payload(raw)
        if payload is None:
            return None
        try:
            return item_cls.from_dict(payload)
        except Exception:
            return None

    def _list(
        self,
        table: str,
        cls: type[T],
        where: str = "",
        params: tuple[Any, ...] = (),
        limit: int = 100,
    ) -> list[T]:
        query = f"SELECT payload_json FROM {table}"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY rowid DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, (*params, self._limit(limit))).fetchall()
        return [
            item
            for row in rows
            if (item := self._decode_item(row[0], cls)) is not None
        ]

    # ---- Driver ----

    def save_driver(self, item: EarningsDriver) -> None:
        """保存盈利驱动。"""
        self._upsert(
            "earnings_drivers",
            "driver_id",
            item.driver_id,
            {"driver_type": item.driver_type, "created_at": item.created_at},
            item.to_dict(),
        )

    def get_driver(self, driver_id: str) -> EarningsDriver | None:
        return self._get("earnings_drivers", "driver_id", driver_id, EarningsDriver)

    def list_drivers(self, company_symbol: str = "", limit: int = 100) -> list[EarningsDriver]:
        where = "payload_json LIKE ?" if company_symbol else ""
        params = (f'%"company_symbol":"{company_symbol}"%',) if company_symbol else ()
        return self._list("earnings_drivers", EarningsDriver, where, params, limit)

    # ---- ImpactAssumption ----

    def save_assumption(self, item: ImpactAssumption) -> None:
        """保存计算假设（版本化）。"""
        self._upsert(
            "earnings_assumptions",
            "assumption_id",
            item.assumption_id,
            {"variable_name": item.variable_name, "as_of": item.as_of},
            item.to_dict(),
        )

    def get_assumption(self, assumption_id: str) -> ImpactAssumption | None:
        return self._get("earnings_assumptions", "assumption_id", assumption_id, ImpactAssumption)

    def list_assumptions(self, variable_name: str = "", limit: int = 100) -> list[ImpactAssumption]:
        """查询假设列表，支持 variable_name 过滤。"""
        where = "variable_name=?" if variable_name else ""
        params = (variable_name,) if variable_name else ()
        return self._list("earnings_assumptions", ImpactAssumption, where, params, limit)

    # 兼容别名：旧接口使用 save_input/get_input/list_inputs
    def save_input(self, item: ImpactAssumption) -> None:
        """兼容旧名：保存计算假设。"""
        self.save_assumption(item)

    def get_input(self, input_id: str) -> ImpactAssumption | None:
        """兼容旧名：获取计算假设。"""
        return self.get_assumption(input_id)

    def list_inputs(self, name: str = "", limit: int = 100) -> list[ImpactAssumption]:
        """兼容旧名：按 name（对应 variable_name）查询假设。"""
        return self.list_assumptions(variable_name=name, limit=limit)

    # ---- RevenueExposure ----

    def save_exposure(self, item: RevenueExposure) -> None:
        """保存收入敞口。"""
        self._upsert(
            "earnings_exposures",
            "exposure_id",
            item.exposure_id,
            {"source_kind": item.source_kind, "as_of": item.as_of},
            item.to_dict(),
        )

    def get_exposure(self, exposure_id: str) -> RevenueExposure | None:
        return self._get("earnings_exposures", "exposure_id", exposure_id, RevenueExposure)

    def list_exposures(self, driver_id: str = "", limit: int = 100) -> list[RevenueExposure]:
        where = "payload_json LIKE ?" if driver_id else ""
        params = (f'%"driver_id":"{driver_id}"%',) if driver_id else ()
        return self._list("earnings_exposures", RevenueExposure, where, params, limit)

    # ---- ShareCapitalVersion ----

    def save_capital(self, item: ShareCapitalVersion) -> None:
        """保存股本版本。"""
        self._upsert(
            "earnings_capital_versions",
            "capital_id",
            item.capital_id,
            {"company_symbol": item.company_symbol, "as_of": item.as_of},
            item.to_dict(),
        )

    def get_capital(self, capital_id: str) -> ShareCapitalVersion | None:
        return self._get("earnings_capital_versions", "capital_id", capital_id, ShareCapitalVersion)

    def list_capitals(self, company_symbol: str = "", limit: int = 100) -> list[ShareCapitalVersion]:
        where = "company_symbol=?" if company_symbol else ""
        params = (company_symbol,) if company_symbol else ()
        return self._list("earnings_capital_versions", ShareCapitalVersion, where, params, limit)

    # ---- 版本化模型容器（Demand/Capacity/Pricing/Margin） ----

    def save_model(self, model_type: str, item: Any) -> None:
        """保存版本化模型容器。

        Args:
            model_type: 模型类型（demand/capacity/pricing/margin）。
            item: 模型 dataclass 实例（DemandModel/CapacityModel/PricingModel/MarginModel）。
        """
        if model_type not in self._MODEL_CLASSES:
            raise ValueError(f"未知模型类型: {model_type}，支持: {list(self._MODEL_CLASSES.keys())}")
        payload = item.to_dict()
        # 在 payload 中也存一份 _model_type，便于从 get_model 反序列化
        payload["_model_type"] = model_type
        self._upsert(
            "earnings_models",
            "model_id",
            item.model_id,
            {
                "driver_id": item.driver_id,
                "model_type": model_type,
                "created_at": utc_now(),
            },
            payload,
        )

    def save_demand_model(self, item: DemandModel) -> None:
        """保存需求模型。"""
        self.save_model("demand", item)

    def save_capacity_model(self, item: CapacityModel) -> None:
        """保存产能模型。"""
        self.save_model("capacity", item)

    def save_pricing_model(self, item: PricingModel) -> None:
        """保存定价模型。"""
        self.save_model("pricing", item)

    def save_margin_model(self, item: MarginModel) -> None:
        """保存利润率模型。"""
        self.save_model("margin", item)

    def get_model(self, model_id: str) -> Any | None:
        """查询单个模型，自动判断类型。

        Args:
            model_id: 模型 ID。

        Returns:
            模型 dataclass 实例，或 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json, model_type FROM earnings_models WHERE model_id=?",
                (model_id,),
            ).fetchone()
        if not row:
            return None
        payload = self._decode_payload(row[0])
        if payload is None:
            return None
        model_type = row[1] or payload.get("_model_type", "")
        cls = self._MODEL_CLASSES.get(model_type)
        if cls is None:
            return None
        return self._decode_item(row[0], cls)

    def list_models(
        self,
        driver_id: str = "",
        model_type: str = "",
        limit: int = 100,
    ) -> list[Any]:
        """查询模型列表，支持 driver_id 和 model_type 过滤。

        Args:
            driver_id: 驱动 ID 过滤。
            model_type: 模型类型过滤（demand/capacity/pricing/margin）。
            limit: 返回上限。

        Returns:
            模型 dataclass 实例列表。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if driver_id:
            clauses.append("driver_id=?")
            params.append(driver_id)
        if model_type:
            clauses.append("model_type=?")
            params.append(model_type)
        where = " AND ".join(clauses) if clauses else ""
        query = "SELECT payload_json, model_type FROM earnings_models"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY rowid DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, (*params, self._limit(limit))).fetchall()
        results: list[Any] = []
        for row in rows:
            payload = self._decode_payload(row[0])
            if payload is None:
                continue
            mt = row[1] or payload.get("_model_type", "")
            cls = self._MODEL_CLASSES.get(mt)
            if cls is not None:
                item = self._decode_item(row[0], cls)
                if item is not None:
                    results.append(item)
        return results

    def list_demand_models(self, driver_id: str = "", limit: int = 100) -> list[DemandModel]:
        """查询需求模型列表。"""
        return self.list_models(driver_id, "demand", limit)

    def list_capacity_models(self, driver_id: str = "", limit: int = 100) -> list[CapacityModel]:
        """查询产能模型列表。"""
        return self.list_models(driver_id, "capacity", limit)

    def list_pricing_models(self, driver_id: str = "", limit: int = 100) -> list[PricingModel]:
        """查询定价模型列表。"""
        return self.list_models(driver_id, "pricing", limit)

    def list_margin_models(self, driver_id: str = "", limit: int = 100) -> list[MarginModel]:
        """查询利润率模型列表。"""
        return self.list_models(driver_id, "margin", limit)

    # ---- EarningsImpact ----

    def save_impact(self, item: EarningsImpact) -> None:
        """保存盈利测算结果（含情景、敏感性、缺口）。"""
        self._upsert(
            "earnings_impacts",
            "impact_id",
            item.impact_id,
            {"status": item.status, "created_at": item.created_at},
            item.to_dict(),
        )
        # 保存情景
        for scenario in item.scenarios:
            self._upsert(
                "earnings_scenarios",
                "scenario_id",
                scenario.scenario_id or f"{scenario.kind}_{item.impact_id}",
                {"kind": scenario.kind, "created_at": item.created_at},
                {**scenario.to_dict(), "impact_id": item.impact_id},
            )
        # 保存缺口
        for gap in item.data_gaps:
            self._upsert(
                "earnings_gaps",
                "gap_id",
                gap.gap_id,
                {"severity": gap.severity, "created_at": utc_now()},
                gap.to_dict(),
            )
        # 状态审计
        self._record_audit(
            entity_type="impact",
            entity_id=item.impact_id,
            from_status="",
            to_status=item.status,
            reason=f"impact created, driver={item.driver_id}",
        )

    def get_impact(self, impact_id: str) -> EarningsImpact | None:
        return self._get("earnings_impacts", "impact_id", impact_id, EarningsImpact)

    def list_impacts(
        self, driver_id: str = "", status: str = "", limit: int = 100
    ) -> list[EarningsImpact]:
        clauses: list[str] = []
        params: list[Any] = []
        if driver_id:
            clauses.append("payload_json LIKE ?")
            params.append(f'%"driver_id":"{driver_id}"%')
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " AND ".join(clauses) if clauses else ""
        return self._list("earnings_impacts", EarningsImpact, where, tuple(params), limit)

    # 兼容别名：旧接口使用 save_result/get_result/list_results
    def save_result(self, item: EarningsImpact) -> None:
        """兼容旧名：保存盈利测算结果。"""
        self.save_impact(item)

    def get_result(self, result_id: str) -> EarningsImpact | None:
        """兼容旧名：获取盈利测算结果。"""
        return self.get_impact(result_id)

    def list_results(
        self, driver_id: str = "", status: str = "", limit: int = 100
    ) -> list[EarningsImpact]:
        """兼容旧名：查询盈利测算结果列表。"""
        return self.list_impacts(driver_id=driver_id, status=status, limit=limit)

    def list_gaps(self, driver_id: str = "", limit: int = 100) -> list[InputGap]:
        where = "payload_json LIKE ?" if driver_id else ""
        params = (f'%"driver_id":"{driver_id}"%',) if driver_id else ()
        return self._list("earnings_gaps", InputGap, where, params, limit)

    # ---- 状态审计 ----

    def _record_audit(
        self,
        entity_type: str,
        entity_id: str,
        from_status: str,
        to_status: str,
        reason: str,
        operator: str = "system",
    ) -> None:
        """记录状态迁移审计。"""
        audit_id = f"audit_{uuid.uuid4().hex[:16]}"
        payload = {
            "audit_id": audit_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "operator": operator,
            "created_at": utc_now(),
        }
        self._upsert(
            "earnings_state_audit",
            "audit_id",
            audit_id,
            {"entity_type": entity_type, "created_at": payload["created_at"]},
            payload,
        )

    def list_audit(
        self,
        entity_type: str = "",
        entity_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询审计记录。"""
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id:
            clauses.append("payload_json LIKE ?")
            params.append(f'%"entity_id":"{entity_id}"%')
        where = " AND ".join(clauses) if clauses else ""
        query = "SELECT payload_json FROM earnings_state_audit"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY rowid DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, (*params, self._limit(limit))).fetchall()
        return [
            payload
            for row in rows
            if (payload := self._decode_payload(row[0])) is not None
        ]

    # ---- 统计 ----

    def stats(self) -> dict[str, Any]:
        """返回存储统计。"""
        with self._connect() as conn:
            counts: dict[str, int] = {}
            for table in self._SCHEMA:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = int(row[0]) if row else 0
            version_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        try:
            schema_version = int(version_row[0]) if version_row else 0
        except (TypeError, ValueError):
            schema_version = 0
        return {
            "schema_version": schema_version,
            "counts": counts,
        }

    # ---- IntegratedForecast（P1.4）----

    def save_forecast(self, item: IntegratedForecast) -> None:
        """保存三表联动盈利预测（版本化）。

        预测版本不可覆盖：
        - 相同 (symbol, period, scenario, model_version, forecast_version) 已存在时抛 ValueError。
        - 不同 forecast_version 可以并存。
        - forecast_id 主键已存在时也抛 ValueError。

        Args:
            item: IntegratedForecast 实例。

        Raises:
            ValueError: 当 forecast_id 或 (symbol, period, scenario, model_version, forecast_version) 已存在。
        """
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT forecast_id FROM earnings_forecasts WHERE forecast_id=?",
                (item.forecast_id,),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"forecast_id={item.forecast_id} 已存在，预测版本不可覆盖"
                )
            existing_version = conn.execute(
                "SELECT forecast_id FROM earnings_forecasts "
                "WHERE symbol=? AND period=? AND scenario=? "
                "AND model_version=? AND forecast_version=?",
                (
                    item.symbol,
                    item.period,
                    item.scenario,
                    item.model_version,
                    str(item.forecast_version),
                ),
            ).fetchone()
            if existing_version is not None:
                raise ValueError(
                    f"同 (symbol={item.symbol}, period={item.period}, "
                    f"scenario={item.scenario}, model_version={item.model_version}, "
                    f"forecast_version={item.forecast_version}) 已存在，不可覆盖"
                )
            payload = item.to_dict()
            conn.execute(
                "INSERT INTO earnings_forecasts("
                "forecast_id, scenario, created_at, symbol, period, horizon, "
                "model_version, forecast_version, as_of, payload_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    item.forecast_id,
                    item.scenario,
                    item.created_at,
                    item.symbol,
                    item.period,
                    item.horizon,
                    item.model_version,
                    str(item.forecast_version),
                    item.as_of,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False),
                ),
            )
        # 状态审计
        self._record_audit(
            entity_type="forecast",
            entity_id=item.forecast_id,
            from_status="",
            to_status=item.scenario,
            reason=(
                f"forecast created, symbol={item.symbol}, "
                f"period={item.period}, version={item.forecast_version}"
            ),
        )

    def get_forecast(self, forecast_id: str) -> IntegratedForecast | None:
        """按 forecast_id 查询预测。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM earnings_forecasts WHERE forecast_id=?",
                (forecast_id,),
            ).fetchone()
        return self._decode_item(row[0], IntegratedForecast) if row else None

    def list_forecasts(
        self,
        symbol: str = "",
        period: str = "",
        scenario: str = "",
        limit: int = 100,
    ) -> list[IntegratedForecast]:
        """查询预测列表，支持 symbol/period/scenario 过滤。

        按 forecast_version 降序返回（最新版本在前）。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if period:
            clauses.append("period=?")
            params.append(period)
        if scenario:
            clauses.append("scenario=?")
            params.append(scenario)
        where = " AND ".join(clauses) if clauses else ""
        query = "SELECT payload_json FROM earnings_forecasts"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY CAST(forecast_version AS INTEGER) DESC, rowid DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(query, (*params, self._limit(limit))).fetchall()
        return [
            item
            for row in rows
            if (item := self._decode_item(row[0], IntegratedForecast)) is not None
        ]

    def get_latest_forecast(
        self,
        symbol: str,
        period: str,
        scenario: str = "",
        as_of: str = "",
    ) -> IntegratedForecast | None:
        """获取 PIT 可见的最新预测版本。

        Args:
            symbol: 公司代码（必填）。
            period: 期间（必填）。
            scenario: 情景类型过滤（可选，空字符串表示任意）。
            as_of: PIT 时点（ISO 8601）。仅返回 as_of <= 该时点的预测；为空时不过滤。

        Returns:
            最新 forecast_version 的预测；无匹配返回 None。
        """
        if not symbol or not period:
            return None
        clauses = ["symbol=?", "period=?"]
        params: list[Any] = [symbol, period]
        if scenario:
            clauses.append("scenario=?")
            params.append(scenario)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        where = " AND ".join(clauses)
        query = (
            "SELECT payload_json FROM earnings_forecasts "
            f"WHERE {where} "
            "ORDER BY CAST(forecast_version AS INTEGER) DESC, rowid DESC LIMIT 1"
        )
        with self._connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return self._decode_item(row[0], IntegratedForecast) if row else None

    def list_forecast_versions(
        self,
        symbol: str,
        period: str,
        scenario: str = "",
    ) -> list[int]:
        """查询预测的所有版本号（升序）。"""
        if not symbol or not period:
            return []
        clauses = ["symbol=?", "period=?"]
        params: list[Any] = [symbol, period]
        if scenario:
            clauses.append("scenario=?")
            params.append(scenario)
        where = " AND ".join(clauses)
        query = (
            "SELECT DISTINCT forecast_version FROM earnings_forecasts "
            f"WHERE {where} ORDER BY CAST(forecast_version AS INTEGER) ASC"
        )
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        versions: list[int] = []
        for row in rows:
            try:
                if row[0]:
                    versions.append(int(row[0]))
            except (TypeError, ValueError):
                continue
        return versions
