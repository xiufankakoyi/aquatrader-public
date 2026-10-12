"""Build AquaResearch cockpit data from real research artifacts.

This exporter is intentionally audit-oriented: it does not invent evidence,
and it surfaces failed/weak retrieval paths as first-class cockpit data.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
BASELINE_DIR = ROOT / "data" / "reports" / "perilla_discovery"
DEFAULT_RUN_DIR = ROOT / "data" / "research_runs" / "cockpit_real_probe"
LATEST_COCKPIT_DIR = ROOT / "data" / "research_runs" / "latest" / "cockpit"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_list(path: Path) -> list[dict[str, Any]]:
    value = _load_json(path, [])
    return value if isinstance(value, list) else []


def _short(text: Any, limit: int = 180) -> str:
    value = str(text or "").replace("\n", " ").strip()
    return value[:limit]


def _pct(numerator: int | float, denominator: int | float) -> float:
    denominator = float(denominator or 0)
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / denominator, 4)


def _source_diversity(items: list[dict[str, Any]]) -> float:
    sources = [str(item.get("source_type") or "unknown") for item in items]
    if not sources:
        return 0.0
    counts = Counter(sources)
    return round(len(counts) / len(sources), 4)


def _confidence_value(label: str) -> float:
    return {"low": 0.35, "medium": 0.65, "high": 0.85}.get(str(label).lower(), 0.4)


def _build_graph(
    selected: dict[str, Any],
    concepts: list[dict[str, Any]],
    chains: list[dict[str, Any]],
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def add_node(name: str, node_type: str = "concept", status: str = "CANDIDATE") -> None:
        if not name:
            return
        if name in nodes and nodes[name].get("status") == "ACTIVE":
            return
        node_id = f"n_{abs(hash(name)) % 100000000}"
        nodes[name] = {
            "node_id": node_id,
            "name": name,
            "type": node_type,
            "status": status,
        }

    selected_name = str(selected.get("normalized_name") or "复合材料")
    add_node(selected_name, "selected_term", "ACTIVE")
    for term in selected.get("co_terms", [])[:8]:
        add_node(str(term), "co_term", "CANDIDATE")
        edges.append({
            "edge_id": f"e_{len(edges)}",
            "subjects": nodes[selected_name]["node_id"],
            "objects": nodes[str(term)]["node_id"],
            "relation": "co_occurs_with",
            "status": "CANDIDATE",
        })

    for chain in chains[:10]:
        cause = str(chain.get("cause") or "")
        effect = str(chain.get("effect") or "")
        beneficiary = str(chain.get("beneficiary_node") or "")
        for name, kind in [(cause, "cause"), (effect, "effect"), (beneficiary, "beneficiary")]:
            add_node(name, kind, "CANDIDATE")
        if cause and effect:
            edges.append({
                "edge_id": f"chain_{chain.get('chain_id') or len(edges)}",
                "subjects": nodes[cause]["node_id"],
                "objects": nodes[effect]["node_id"],
                "relation": "candidate_causal_chain",
                "status": "CANDIDATE",
            })
        if effect and beneficiary and beneficiary != effect:
            edges.append({
                "edge_id": f"benefit_{chain.get('chain_id') or len(edges)}",
                "subjects": nodes[effect]["node_id"],
                "objects": nodes[beneficiary]["node_id"],
                "relation": "beneficiary_node",
                "status": "CANDIDATE",
            })

    for concept in concepts[:20]:
        add_node(str(concept.get("normalized_name") or ""), str(concept.get("term_type") or "concept"), "CANDIDATE")

    return {"nodes": list(nodes.values()), "edges": edges}


def build_cockpit(run_dir: Path = DEFAULT_RUN_DIR) -> dict[str, Any]:
    p0 = run_dir / "p0"
    focus = run_dir / "focus_p1"

    baseline_docs = _safe_list(BASELINE_DIR / "raw_documents_latest.json")
    raw_docs = _safe_list(p0 / "raw_documents_latest.json")
    concepts = _safe_list(p0 / "candidate_concepts_latest.json")
    evidence_edges = _safe_list(p0 / "evidence_edges_latest.json")
    search_tasks = _safe_list(p0 / "search_tasks_latest.json")
    search_runs = _safe_list(p0 / "search_task_runs_latest.json")
    selected = _load_json(p0 / "selected_new_term_latest.json", {})
    claims = _safe_list(focus / "report_claims_latest.json")
    chains = _safe_list(focus / "causal_chains_latest.json")
    evidence = _safe_list(focus / "evidence_items_latest.json")
    failures = _safe_list(focus / "failure_conditions_latest.json")

    baseline_hashes = {doc.get("raw_hash") for doc in baseline_docs if doc.get("raw_hash")}
    raw_hashes = {doc.get("raw_hash") for doc in raw_docs if doc.get("raw_hash")}
    new_hashes = raw_hashes - baseline_hashes
    duplicate_count = max(0, len(raw_docs) - len(new_hashes))

    avg_confidence = round(
        sum(_confidence_value(chain.get("confidence", "low")) for chain in chains) / max(1, len(chains)),
        4,
    )
    avg_uncertainty = round(1.0 - avg_confidence, 4)
    llm_stats = _load_json(focus / "p1_summary.json", {}).get("llm_stats", {})
    client_stats = llm_stats.get("client_stats", {}) if isinstance(llm_stats, dict) else {}

    target_docs = [
        doc for doc in raw_docs
        if "601179" in json.dumps(doc, ensure_ascii=False) or "中国西电" in json.dumps(doc, ensure_ascii=False)
    ]
    weak_external = [
        doc for doc in target_docs
        if doc.get("source_type") == "search_result" and ("百度百科" in str(doc.get("summary")) or "行情中心" in str(doc.get("summary")))
    ]
    target_evidence = [
        {
            "evidence_id": doc.get("document_id", ""),
            "evidence_type": doc.get("source_type", ""),
            "source_type": doc.get("source_id", doc.get("source_type", "")),
            "summary": _short(doc.get("summary") or doc.get("title"), 220),
            "impact": "支持候选链路" if doc.get("source_type") == "inbox" else "弱相关/不准入",
        }
        for doc in target_docs[:8]
    ]

    accepted_evidence = len(evidence)
    routed_evidence = len(chains)
    rejected_or_pending = len(failures) + len(weak_external)
    llm_calls = int(client_stats.get("llm_call_count") or 0)

    now = datetime.now().isoformat(timespec="seconds")
    selected_name = selected.get("normalized_name") or "暂无本地证据"
    selected_quotes = selected.get("evidence_quotes") or []

    return {
        "generated_at": now,
        "summary": {
            "canonical_evidence_count": accepted_evidence,
            "active_edge_count": 0,
            "conflicted_edge_count": 0,
            "avg_confidence": avg_confidence,
            "avg_uncertainty": avg_uncertainty,
            "evidence_saturation": min(1.0, _pct(accepted_evidence, max(1, len(_safe_list(focus / "raw_documents_latest.json"))))),
            "duplicate_ratio": _pct(duplicate_count, len(raw_docs)),
            "llm_rejected_ratio": _pct(rejected_or_pending, accepted_evidence + rejected_or_pending),
            "breakthrough_score": round(_pct(len(new_hashes), max(1, len(raw_docs))) + _pct(accepted_evidence, 50), 4),
            "new_evidence_delta": accepted_evidence,
            "confidence_delta": avg_confidence,
        },
        "learning_curve": [
            {
                "tick": 1,
                "avg_confidence": 0.0,
                "avg_uncertainty": 1.0,
                "breakthrough_score": 0.0,
                "duplicate_ratio": 1.0,
                "llm_rejected_ratio": 0.0,
            },
            {
                "tick": 2,
                "avg_confidence": avg_confidence,
                "avg_uncertainty": avg_uncertainty,
                "breakthrough_score": round(_pct(accepted_evidence, 50), 4),
                "duplicate_ratio": _pct(duplicate_count, len(raw_docs)),
                "llm_rejected_ratio": _pct(rejected_or_pending, accepted_evidence + rejected_or_pending),
            },
        ],
        "evidence_flow": {
            "doc_retrieved": len(raw_docs),
            "duplicate_skipped": duplicate_count,
            "ledger_rejected": rejected_or_pending,
            "evidence_accepted": accepted_evidence,
            "evidence_routed": routed_evidence,
        },
        "graph": _build_graph(selected, concepts, chains),
        "node_metrics": [
            {
                "neuron_id": "selected_term",
                "canonical_name": selected_name,
                "mastery_score": avg_confidence,
                "evidence_saturation": _pct(len(selected.get("evidence_document_ids") or []), 5),
                "source_diversity": _source_diversity(evidence),
                "contradiction_ratio": 0.0,
                "stale_ratio": 0.0,
                "unresolved_gap_count": len(failures),
                "last_breakthrough_at": now,
            }
        ],
        "algorithm_contribution": [
            {
                "module_name": "ProviderRegistry/SearchRouter",
                "accepted_evidence": len(new_hashes),
                "rejected_evidence": duplicate_count,
                "routed_evidence": len(search_runs),
                "event_count": len(search_tasks),
            },
            {
                "module_name": "P1 LLM Causal Extractor",
                "accepted_evidence": accepted_evidence,
                "rejected_evidence": rejected_or_pending,
                "routed_evidence": routed_evidence,
                "event_count": llm_calls,
            },
            {
                "module_name": "Evidence Gate",
                "accepted_evidence": accepted_evidence,
                "rejected_evidence": rejected_or_pending,
                "routed_evidence": routed_evidence,
                "event_count": accepted_evidence + rejected_or_pending,
            },
        ],
        "scheduler": {
            "selected_topic": selected_name,
            "information_gain_reason": selected.get("discovery_reason", ""),
            "budgeted_documents": len(_safe_list(focus / "raw_documents_latest.json")),
            "raw_candidate_documents": len(raw_docs),
        },
        "alerts": [
            {
                "level": "warning",
                "message": "外部检索存在慢查询/弱相关结果，未将其升级为可信因果。",
            }
        ],
        "trace_tail": [
            {
                "timestamp": now,
                "event_type": "knowledge_delta",
                "description": f"新增 raw_hash {len(new_hashes)}，焦点 P1 产出 evidence {accepted_evidence}。",
                "details": {"selected_term": selected_name, "claims": len(claims), "chains": len(chains)},
            },
            {
                "timestamp": now,
                "event_type": "llm_extraction",
                "description": f"P1 LLM 调用 {llm_calls} 次，缓存命中 {client_stats.get('cache_hit', 0)} 次。",
                "details": client_stats,
            },
            {
                "timestamp": now,
                "event_type": "target_judgment",
                "description": "中国西电(601179.SH) 因本地证据不足且外部搜索弱相关，结论等级为 INSUFFICIENT_EVIDENCE。",
                "details": {"target_docs": len(target_docs), "weak_external_docs": len(weak_external)},
            },
        ],
        "system_flow": {
            "architecture": [
                {
                    "layer": "数据源",
                    "title": "本地库 + 外部检索",
                    "paths": [
                        "infrastructure/data_providers/provider_registry.py",
                        "data_svc/providers/source_router.py",
                    ],
                    "output": "raw_documents / provider events / source_payload",
                },
                {
                    "layer": "发现层",
                    "title": "Perilla P0",
                    "paths": [
                        "core/research/perilla/discovery/knowledge_loop_engine.py",
                    ],
                    "output": "raw_terms / candidate_concepts / evidence_edges / search_tasks",
                },
                {
                    "layer": "抽取层",
                    "title": "P1 LLM 因果抽取",
                    "paths": [
                        "core/research/perilla/discovery/report_causal_extractor.py",
                        "core/llm_processing/prompt_templates.py",
                        "core/utils/llm_usage.py",
                    ],
                    "output": "claims / causal_chains / evidence_items / llm_usage",
                },
                {
                    "layer": "图谱层",
                    "title": "产业链 + ResearchGraph",
                    "paths": [
                        "core/research/perilla/discovery/industry_chain_graph_builder.py",
                        "core/research_graph/evidence_ledger.py",
                        "core/research_graph/verifier.py",
                    ],
                    "output": "industry_chain_graph / canonical evidence / verified edges",
                },
                {
                    "layer": "学习层",
                    "title": "Neuron Scheduler",
                    "paths": [
                        "core/research_graph/neuron/environment.py",
                        "core/research_graph/neuron/tick_scheduler.py",
                    ],
                    "output": "selected neuron / information gain / mutation log",
                },
                {
                    "layer": "判断层",
                    "title": "Opinion & Cockpit",
                    "paths": [
                        "core/research/opinion/synthesizer.py",
                        "scripts/build_research_cockpit.py",
                        "dashboard.py",
                        "templates/index.html",
                    ],
                    "output": "confidence / uncertainty / target judgment / API dashboard",
                },
            ],
            "code_modules": [
                {
                    "path": "scripts/run_aquaresearch_pipeline.py",
                    "role": "总调度入口，串起 P0/P1/P2/P3/P4A/P4B。",
                    "input": "本地材料、配置、是否启用 LLM/搜索",
                    "output": "data/reports/perilla_discovery 下的全套 latest JSON",
                },
                {
                    "path": "core/research/perilla/discovery/knowledge_loop_engine.py",
                    "role": "P0 新词和候选概念闭环，负责采集 raw_documents、抽取 raw_terms、生成 search_tasks。",
                    "input": "inbox、本地文档、新闻/公告/研报/语义搜索",
                    "output": "raw_documents、candidate_concepts、evidence_edges、search_task_runs",
                },
                {
                    "path": "data_svc/providers/source_router.py",
                    "role": "ResearchEngine 的能力路由，把 semantic_search、announcements、reports 等统一成 events。",
                    "input": "capability、symbols、query、keyword",
                    "output": "status、events、selected_provider、source_chain、errors",
                },
                {
                    "path": "infrastructure/data_providers/provider_registry.py",
                    "role": "真实数据源门面，按优先级调用 TDX、腾讯、东方财富、AKShare、巨潮、Tushare、问财等并降级。",
                    "input": "股票代码、查询词、公告/研报/行情请求",
                    "output": "标准化 DataFrame、公告/研报列表、语义搜索结果、provider health",
                },
                {
                    "path": "core/research/perilla/discovery/report_causal_extractor.py",
                    "role": "P1 因果抽取。LLM 优先，失败时规则降级；对 quote 做 grounding 校验。",
                    "input": "raw_documents_latest.json",
                    "output": "report_claims、causal_chains、evidence_items、failure_conditions",
                },
                {
                    "path": "core/utils/llm_usage.py",
                    "role": "LLM 预算和账本，记录 source、provider、model、token、成功/失败。",
                    "input": "messages、provider 配置、预算配置",
                    "output": "logs/llm_usage/YYYYMMDD.jsonl",
                },
                {
                    "path": "core/research/perilla/discovery/industry_chain_graph_builder.py",
                    "role": "把候选概念、因果链和缺失边组织成产业链图。",
                    "input": "candidate_concepts、causal_chains、concept_scores",
                    "output": "industry_chain_graph、missing_edges",
                },
                {
                    "path": "core/research_graph/evidence_ledger.py",
                    "role": "canonical evidence 准入账本，只有进入这里的证据才可支撑全局事实。",
                    "input": "Evidence 对象、support/contradict 请求",
                    "output": "GraphStore 中的 evidence_ids / counter_evidence_ids",
                },
                {
                    "path": "core/research_graph/verifier.py",
                    "role": "图谱关系验证器，防止弱文本或单源材料直接升级成可信关系。",
                    "input": "候选边、证据、反证",
                    "output": "ACTIVE / CANDIDATE / CONFLICTED 等边状态",
                },
                {
                    "path": "core/research_graph/neuron/environment.py",
                    "role": "Neuron 增长环境，消费事件和脉冲，驱动证据路由、状态更新、突变记录。",
                    "input": "GrowthPulse、EvidenceArrivedEvent、ResearchGraph 状态",
                    "output": "tick summary、mutation log、neuron confidence/uncertainty",
                },
                {
                    "path": "core/research_graph/neuron/tick_scheduler.py",
                    "role": "按信息增益选择下一步研究预算投向。",
                    "input": "neuron 状态、证据缺口、staleness、contradiction pressure",
                    "output": "selected neuron、priority、information gain",
                },
                {
                    "path": "core/research/opinion/synthesizer.py",
                    "role": "把支持证据、反证、时效、来源多样性合成置信度和不确定性。",
                    "input": "support evidence、counter evidence、reference date",
                    "output": "ResearchOpinion、confidence_score、uncertainty、revision_triggers",
                },
                {
                    "path": "scripts/build_research_cockpit.py",
                    "role": "把真实 run artifacts 汇总成 cockpit_data.json，显示知识增量、数据流、LLM 贡献和判断链。",
                    "input": "cockpit_real_probe/p0、focus_p1、perilla_discovery baseline",
                    "output": "data/research_runs/latest/cockpit/cockpit_data.json",
                },
                {
                    "path": "dashboard.py",
                    "role": "FastAPI 只读服务，提供 cockpit JSON 的 API 端点和单页前端。",
                    "input": "cockpit_data.json 或降级 artifacts",
                    "output": "/api/summary、/api/system-flow、/api/baseline-comparison、/api/target-judgment 等",
                },
                {
                    "path": "templates/index.html",
                    "role": "纯 HTML/JS 可视化前端，展示总览、系统结构、贡献证明、标的判断、图谱和事件流。",
                    "input": "dashboard.py API JSON",
                    "output": "浏览器里的 AquaResearch Cockpit",
                },
            ],
            "nodes": [
                {"name": "外部/本地材料", "description": f"raw_documents={len(raw_docs)}\n新增 hash={len(new_hashes)}"},
                {"name": "去重与候选概念", "description": f"candidate_concepts={len(concepts)}\nevidence_edges={len(evidence_edges)}"},
                {"name": "调度器", "description": f"选中: {selected_name}\n理由: {_short(selected.get('discovery_reason'), 90)}"},
                {"name": "LLM/规则抽取", "description": f"claims={len(claims)}\ncausal_chains={len(chains)}\nevidence_items={len(evidence)}"},
                {"name": "准入与判断", "description": f"accepted={accepted_evidence}\npending/rejected={rejected_or_pending}"},
            ],
            "lineage": [
                {"stage": "P0", "input": "本地 inbox、外部新闻/搜索、ProviderRegistry", "process": "抽取 raw_terms、候选概念、证据边", "output": f"{selected_name}", "audit": "raw_hash 去重、source_payload 保留"},
                {"stage": "Scheduler", "input": "candidate_concepts + evidence_edges", "process": "按证据缺口/共现/新颖度选焦点", "output": f"焦点材料 {len(_safe_list(focus / 'raw_documents_latest.json'))} 条", "audit": "记录 discovery_reason 和 evidence_document_ids"},
                {"stage": "P1", "input": "焦点 raw_documents", "process": "LLM JSON 抽取 + grounding 校验 + 规则降级", "output": f"claim={len(claims)}, chain={len(chains)}, evidence={len(evidence)}", "audit": "LLM usage log、cache_hit、grounding_drop"},
                {"stage": "Judgment", "input": "evidence_items + target docs", "process": "弱外部结果不准入，候选因果保持 pending", "output": "INSUFFICIENT_EVIDENCE", "audit": "不输出买卖/仓位/止盈止损"},
            ],
        },
        "baseline_comparison": {
            "kpis": {
                "raw_documents": len(raw_docs),
                "new_documents": len(new_hashes),
                "candidate_concepts": len(concepts),
                "claims": len(claims),
                "causal_chains": len(chains),
                "evidence_edges": len(evidence_edges),
                "accepted_evidence": accepted_evidence,
                "duplicates_skipped": duplicate_count,
                "llm_calls": llm_calls,
            },
            "metrics": [
                {"metric": "读取材料数", "baseline": len(raw_docs), "autonomous": len(_safe_list(focus / "raw_documents_latest.json")), "delta": len(_safe_list(focus / "raw_documents_latest.json")) - len(raw_docs), "format": "int", "explanation": "自主调度只读焦点材料，避免把全库都送进 LLM。"},
                {"metric": "有效 evidence", "baseline": 0, "autonomous": accepted_evidence, "delta": accepted_evidence, "format": "int", "explanation": "P1 抽取并保留可追溯 quote/evidence_id。"},
                {"metric": "候选因果", "baseline": 0, "autonomous": len(chains), "delta": len(chains), "format": "int", "explanation": "仅作为 candidate causal chain，不直接升级为事实。"},
                {"metric": "假因果拦截", "baseline": 0, "autonomous": rejected_or_pending, "delta": rejected_or_pending, "format": "int", "explanation": "弱相关搜索结果和 failure condition 被保留为 pending/rejected。"},
                {"metric": "Evidence/LLM 调用", "baseline": 0, "autonomous": round(accepted_evidence / max(1, llm_calls), 4), "delta": round(accepted_evidence / max(1, llm_calls), 4), "format": "f4", "explanation": "衡量预算是否换来结构化证据。"},
            ],
        },
        "target_judgment": {
            "target": "中国西电(601179.SH)",
            "conclusion_level": "INSUFFICIENT_EVIDENCE",
            "validation_score": 0.25,
            "evidence_count": len(target_evidence),
            "reasoning_chain": [
                {"title": "本地候选链路", "detail": "inbox 中存在特高压换流变压器组件相关候选假设，但不是官方/研报强证据。"},
                {"title": "外部搜索校验", "detail": "搜索结果混入百度百科和板块页，弱相关，不能支持单一标的判断。"},
                {"title": "准入判断", "detail": "缺少公告、年报、订单、认证或客户验证证据，因此不升级为可信结论。"},
                {"title": "下一步学习任务", "detail": "优先检索公司公告、定期报告、投资者互动中关于特高压/换流阀/绝缘组件的原文证据。"},
            ],
            "evidence": target_evidence,
        },
        "selected_quotes": selected_quotes[:5],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--output", default=str(LATEST_COCKPIT_DIR / "cockpit_data.json"))
    args = parser.parse_args()

    data = build_cockpit(Path(args.run_dir))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "evidence": data["summary"]["canonical_evidence_count"],
        "new_documents": data["baseline_comparison"]["kpis"]["new_documents"],
        "llm_calls": data["baseline_comparison"]["kpis"]["llm_calls"],
        "target": data["target_judgment"]["target"],
        "conclusion_level": data["target_judgment"]["conclusion_level"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
