"""P3 产业链知识图谱与缺失边发现。

将 candidate_concepts / evidence_edges / causal_chains 连接为产业链图谱，
自动发现缺失边并编译搜索任务。

节点类型限定 9 类：theme / product_platform / technology_route / component /
material / upstream_resource / investment_logic / company / unknown

关系限定 5 类：belongs_to / co_occurs_with / possible_upstream_of /
possible_downstream_of / needs_validation

confidence_level 禁止 probable / verified，只允许 hypothesis / weak_signal。
禁止 supplies / confirmed_supplier / substitutes / evolves_to / uses_material。

输入：candidate_concepts / evidence_edges / causal_chains / concept_scores
输出：industry_chain_graph_latest.json / missing_edges_latest.json /
      edge_validation_history_latest.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# 常量定义
# ============================================================================

_DEFAULT_INPUT_DIR = Path("data/reports/perilla_discovery")
_DEFAULT_OUTPUT_DIR = Path("data/reports/perilla_discovery")

# 节点类型限定 9 类
_ALLOWED_NODE_TYPES: Set[str] = {
    "theme", "product_platform", "technology_route", "component",
    "material", "upstream_resource", "investment_logic", "company", "unknown",
}

# 关系限定 5 类
_ALLOWED_RELATIONS: Set[str] = {
    "belongs_to", "co_occurs_with", "possible_upstream_of",
    "possible_downstream_of", "needs_validation",
}

# 禁止的关系
_FORBIDDEN_RELATIONS: Set[str] = {
    "supplies", "confirmed_supplier", "substitutes", "evolves_to", "uses_material",
}

# confidence_level 只允许 hypothesis / weak_signal
_ALLOWED_EDGE_CONFIDENCE: Set[str] = {"hypothesis", "weak_signal"}

# 缺失边检测模板（按 source_type 分类）
# product_platform 高分节点缺少的下钻边
_PRODUCT_PLATFORM_DOWNSTREAM: List[str] = [
    "PCB", "光模块", "连接器", "电源", "液冷", "高端铜箔", "电子布", "PTFE", "特种树脂",
]

# technology_route 高分节点缺少的下钻边
_TECH_ROUTE_DOWNSTREAM: List[str] = [
    "光模块", "光芯片", "光引擎", "激光器", "磷化铟", "硅光", "薄膜铌酸锂",
]

# component 高分节点缺少的下钻边
_COMPONENT_DOWNSTREAM: List[str] = [
    "PCB", "连接器", "电源", "散热", "基板",
]

# material 高分节点缺少的下钻边
_MATERIAL_DOWNSTREAM: List[str] = [
    "上游原料", "产能", "供应商", "价格", "认证",
]

# 按节点类型映射到下钻模板
_DOWNSTREAM_TEMPLATES: Dict[str, List[str]] = {
    "product_platform": _PRODUCT_PLATFORM_DOWNSTREAM,
    "technology_route": _TECH_ROUTE_DOWNSTREAM,
    "component": _COMPONENT_DOWNSTREAM,
    "material": _MATERIAL_DOWNSTREAM,
}


# ============================================================================
# 工具函数
# ============================================================================


def _short_hash(text: str) -> str:
    """计算短哈希。"""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    """当前时间 ISO 格式。"""
    return datetime.now().isoformat()


def _mapping_records(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _safe_text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) and not isinstance(value, complex):
        return str(value)
    return default


def _safe_text_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := _safe_text(item)).strip()]


def _safe_float01(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if not math.isfinite(number):
            return default
        return max(0.0, min(1.0, number))
    except (TypeError, ValueError, OverflowError):
        return default


# ============================================================================
# 核心函数实现
# ============================================================================


def load_inputs(input_dir: Optional[Path] = None) -> Dict[str, Any]:
    """加载 P3 输入数据。

    读取 candidate_concepts / evidence_edges / causal_chains / concept_scores。

    Args:
        input_dir: 输入目录，默认为 data/reports/perilla_discovery/

    Returns:
        dict，含 candidate_concepts / evidence_edges / causal_chains / concept_scores
    """
    result: Dict[str, Any] = {
        "candidate_concepts": [],
        "evidence_edges": [],
        "causal_chains": [],
        "concept_scores": [],
    }
    try:
        base = Path(input_dir) if input_dir is not None else _DEFAULT_INPUT_DIR
    except (TypeError, ValueError, OSError):
        return result

    for key, filename in [
        ("candidate_concepts", "candidate_concepts_latest.json"),
        ("evidence_edges", "evidence_edges_latest.json"),
        ("causal_chains", "causal_chains_latest.json"),
        ("concept_scores", "concept_scores_latest.json"),
    ]:
        path = base / filename
        if path.exists():
            try:
                result[key] = _mapping_records(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("读取 %s 失败: %s", filename, exc)

    return result


def build_nodes(candidate_concepts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 candidate_concepts 生成产业链节点。

    节点类型限定 9 类：theme / product_platform / technology_route / component /
    material / upstream_resource / investment_logic / company / unknown

    Args:
        candidate_concepts: P0/P2 的 candidate_concepts 列表

    Returns:
        节点列表，每条含 node_id / node_name / node_type / root_theme /
        source_count / final_score / evidence_document_ids / status
    """
    nodes: List[Dict[str, Any]] = []
    seen_names: Set[str] = set()

    for concept in _mapping_records(candidate_concepts):
        name = _safe_text(
            concept.get("normalized_name") or concept.get("concept_name")
        ).strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)

        # 映射 term_type 到 node_type
        term_type = _safe_text(concept.get("term_type"), "unclassified")
        node_type = term_type if term_type in _ALLOWED_NODE_TYPES else "unknown"

        nodes.append({
            "node_id": f"node_{_short_hash(name)}",
            "node_name": name,
            "node_type": node_type,
            "root_theme": _safe_text(concept.get("root_theme")),
            "source_count": len(_safe_text_list(concept.get("evidence_document_ids"))),
            "final_score": _safe_float01(concept.get("final_score")),
            "evidence_document_ids": _safe_text_list(concept.get("evidence_document_ids"))[:10],
            "status": "active",
        })

    return nodes


