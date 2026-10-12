"""P4A 多智能体假设生成与反方审查 — 假设生成引擎。

hybrid 模式：LLM 优先用 ConceptLLMExplainer 生成假设文本，失败时降级到三种规则模式。
假设状态限定 born/pending/weak_signal/rejected/expired，禁止 supported/tradeable_watch。

输入：candidate_concepts / causal_chains / industry_chain_graph / missing_edges / concept_scores
输出：research_hypotheses_latest.json / hypothesis_lifecycle_latest.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from core.llm_processing.prompt_templates import (
    P4A_PROMPT_VERSION,
    build_p4a_hypothesis_prompt,
    check_compliance,
    get_p4a_hypothesis_system_prompt,
)
from core.utils.llm_usage import (
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    record_llm_usage,
)

logger = logging.getLogger(__name__)

_DEFAULT_INPUT_DIR = Path("data/reports/perilla_discovery")

# 假设状态限定
_ALLOWED_STATES: Set[str] = {"born", "pending", "weak_signal", "rejected", "expired"}
_FORBIDDEN_STATES: Set[str] = {"supported", "tradeable_watch"}

# 假设类型
_HYPOTHESIS_TYPES = {
    "product_platform_demand": "产品平台需求带动",
    "tech_route_impact": "技术路线影响",
    "investment_logic_signal": "投资逻辑早期信号",
}

def _env_nonnegative_int(name: str, default: int) -> int:
    try:
        return max(0, int(float(os.getenv(name, str(default)) or default)))
    except (TypeError, ValueError, OverflowError):
        return default


_MAX_LLM_ITEMS = _env_nonnegative_int("AQUARESEARCH_LLM_MAX_ITEMS", 20)


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


def _short_hash(text: str) -> str:
    """计算短哈希。"""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    """当前时间 ISO 格式。"""
    return datetime.now().isoformat()


def load_inputs(input_dir: Optional[Path] = None) -> Dict[str, Any]:
    """加载 P4A 输入数据。

    读取 candidate_concepts / causal_chains / industry_chain_graph /
    missing_edges / concept_scores。

    Args:
        input_dir: 输入目录

    Returns:
        dict，含各输入数据
    """
    result: Dict[str, Any] = {
        "candidate_concepts": [],
        "causal_chains": [],
        "industry_chain_graph": {},
        "missing_edges": [],
        "concept_scores": [],
    }
    try:
        base = Path(input_dir) if input_dir is not None else _DEFAULT_INPUT_DIR
    except (TypeError, ValueError, OSError):
        return result

    for key, filename in [
        ("candidate_concepts", "candidate_concepts_latest.json"),
        ("causal_chains", "causal_chains_latest.json"),
        ("missing_edges", "missing_edges_latest.json"),
        ("concept_scores", "concept_scores_latest.json"),
    ]:
        path = base / filename
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                result[key] = _mapping_records(payload)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("读取 %s 失败: %s", filename, exc)

    # industry_chain_graph 是含 nodes/edges 的对象
    graph_path = base / "industry_chain_graph_latest.json"
    if graph_path.exists():
        try:
            payload = json.loads(graph_path.read_text(encoding="utf-8"))
            result["industry_chain_graph"] = payload if isinstance(payload, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("读取 industry_chain_graph_latest.json 失败: %s", exc)

    return result


def _generate_hypotheses_by_rules(
    candidate_concepts: List[Dict[str, Any]],
    causal_chains: List[Dict[str, Any]],
    missing_edges: List[Dict[str, Any]],
    concept_scores: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """规则模式生成假设（三种生成模式）。

    模式1：product_platform 与 component/material 共现
    模式2：technology_route 与 component/material 共现
    模式3：investment_logic 与 material/component 共现

    Args:
        candidate_concepts: 候选概念列表
        causal_chains: 因果链列表
        missing_edges: 缺失边列表
        concept_scores: 概念评分列表

    Returns:
        假设列表
    """
    candidate_concepts = _mapping_records(candidate_concepts)
    causal_chains = _mapping_records(causal_chains)
    missing_edges = _mapping_records(missing_edges)
    concept_scores = _mapping_records(concept_scores)

    # 按类型分组概念
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for concept in candidate_concepts:
        term_type = _safe_text(concept.get("term_type"), "unclassified")
        by_type.setdefault(term_type, []).append(concept)

    # 概念评分映射
    score_map = {
        _safe_text(score.get("concept_name")): _clamp01(score.get("final_score"))
        for score in concept_scores
    }

    hypotheses: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()

    # 模式1：product_platform 与 component/material 共现
    for pp in by_type.get("product_platform", []):
        pp_name = _safe_text(pp.get("normalized_name"))
        pp_root = _safe_text(pp.get("root_theme"))
        for comp in by_type.get("component", []) + by_type.get("material", []):
            comp_name = _safe_text(comp.get("normalized_name"))
            if not comp_name or comp_name == pp_name:
                continue
            key = f"{pp_name}|{comp_name}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # 检查是否有因果链支持
            chain_support = any(
                str(c.get("cause", "")).find(pp_name) >= 0 and
                str(c.get("beneficiary_node", "")).find(comp_name) >= 0
                for c in causal_chains
            )

            evidence_quotes = []
            for c in causal_chains:
                if pp_name in str(c.get("cause", "")) and comp_name in str(c.get("beneficiary_node", "")):
                    if c.get("evidence_quote"):
                        evidence_quotes.append(str(c["evidence_quote"]))

            novelty_score = round(1.0 - score_map.get(comp_name, 0.5), 4)
            evidence_score = round(min(1.0, len(evidence_quotes) / 3.0 + 0.2), 4)
            crowding_score = round(score_map.get(pp_name, 0.5) * 0.5, 4)

            hypotheses.append({
                "hypothesis_id": f"hyp_{_short_hash(key)}",
                "hypothesis_text": f"{pp_name} 升级可能带动 {comp_name} 需求。",
                "hypothesis_type": "product_platform_demand",
                "source_nodes": [pp_name, comp_name],
                "logic_chain": f"{pp_name} 放量 → {comp_name} 需求提升",
                "evidence_document_ids": _safe_text_list(pp.get("evidence_document_ids"))[:5],
                "evidence_quotes": evidence_quotes[:3],
                "related_missing_edges": [
                    _safe_text(m.get("missing_edge_id"))
                    for m in missing_edges
                    if m.get("source_node") == pp_name
                ][:3],
                "novelty_score": novelty_score,
                "evidence_score": evidence_score,
                "crowding_score": crowding_score,
                "skepticism_score": 0.0,  # 由 EvidenceReviewAgent 计算
                "status": "born" if not chain_support else "pending",
                "created_at": _now_iso(),
            })

    # 模式2：technology_route 与 component/material 共现
    for tr in by_type.get("technology_route", []):
        tr_name = _safe_text(tr.get("normalized_name"))
        tr_root = _safe_text(tr.get("root_theme"))
        for comp in by_type.get("component", []) + by_type.get("material", []):
            comp_name = _safe_text(comp.get("normalized_name"))
            if not comp_name or comp_name == tr_name:
                continue
            key = f"{tr_name}|{comp_name}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            evidence_quotes = []
            for c in causal_chains:
                if tr_name in str(c.get("cause", "")) and comp_name in str(c.get("beneficiary_node", "")):
                    if c.get("evidence_quote"):
                        evidence_quotes.append(str(c["evidence_quote"]))

            novelty_score = round(1.0 - score_map.get(comp_name, 0.5), 4)
            evidence_score = round(min(1.0, len(evidence_quotes) / 3.0 + 0.2), 4)
            crowding_score = round(score_map.get(tr_name, 0.5) * 0.5, 4)

            hypotheses.append({
                "hypothesis_id": f"hyp_{_short_hash(key)}",
                "hypothesis_text": f"{tr_name} 可能影响 {comp_name} 在 {tr_root} 主线中的需求结构。",
                "hypothesis_type": "tech_route_impact",
                "source_nodes": [tr_name, comp_name],
                "logic_chain": f"{tr_name} 发展 → {comp_name} 需求结构变化",
                "evidence_document_ids": _safe_text_list(tr.get("evidence_document_ids"))[:5],
                "evidence_quotes": evidence_quotes[:3],
                "related_missing_edges": [
                    _safe_text(m.get("missing_edge_id"))
                    for m in missing_edges
                    if m.get("source_node") == tr_name
                ][:3],
                "novelty_score": novelty_score,
                "evidence_score": evidence_score,
                "crowding_score": crowding_score,
                "skepticism_score": 0.0,
                "status": "born",
                "created_at": _now_iso(),
            })

    # 模式3：investment_logic 与 material/component 共现
    for il in by_type.get("investment_logic", []):
        il_name = _safe_text(il.get("normalized_name"))
        for comp in by_type.get("material", []) + by_type.get("component", []):
            comp_name = _safe_text(comp.get("normalized_name"))
            if not comp_name or comp_name == il_name:
                continue
            key = f"{il_name}|{comp_name}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            novelty_score = round(1.0 - score_map.get(comp_name, 0.5), 4)
            evidence_score = round(0.2, 4)
            crowding_score = round(score_map.get(il_name, 0.5) * 0.3, 4)

            hypotheses.append({
                "hypothesis_id": f"hyp_{_short_hash(key)}",
                "hypothesis_text": f"{il_name} 逻辑可能在 {comp_name} 环节出现早期信号。",
                "hypothesis_type": "investment_logic_signal",
                "source_nodes": [il_name, comp_name],
                "logic_chain": f"{il_name} 逻辑 → {comp_name} 早期信号",
                "evidence_document_ids": _safe_text_list(il.get("evidence_document_ids"))[:5],
                "evidence_quotes": [],
                "related_missing_edges": [],
                "novelty_score": novelty_score,
                "evidence_score": evidence_score,
                "crowding_score": crowding_score,
                "skepticism_score": 0.0,
                "status": "born",
                "created_at": _now_iso(),
            })

    return hypotheses


def _local_llm_enabled() -> bool:
    """环境变量层面的 LLM 开关。"""
    return os.getenv("LOCAL_LLM_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """解析 LLM 返回的 JSON 对象。"""
    clean = str(text or "").strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    try:
        parsed = json.loads(clean)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(clean[start:end])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _clamp01(value: Any, default: float = 0.0) -> float:
    """把分数限制在 0-1。"""
    try:
        number = float(value)
        if not math.isfinite(number):
            return round(default, 4)
        return round(max(0.0, min(1.0, number)), 4)
    except (TypeError, ValueError, OverflowError):
        return round(default, 4)


def _load_llm_json_from_cache(
    cache_dir: Path,
    cache_key: str,
) -> Optional[Dict[str, Any]]:
    """读取 P4A JSON 缓存。"""
    cache_path = cache_dir / f"{cache_key}.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("prompt_version") != P4A_PROMPT_VERSION:
            return None
        parsed = data.get("parsed_result")
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.debug("P4A 假设 LLM 缓存读取失败: %s", exc)
        return None


def _write_llm_json_cache(
    cache_dir: Path,
    cache_key: str,
    parsed: Dict[str, Any],
    model_name: str,
) -> None:
    """写入 P4A JSON 缓存。"""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{cache_key}.json").write_text(
            json.dumps({
                "prompt_version": P4A_PROMPT_VERSION,
                "model_name": model_name,
                "parsed_result": parsed,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("P4A 假设 LLM 缓存写入失败: %s", exc)


def _call_hypothesis_llm(
    explainer: Any,
    system_prompt: str,
    user_prompt: str,
) -> Optional[Dict[str, Any]]:
    """复用 ConceptLLMExplainer 配置调用 JSON LLM。"""
    if hasattr(explainer, "generate_json"):
        parsed = explainer.generate_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=P4A_PROMPT_VERSION,
        )
        return parsed if isinstance(parsed, dict) else None

    providers = getattr(explainer, "providers", []) or []
    if providers:
        try:
            import requests

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            prompt_tokens = estimate_messages_tokens(messages)
            timeout = int(getattr(explainer, "timeout", 20) or 20)
            check_llm_budget_or_raise(prompt_tokens, 0, source="hypothesis_engine._call_hypothesis_llm")
            for provider in providers:
                headers: Dict[str, str] = {"Content-Type": "application/json"}
                if provider.api_key:
                    headers["Authorization"] = f"Bearer {provider.api_key}"
                body: Dict[str, Any] = {
                    "model": provider.model_name,
                    "messages": messages,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                }
                try:
                    response = requests.post(
                        f"{provider.api_base}/chat/completions",
                        json=body,
                        headers=headers,
                        timeout=timeout,
                    )
                    if response.status_code >= 400:
                        body.pop("response_format", None)
                        response = requests.post(
                            f"{provider.api_base}/chat/completions",
                            json=body,
                            headers=headers,
                            timeout=timeout,
                        )
                    response.raise_for_status()
                    content = str(response.json()["choices"][0]["message"]["content"])
                    record_llm_usage(
                        source="hypothesis_engine._call_hypothesis_llm",
                        provider=provider.name,
                        model=provider.model_name,
                        prompt_tokens_est=prompt_tokens,
                        completion_tokens_est=estimate_tokens(content),
                        success=True,
                    )
                    return _parse_json_object(content)
                except Exception as provider_exc:
                    record_llm_usage(
                        source="hypothesis_engine._call_hypothesis_llm",
                        provider=provider.name,
                        model=provider.model_name,
                        prompt_tokens_est=prompt_tokens,
                        completion_tokens_est=0,
                        success=False,
                        error=str(provider_exc),
                    )
        except Exception as exc:
            logger.warning("P4A hypothesis LLM provider path failed: %s", exc)

    try:
        import requests

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        api_key = str(getattr(explainer, "api_key", "") or "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        body: Dict[str, Any] = {
            "model": str(getattr(explainer, "model_name", "") or ""),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        api_base = str(getattr(explainer, "api_base", "") or "").rstrip("/")
        timeout = int(getattr(explainer, "timeout", 20) or 20)
        response = requests.post(
            f"{api_base}/chat/completions",
            json=body,
            headers=headers,
            timeout=timeout,
        )
        if response.status_code >= 400:
            body.pop("response_format", None)
            response = requests.post(
                f"{api_base}/chat/completions",
                json=body,
                headers=headers,
                timeout=timeout,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return _parse_json_object(str(content))
    except Exception as exc:
        logger.warning("P4A 假设 LLM 调用失败: %s", exc)
        return None


def _collect_allowed_quotes(inputs: Dict[str, Any]) -> Set[str]:
    """收集 LLM 输出可引用的原文证据。"""
    quotes: Set[str] = set()
    for chain in _mapping_records(inputs.get("causal_chains")):
        quote = _safe_text(chain.get("evidence_quote")).strip()
        if quote:
            quotes.add(quote)
    for concept in _mapping_records(inputs.get("candidate_concepts")):
        for quote_s in _safe_text_list(concept.get("evidence_quotes")):
            quote_s = quote_s.strip()
            if quote_s:
                quotes.add(quote_s)
    return quotes


def _doc_ids_for_nodes(inputs: Dict[str, Any], nodes: List[str]) -> List[str]:
    """根据 source_nodes 回填 evidence_document_ids。"""
    node_set = {str(node) for node in nodes}
    doc_ids: List[str] = []
    for concept in _mapping_records(inputs.get("candidate_concepts")):
        name = _safe_text(concept.get("normalized_name") or concept.get("concept_name"))
        if name in node_set:
            for doc_id in _safe_text_list(concept.get("evidence_document_ids")):
                if doc_id and doc_id not in doc_ids:
                    doc_ids.append(str(doc_id))
    return doc_ids[:5]


def _missing_edges_for_nodes(inputs: Dict[str, Any], nodes: List[str]) -> List[str]:
    """根据 source_nodes 回填 related_missing_edges。"""
    node_set = {str(node) for node in nodes}
    edge_ids: List[str] = []
    for edge in _mapping_records(inputs.get("missing_edges")):
        if _safe_text(edge.get("source_node")) in node_set:
            edge_id = _safe_text(edge.get("missing_edge_id"))
            if edge_id and edge_id not in edge_ids:
                edge_ids.append(edge_id)
    return edge_ids[:3]


def _build_hypotheses_from_llm(
    parsed: Dict[str, Any],
    inputs: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """将 LLM JSON 转换为受约束的 research_hypotheses。"""
    if not check_compliance(json.dumps(parsed, ensure_ascii=False)):
        logger.warning("P4A 假设 LLM 输出命中合规禁用词，丢弃")
        return []

    raw_items = parsed.get("hypotheses", [])
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        return []

    allowed_quotes = _collect_allowed_quotes(inputs)
    hypotheses: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        text = _safe_text(item.get("hypothesis_text")).strip()
        if not text or not check_compliance(text):
            continue
        htype = _safe_text(item.get("hypothesis_type"), "product_platform_demand")
        if htype not in _HYPOTHESIS_TYPES:
            htype = "product_platform_demand"
        source_nodes = _safe_text_list(item.get("source_nodes"))
        if len(source_nodes) < 2:
            continue
        quotes = []
        for quote_s in _safe_text_list(item.get("evidence_quotes")):
            quote_s = quote_s.strip()
            if not quote_s:
                continue
            if allowed_quotes and not any(
                quote_s in allowed or allowed in quote_s for allowed in allowed_quotes
            ):
                continue
            quotes.append(quote_s)

        key = text + "|" + "|".join(source_nodes)
        if key in seen:
            continue
        seen.add(key)
        status = _safe_text(item.get("status"), "born").strip()
        if status not in {"born", "pending"}:
            status = "born"

        hypotheses.append({
            "hypothesis_id": f"hyp_{_short_hash(key)}",
            "hypothesis_text": text,
            "hypothesis_type": htype,
            "source_nodes": source_nodes,
            "logic_chain": _safe_text(item.get("logic_chain")),
            "evidence_document_ids": _doc_ids_for_nodes(inputs, source_nodes),
            "evidence_quotes": quotes[:3],
            "related_missing_edges": _missing_edges_for_nodes(inputs, source_nodes),
            "novelty_score": _clamp01(item.get("novelty_score", 0.0)),
            "evidence_score": _clamp01(item.get("evidence_score", 0.0)),
            "crowding_score": _clamp01(item.get("crowding_score", 0.0)),
            "skepticism_score": 0.0,
            "status": status,
            "created_at": _now_iso(),
            "llm_status": "llm",
            "llm_model": _safe_text(item.get("llm_model")),
        })

    return hypotheses


def generate_hypotheses(
    inputs: Dict[str, Any],
    use_llm: bool = True,
    llm_explainer_factory: Optional[Callable[[], Any]] = None,
) -> List[Dict[str, Any]]:
    """生成研究假设（hybrid 模式）。

    LLM 优先用 ConceptLLMExplainer 生成假设文本，失败时降级到三种规则模式。
    LLM 输出经 check_compliance 校验，合规拦截的整条丢弃。

    Args:
        inputs: load_inputs 返回的输入数据
        use_llm: 是否尝试使用 LLM

    Returns:
        假设列表
    """
    if not isinstance(inputs, dict):
        inputs = {}

    # 规则模式生成（作为降级兜底）
    rule_hypotheses = _generate_hypotheses_by_rules(
        inputs.get("candidate_concepts", []),
        inputs.get("causal_chains", []),
        inputs.get("missing_edges", []),
        inputs.get("concept_scores", []),
    )

    if not use_llm or not _local_llm_enabled():
        return rule_hypotheses

    # 尝试 LLM 模式
    try:
        if llm_explainer_factory is not None:
            explainer = llm_explainer_factory()
        else:
            from core.research.perilla.discovery.concept_llm_explainer import ConceptLLMExplainer
            explainer = ConceptLLMExplainer()
        if not explainer.available:
            logger.info("ConceptLLMExplainer 不可用，降级到规则模式")
            return rule_hypotheses

        # 构建 LLM Prompt
        concepts_str = json.dumps(
            _mapping_records(inputs.get("candidate_concepts"))[:_MAX_LLM_ITEMS],
            ensure_ascii=False,
        )
        chains_str = json.dumps(
            _mapping_records(inputs.get("causal_chains"))[:_MAX_LLM_ITEMS],
            ensure_ascii=False,
        )
        missing_str = json.dumps(
            _mapping_records(inputs.get("missing_edges"))[:_MAX_LLM_ITEMS],
            ensure_ascii=False,
        )

        prompt = build_p4a_hypothesis_prompt(concepts_str, chains_str, missing_str)
        system_prompt = get_p4a_hypothesis_system_prompt()
        model_name = str(getattr(explainer, "model_name", "") or "")
        cache_dir = Path(getattr(explainer, "cache_dir", Path("data/llm_cache/perilla_concept_explanations")))
        cache_key = _short_hash(P4A_PROMPT_VERSION + model_name + prompt)
        parsed = _load_llm_json_from_cache(cache_dir, cache_key)
        if parsed is None:
            parsed = _call_hypothesis_llm(explainer, system_prompt, prompt)
            if parsed:
                _write_llm_json_cache(cache_dir, cache_key, parsed, model_name)

        if parsed:
            llm_hypotheses = _build_hypotheses_from_llm(parsed, inputs)
            if llm_hypotheses:
                for hyp in llm_hypotheses:
                    hyp["llm_model"] = model_name
                return llm_hypotheses

        for hyp in rule_hypotheses:
            hyp["llm_status"] = "rule_fallback"
            hyp["llm_model"] = model_name
        return rule_hypotheses

    except Exception as exc:
        logger.warning("LLM 假设生成失败，降级到规则模式: %s", exc)
        for hyp in rule_hypotheses:
            hyp["llm_status"] = "rule_fallback"
        return rule_hypotheses


def initialize_lifecycle(
    hypotheses: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """初始化假设生命周期。

    state 限定 born/pending/weak_signal/rejected/expired，禁止 supported/tradeable_watch。

    Args:
        hypotheses: 假设列表

    Returns:
        生命周期列表
    """
    lifecycle: List[Dict[str, Any]] = []
    for hyp in _mapping_records(hypotheses):
        state = _safe_text(hyp.get("status"), "born")
        # 强制限定 state 枚举
        if state not in _ALLOWED_STATES:
            state = "born"
        if state in _FORBIDDEN_STATES:
            state = "born"

        lifecycle.append({
            "hypothesis_id": _safe_text(hyp.get("hypothesis_id")),
            "current_state": state,
            "state_history": [
                {
                    "state": state,
                    "timestamp": _now_iso(),
                    "reason": "P4A 初始化",
                }
            ],
            "created_at": _now_iso(),
            "last_updated_at": _now_iso(),
            "next_required_evidence": _next_required_evidence(hyp, state),
        })

    return lifecycle


def _next_required_evidence(hyp: Dict[str, Any], state: str) -> str:
    """根据假设和状态推断下一步需要的证据。

    Args:
        hyp: 假设
        state: 当前状态

    Returns:
        下一步需要的证据描述
    """
    if state == "born":
        return "需要补充公告/官网/客户认证等官方证据"
    if state == "pending":
        return "需要补充订单、产能、价格等实质性证据"
    if state == "weak_signal":
        return "需要补充多来源交叉验证证据"
    if state == "rejected":
        return "假设已被拒绝，无需补充证据"
    if state == "expired":
        return "假设已过期，无需补充证据"
    return ""


def run_hypothesis_engine(
    input_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """运行 P4A 假设生成引擎全流程。

    Args:
        input_dir: 输入目录
        output_dir: 输出目录
        use_llm: 是否使用 LLM

    Returns:
        dict summary
    """
    try:
        base = Path(input_dir) if input_dir is not None else _DEFAULT_INPUT_DIR
        out = Path(output_dir) if output_dir is not None else base
        inputs = load_inputs(base)
        hypotheses = generate_hypotheses(inputs, use_llm=use_llm)
        lifecycle = initialize_lifecycle(hypotheses)

        # 输出文件
        out.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        hyp_path = out / "research_hypotheses_latest.json"
        hyp_path.write_text(
            json.dumps(hypotheses, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(hyp_path))

        lifecycle_path = out / "hypothesis_lifecycle_latest.json"
        lifecycle_path.write_text(
            json.dumps(lifecycle, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(lifecycle_path))

        return {
            "module": "hypothesis_engine",
            "success": True,
            "outputs": outputs,
            "counts": {
                "hypotheses": len(hypotheses),
                "lifecycle": len(lifecycle),
            },
        }
    except Exception as exc:
        logger.error("run_hypothesis_engine 失败: %s", exc, exc_info=True)
        return {
            "module": "hypothesis_engine",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }
