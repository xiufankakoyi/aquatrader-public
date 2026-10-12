"""P1.5 产业画像持久化仓储（SQLite + PIT 过滤 + 幂等迁移）。

按 P1 计划 §5 Task P1.5 与 P1.10 整改要求：
- 三张表：industry_economics / industry_competition / industry_lifecycle
- 每张表以 stable_id 为主键，company_symbol + industry_code + as_of 索引
- PIT 过滤：as_of <= query_as_of
- 幂等迁移：migrate() 可重复执行
- 来源追溯：source_trace_id + source_refs 通过 payload_json 完整保存
- 不输出投资建议（AGENTS.md）
- 缺数据降级（AGENTS.md）：get_latest_profile 返回 None 时 CfaReportBuilder 章节 3 降级

参考 EarningsRepository 模式：_SCHEMA + _upsert / _get / _list 通用方法。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Any, Iterator

from .contracts import (
    BargainingPower,
    CapexCycle,
    CompanyMapping,
    CompetitiveLandscape,
    DemandDriver,
    IndustryGap,
    IndustryProfile,
    InventoryCycle,
    LifecycleStage,
    MarketSizing,
    Participant,
    PriceCostCurve,
    SupplyProfile,
    TechnologyRoute,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return default


def _point_list(value: Any) -> list[tuple[str, float]]:
    if not isinstance(value, list):
        return []
    points: list[tuple[str, float]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        period = _text(item[0])
        number = _safe_float(item[1])
        if period and number is not None:
            points.append((period, number))
    return points


# ---------------------------------------------------------------------------
# 子结构反序列化辅助
# ---------------------------------------------------------------------------

def _dict_to_market_sizing(data: dict[str, Any] | None) -> MarketSizing | None:
    """从 dict 还原 MarketSizing；None 入参返回 None。"""
    data = _as_mapping(data)
    if not data:
        return None
    return MarketSizing(
        tam=_safe_float(data.get("tam")),
        sam=_safe_float(data.get("sam")),
        som=_safe_float(data.get("som")),
        unit=_text(data.get("unit")),
        currency=_text(data.get("currency")),
        period=_text(data.get("period")),
        method=_text(data.get("method")),
        source_refs=_text_list(data.get("source_refs")),
        available_at=_text(data.get("available_at")),
        grounding_status=_text(data.get("grounding_status"), "missing"),
        note=_text(data.get("note")),
    )


def _dict_to_supply_profile(data: dict[str, Any] | None) -> SupplyProfile | None:
    """从 dict 还原 SupplyProfile。"""
    data = _as_mapping(data)
    if not data:
        return None
    return SupplyProfile(
        industry_capacity=_safe_float(data.get("industry_capacity")),
        capacity_unit=_text(data.get("capacity_unit")),
        utilization_rate=_safe_float(data.get("utilization_rate")),
        expansion_lead_time_months=_safe_int(data.get("expansion_lead_time_months")),
        expansion_phase=_text(data.get("expansion_phase")),
        period=_text(data.get("period")),
        source_refs=_text_list(data.get("source_refs")),
        available_at=_text(data.get("available_at")),
        grounding_status=_text(data.get("grounding_status"), "missing"),
        note=_text(data.get("note")),
    )


def _dict_to_price_cost_curve(data: dict[str, Any] | None) -> PriceCostCurve | None:
    """从 dict 还原 PriceCostCurve。"""
    data = _as_mapping(data)
    if not data:
        return None
    return PriceCostCurve(
        price_points=_point_list(data.get("price_points")),
        cost_points=_point_list(data.get("cost_points")),
        unit=_text(data.get("unit")),
        currency=_text(data.get("currency")),
        trend=_text(data.get("trend")),
        period=_text(data.get("period")),
        source_refs=_text_list(data.get("source_refs")),
        available_at=_text(data.get("available_at")),
        grounding_status=_text(data.get("grounding_status"), "missing"),
        note=_text(data.get("note")),
    )


def _dict_to_inventory_cycle(data: dict[str, Any] | None) -> InventoryCycle | None:
    """从 dict 还原 InventoryCycle。"""
    data = _as_mapping(data)
    if not data:
        return None
    return InventoryCycle(
        inventory_days=_safe_float(data.get("inventory_days")),
        delivery_lead_time_days=_safe_int(data.get("delivery_lead_time_days")),
        inventory_trend=_text(data.get("inventory_trend")),
        period=_text(data.get("period")),
        source_refs=_text_list(data.get("source_refs")),
        available_at=_text(data.get("available_at")),
        grounding_status=_text(data.get("grounding_status"), "missing"),
        note=_text(data.get("note")),
    )


def _dict_to_capex_cycle(data: dict[str, Any] | None) -> CapexCycle | None:
    """从 dict 还原 CapexCycle。"""
    data = _as_mapping(data)
    if not data:
        return None
    return CapexCycle(
        industry_capex=_safe_float(data.get("industry_capex")),
        capex_unit=_text(data.get("capex_unit")),
        capex_currency=_text(data.get("capex_currency")),
        fcf_cycle_phase=_text(data.get("fcf_cycle_phase")),
        capex_to_revenue_ratio=_safe_float(data.get("capex_to_revenue_ratio")),
        cycle_period_years=_safe_int(data.get("cycle_period_years")),
        period=_text(data.get("period")),
        source_refs=_text_list(data.get("source_refs")),
        available_at=_text(data.get("available_at")),
        grounding_status=_text(data.get("grounding_status"), "missing"),
        note=_text(data.get("note")),
    )


def _dict_to_lifecycle_stage(data: dict[str, Any] | None) -> LifecycleStage | None:
    """从 dict 还原 LifecycleStage。"""
    data = _as_mapping(data)
    if not data:
        return None
    return LifecycleStage(
        stage=_text(data.get("stage")),
        stage_evidence_id=_text(data.get("stage_evidence_id")),
        stage_dated_at=_text(data.get("stage_dated_at")),
        failure_conditions=_text_list(data.get("failure_conditions")),
        period=_text(data.get("period")),
        source_refs=_text_list(data.get("source_refs")),
        available_at=_text(data.get("available_at")),
        grounding_status=_text(data.get("grounding_status"), "missing"),
        note=_text(data.get("note")),
    )


def _dict_to_demand_drivers(data: list[dict[str, Any]] | None) -> list[DemandDriver]:
    """从 dict 列表还原 DemandDriver 列表。"""
    if not isinstance(data, list):
        return []
    if not data:
        return []
    result: list[DemandDriver] = []
    for item in data:
        item = _as_mapping(item)
        if not item:
            continue
        result.append(DemandDriver(
            name=_text(item.get("name")),
            direction=_text(item.get("direction")),
            magnitude=_safe_float(item.get("magnitude")),
            magnitude_unit=_text(item.get("magnitude_unit")),
            penetration_rate=_safe_float(item.get("penetration_rate")),
            period=_text(item.get("period")),
            source_refs=_text_list(item.get("source_refs")),
            available_at=_text(item.get("available_at")),
            heat_only=_safe_bool(item.get("heat_only")),
            grounding_status=_text(item.get("grounding_status"), "missing"),
            note=_text(item.get("note")),
        ))
    return result


def _dict_to_competitive_landscape(data: dict[str, Any] | None) -> CompetitiveLandscape | None:
    """从 dict 还原 CompetitiveLandscape。"""
    data = _as_mapping(data)
    if not data:
        return None
    raw_participants = data.get("participants", [])
    raw_participants = raw_participants if isinstance(raw_participants, list) else []
    participants: list[Participant] = []
    for p in raw_participants:
        p = _as_mapping(p)
        if not p:
            continue
        participants.append(Participant(
            name=_text(p.get("name")),
            symbol=_text(p.get("symbol")),
            market_share=_safe_float(p.get("market_share")),
            share_basis=_text(p.get("share_basis")),
            share_source_ref=_text(p.get("share_source_ref")),
            share_period=_text(p.get("share_period")),
            role=_text(p.get("role")),
        ))
    return CompetitiveLandscape(
        concentration_metric=_text(data.get("concentration_metric")),
        concentration_value=_safe_float(data.get("concentration_value")),
        hhi=_safe_float(data.get("hhi")),
        participants=participants,
        share_source_refs=_text_list(data.get("share_source_refs")),
        period=_text(data.get("period")),
        member_of_theme_excluded=_safe_bool(
            data.get("member_of_theme_excluded"), default=True,
        ),
        available_at=_text(data.get("available_at")),
        grounding_status=_text(data.get("grounding_status"), "missing"),
        note=_text(data.get("note")),
    )


def _dict_to_technology_routes(data: list[dict[str, Any]] | None) -> list[TechnologyRoute]:
    """从 dict 列表还原 TechnologyRoute 列表。"""
    if not isinstance(data, list):
        return []
    if not data:
        return []
    result: list[TechnologyRoute] = []
    for item in data:
        item = _as_mapping(item)
        if not item:
            continue
        result.append(TechnologyRoute(
            name=_text(item.get("name")),
            status=_text(item.get("status")),
            adoption_rate=_safe_float(item.get("adoption_rate")),
            alternatives=_text_list(item.get("alternatives")),
            certification_barriers=_text_list(item.get("certification_barriers")),
            period=_text(item.get("period")),
            source_refs=_text_list(item.get("source_refs")),
            available_at=_text(item.get("available_at")),
            grounding_status=_text(item.get("grounding_status"), "missing"),
            note=_text(item.get("note")),
        ))
    return result


def _dict_to_bargaining_powers(data: list[dict[str, Any]] | None) -> list[BargainingPower]:
    """从 dict 列表还原 BargainingPower 列表。"""
    if not isinstance(data, list):
        return []
    if not data:
        return []
    result: list[BargainingPower] = []
    for item in data:
        item = _as_mapping(item)
        if not item:
            continue
        result.append(BargainingPower(
            direction=_text(item.get("direction")),
            level=_text(item.get("level")),
            factors=_text_list(item.get("factors")),
            period=_text(item.get("period")),
            source_refs=_text_list(item.get("source_refs")),
            available_at=_text(item.get("available_at")),
            grounding_status=_text(item.get("grounding_status"), "missing"),
            note=_text(item.get("note")),
        ))
    return result


def _dict_to_company_mappings(data: list[dict[str, Any]] | None) -> list[CompanyMapping]:
    """从 dict 列表还原 CompanyMapping 列表。"""
    if not isinstance(data, list):
        return []
    if not data:
        return []
    result: list[CompanyMapping] = []
    for item in data:
        item = _as_mapping(item)
        if not item:
            continue
        result.append(CompanyMapping(
            symbol=_text(item.get("symbol")),
            industry_role=_text(item.get("industry_role")),
            exposure_to_industry=_safe_float(item.get("exposure_to_industry")),
            evidence_ids=_text_list(item.get("evidence_ids")),
            has_local_evidence=_safe_bool(item.get("has_local_evidence")),
            period=_text(item.get("period")),
            note=_text(item.get("note")),
        ))
    return result


def _dict_to_gaps(data: list[dict[str, Any]] | None) -> list[IndustryGap]:
    """从 dict 列表还原 IndustryGap 列表。"""
    if not isinstance(data, list):
        return []
    if not data:
        return []
    result: list[IndustryGap] = []
    for item in data:
        item = _as_mapping(item)
        if not item:
            continue
        result.append(IndustryGap(
            field_name=_text(item.get("field_name")),
            reason=_text(item.get("reason")),
            blocker_kind=_text(item.get("blocker_kind")),
            severity=_text(item.get("severity")),
            note=_text(item.get("note")),
        ))
    return result


def profile_from_dict(raw: dict[str, Any]) -> IndustryProfile:
    """从 dict 反序列化为 IndustryProfile。

    Args:
        raw: 序列化字典

    Returns:
        IndustryProfile 实例
    """
    data = _as_mapping(raw)
    return IndustryProfile(
        industry_name=_text(data.get("industry_name")),
        industry_code=_text(data.get("industry_code")),
        code_system=_text(data.get("code_system"), "sw"),
        period=_text(data.get("period")),
        market_sizing=_dict_to_market_sizing(data.get("market_sizing")),
        demand_drivers=_dict_to_demand_drivers(data.get("demand_drivers")),
        supply_profile=_dict_to_supply_profile(data.get("supply_profile")),
        price_cost_curve=_dict_to_price_cost_curve(data.get("price_cost_curve")),
        inventory_cycle=_dict_to_inventory_cycle(data.get("inventory_cycle")),
        competitive_landscape=_dict_to_competitive_landscape(data.get("competitive_landscape")),
        technology_routes=_dict_to_technology_routes(data.get("technology_routes")),
        bargaining_powers=_dict_to_bargaining_powers(data.get("bargaining_powers")),
        capex_cycle=_dict_to_capex_cycle(data.get("capex_cycle")),
        lifecycle_stage=_dict_to_lifecycle_stage(data.get("lifecycle_stage")),
        company_mappings=_dict_to_company_mappings(data.get("company_mappings")),
        gaps=_dict_to_gaps(data.get("gaps")),
        source_trace_id=_text(data.get("source_trace_id")),
        schema_version=_text(data.get("schema_version"), "industry_v1"),
    )


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------

class IndustryRepository:
    """产业画像 SQLite 持久化仓储。

    三张表分别存储经济画像、竞争画像、生命周期阶段；CfaReportBuilder 章节 3
    通过 ``get_latest_profile`` 取整合后的 IndustryProfile。

    SCHEMA_VERSION=1，幂等迁移；PIT 过滤 as_of <= query_as_of。

    设计要点：
    - 三张表的主键均为稳定 ID（stable_id），由调用方传入或自动生成
    - industry_economics / industry_competition 通过 company_symbol 索引；
      industry_lifecycle 通过 industry_code 索引（同行业共享生命周期）
    - get_latest_profile 取 economics + competition + lifecycle 合并为 IndustryProfile
    - 不输出投资建议（AGENTS.md）
    - 缺数据降级（AGENTS.md）：返回 None，调用方降级处理
    """

    SCHEMA_VERSION = 1

    _SCHEMA: dict[str, tuple[str, str, str, str]] = {
        # 表名 -> (主键列, 业务过滤列, 时间列, 业务列2)
        "industry_economics": (
            "economics_id", "company_symbol", "as_of", "industry_code",
        ),
        "industry_competition": (
            "competition_id", "company_symbol", "as_of", "industry_code",
        ),
        "industry_lifecycle": (
            "lifecycle_id", "industry_code", "as_of", "company_symbol",
        ),
    }

    def __init__(self, db_path: str | Path | None = None) -> None:
        """初始化仓储并执行幂等迁移。

        Args:
            db_path: SQLite 数据库路径。``None`` 时从环境变量
                ``INDUSTRY_DB_PATH`` 读取，默认
                ``data/research_control/industry.db``。
        """
        if db_path is None:
            db_path = os.getenv(
                "INDUSTRY_DB_PATH",
                str(PROJECT_ROOT / "data" / "research_control" / "industry.db"),
            )
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """获取 SQLite 连接上下文管理器。"""
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
        """创建/升级 schema；可重复执行（幂等）。

        三张表均使用 (主键, 业务列1, 时间列, 业务列2, payload_json) 五列结构，
        并对业务列建索引。
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS industry_schema_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            for table, (pk, col1, time_col, col2) in self._SCHEMA.items():
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"{pk} TEXT PRIMARY KEY, "
                    f"{col1} TEXT NOT NULL DEFAULT '', "
                    f"{time_col} TEXT NOT NULL DEFAULT '', "
                    f"{col2} TEXT NOT NULL DEFAULT '', "
                    f"payload_json TEXT NOT NULL)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{col1} ON {table}({col1})"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{time_col} ON {table}({time_col})"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{col2} ON {table}({col2})"
                )
            conn.execute(
                "INSERT INTO industry_schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(self.SCHEMA_VERSION),),
            )

    # ---- 通用 upsert / get / list ----

    def _upsert(
        self,
        table: str,
        pk: str,
        key: str,
        index_values: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """通用 upsert：主键冲突时更新业务列与 payload。

        Args:
            table: 表名
            pk: 主键列名
            key: 主键值
            index_values: 业务列与值（不含主键与 payload_json）
            payload: 完整 payload（序列化为 JSON）
        """
        self._validate_table(table, pk)
        expected_indexes = set(self._SCHEMA[table][1:])
        if set(index_values) != expected_indexes:
            raise ValueError("非法索引列")
        columns = [pk, *index_values.keys(), "payload_json"]
        values = [key, *index_values.values(), json.dumps(payload, ensure_ascii=False, allow_nan=False)]
        updates = ",".join(f"{col}=excluded.{col}" for col in columns[1:])
        placeholders = ",".join("?" for _ in columns)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders}) "
                f"ON CONFLICT({pk}) DO UPDATE SET {updates}",
                values,
            )

    def _get(
        self,
        table: str,
        pk: str,
        key: str,
    ) -> dict[str, Any] | None:
        """按主键查询单条记录的 payload。"""
        self._validate_table(table, pk)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload_json FROM {table} WHERE {pk}=?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _list(
        self,
        table: str,
        where: str = "",
        params: tuple[Any, ...] = (),
        limit: int = 100,
        order_by: str = "as_of DESC, rowid DESC",
    ) -> list[dict[str, Any]]:
        """通用列表查询，返回 payload dict 列表。"""
        self._validate_table(table)
        if order_by not in {"as_of DESC, rowid DESC", "as_of ASC, rowid ASC"}:
            raise ValueError("非法排序字段")
        query = f"SELECT payload_json FROM {table}"
        if where:
            query += f" WHERE {where}"
        query += f" ORDER BY {order_by} LIMIT ?"
        parsed_limit = _safe_int(limit)
        safe_limit = max(1, min(parsed_limit, 1000)) if parsed_limit is not None else 100
        with self._connect() as conn:
            rows = conn.execute(
                query, (*params, safe_limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                result.append(payload)
        return result

    # ---- Economics（经济画像）----

    def save_economics(
        self,
        company_symbol: str,
        industry_code: str,
        as_of: str,
        profile: IndustryProfile,
        economics_id: str | None = None,
    ) -> str:
        """保存经济画像快照（含 market_sizing / demand_drivers / supply_profile /
        price_cost_curve / inventory_cycle / capex_cycle / lifecycle_stage）。

        Args:
            company_symbol: 公司代码
            industry_code: 行业代码
            as_of: PIT 时点
            profile: IndustryProfile 实例（仅保存经济侧字段）
            economics_id: 可选稳定 ID；为空时基于 (symbol, industry_code, as_of) 生成

        Returns:
            实际写入的 economics_id
        """
        stable_id = economics_id or self._make_id(
            "econ", company_symbol, industry_code, as_of,
        )
        profile_dict = profile.to_dict()
        payload = {
            "economics_id": stable_id,
            "company_symbol": company_symbol,
            "industry_code": industry_code,
            "industry_name": profile_dict["industry_name"],
            "code_system": profile_dict["code_system"],
            "as_of": as_of,
            "period": profile_dict["period"],
            "source_trace_id": profile_dict["source_trace_id"],
            "schema_version": profile_dict["schema_version"],
            "market_sizing": profile_dict["market_sizing"],
            "demand_drivers": profile_dict["demand_drivers"],
            "supply_profile": profile_dict["supply_profile"],
            "price_cost_curve": profile_dict["price_cost_curve"],
            "inventory_cycle": profile_dict["inventory_cycle"],
            "capex_cycle": profile_dict["capex_cycle"],
            "lifecycle_stage": profile_dict["lifecycle_stage"],
        }
        self._upsert(
            "industry_economics",
            "economics_id",
            stable_id,
            {
                "company_symbol": company_symbol,
                "as_of": as_of,
                "industry_code": industry_code,
            },
            payload,
        )
        return stable_id

    def get_economics(self, economics_id: str) -> dict[str, Any] | None:
        """按 economics_id 查询经济画像。

        Args:
            economics_id: 经济画像稳定 ID

        Returns:
            payload dict；无返回 None
        """
        return self._get("industry_economics", "economics_id", economics_id)

    def list_economics(
        self,
        company_symbol: str = "",
        industry_code: str = "",
        as_of: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询经济画像列表，支持 company_symbol / industry_code / as_of 过滤。

        Args:
            company_symbol: 公司代码过滤
            industry_code: 行业代码过滤
            as_of: PIT 过滤，仅返回 as_of <= 该时点的记录
            limit: 上限，1-1000

        Returns:
            payload dict 列表，按 as_of DESC 排序
        """
        clauses: list[str] = []
        params: list[Any] = []
        if company_symbol:
            clauses.append("company_symbol=?")
            params.append(company_symbol)
        if industry_code:
            clauses.append("industry_code=?")
            params.append(industry_code)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        where = " AND ".join(clauses) if clauses else ""
        return self._list("industry_economics", where, tuple(params), limit)

    def get_latest_economics(
        self,
        company_symbol: str,
        as_of: str = "",
    ) -> dict[str, Any] | None:
        """按 PIT 取公司最新经济画像。

        Args:
            company_symbol: 公司代码（必填）
            as_of: PIT 时点；空表示不限

        Returns:
            最新经济画像 payload；无返回 None
        """
        if not company_symbol:
            return None
        items = self.list_economics(
            company_symbol=company_symbol, as_of=as_of, limit=1,
        )
        return items[0] if items else None

    # ---- Competition（竞争画像）----

    def save_competition(
        self,
        company_symbol: str,
        industry_code: str,
        as_of: str,
        profile: IndustryProfile,
        competition_id: str | None = None,
    ) -> str:
        """保存竞争画像快照（含 competitive_landscape / technology_routes /
        bargaining_powers / company_mappings / gaps）。

        Args:
            company_symbol: 公司代码
            industry_code: 行业代码
            as_of: PIT 时点
            profile: IndustryProfile 实例（仅保存竞争侧字段）
            competition_id: 可选稳定 ID；为空时自动生成

        Returns:
            实际写入的 competition_id
        """
        stable_id = competition_id or self._make_id(
            "comp", company_symbol, industry_code, as_of,
        )
        profile_dict = profile.to_dict()
        payload = {
            "competition_id": stable_id,
            "company_symbol": company_symbol,
            "industry_code": industry_code,
            "industry_name": profile_dict["industry_name"],
            "code_system": profile_dict["code_system"],
            "as_of": as_of,
            "period": profile_dict["period"],
            "source_trace_id": profile_dict["source_trace_id"],
            "schema_version": profile_dict["schema_version"],
            "competitive_landscape": profile_dict["competitive_landscape"],
            "technology_routes": profile_dict["technology_routes"],
            "bargaining_powers": profile_dict["bargaining_powers"],
            "company_mappings": profile_dict["company_mappings"],
            "gaps": profile_dict["gaps"],
        }
        self._upsert(
            "industry_competition",
            "competition_id",
            stable_id,
            {
                "company_symbol": company_symbol,
                "as_of": as_of,
                "industry_code": industry_code,
            },
            payload,
        )
        return stable_id

    def get_competition(self, competition_id: str) -> dict[str, Any] | None:
        """按 competition_id 查询竞争画像。

        Args:
            competition_id: 竞争画像稳定 ID

        Returns:
            payload dict；无返回 None
        """
        return self._get("industry_competition", "competition_id", competition_id)

    def list_competition(
        self,
        company_symbol: str = "",
        industry_code: str = "",
        as_of: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询竞争画像列表，支持 company_symbol / industry_code / as_of 过滤。

        Args:
            company_symbol: 公司代码过滤
            industry_code: 行业代码过滤
            as_of: PIT 过滤
            limit: 上限

        Returns:
            payload dict 列表，按 as_of DESC 排序
        """
        clauses: list[str] = []
        params: list[Any] = []
        if company_symbol:
            clauses.append("company_symbol=?")
            params.append(company_symbol)
        if industry_code:
            clauses.append("industry_code=?")
            params.append(industry_code)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        where = " AND ".join(clauses) if clauses else ""
        return self._list("industry_competition", where, tuple(params), limit)

    def get_latest_competition(
        self,
        company_symbol: str,
        as_of: str = "",
    ) -> dict[str, Any] | None:
        """按 PIT 取公司最新竞争画像。

        Args:
            company_symbol: 公司代码（必填）
            as_of: PIT 时点；空表示不限

        Returns:
            最新竞争画像 payload；无返回 None
        """
        if not company_symbol:
            return None
        items = self.list_competition(
            company_symbol=company_symbol, as_of=as_of, limit=1,
        )
        return items[0] if items else None

    # ---- Lifecycle（生命周期，按行业维度共享）----

    def save_lifecycle(
        self,
        industry_code: str,
        as_of: str,
        lifecycle: LifecycleStage,
        company_symbol: str = "",
        industry_name: str = "",
        code_system: str = "sw",
        source_trace_id: str = "",
        schema_version: str = "industry_v1",
        lifecycle_id: str | None = None,
    ) -> str:
        """保存行业生命周期阶段快照。

        生命周期按 industry_code 共享（同行业同阶段）；company_symbol 字段记录
        触发公司，便于多公司查询。

        Args:
            industry_code: 行业代码（必填）
            as_of: PIT 时点
            lifecycle: LifecycleStage 实例
            company_symbol: 触发公司代码（可空）
            industry_name: 行业名
            code_system: 代码体系
            source_trace_id: 来源追溯 ID
            schema_version: 契约版本
            lifecycle_id: 可选稳定 ID；为空时自动生成

        Returns:
            实际写入的 lifecycle_id
        """
        if not industry_code:
            raise ValueError("industry_code 必填")
        stable_id = lifecycle_id or self._make_id(
            "life", industry_code, company_symbol, as_of,
        )
        payload = {
            "lifecycle_id": stable_id,
            "industry_code": industry_code,
            "industry_name": industry_name,
            "code_system": code_system,
            "company_symbol": company_symbol,
            "as_of": as_of,
            "period": lifecycle.period,
            "source_trace_id": source_trace_id,
            "schema_version": schema_version,
            "lifecycle_stage": lifecycle.to_dict(),
        }
        self._upsert(
            "industry_lifecycle",
            "lifecycle_id",
            stable_id,
            {
                "industry_code": industry_code,
                "as_of": as_of,
                "company_symbol": company_symbol,
            },
            payload,
        )
        return stable_id

    def get_lifecycle(self, lifecycle_id: str) -> dict[str, Any] | None:
        """按 lifecycle_id 查询生命周期阶段。

        Args:
            lifecycle_id: 生命周期稳定 ID

        Returns:
            payload dict；无返回 None
        """
        return self._get("industry_lifecycle", "lifecycle_id", lifecycle_id)

    def list_lifecycle(
        self,
        industry_code: str = "",
        company_symbol: str = "",
        as_of: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询生命周期列表，支持 industry_code / company_symbol / as_of 过滤。

        Args:
            industry_code: 行业代码过滤
            company_symbol: 公司代码过滤
            as_of: PIT 过滤
            limit: 上限

        Returns:
            payload dict 列表，按 as_of DESC 排序
        """
        clauses: list[str] = []
        params: list[Any] = []
        if industry_code:
            clauses.append("industry_code=?")
            params.append(industry_code)
        if company_symbol:
            clauses.append("company_symbol=?")
            params.append(company_symbol)
        if as_of:
            clauses.append("as_of<>'' AND as_of<=?")
            params.append(as_of)
        where = " AND ".join(clauses) if clauses else ""
        return self._list("industry_lifecycle", where, tuple(params), limit)

    def get_latest_lifecycle(
        self,
        industry_code: str,
        as_of: str = "",
    ) -> dict[str, Any] | None:
        """按 PIT 取行业最新生命周期阶段。

        Args:
            industry_code: 行业代码（必填）
            as_of: PIT 时点；空表示不限

        Returns:
            最新生命周期 payload；无返回 None
        """
        if not industry_code:
            return None
        items = self.list_lifecycle(
            industry_code=industry_code, as_of=as_of, limit=1,
        )
        return items[0] if items else None

    # ---- 顶层聚合：get_latest_profile / get_profile ----

    def get_latest_profile(
        self,
        company_symbol: str,
        as_of: str = "",
    ) -> IndustryProfile | None:
        """按 PIT 取公司最新完整产业画像（经济 + 竞争 + 生命周期合并）。

        用于 CfaReportBuilder 章节 3 构建。任一部分缺失时仍返回合并后的
        IndustryProfile，调用方按 grounding_status 区分降级。

        Args:
            company_symbol: 公司代码（必填）
            as_of: PIT 时点；空表示不限

        Returns:
            合并后的 IndustryProfile；若经济 + 竞争 + 生命周期三部分均缺，返回 None
        """
        if not company_symbol:
            return None
        economics = self.get_latest_economics(company_symbol, as_of=as_of)
        competition = self.get_latest_competition(company_symbol, as_of=as_of)
        # 生命周期按 industry_code 取（优先用 economics / competition 中的 industry_code）
        industry_code = ""
        if economics:
            industry_code = str(economics.get("industry_code", ""))
        if not industry_code and competition:
            industry_code = str(competition.get("industry_code", ""))
        lifecycle = (
            self.get_latest_lifecycle(industry_code, as_of=as_of)
            if industry_code else None
        )
        if not economics and not competition and not lifecycle:
            return None
        return self._merge_profile(economics, competition, lifecycle)

    def get_profile(
        self,
        company_symbol: str,
        as_of: str = "",
    ) -> IndustryProfile | None:
        """get_latest_profile 的兼容别名（与 CfaReportBuilder 历史调用一致）。"""
        return self.get_latest_profile(company_symbol, as_of=as_of)

    def _merge_profile(
        self,
        economics: dict[str, Any] | None,
        competition: dict[str, Any] | None,
        lifecycle: dict[str, Any] | None,
    ) -> IndustryProfile:
        """合并经济 / 竞争 / 生命周期三部分为 IndustryProfile。

        Args:
            economics: 经济画像 payload（可空）
            competition: 竞争画像 payload（可空）
            lifecycle: 生命周期 payload（可空）

        Returns:
            合并后的 IndustryProfile
        """
        eco = economics or {}
        comp = competition or {}
        life = lifecycle or {}

        # 基础字段优先取 economics，回退到 competition
        industry_name = _text(eco.get("industry_name") or comp.get("industry_name"))
        industry_code = _text(eco.get("industry_code") or comp.get("industry_code"))
        code_system = _text(
            eco.get("code_system") or comp.get("code_system"), "sw",
        )
        period = _text(eco.get("period") or comp.get("period"))
        source_trace_id = _text(
            eco.get("source_trace_id") or comp.get("source_trace_id") or ""
        )
        schema_version = _text(
            eco.get("schema_version") or comp.get("schema_version"), "industry_v1"
        )

        # 经济侧字段（仅来自 economics）
        market_sizing = _dict_to_market_sizing(eco.get("market_sizing"))
        demand_drivers = _dict_to_demand_drivers(eco.get("demand_drivers"))
        supply_profile = _dict_to_supply_profile(eco.get("supply_profile"))
        price_cost_curve = _dict_to_price_cost_curve(eco.get("price_cost_curve"))
        inventory_cycle = _dict_to_inventory_cycle(eco.get("inventory_cycle"))
        capex_cycle = _dict_to_capex_cycle(eco.get("capex_cycle"))

        # 生命周期：economics.lifecycle_stage 优先，回退到 lifecycle 表
        lifecycle_stage = _dict_to_lifecycle_stage(eco.get("lifecycle_stage"))
        if lifecycle_stage is None and life:
            lifecycle_stage = _dict_to_lifecycle_stage(life.get("lifecycle_stage"))

        # 竞争侧字段（仅来自 competition）
        competitive_landscape = _dict_to_competitive_landscape(
            comp.get("competitive_landscape")
        )
        technology_routes = _dict_to_technology_routes(
            comp.get("technology_routes")
        )
        bargaining_powers = _dict_to_bargaining_powers(
            comp.get("bargaining_powers")
        )
        company_mappings = _dict_to_company_mappings(
            comp.get("company_mappings")
        )
        gaps = _dict_to_gaps(comp.get("gaps"))

        return IndustryProfile(
            industry_name=industry_name,
            industry_code=industry_code,
            code_system=code_system,
            period=period,
            market_sizing=market_sizing,
            demand_drivers=demand_drivers,
            supply_profile=supply_profile,
            price_cost_curve=price_cost_curve,
            inventory_cycle=inventory_cycle,
            competitive_landscape=competitive_landscape,
            technology_routes=technology_routes,
            bargaining_powers=bargaining_powers,
            capex_cycle=capex_cycle,
            lifecycle_stage=lifecycle_stage,
            company_mappings=company_mappings,
            gaps=gaps,
            source_trace_id=source_trace_id,
            schema_version=schema_version,
        )

    # ---- 工具 ----

    def _validate_table(self, table: str, pk: str | None = None) -> None:
        """Restrict internal dynamic SQL identifiers to the schema whitelist."""
        spec = self._SCHEMA.get(table)
        if spec is None:
            raise ValueError(f"未知产业仓储表: {table}")
        if pk is not None and pk != spec[0]:
            raise ValueError(f"非法主键列: {pk}")

    @staticmethod
    def _make_id(prefix: str, *parts: str) -> str:
        """基于业务字段生成稳定 ID（同输入产生同 ID，便于幂等）。

        Args:
            prefix: ID 前缀（如 econ / comp / life）
            parts: 业务字段元组

        Returns:
            形如 "ind_econ_<sha256_16>" 的稳定 ID
        """
        import hashlib
        raw = "|".join(str(p) for p in parts if p)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"ind_{prefix}_{digest}"

    def stats(self) -> dict[str, Any]:
        """返回仓储统计信息。"""
        with self._connect() as conn:
            counts: dict[str, int] = {}
            for table in self._SCHEMA:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = int(row[0]) if row else 0
            version_row = conn.execute(
                "SELECT value FROM industry_schema_meta WHERE key='schema_version'"
            ).fetchone()
        return {
            "schema_version": (_safe_int(version_row[0]) or 0) if version_row else 0,
            "counts": counts,
        }