def build_allowed_edges(
    candidate_concepts: List[Dict[str, Any]],
    evidence_edges: List[Dict[str, Any]],
    causal_chains: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """构建允许的关系边。

    保留 belongs_to / co_occurs_with，从 causal_chains 生成
    cause → possible_downstream_of → beneficiary_node。

    confidence_level 只允许 hypothesis / weak_signal（禁止 probable / verified）。
    禁止 supplies / confirmed_supplier / substitutes / evolves_to / uses_material。

    Args:
        candidate_concepts: candidate_concepts 列表
        evidence_edges: P0 的 evidence_edges 列表
        causal_chains: P1 的 causal_chains 列表

    Returns:
        边列表，每条含 edge_id / source_node / source_type / relation /
        target_node / target_type / confidence_level / evidence_document_ids /
        evidence_quotes / status / created_at
    """
    edges: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()

    # 1. 从 evidence_edges 保留 belongs_to / co_occurs_with
    for edge in _mapping_records(evidence_edges):
        relation = _safe_text(edge.get("relation"))
        if relation not in _ALLOWED_RELATIONS:
            continue
        if relation in _FORBIDDEN_RELATIONS:
            continue

        source_node = _safe_text(edge.get("source_node")).strip()
        target_node = _safe_text(edge.get("target_node")).strip()
        if not source_node or not target_node:
            continue

        key = f"{source_node}|{relation}|{target_node}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        # confidence_level 强制限定为 hypothesis / weak_signal
        raw_confidence = _safe_text(edge.get("confidence_level"), "hypothesis")
        confidence_level = raw_confidence if raw_confidence in _ALLOWED_EDGE_CONFIDENCE else "hypothesis"

        edges.append({
            "edge_id": f"edge_{_short_hash(key)}",
            "source_node": source_node,
            "source_type": _safe_text(edge.get("source_type"), "unknown"),
            "relation": relation,
            "target_node": target_node,
            "target_type": _safe_text(edge.get("target_type"), "unknown"),
            "confidence_level": confidence_level,
            "evidence_document_ids": _safe_text_list(edge.get("evidence_document_ids"))[:8],
            "evidence_quotes": _safe_text_list(edge.get("evidence_quotes"))[:3],
            "status": "pending" if confidence_level == "hypothesis" else "weak_signal",
            "created_at": _now_iso(),
        })

    # 2. 从 causal_chains 生成 cause → possible_downstream_of → beneficiary_node
    for chain in _mapping_records(causal_chains):
        cause = _safe_text(chain.get("cause")).strip()
        beneficiary = _safe_text(chain.get("beneficiary_node")).strip()
        if not cause or not beneficiary or cause == "未明确" or beneficiary == "未明确":
            continue

        relation = "possible_downstream_of"
        key = f"{cause}|{relation}|{beneficiary}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        edges.append({
            "edge_id": f"edge_{_short_hash(key)}",
            "source_node": cause,
            "source_type": "unknown",
            "relation": relation,
            "target_node": beneficiary,
            "target_type": "unknown",
            "confidence_level": "hypothesis",  # 从 causal_chain 生成的边默认 hypothesis
            "evidence_document_ids": _safe_text_list(chain.get("document_id")),
            "evidence_quotes": _safe_text_list(chain.get("evidence_quote")),
            "status": "pending",
            "created_at": _now_iso(),
        })

    return edges


