"""主动学习计划：把 EvidenceGap 变成高价值信息获取任务。

解决差距 5：有 Evidence Gap，但没有"主动学习计划"。

核心能力：
  1. 计算"补这个缺口的信息价值"（预期置信度提升）
  2. 估算搜索成本（按数据源类型）
  3. 按价值/成本比排序，生成学习计划
  4. 跟踪执行前后置信度变化
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Mapping


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return normalized if math.isfinite(normalized) else default


def _text(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


@dataclass
class LearningPlanItem:
    """单个学习/补证据任务。

    Attributes:
        plan_id: 任务唯一 ID。
        gap_id: 关联 EvidenceGap ID。
        root_theme: 所属主题/根概念。
        path: 关联路径节点列表。
        missing_entity: 缺失证据的实体。
        missing_relation: 缺失的关系类型。
        missing_source_type: 推荐数据源类型。
        current_confidence: 当前路径/观点置信度。
        expected_confidence_after: 补上缺口后的预期置信度。
        information_value: 信息价值 = 预期置信度提升。
        estimated_search_cost: 估算搜索成本（相对工时，1-10）。
        rank_score: 排序分 = information_value / (cost + 1)。
        suggested_queries: 推荐搜索查询。
        status: 任务状态 pending / scheduled / completed / expired。
        created_at: 创建时间 ISO。
        executed_at: 执行时间 ISO。
        before_after_delta: 执行后实际置信度变化（完成后填写）。
        notes: 备注。
    """

    plan_id: str
    gap_id: str
    root_theme: str = ""
    path: list[str] = field(default_factory=list)
    missing_entity: str = ""
    missing_relation: str = ""
    missing_source_type: str = ""
    current_confidence: float = 0.0
    expected_confidence_after: float = 0.0
    information_value: float = 0.0
    estimated_search_cost: float = 1.0
    rank_score: float = 0.0
    suggested_queries: list[str] = field(default_factory=list)
    status: str = "pending"
    created_at: str = ""
    executed_at: str = ""
    before_after_delta: float = 0.0
    notes: str = ""

    def __post_init__(self) -> None:
        """补全默认值并校验状态。"""
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if self.status not in {"pending", "scheduled", "completed", "expired"}:
            self.status = "pending"

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict。"""
        return {
            "plan_id": self.plan_id,
            "gap_id": self.gap_id,
            "root_theme": self.root_theme,
            "path": [item for item in self.path if isinstance(item, str)] if isinstance(self.path, (list, tuple)) else [],
            "missing_entity": self.missing_entity,
            "missing_relation": self.missing_relation,
            "missing_source_type": self.missing_source_type,
            "current_confidence": round(_safe_float(self.current_confidence), 4),
            "expected_confidence_after": round(_safe_float(self.expected_confidence_after), 4),
            "information_value": round(_safe_float(self.information_value), 4),
            "estimated_search_cost": round(_safe_float(self.estimated_search_cost), 4),
            "rank_score": round(_safe_float(self.rank_score), 4),
            "suggested_queries": [item for item in self.suggested_queries if isinstance(item, str)] if isinstance(self.suggested_queries, (list, tuple)) else [],
            "status": self.status,
            "created_at": self.created_at,
            "executed_at": self.executed_at,
            "before_after_delta": round(_safe_float(self.before_after_delta), 4),
            "notes": self.notes,
        }