def detect_missing_edges(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    priority_weights: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """检测缺失的下钻边。

    按四类模板检测：
    - product_platform 高分节点缺少 PCB/光模块/连接器/电源/液冷/高端铜箔/电子布/PTFE/特种树脂
    - technology_route 高分节点缺少光模块/光芯片/光引擎/激光器/磷化铟/硅光/薄膜铌酸锂
    - component 高分节点缺少 PCB/连接器/电源/散热/基板
    - material 高分节点缺少 上游原料/产能/供应商/价格/认证

    Args:
        nodes: 产业链节点列表
        edges: 已有的边列表

    Returns:
        缺失边列表，每条含 missing_edge_id / source_node / source_type /
        expected_relation / target_hint / reason / priority_score / status
    """
    nodes = _mapping_records(nodes)
    edges = _mapping_records(edges)
    if priority_weights is None:
        priority_weights = {
            "research_importance": 0.30,
            "evidence_gap": 0.25,
            "expected_information_gain": 0.25,
            "timeliness": 0.15,
            "cost_factor": 0.05,
        }
        try:
            from core.research.control_plane.repository import ResearchRepository
            active = ResearchRepository().get_active_policy("retrieval")
            configured = active.parameters.get("priority_feature_weights", {}) if active else {}
            if isinstance(configured, dict):
                priority_weights.update({
                    k: _safe_float01(v)
                    for k, v in configured.items()
                    if isinstance(k, str)
                })
        except Exception:
            pass
    if not isinstance(priority_weights, dict):
        priority_weights = {}
    priority_weights = {
        key: _safe_float01(value)
        for key, value in priority_weights.items()
        if isinstance(key, str)
    }

    # 读取校准产出的 priority_score_threshold，覆盖默认 0.3 阈值
    priority_score_threshold = 0.3
    try:
        from core.research.control_plane.repository import ResearchRepository
        active_policy = ResearchRepository().get_active_policy("retrieval")
        if active_policy is not None:
            learned_threshold = active_policy.parameters.get("priority_score_threshold")
            if isinstance(learned_threshold, (int, float)):
                priority_score_threshold = max(
                    0.1, min(0.8, _safe_float01(learned_threshold))
                )
    except Exception:
        pass

    # 建立已有边的 source_node → target_node 集合
    existing_targets: Dict[str, Set[str]] = {}
    for edge in edges:
        source = _safe_text(edge.get("source_node")).strip()
        target = _safe_text(edge.get("target_node")).strip()
        if source and target:
            existing_targets.setdefault(source, set()).add(target)

    missing_edges: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()

    for node in nodes:
        node_type = _safe_text(node.get("node_type"))
        node_name = _safe_text(node.get("node_name")).strip()
        final_score = _safe_float01(node.get("final_score"))
        if not node_name:
            continue

        # 只对高分节点检测缺失边（final_score >= priority_score_threshold）
        if final_score < priority_score_threshold:
            continue

        # 获取该节点类型的下钻模板
        downstream_list = _DOWNSTREAM_TEMPLATES.get(node_type, [])
        if not downstream_list:
            continue

        # 检查每个下钻目标是否缺失
        existing = existing_targets.get(node_name, set())
        for target_hint in downstream_list:
            # 检查是否已存在相关边（模糊匹配）
            has_edge = any(
                target_hint in existing_target or existing_target in target_hint
                for existing_target in existing
            )
            if has_edge:
                continue

            key = f"{node_name}|{target_hint}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # 节点类型仅用于描述，优先级由研究价值、缺口、信息增益、时效和成本组成。
            features = {
                "research_importance": _safe_float01(
                    node.get("research_importance"), final_score,
                ),
                "evidence_gap": _safe_float01(
                    node.get("evidence_gap"), 1.0 - final_score,
                ),
                "expected_information_gain": _safe_float01(
                    node.get("expected_information_gain"), 0.5,
                ),
                "timeliness": _safe_float01(node.get("timeliness"), 0.5),
                "cost_factor": _safe_float01(node.get("cost_factor"), 0.5),
            }
            benefit = sum(features[key] * priority_weights.get(key, 0.0) for key in features if key != "cost_factor")
            cost_penalty = features["cost_factor"] * priority_weights.get("cost_factor", 0.05)
            priority_score = round(max(0.0, min(1.0, benefit - cost_penalty)), 4)

            missing_edges.append({
                "missing_edge_id": f"missing_{_short_hash(key)}",
                "source_node": node_name,
                "source_type": node_type,
                "expected_relation": "possible_downstream_of",
                "target_hint": target_hint,
                "reason": f"{node_name}（{node_type}）高分节点缺少与 {target_hint} 的下钻关系",
                "priority_score": priority_score,
                "status": "pending",
                "created_at": _now_iso(),
            })

    # 按优先级降序排序
    missing_edges.sort(key=lambda x: x["priority_score"], reverse=True)
    return missing_edges


def export_industry_chain_graph(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    missing_edges: List[Dict[str, Any]],
    output_dir: Optional[Path] = None,
) -> Dict[str, str]:
    """导出产业链图谱 JSON 文件。

    输出：
    - industry_chain_graph_latest.json（含 nodes 和 edges）
    - missing_edges_latest.json
    - edge_validation_history_latest.json

    Args:
        nodes: 节点列表
        edges: 边列表
        missing_edges: 缺失边列表
        output_dir: 输出目录

    Returns:
        dict，含各输出文件路径
    """
    nodes = _mapping_records(nodes)
    edges = _mapping_records(edges)
    missing_edges = _mapping_records(missing_edges)
    out = Path(output_dir) if output_dir is not None else _DEFAULT_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    outputs: Dict[str, str] = {}

    # 1. industry_chain_graph_latest.json
    graph = {
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "generated_at": _now_iso(),
        },
    }
    graph_path = out / "industry_chain_graph_latest.json"
    graph_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    outputs["industry_chain_graph"] = str(graph_path)

    # 2. missing_edges_latest.json
    missing_path = out / "missing_edges_latest.json"
    missing_path.write_text(json.dumps(missing_edges, ensure_ascii=False, indent=2), encoding="utf-8")
    outputs["missing_edges"] = str(missing_path)

    # 3. edge_validation_history_latest.json（初始空记录）
    history: List[Dict[str, Any]] = []
    for edge in edges:
        history.append({
            "event_id": f"event_{_short_hash(edge.get('edge_id', ''))}",
            "edge_or_missing_edge_id": edge.get("edge_id", ""),
            "action": "created",
            "before_status": "",
            "after_status": edge.get("status", "pending"),
            "reason": "P3 初始化构建",
            "created_at": _now_iso(),
        })
    history_path = out / "edge_validation_history_latest.json"
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    outputs["edge_validation_history"] = str(history_path)

    return outputs