class ActiveLearningPlanner:
    """把 EvidenceGap 排序为高价值学习计划。

    规则化实现，不引入外部 ML：
      - 信息价值 = 补缺口后预期置信度 - 当前置信度
      - 预期置信度基于缺口类型和当前路径 alpha 分估算
      - 搜索成本按数据源类型离线/在线、公开/付费划分
      - 排序分 = 信息价值 / (成本 + 1)
    """

    # 数据源相对成本：数值越大越贵（时间/金钱/获取难度）
    _SOURCE_COST: dict[str, float] = {
        "网页搜索": 1.0,
        "web_search": 1.0,
        "公告": 2.0,
        "年报": 3.0,
        "研报": 3.5,
        "专利": 4.0,
        "白皮书": 3.0,
        "问财": 1.5,
        "东财": 1.5,
        "unknown": 2.0,
    }

    # 不同缺口类型补完后的预期置信度提升上限
    _GAP_TYPE_VALUE_CAP: dict[str, float] = {
        "evidence": 0.15,
        "relation": 0.20,
        "path_evidence": 0.25,
        "multi_source": 0.10,
        "bottleneck": 0.20,
    }

    def __init__(self, storage_path: str | Path | None = None) -> None:
        """初始化。

        Args:
            storage_path: 学习计划持久化路径，默认 data/research_runs/learning_plans。
        """
        if storage_path is None:
            project_root = Path(__file__).resolve().parents[3]
            storage_path = project_root / "data" / "research_runs" / "learning_plans"
        self._storage_path = Path(storage_path)
        self._storage_path.mkdir(parents=True, exist_ok=True)

    def plan_from_gaps(
        self,
        evidence_gaps: list[Any],
        root_theme: str = "",
        current_confidence: float = 0.0,
        path_alpha_score: float = 0.0,
        *,
        reference_date: str = "",
    ) -> list[LearningPlanItem]:
        """从 EvidenceGap 列表生成学习计划。

        Args:
            evidence_gaps: EvidenceGap 对象或兼容 dict 列表。
            root_theme: 所属主题。
            current_confidence: 当前路径/观点置信度。
            path_alpha_score: 路径 alpha 分，用于加权信息价值。
            reference_date: 参考日期。

        Returns:
            按 rank_score 降序排列的 LearningPlanItem 列表。
        """
        plans: list[LearningPlanItem] = []
        raw_gaps = evidence_gaps if isinstance(evidence_gaps, (list, tuple)) else []
        for gap in raw_gaps:
            plan = self._build_plan_item(
                gap,
                root_theme=root_theme,
                current_confidence=current_confidence,
                path_alpha_score=path_alpha_score,
                reference_date=reference_date,
            )
            if plan:
                plans.append(plan)

        plans.sort(key=lambda p: p.rank_score, reverse=True)
        return plans

    def plan_from_alpha_result(
        self,
        alpha_result: dict[str, Any],
        *,
        reference_date: str = "",
    ) -> list[LearningPlanItem]:
        """从 AlphaDiscoveryEngine.discover() 结果批量生成学习计划。

        Args:
            alpha_result: discover() 返回结果字典。
            reference_date: 参考日期。

        Returns:
            排序后的 LearningPlanItem 列表。
        """
        alpha_result = alpha_result if isinstance(alpha_result, Mapping) else {}
        root_theme = str(alpha_result.get("root", "") or "")
        alpha_path_results = alpha_result.get("alpha_path_results", [])
        alpha_path_results = alpha_path_results if isinstance(alpha_path_results, (list, tuple)) else []
        evidence_gaps = alpha_result.get("evidence_gaps", [])
        evidence_gaps = evidence_gaps if isinstance(evidence_gaps, (list, tuple)) else []

        # 把每个 gap 关联到对应路径的置信度
        path_confidence_by_node: dict[str, float] = {}
        for apr in alpha_path_results:
            path = apr.get("path") if isinstance(apr, Mapping) else getattr(apr, "path", [])
            conf = (
                apr.get("path_confidence", 0.0)
                if isinstance(apr, Mapping)
                else getattr(apr, "path_confidence", 0.0)
            )
            path = path if isinstance(path, (list, tuple)) else []
            conf = _safe_float(conf)
            for node in path:
                if isinstance(node, str):
                    path_confidence_by_node[node] = max(path_confidence_by_node.get(node, 0.0), conf)

        plans: list[LearningPlanItem] = []
        for gap in evidence_gaps:
            try:
                gap_dict = gap.to_dict() if hasattr(gap, "to_dict") else dict(gap)
            except (TypeError, ValueError):
                continue
            if not isinstance(gap_dict, Mapping):
                continue
            gap_dict = dict(gap_dict)
            path = gap_dict.get("path", [])
            path_conf = max(
                current_confidence_for_path(path, path_confidence_by_node),
                _safe_float(gap_dict.get("priority", 0.0)),
            )
            alpha_score = _safe_float(gap_dict.get("priority", 0.0))
            plans.extend(
                self.plan_from_gaps(
                    [gap],
                    root_theme=root_theme,
                    current_confidence=path_conf,
                    path_alpha_score=alpha_score,
                    reference_date=reference_date,
                )
            )

        # 去重并按 rank_score 排序
        seen: set[str] = set()
        unique: list[LearningPlanItem] = []
        for p in plans:
            if p.gap_id not in seen:
                seen.add(p.gap_id)
                unique.append(p)
        unique.sort(key=lambda p: p.rank_score, reverse=True)
        return unique

    def mark_scheduled(self, plans: list[LearningPlanItem]) -> list[LearningPlanItem]:
        """把计划标记为已安排（可映射到定时任务）。"""
        if not isinstance(plans, (list, tuple)):
            return []
        for p in plans:
            if p.status == "pending":
                p.status = "scheduled"
        return list(plans)

    def mark_executed(
        self,
        plan: LearningPlanItem,
        confidence_after: float,
    ) -> LearningPlanItem:
        """标记计划已执行并记录实际置信度变化。

        Args:
            plan: 学习任务。
            confidence_after: 执行后的实际置信度。

        Returns:
            更新后的 LearningPlanItem。
        """
        if not isinstance(plan, LearningPlanItem):
            raise TypeError("plan must be a LearningPlanItem")
        plan.status = "completed"
        plan.executed_at = datetime.now(timezone.utc).isoformat()
        plan.before_after_delta = round(_safe_float(confidence_after) - _safe_float(plan.current_confidence), 4)
        return plan

    def save_plans(
        self,
        plans: list[LearningPlanItem],
        filename: str = "learning_plan_latest.json",
    ) -> Path:
        """持久化学习计划到 JSON。

        Args:
            plans: 学习计划列表。
            filename: 文件名。

        Returns:
            保存路径。
        """
        path = self._safe_storage_path(filename)
        safe_plans = [plan for plan in plans if isinstance(plan, LearningPlanItem)] if isinstance(plans, (list, tuple)) else []
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "plan_count": len(safe_plans),
            "plans": [p.to_dict() for p in safe_plans],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_plans(self, filename: str = "learning_plan_latest.json") -> list[LearningPlanItem]:
        """从 JSON 加载学习计划。"""
        path = self._safe_storage_path(filename)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(data, Mapping) or not isinstance(data.get("plans"), list):
            return []
        plans: list[LearningPlanItem] = []
        for item in data["plans"]:
            if not isinstance(item, Mapping):
                continue
            try:
                plan = LearningPlanItem(**dict(item))
            except (TypeError, ValueError):
                continue
            if plan.gap_id:
                plans.append(plan)
        return plans

    def _safe_storage_path(self, filename: str) -> Path:
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("filename must be a non-empty relative filename")
        candidate = Path(filename)
        base = self._storage_path.resolve()
        resolved = (base / candidate).resolve()
        if candidate.is_absolute() or base not in resolved.parents:
            raise ValueError("filename must stay within the learning plan storage directory")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def _build_plan_item(
        self,
        gap: Any,
        root_theme: str,
        current_confidence: float,
        path_alpha_score: float,
        reference_date: str,
    ) -> LearningPlanItem | None:
        """从单个 EvidenceGap 构造 LearningPlanItem。"""
        try:
            gap_dict = gap.to_dict() if hasattr(gap, "to_dict") else dict(gap)
        except (TypeError, ValueError):
            return None
        if not isinstance(gap_dict, Mapping):
            return None
        gap_dict = dict(gap_dict)
        gap_id = _text(gap_dict.get("gap_id"))
        if not gap_id:
            return None

        missing_relation = _text(gap_dict.get("missing_relation"), "evidence")
        missing_source = _text(gap_dict.get("missing_source_type"), "unknown")
        cost = self._estimate_cost(missing_source)

        # 信息价值：基于当前置信度缺口和路径 alpha 分
        current_confidence = max(0.0, min(1.0, _safe_float(current_confidence)))
        path_alpha_score = max(0.0, min(1.0, _safe_float(path_alpha_score)))
        value_cap = self._GAP_TYPE_VALUE_CAP.get(missing_relation, 0.15)
        confidence_gap = max(0.0, 0.85 - current_confidence)
        information_value = min(value_cap, confidence_gap) * (0.5 + 0.5 * path_alpha_score)
        expected_after = min(1.0, current_confidence + information_value)

        # 排序分：价值 / 成本，稍微惩罚低 alpha 路径
        rank_score = information_value / (cost + 1.0)

        suggested_queries = self._suggested_queries(gap_dict, root_theme)

        return LearningPlanItem(
            plan_id=f"plan_{uuid.uuid4().hex[:12]}",
            gap_id=gap_id,
            root_theme=root_theme,
            path=[item for item in gap_dict.get("path", []) if isinstance(item, str)] if isinstance(gap_dict.get("path"), (list, tuple)) else [],
            missing_entity=_text(gap_dict.get("missing_entity")),
            missing_relation=missing_relation,
            missing_source_type=missing_source,
            current_confidence=round(current_confidence, 4),
            expected_confidence_after=round(expected_after, 4),
            information_value=round(information_value, 4),
            estimated_search_cost=round(cost, 2),
            rank_score=round(rank_score, 4),
            suggested_queries=suggested_queries,
            notes=f"参考日期:{reference_date or 'latest'}",
        )

    def _estimate_cost(self, source_type: str) -> float:
        """按数据源类型估算相对搜索成本。"""
        source_type = _text(source_type)
        for key, cost in self._SOURCE_COST.items():
            if key in source_type:
                return cost
        return self._SOURCE_COST["unknown"]

    @staticmethod
    def _suggested_queries(gap_dict: dict[str, Any], root_theme: str) -> list[str]:
        """生成推荐搜索查询。"""
        entity = _text(gap_dict.get("missing_entity"))
        relation = _text(gap_dict.get("missing_relation"))
        queries: list[str] = []
        if entity:
            queries.append(f"{entity} {relation}")
        if root_theme and entity:
            queries.append(f"{root_theme} {entity} 最新")
        if not queries:
            queries.append(f"{root_theme} {relation} 证据")
        return queries


def current_confidence_for_path(
    path: list[str],
    node_confidence: dict[str, float],
) -> float:
    """取路径上节点置信度的均值作为路径置信度代理。"""
    if not path:
        return 0.0
    if not isinstance(path, (list, tuple)) or not isinstance(node_confidence, Mapping):
        return 0.0
    scores = [_safe_float(node_confidence.get(node, 0.0)) for node in path if isinstance(node, str)]
    if not scores:
        return 0.0
    return sum(scores) / len(scores)