def run_industry_chain_graph(
    input_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """运行 P3 产业链图谱构建全流程。

    串联：load_inputs → build_nodes → build_allowed_edges →
          detect_missing_edges → export_industry_chain_graph

    Args:
        input_dir: 输入目录，默认为 data/reports/perilla_discovery/
        output_dir: 输出目录，默认同 input_dir

    Returns:
        dict summary，含 module / success / outputs / counts
    """
    try:
        base = Path(input_dir) if input_dir is not None else _DEFAULT_INPUT_DIR
        out = Path(output_dir) if output_dir is not None else base
        inputs = load_inputs(base)
        nodes = build_nodes(inputs["candidate_concepts"])
        edges = build_allowed_edges(
            inputs["candidate_concepts"],
            inputs["evidence_edges"],
            inputs["causal_chains"],
        )
        missing_edges = detect_missing_edges(nodes, edges)
        outputs = export_industry_chain_graph(nodes, edges, missing_edges, out)

        return {
            "module": "industry_chain_graph",
            "success": True,
            "outputs": list(outputs.values()),
            "counts": {
                "nodes": len(nodes),
                "edges": len(edges),
                "missing_edges": len(missing_edges),
            },
        }
    except Exception as exc:
        logger.error("run_industry_chain_graph 失败: %s", exc, exc_info=True)
        return {
            "module": "industry_chain_graph",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }
