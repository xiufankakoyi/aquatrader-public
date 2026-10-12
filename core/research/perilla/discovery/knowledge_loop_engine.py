"""Evidence-bound Perilla knowledge loop.

This module implements the local first-stage loop described in goal.md:

raw_documents -> raw_terms -> candidate_concepts -> evidence_edges ->
search_tasks -> AquaTrade ResearchEngine execution -> raw_documents refresh.

Data sources for raw_documents:
1. inbox (inbox_items.json)
2. local_titles (data/documents/ 下所有 .txt)
3. 在线内容源 (财经新闻/公告/研报/社交媒体，通过模块级采集函数)
4. ResearchEngine (search task execution via query_source)
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase
from core.research.perilla.bridge.perilla_sag_bridge import PerillaSAGBridge, PerillaSearchTask
from core.research.perilla.discovery.concept_llm_explainer import ConceptLLMExplainer
from core.research.perilla.discovery.concept_signal_loader import ConceptSignalLoader
from core.research.perilla.discovery.concept_taxonomy import ConceptTaxonomy
from core.research.perilla.discovery.models import ConceptSignal
from core.research.perilla.event.event_graph_schema import EventClaim, EventClaimStatus, EventType
from core.research_graph.raw_document import RawDocument

logger = logging.getLogger(__name__)

SAFE_LEVELS = {"HYPOTHESIS", "RUMOR_ONLY", "EARLY_SIGNAL", "CONCEPT_CLAIM_ONLY"}

# 质量阶梯调度配置
TIER_ORDER: tuple[str, ...] = ("high", "medium", "low")
CAPABILITY_TO_TIER: dict[str, str] = {
    "announcements": "high",
    "reports": "high",
    "periodic_reports": "high",
    "investor_interactions": "high",
    "news": "medium",
    "semantic_search": "low",
    "market_data": "low",
}

# 文档 source_type -> 质量阶梯，用于执行后评估
SOURCE_TYPE_TO_TIER: dict[str, str] = {
    "announcement": "high",
    "research_report": "high",
    "periodic_report": "high",
    "investor_interaction": "high",
    "news": "medium",
    "online_news": "medium",
    "semantic_search": "low",
    "hot_theme": "low",
    "live_feed": "low",
}
ROOTS = {"\u5149", "\u5b58", "\u7b97", "\u7535"}

_ABBR_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,8}\b|\b\d(?:\.\d)?T\b|\b\d{3,4}[GV]\b")
_SYMBOL_RE = re.compile(r"\b(?:\d{6}\.(?:SZ|SH|BJ)|[A-Z]{0,2}\d{6})\b", re.IGNORECASE)
_TECH_TERM_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{2,14}"
    r"(?:\u6a21\u5757|\u82af\u7247|\u5f15\u64ce|\u5668\u4ef6|\u7535\u6e90|"
    r"\u8fde\u63a5\u5668|\u63a7\u5236\u5668|\u5c01\u88c5|\u6750\u6599|\u8bbe\u5907|"
    r"\u4f9b\u7535|\u6db2\u51b7|\u5149\u4e92\u8fde|\u9ad8\u538b\u76f4\u6d41|"
    r"\u63a2\u6d4b\u5668|\u886c\u5e95|\u8d34\u7247\u673a|\u94dc\u7b94|"
    r"\u6811\u8102|\u78c1\u6027\u6750\u6599|\u6d47\u6ce8\u8bbe\u5907)"
)
_BAD_TERM_FRAGMENTS = (
    "\u8be5\u73af\u8282", "\u662f", "\u4f46", "\u76ee\u524d", "\u9700\u8981",
    "\u5168\u7403", "\u4f9b\u5e94\u9ad8\u5ea6", "\u7f3a\u4e86\u4e0d\u53ef",
    "\u548c\u8d85\u7b97\u96c6\u7fa4", "\u7ec8\u7aef\u5149\u4ea4\u6362\u8bbe\u5907",
)
_STOP_TERMS = {
    "LOW", "HIGH", "MEDIUM", "WATCHING", "DISMISSED", "OTHER", "UNKNOWN",
    "HTTPS", "HTTP", "HTML", "WWW", "COM", "CN", "ORG", "NET", "INDEX",
    "FORM", "ID", "SORTID", "MENUID", "LANID", "MSV", "VIEWS", "WAP",
    "GROUP", "CHINA", "HOLDINGS", "INC", "BAIDU", "XUEQIU", "ZH", "EN",
    "US", "HK", "TW", "SH", "SZ", "JD", "ASP",
}
_ALLOWED_ABBR = {
    "CPO", "LPO", "NPO", "HBM", "CXL", "ASIC", "GPU", "DRAM", "NAND",
    "SSD", "HVDC", "UPS", "PDU", "PCB", "PTFE", "PPE", "MLCC", "GB200",
    "G200", "H100", "H200", "NVL72", "800G", "1.6T", "800V", "IGBT",
    "INP", "SPARC",
}
_OLD_SEED_FRAGMENTS = {
    "CPO", "LPO", "NPO", "HBM", "800G", "1.6T", "800V",
    "GPU", "ASIC", "DRAM", "NAND", "SSD", "UPS", "PDU", "PCB", "IGBT",
    "\u78f7\u5316\u94df", "InP", "\u7845\u5149", "\u886c\u5e95", "\u57fa\u6750",
    "\u5149\u6a21\u5757", "\u5149\u82af\u7247", "\u5149\u5f15\u64ce",
}
_GENERIC_COLD_EXCLUDE = {
    "\u7535\u529b\u8bbe\u5907", "\u4ea4\u6362\u673a", "\u5149\u4e92\u8fde",
    "\u5149\u5f15\u64ce", "\u5149\u82af\u7247", "\u5149\u6a21\u5757",
    "\u53d8\u538b\u5668", "AI\u82af\u7247", "AI\u670d\u52a1\u5668",
    "\u6fc0\u5149\u5668", "\u78f7\u5316\u94df\u886c\u5e95",
    "\u78f7\u5316\u94df", "\u78f7\u5316\u9502", "\u78b3\u5316\u7845",
    "\u7845\u5149\u82af\u7247", "\u5149\u7535\u5171\u5c01\u88c5",
}


def _safe_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) and not isinstance(value, complex):
        return str(value)
    return default


def _mapping_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _safe_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := _safe_text(item)).strip()]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _response_mapping(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _nested_records(payload: Any, *keys: str) -> list[dict[str, Any]]:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return []
        current = current.get(key)
    return [item for item in current if isinstance(item, dict)] if isinstance(current, list) else []


def _frame_records(frame: Any) -> list[dict[str, Any]]:
    try:
        records = frame.to_dict(orient="records")
    except (AttributeError, TypeError, ValueError):
        return []
    return [item for item in records if isinstance(item, dict)] if isinstance(records, list) else []

# ======================================================================
# 在线内容型数据源采集函数（模块级，便于独立测试和 mock）
# 数据源：财经新闻 / 公告 / 研报 / 社交媒体
# ======================================================================

_NEWS_PAGES = 5  # 新浪财经每源最大采集页数
_NEWS_PAGE_SIZE = 50


def _fetch_sina_news(pages: int = _NEWS_PAGES) -> list[dict[str, Any]]:
    """新浪财经 API 多页采集（多分类：财经要闻+全球财经+股票）。"""
    try:
        import requests
        result: list[dict[str, Any]] = []
        url = "https://feed.mix.sina.com.cn/api/roll/get"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
                    "Referer": "https://finance.sina.com.cn/"}
        # lid=2509 财经要闻, lid=2516 全球财经, lid=2510 股票
        for lid in ("2509", "2516", "2510"):
            for page in range(1, pages + 1):
                params = {"pageid": "153", "lid": lid, "k": "", "num": str(_NEWS_PAGE_SIZE), "page": str(page)}
                try:
                    resp = requests.get(url, params=params, headers=headers, timeout=10)
                    if resp.status_code != 200:
                        break
                    items = _nested_records(_response_mapping(resp), "result", "data")
                    if not items:
                        break
                    for item in items:
                        title = _safe_text(item.get("title")).strip()
                        if title:
                            result.append({"title": title, "url": _safe_text(item.get("url")),
                                           "time": _safe_text(item.get("ctime")), "digest": _safe_text(item.get("digest")).strip()})
                except Exception:
                    break
                time.sleep(0.3)
        return result
    except Exception as e:
        logger.warning("新浪财经 API 调用失败: %s", e)
        return []


def _fetch_eastmoney_news() -> list[dict[str, Any]]:
    """东方财富财经要闻 + 7x24 快讯。"""
    try:
        import requests
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
        result: list[dict[str, Any]] = []
        # 财经要闻 (column=350) + 7x24快讯 (column=351)
        for column, label in [("350", "要闻"), ("351", "7x24快讯")]:
            try:
                params = {"client": "web", "biz": "web_news_col", "column": column,
                          "order": "1", "needInteract": "0", "page_index": "1",
                          "page_size": "100", "req_trace": f"em_{column}"}
                resp = requests.get(
                    "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
                    params=params, headers=headers, timeout=10,
                )
                if resp.status_code != 200:
                    continue
                items = _nested_records(_response_mapping(resp), "data", "list")
                for item in items:
                    title = _safe_text(item.get("title")).strip()
                    if title:
                        result.append({"title": title, "url": _safe_text(item.get("url")),
                                       "time": _safe_text(item.get("showTime")),
                                       "digest": _safe_text(item.get("summary") or item.get("digest")).strip()})
            except Exception:
                continue
        return result
    except Exception as e:
        logger.warning("东方财富 API 调用失败: %s", e)
        return []


def _fetch_ths_headlines() -> list[dict[str, Any]]:
    """同花顺快讯/头条。"""
    try:
        import requests
        url = "https://news.10jqka.com.cn/tapp/news/push/stock"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
        resp = requests.get(url, timeout=10, headers=headers)
        if resp.status_code != 200:
            return []
        items = _nested_records(_response_mapping(resp), "data", "list")
        result = []
        for item in items:
            title = _safe_text(item.get("title")).strip()
            if not title:
                continue
            ts = _safe_text(item.get("ctime"))
            time_str = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M") if ts.isdigit() else ""
            result.append({"title": title, "url": _safe_text(item.get("shareUrl") or item.get("url")),
                           "time": time_str, "digest": _safe_text(item.get("digest")).strip()})
        return result
    except Exception as e:
        logger.warning("同花顺快讯 API 调用失败: %s", e)
        return []


def _fetch_eastmoney_guba() -> list[dict[str, Any]]:
    """东方财富 7x24 快讯（原股吧 API 已封，改用 7x24 快讯作为社交媒体替代源）。

    7x24 快讯包含实时市场动态、个股异动、机构观点等高价值信息。
    """
    try:
        import requests
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
        result: list[dict[str, Any]] = []
        # 多页采集 7x24 快讯
        for page in range(1, 4):
            try:
                params = {"client": "web", "biz": "web_news_col", "column": "351",
                          "order": "1", "needInteract": "0", "page_index": str(page),
                          "page_size": "100", "req_trace": f"em_351_p{page}"}
                resp = requests.get(
                    "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
                    params=params, headers=headers, timeout=10,
                )
                if resp.status_code != 200:
                    continue
                items = _nested_records(_response_mapping(resp), "data", "list")
                for item in items:
                    title = _safe_text(item.get("title")).strip()
                    if title:
                        result.append({"title": title, "url": _safe_text(item.get("url")),
                                       "time": _safe_text(item.get("showTime")),
                                       "digest": _safe_text(item.get("summary")).strip()[:500],
                                       "board": "7x24快讯"})
            except Exception:
                continue
            time.sleep(0.2)
        return result
    except Exception as e:
        logger.warning("东方财富 7x24 快讯 API 调用失败: %s", e)
        return []


def _fetch_tushare_news() -> list[dict[str, Any]]:
    """Tushare 财经新闻（需 token）。"""
    try:
        import tushare as ts
        from config.setting import Setting
        token = Setting.TUSHARE_TOKEN
        if not token:
            return []
        pro = ts.pro_api(token)
        result: list[dict[str, Any]] = []
        for days_back in range(3):
            date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
            try:
                df = pro.news(src="sina", start_date=date, end_date=date)
                for row in _frame_records(df):
                    title = _safe_text(row.get("title")).strip()
                    if title:
                        result.append({"title": title, "url": _safe_text(row.get("channels")),
                                       "time": _safe_text(row.get("datetime")),
                                       "digest": _safe_text(row.get("content"))[:500]})
            except Exception:
                continue
            time.sleep(0.5)
        return result
    except ImportError:
        return []
    except Exception as e:
        logger.warning("Tushare 新闻 API 调用失败: %s", e)
        return []


def _fetch_tushare_announcements() -> list[dict[str, Any]]:
    """Tushare 上市公司公告（需 token）。"""
    try:
        import tushare as ts
        from config.setting import Setting
        token = Setting.TUSHARE_TOKEN
        if not token:
            return []
        pro = ts.pro_api(token)
        result: list[dict[str, Any]] = []
        for days_back in range(3):
            date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
            try:
                df = pro.anns(ann_date=date)
                for row in _frame_records(df):
                    ann_type = _safe_text(row.get("ann_type")).strip()
                    ts_code = _safe_text(row.get("ts_code")).strip()
                    if ann_type or ts_code:
                        result.append({"title": f"{ts_code} {ann_type}", "url": "", "time": date,
                                       "digest": f"公告类型:{ann_type} 公司:{ts_code}", "ts_code": ts_code})
            except Exception:
                continue
            time.sleep(0.5)
        return result
    except ImportError:
        return []
    except Exception as e:
        logger.warning("Tushare 公告 API 调用失败: %s", e)
        return []


def _fetch_tushare_reports() -> list[dict[str, Any]]:
    """Tushare 研究报告（需 token）。"""
    try:
        import tushare as ts
        from config.setting import Setting
        token = Setting.TUSHARE_TOKEN
        if not token:
            return []
        pro = ts.pro_api(token)
        result: list[dict[str, Any]] = []
        for days_back in range(3):
            date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
            try:
                df = pro.report_rc(pub_date=date)
                for row in _frame_records(df):
                    title = _safe_text(row.get("title")).strip()
                    if title:
                        result.append({"title": title, "url": _safe_text(row.get("url")), "time": date,
                                       "digest": f"机构:{_safe_text(row.get('researcher'))} 评级:{_safe_text(row.get('em_rating'))}",
                                       "ts_code": _safe_text(row.get("ts_code"))})
            except Exception:
                continue
            time.sleep(0.5)
        return result
    except ImportError:
        return []
    except Exception as e:
        logger.warning("Tushare 研报 API 调用失败: %s", e)
        return []


@dataclass
class LoopRunResult:
    raw_sources: List[Dict[str, Any]]
    raw_documents: list[RawDocument]
    raw_terms: List[Dict[str, Any]]
    candidate_concepts: List[Dict[str, Any]]
    llm_explanations: List[Dict[str, Any]]
    evidence_edges: List[Dict[str, Any]]
    search_tasks: List[Dict[str, Any]]
    task_runs: List[Dict[str, Any]]
    selected_new_term: Dict[str, Any]
    summary_text: str
    outputs: Dict[str, str]
    drill_down_tree: Optional[Dict[str, Any]] = None
    drill_down_graph: Optional[Dict[str, Any]] = None
    sag_stats: Dict[str, Any] = field(default_factory=dict)
    promotion_candidates: List[Dict[str, Any]] = field(default_factory=list)


class PerillaKnowledgeLoopEngine:
    """Run the evidence-bound new-term discovery loop."""

    def __init__(
        self,
        project_root: str | Path = ".",
        output_dir: str | Path = "data/reports/perilla_discovery",
        max_execute_tasks: int = 20,
        use_llm_explanations: bool = True,
        max_llm_terms: int = 10,
        enable_drill_down: bool = False,
        drill_down_depth: int = 3,
        kb: Optional[SAGKnowledgeBase] = None,
        storage_path: str = "",
        bridge: Optional[PerillaSAGBridge] = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.output_dir = Path(output_dir)
        if not self.output_dir.is_absolute():
            self.output_dir = self.project_root / self.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.loader = ConceptSignalLoader(self.project_root, self.output_dir)
        self.taxonomy = ConceptTaxonomy()
        self.max_execute_tasks = _safe_nonnegative_int(max_execute_tasks, 20)
        self.use_llm_explanations = use_llm_explanations
        self.max_llm_terms = _safe_nonnegative_int(max_llm_terms, 0)
        self.llm_explainer = (
            ConceptLLMExplainer(self.project_root / "data" / "llm_cache" / "perilla_concept_explanations")
            if use_llm_explanations and self.max_llm_terms > 0
            else None
        )
        self.enable_drill_down = enable_drill_down
        self.drill_down_depth = _safe_nonnegative_int(drill_down_depth, 0)

        # SAG 与桥接层
        if kb is None and storage_path:
            kb = SAGKnowledgeBase(storage_path=storage_path)
        self.kb = kb
        self.bridge = bridge or (PerillaSAGBridge(kb=self.kb) if self.kb else None)

    def run(
        self,
        execute_search: bool = True,
        pending_search_tasks: Optional[List[PerillaSearchTask]] = None,
    ) -> LoopRunResult:
        raw_sources = self._build_raw_sources()
        raw_documents = self._build_raw_documents()
        raw_terms = self._extract_raw_terms(raw_documents)
        candidate_concepts = self._build_candidate_concepts(raw_terms)
        llm_explanations: list[dict[str, Any]] = []
        candidate_concepts, llm_explanations = self._enrich_candidate_concepts_with_llm(
            candidate_concepts,
            raw_terms,
        )
        evidence_edges = self._build_evidence_edges(candidate_concepts, raw_terms)
        selected = self._select_cold_new_term(candidate_concepts)
        search_tasks = self._build_search_tasks(
            candidate_concepts,
            evidence_edges,
            preferred_terms=[selected.get("normalized_name", "")],
        )

        # 合并来自 AlphaDiscoveryEngine 的搜索任务
        if pending_search_tasks:
            existing_ids = {_safe_text(t.get("task_id")) for t in _mapping_records(search_tasks)}
            pending = pending_search_tasks if isinstance(pending_search_tasks, (list, tuple)) else []
            for task in pending:
                try:
                    task_dict = task.to_dict() if hasattr(task, "to_dict") else task
                except Exception:
                    task_dict = {}
                if not isinstance(task_dict, dict):
                    continue
                task_id = _safe_text(task_dict.get("task_id"))
                if task_id and task_id not in existing_ids:
                    search_tasks.append(self._perilla_task_to_loop_task(task_dict))
                    existing_ids.add(task_id)
            search_tasks.sort(key=lambda t: _safe_float(t.get("priority_score")), reverse=True)

        task_runs: list[dict[str, Any]] = []
        if execute_search:
            task_runs, new_docs = self._execute_search_tasks(search_tasks)
            self._persist_research_control_tasks(
                search_tasks,
                task_runs=task_runs,
                limit=self.max_execute_tasks,
            )
            if new_docs:
                raw_documents = self._dedupe_documents(raw_documents + new_docs)
                raw_terms = self._extract_raw_terms(raw_documents)
                candidate_concepts = self._build_candidate_concepts(raw_terms)
                candidate_concepts, llm_explanations = self._enrich_candidate_concepts_with_llm(
                    candidate_concepts,
                    raw_terms,
                )
                evidence_edges = self._build_evidence_edges(candidate_concepts, raw_terms)
                selected = self._select_cold_new_term(candidate_concepts)
                search_tasks = self._build_search_tasks(
                    candidate_concepts,
                    evidence_edges,
                    preferred_terms=[selected.get("normalized_name", "")],
                )
        else:
            self._persist_research_control_tasks(
                search_tasks,
                limit=self.max_execute_tasks,
            )

        # 将候选概念与证据边写入 SAG Event Graph
        sag_stats = self._write_candidate_concepts_to_sag(candidate_concepts, raw_documents)
        promotion_candidates = self._build_promotion_candidates()

        summary = self._render_summary(selected, raw_terms, candidate_concepts, evidence_edges, search_tasks)

        # 下钻触发模块：对候选概念中类型为 component/material 且非原子的概念触发下钻
        drill_down_tree: dict[str, Any] | None = None
        drill_down_graph: dict[str, Any] | None = None
        if self.enable_drill_down:
            drill_down_tree, drill_down_graph = self._execute_drill_down(
                candidate_concepts, evidence_edges,
            )

        outputs = self._write_outputs(
            sag_stats=sag_stats,
            promotion_candidates=promotion_candidates,
            raw_sources=raw_sources,
            raw_documents=raw_documents,
            raw_terms=raw_terms,
            candidate_concepts=candidate_concepts,
            llm_explanations=llm_explanations,
            evidence_edges=evidence_edges,
            search_tasks=search_tasks,
            task_runs=task_runs,
            summary=summary,
            selected_new_term=selected,
        )
        return LoopRunResult(
            raw_sources=raw_sources,
            raw_documents=raw_documents,
            raw_terms=raw_terms,
            candidate_concepts=candidate_concepts,
            llm_explanations=llm_explanations,
            evidence_edges=evidence_edges,
            search_tasks=search_tasks,
            task_runs=task_runs,
            selected_new_term=selected,
            summary_text=summary,
            outputs=outputs,
            drill_down_tree=drill_down_tree,
            drill_down_graph=drill_down_graph,
            sag_stats=sag_stats,
            promotion_candidates=promotion_candidates,
        )

    def _write_candidate_concepts_to_sag(
        self,
        candidate_concepts: List[Dict[str, Any]],
        raw_documents: list[RawDocument],
    ) -> Dict[str, Any]:
        """将候选概念写入 SAG Event Graph。

        Args:
            candidate_concepts: 候选概念列表。
            raw_documents: 原始文档列表，用于补充证据文本。

        Returns:
            统计信息 dict。
        """
        if not self.bridge or not self.kb:
            return {"ingested": 0, "skipped": len(_mapping_records(candidate_concepts))}

        doc_by_id = {
            doc.document_id: doc
            for doc in raw_documents if isinstance(doc, RawDocument)
        }
        ingested = 0
        skipped = 0
        for concept in _mapping_records(candidate_concepts):
            name = _safe_text(concept.get("normalized_name") or concept.get("concept_name"))
            if not name:
                skipped += 1
                continue
            evidence_ids = _safe_text_list(concept.get("evidence_document_ids"))
            evidence_text = ""
            for eid in evidence_ids[:3]:
                doc = doc_by_id.get(str(eid))
                if doc:
                    evidence_text = _safe_text(doc.content or doc.title)[:500]
                    break
            if not evidence_text:
                evidence_text = _safe_text(concept.get("llm_explanation"))[:500]

            signal = ConceptSignal(
                concept_id=_safe_text(concept.get("concept_id"), f"concept_{_short_hash(name)}"),
                name=name,
                aliases=[name],
                detected_from=_safe_text_list(concept.get("evidence_titles")),
                source_types=["local_evidence"],
                first_seen_at=_safe_text(concept.get("first_seen_at")),
                last_seen_at=_safe_text(concept.get("last_seen_at")),
                heat_score=_safe_float(concept.get("concept_score")),
                novelty_score=_safe_float(concept.get("novelty_score")),
                confidence="medium" if _safe_float(concept.get("concept_score")) >= 0.5 else "low",
                raw_evidence_ids=evidence_ids,
                notes=evidence_text,
            )
            try:
                self.bridge.ingest_concept_signal(signal)
                ingested += 1
            except Exception as exc:
                logger.warning("写入概念到 SAG 失败: %s", exc)
                skipped += 1

        return {"ingested": ingested, "skipped": skipped}

    def _build_promotion_candidates(self) -> List[Dict[str, Any]]:
        """从 SAG Event Graph 构建晋升候选。

        Returns:
            PromotionCandidate dict 列表。
        """
        if not self.bridge or not self.kb:
            return []
        try:
            candidates = self.bridge.build_promotion_candidates(max_candidates=50)
            return [self._candidate_to_dict(c) for c in candidates]
        except Exception as exc:
            logger.warning("构建晋升候选失败: %s", exc)
            return []

    @staticmethod
    def _candidate_to_dict(candidate: Any) -> Dict[str, Any]:
        """将 PromotionCandidate 转为可序列化 dict。"""
        return {
            "candidate_id": getattr(candidate, "candidate_id", ""),
            "event_edge_key": getattr(candidate, "event_edge_key", ()),
            "proposed_head": getattr(candidate, "proposed_head", ""),
            "proposed_relation": getattr(candidate, "proposed_relation", ""),
            "proposed_tail": getattr(candidate, "proposed_tail", ""),
            "evidence_summary": getattr(candidate, "evidence_summary", ""),
            "source_types": getattr(candidate, "source_types", []),
            "source_count": getattr(candidate, "source_count", 0),
            "promotion_score": getattr(candidate, "promotion_score", 0.0),
            "status": getattr(candidate, "status", ""),
            "reason": getattr(candidate, "reason", ""),
        }

    def _build_raw_sources(self) -> List[Dict[str, Any]]:
        now = datetime.now().isoformat()
        return [
            {
                "source_id": "research_inbox",
                "source_name": "AquaTrader research inbox",
                "source_type": "inbox",
                "access_method": "local_json",
                "base_url": str(self.loader.inbox_path),
                "enabled": True,
                "update_frequency": "daily",
                "last_checked_at": now,
                "notes": "Local evidence source.",
            },
            {
                "source_id": "local_titles",
                "source_name": "AquaTrader local news/report titles",
                "source_type": "local_documents",
                "access_method": "local_files",
                "base_url": str(self.project_root / "data" / "documents"),
                "enabled": True,
                "update_frequency": "daily",
                "last_checked_at": now,
                "notes": "Title and text snippets already inside AquaTrader.",
            },
            {
                "source_id": "aquatrade_research_engine",
                "source_name": "AquaTrade ResearchEngine SourceRouter",
                "source_type": "router",
                "access_method": "ResearchEngine.query_source",
                "base_url": "internal://core.research.engine.ResearchEngine",
                "enabled": True,
                "update_frequency": "on_demand",
                "last_checked_at": now,
                "notes": "Search task execution must use this router.",
            },
        ]

    def _build_raw_documents(self) -> list[RawDocument]:
        """从所有数据源构建 raw_documents。

        数据源：
        1. 收件箱 (inbox_items.json)
        2. 本地文件 (data/documents/ 下所有 .txt)
        3. 在线内容源 (财经新闻/公告/研报/社交媒体)
        """
        docs: list[RawDocument] = []

        # 1. 收件箱
        for item in _mapping_records(self.loader.load_inbox_items()):
            text = " ".join(_flatten_text(item))
            docs.append(
                self._make_document(
                    source_id="research_inbox",
                    source_type="inbox",
                    title=_safe_text(item.get("main_dish") or item.get("concept") or item.get("chain_node")),
                    summary=_safe_text(item.get("bottleneck_logic") or item.get("notes")),
                    content_text=text,
                    related_companies=[self._format_company(item)] if item.get("company_name") else [],
                    related_themes=_safe_text_list(item.get("concept")),
                    published_at=_safe_text(item.get("first_seen_at")),
                    collected_at=_safe_text(item.get("last_updated_at"), datetime.now().isoformat()),
                    source_payload=item,
                )
            )

        # 2. 本地文件
        for item in _mapping_records(self.loader.load_title_items()):
            docs.append(
                self._make_document(
                    source_id="local_titles",
                    source_type=_safe_text(item.get("category"), "local_title") or "local_title",
                    title=_safe_text(item.get("title")),
                    summary=_safe_text(item.get("summary")),
                    content_text=" ".join(_flatten_text(item)),
                    related_companies=[],
                    related_themes=[],
                    published_at=_safe_text(item.get("last_updated_at")),
                    collected_at=datetime.now().isoformat(),
                    source_payload=item,
                )
            )

        # 3. 在线内容源（财经新闻/公告/研报/社交媒体/热点题材）
        online_fetchers = [
            ("sina", "online_news", _fetch_sina_news),
            ("eastmoney", "online_news", _fetch_eastmoney_news),
            ("ths", "online_news", _fetch_ths_headlines),
            ("tushare_news", "online_news", _fetch_tushare_news),
            ("tushare_ann", "announcement", _fetch_tushare_announcements),
            ("tushare_report", "research_report", _fetch_tushare_reports),
            ("eastmoney_guba", "live_feed", _fetch_eastmoney_guba),
        ]
        for src_id, src_type, fetcher in online_fetchers:
            try:
                articles = _mapping_records(fetcher())
                for article in articles:
                    title = _safe_text(article.get("title"))
                    digest = _safe_text(article.get("digest"))
                    ts_code = _safe_text(article.get("ts_code"))
                    docs.append(self._make_document(
                        source_id=src_id,
                        source_type=src_type,
                        title=title,
                        summary=digest,
                        content_text=f"{title}\n{digest}" if digest else title,
                        related_companies=[ts_code] if ts_code else [],
                        related_themes=[],
                        published_at=_safe_text(article.get("time")),
                        collected_at=datetime.now().isoformat(),
                        source_payload=article,
                    ))
                if articles:
                    logger.info("在线内容源 %s: %d 条", src_id, len(articles))
            except Exception as e:
                logger.warning("在线内容源 %s 采集失败: %s", src_id, e)

        # 4. ProviderRegistry 增强数据源（公告/研报/热点题材/语义搜索）
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry
            registry = ProviderRegistry()

            # 公告（巨潮资讯 → Tushare → AKShare 三层降级）
            for code in self._get_top_candidate_codes(docs, limit=20):
                try:
                    anns = _mapping_records(registry.get_announcements(code=code, count=5))
                    for ann in anns:
                        title = _safe_text(ann.get("title"))
                        if title:
                            docs.append(self._make_document(
                                source_id="cninfo_ann", source_type="announcement",
                                title=title, summary=_safe_text(ann.get("digest")),
                                content_text=title,
                                related_companies=[code],
                                related_themes=[],
                                published_at=_safe_text(ann.get("date")),
                                collected_at=datetime.now().isoformat(),
                                source_payload=ann,
                            ))
                except Exception:
                    pass

            # 研报（AKShare）
            for code in self._get_top_candidate_codes(docs, limit=10):
                try:
                    reports = _mapping_records(registry.get_research_reports(code=code, count=3))
                    for rpt in reports:
                        title = _safe_text(rpt.get("title"))
                        if title:
                            docs.append(self._make_document(
                                source_id="akshare_report", source_type="research_report",
                                title=title, summary=_safe_text(rpt.get("digest")),
                                content_text=title,
                                related_companies=[code],
                                related_themes=[],
                                published_at=_safe_text(rpt.get("date")),
                                collected_at=datetime.now().isoformat(),
                                source_payload=rpt,
                            ))
                except Exception:
                    pass

            # Periodic reports: annual / semiannual / quarterly reports.
            for code in self._get_top_candidate_codes(docs, limit=10):
                try:
                    periodic_reports = _mapping_records(registry.get_periodic_reports(code=code, count=6))
                    for rpt in periodic_reports:
                        title = _safe_text(rpt.get("title"))
                        if title:
                            source_type = _safe_text(rpt.get("report_type"), "periodic_report") or "periodic_report"
                            docs.append(self._make_document(
                                source_id="periodic_report", source_type=source_type,
                                title=title, summary=_safe_text(rpt.get("digest")),
                                content_text=title,
                                related_companies=[code],
                                related_themes=[],
                                published_at=_safe_text(rpt.get("date")),
                                collected_at=datetime.now().isoformat(),
                                source_payload=rpt,
                                url=_safe_text(rpt.get("pdf_url") or rpt.get("link")),
                            ))
                except Exception:
                    pass

            # Investor relations / board secretary interactions.
            for code in self._get_top_candidate_codes(docs, limit=10):
                try:
                    interactions = _mapping_records(registry.get_investor_interactions(code=code, count=5))
                    for item in interactions:
                        question = _safe_text(item.get("question"))
                        answer = _safe_text(item.get("answer"))
                        title = question[:80] or f"{code} investor interaction"
                        if question or answer:
                            docs.append(self._make_document(
                                source_id="investor_interaction",
                                source_type="investor_interaction",
                                title=title,
                                summary=answer[:200],
                                content_text=f"{question}\n{answer}".strip(),
                                related_companies=[code],
                                related_themes=[],
                                published_at=_safe_text(item.get("date")),
                                collected_at=datetime.now().isoformat(),
                                source_payload=item,
                                url=_safe_text(item.get("url")),
                            ))
                except Exception:
                    pass

            # 热点题材（iFinD → AKShare）
            try:
                themes = registry.get_hot_themes()
                if isinstance(themes, dict) and themes and "error" not in themes:
                    for sector_list_key in ("industry", "concept"):
                        for sector in _mapping_records(themes.get(sector_list_key))[:10]:
                            name = _safe_text(sector.get("name") or sector.get("板块"))
                            if name:
                                docs.append(self._make_document(
                                    source_id="hot_themes", source_type="hot_theme",
                                    title=name, summary=f"{sector_list_key}板块",
                                    content_text=" ".join(_flatten_text(sector)),
                                    related_companies=[], related_themes=[name],
                                    published_at="", collected_at=datetime.now().isoformat(),
                                    source_payload=sector,
                                ))
            except Exception:
                pass

            logger.info("ProviderRegistry 增强数据源已接入")
        except Exception as e:
            logger.warning("ProviderRegistry 增强数据源接入失败: %s", e)

        return self._dedupe_documents(docs)

    def _extract_raw_terms(self, raw_documents: list[RawDocument]) -> List[Dict[str, Any]]:
        """从原始文档中抽取术语及证据片段.

        噪声抑制约束（Task 13.1）：
        - 搜索标题仅作为文档元数据（related_titles），不参与术语抽取与证据片段提取；
        - 证据片段（evidence_quotes）必须来源于文档正文（summary + content），
          不能来源于标题，避免标题被算作独立证据。

        Args:
            raw_documents: 原始文档列表。

        Returns:
            术语行列表，每行含 term / normalized_term / term_type / 证据片段等字段。
        """
        term_docs: dict[str, set[str]] = defaultdict(set)
        term_titles: dict[str, list[str]] = defaultdict(list)
        term_quotes: dict[str, list[str]] = defaultdict(list)
        term_companies: dict[str, list[str]] = defaultdict(list)
        term_co_terms: dict[str, Counter[str]] = defaultdict(Counter)
        term_first: dict[str, str] = {}
        term_last: dict[str, str] = {}

        for doc in raw_documents if isinstance(raw_documents, (list, tuple)) else []:
            if not isinstance(doc, RawDocument):
                continue
            # 标题仅作为元数据记录，不进入正文文本，避免标题被算作独立证据
            body_text = " ".join(
                part for part in (_safe_text(doc.summary), _safe_text(doc.content)) if part
            )
            terms = self._extract_terms_from_text(body_text)
            for term in terms:
                if not self._is_valid_term(term):
                    continue
                doc_id = str(doc.document_id)
                term_docs[term].add(doc_id)
                title = _safe_text(doc.title)
                if title and title not in term_titles[term]:
                    term_titles[term].append(title)
                # 证据片段必须来自正文，不能来自标题
                quote = _quote_around(body_text, term)
                if quote and quote not in term_quotes[term]:
                    term_quotes[term].append(quote)
                for company in _safe_text_list(doc.related_companies):
                    if company not in term_companies[term]:
                        term_companies[term].append(company)
                for co_term in terms:
                    if co_term != term and self._is_valid_term(co_term):
                        term_co_terms[term][co_term] += 1
                seen_at = _safe_text(doc.publish_date or doc.collected_at)
                if seen_at:
                    term_first[term] = min(term_first.get(term, seen_at), seen_at)
                    term_last[term] = max(term_last.get(term, seen_at), seen_at)

        rows: list[dict[str, Any]] = []
        for term, doc_ids in term_docs.items():
            source_count = len(doc_ids)
            recent_count = source_count
            co_terms = [item[0] for item in term_co_terms[term].most_common(8)]
            burst_score = round(math.log1p(source_count) * max(recent_count, 1), 4)
            rows.append(
                {
                    "term": term,
                    "normalized_term": self.taxonomy.normalize(term, " ".join(co_terms)),
                    "term_type": self._infer_term_type(term),
                    "source_count": source_count,
                    "recent_count": recent_count,
                    "burst_score": burst_score,
                    "related_document_ids": sorted(doc_ids),
                    "related_titles": term_titles[term][:8],
                    "related_companies": term_companies[term][:12],
                    "evidence_quotes": term_quotes[term][:5],
                    "co_terms": co_terms,
                    "first_seen_at": term_first.get(term, ""),
                    "last_seen_at": term_last.get(term, ""),
                    "status": "unclassified",
                }
            )
        rows.sort(key=lambda item: (item["source_count"], item["burst_score"]), reverse=True)
        return rows

    def _build_candidate_concepts(self, raw_terms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for term in _mapping_records(raw_terms):
            context = " ".join(
                _safe_text_list(term.get("co_terms"))
                + _safe_text_list(term.get("evidence_quotes"))
            )
            normalized = self.taxonomy.normalize(
                _safe_text(term.get("normalized_term") or term.get("term")), context
            )
            root = self.taxonomy.root_theme_for(normalized, context)
            if root not in ROOTS:
                continue
            source_count = _safe_nonnegative_int(term.get("source_count"), 0)
            score = round(min(1.0, 0.2 + source_count / 20.0 + _safe_float(term.get("burst_score")) / 50.0), 4)
            evidence_level = "EARLY_SIGNAL" if source_count >= 3 else "CONCEPT_CLAIM_ONLY" if source_count >= 2 else "HYPOTHESIS"
            rows.append(
                {
                    "concept_id": f"concept_{_short_hash(normalized)}",
                    "concept_name": normalized,
                    "normalized_name": normalized,
                    "term_type": _safe_text(term.get("term_type"), "unclassified"),
                    "root_theme": root,
                    "chain_layer": self._chain_layer_for(_safe_text(term.get("term_type"))),
                    "evidence_document_ids": _safe_text_list(term.get("related_document_ids")),
                    "evidence_titles": _safe_text_list(term.get("related_titles")),
                    "related_companies": _safe_text_list(term.get("related_companies")),
                    "evidence_quotes": _safe_text_list(term.get("evidence_quotes")),
                    "co_terms": _safe_text_list(term.get("co_terms")),
                    "concept_score": score,
                    "evidence_level": evidence_level,
                    "llm_explanation": self._evidence_bound_explanation(normalized, term),
                    "llm_policy": "evidence_bound_only; no unsupported facts",
                    "discovery_reason": self._candidate_reason(normalized, term, evidence_level),
                    "status": "candidate",
                }
            )
        rows.sort(key=lambda item: (item["concept_score"], len(item["evidence_document_ids"])), reverse=True)
        return rows

    def _build_evidence_edges(
        self,
        candidate_concepts: List[Dict[str, Any]],
        raw_terms: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        safe_terms = _mapping_records(raw_terms)
        safe_concepts = _mapping_records(candidate_concepts)
        term_map = {
            _safe_text(item.get("normalized_term")): item
            for item in safe_terms
            if _safe_text(item.get("normalized_term"))
        }
        edges: list[dict[str, Any]] = []
        seen: set[str] = set()
        for concept in safe_concepts:
            name = _safe_text(concept.get("normalized_name") or concept.get("concept_name"))
            if not name:
                continue
            term_type = _safe_text(concept.get("term_type"), "unclassified")
            root = _safe_text(concept.get("root_theme"))
            self._append_edge(edges, seen, name, term_type, "belongs_to", root, "root_theme", concept)
            raw = term_map.get(name) or next(
                (t for t in safe_terms if _safe_text(t.get("term")) == name), {}
            )
            for co_term in (_safe_text_list(raw.get("co_terms")) or _safe_text_list(concept.get("co_terms")))[:5]:
                self._append_edge(edges, seen, name, term_type, "co_occurs_with", co_term, "term", concept)
        return edges

    def _persist_research_control_tasks(
        self,
        search_tasks: List[Dict[str, Any]],
        *,
        task_runs: List[Dict[str, Any]] | None = None,
        limit: int | None = None,
    ) -> None:
        """只登记本轮预算内任务，并把执行结果回写统一控制面。"""
        import os
        if os.getenv("RESEARCH_CONTROL_PLANE_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
            return
        try:
            from core.research.control_plane.contracts import ResearchTask
            from core.research.control_plane.repository import ResearchRepository

            repository = ResearchRepository(os.getenv("RESEARCH_CONTROL_DB_PATH", "data/research_control/research_control.db"))
            safe_runs = _mapping_records(task_runs)
            safe_tasks = _mapping_records(search_tasks)
            run_by_id = {
                _safe_text(item.get("task_id")): item
                for item in safe_runs
                if _safe_text(item.get("task_id"))
                and not _safe_text(item.get("task_id")).endswith("_counter")
            }
            selected = [
                item for item in safe_tasks
                if not safe_runs or _safe_text(item.get("task_id")) in run_by_id
            ]
            if limit is not None:
                selected = selected[:_safe_nonnegative_int(limit)]
            for legacy in selected:
                task_id = _safe_text(legacy.get("task_id"))
                run = run_by_id.get(task_id)
                run_status = _safe_text((run or {}).get("status")).lower()
                documents_saved = _safe_nonnegative_int((run or {}).get("event_count"))
                if run is None:
                    status = "queued"
                elif run_status in {"completed", "succeeded", "success", "ok"} and documents_saved > 0:
                    status = "succeeded"
                elif run_status in {"failed", "error", "timeout"}:
                    status = "failed"
                else:
                    status = "partial"
                errors = _safe_text_list((run or {}).get("errors"))
                failure_reason = errors[0] if errors else (
                    "no_documents_returned" if run is not None and documents_saved == 0 else ""
                )
                task = ResearchTask(
                    task_id=str(legacy.get("task_id") or ""),
                    task_type="discovery_retrieval",
                    trigger_source="perilla_knowledge_loop",
                    target_claim_id=str(legacy.get("claim_id") or legacy.get("candidate_id") or ""),
                    priority=_safe_float(legacy.get("priority_score")),
                    status=status,
                    params={
                        **legacy,
                        "workflow": "perilla_discovery",
                        "retrieval_hypothesis": _safe_text(legacy.get("query")),
                        "evidence_kind": "retrieval_hypothesis",
                    },
                    progress={
                        "percent": 100 if run is not None else 0,
                        "message": "执行完成" if status == "succeeded" else (
                            "等待执行" if run is None else "执行未取得可落盘文档"
                        ),
                        "documents_saved": documents_saved,
                    },
                    failure={"reason": failure_reason} if failure_reason else {},
                    finished_at=_safe_text((run or {}).get("finished_at")) if run is not None else "",
                )
                repository.save_task(task)
        except Exception as exc:
            logger.warning("统一研究任务登记失败，继续兼容链路: %s", exc)

    def _build_search_tasks(
        self,
        candidate_concepts: List[Dict[str, Any]],
        evidence_edges: List[Dict[str, Any]],
        preferred_terms: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        seen: set[str] = set()
        preferred = {_safe_text(term) for term in (preferred_terms or []) if _safe_text(term)}
        safe_concepts = _mapping_records(candidate_concepts)
        ordered_concepts: list[dict[str, Any]] = []
        ordered_seen: set[str] = set()
        for concept in safe_concepts:
            name = _safe_text(concept.get("normalized_name") or concept.get("concept_name"))
            if name in preferred and name not in ordered_seen:
                ordered_concepts.append(concept)
                ordered_seen.add(name)
        for concept in safe_concepts:
            if len(ordered_concepts) >= 80:
                break
            name = _safe_text(concept.get("normalized_name") or concept.get("concept_name"))
            if not name:
                continue
            if name not in ordered_seen:
                ordered_concepts.append(concept)
                ordered_seen.add(name)

        for concept in ordered_concepts:
            name = concept["normalized_name"]
            priority = self._concept_task_priority(concept, preferred)
            queries = [
                f"{name} \u4ea7\u4e1a\u94fe",
                f"{name} \u6280\u672f \u8bc1\u636e",
                f"{name} A\u80a1 \u76f8\u5173\u516c\u53f8",
            ]
            for query in queries:
                self._append_task(
                    tasks,
                    seen,
                    query,
                    "raw_term",
                    name,
                    "",
                    priority,
                    _safe_text(concept.get("discovery_reason")),
                    capability="semantic_search",
                    keyword=name,
                )

            for company, symbol in _related_company_symbols(concept.get("related_companies", []))[:3]:
                company_label = company or symbol
                for capability, label in (
                    ("reports", "\u7814\u62a5"),
                    ("news", "\u65b0\u95fb"),
                    ("announcements", "\u516c\u544a"),
                ):
                    self._append_task(
                        tasks,
                        seen,
                        f"{name} {company_label} {label}",
                        "company_evidence",
                        name,
                        "",
                        min(1.0, priority + 0.01),
                        "\u6982\u5ff5\u4e0e\u516c\u53f8\u5171\u73b0\uff0c\u9700\u901a\u8fc7 AquaTrader \u516c\u53f8\u7ea7\u6570\u636e\u6e90\u8865\u8bc1\u3002",
                        capability=capability,
                        symbols=[symbol],
                        keyword=name,
                    )
        for edge in _mapping_records(evidence_edges):
            if _safe_nonnegative_int(edge.get("evidence_count")) >= 2:
                continue
            source_node = _safe_text(edge.get("source_node"))
            target_node = _safe_text(edge.get("target_node"))
            if not source_node or not target_node:
                continue
            query = f"{source_node} {target_node} \u8bc1\u636e"
            self._append_task(
                tasks,
                seen,
                query,
                "edge_evidence",
                source_node,
                _safe_text(edge.get("edge_id")),
                0.65,
                "\u8bc1\u636e\u8fb9\u4e0d\u8db3\uff0c\u9700\u8981 AquaTrader \u6570\u636e\u6e90\u8865\u8bc1\u3002",
            )
        tasks.sort(key=lambda item: item["priority_score"], reverse=True)
        return tasks

    def _execute_search_tasks(self, search_tasks: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], list[RawDocument]]:
        """委托唯一 PerillaOrchestrator；每层执行后统一评估再决定继续。"""
        safe_search_tasks = _mapping_records(search_tasks)
        if not safe_search_tasks:
            return [], []

        tiered_tasks = self._classify_tasks_by_tier(safe_search_tasks)
        all_terms = {
            _safe_text(t.get("related_term") or t.get("keyword")).strip()
            for t in safe_search_tasks
        }
        all_terms.discard("")

        from core.research.control_plane.repository import ResearchRepository
        from core.research.control_plane.tier_orchestrator import AdaptiveSearchBudgetPolicy, PerillaOrchestrator

        repository = ResearchRepository()
        orchestrator = PerillaOrchestrator(AdaptiveSearchBudgetPolicy(repository))

        def run_tier(_tier: str, batch: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
            # 每个支持查询同时执行一条反证查询，不能只搜支持材料。
            paired: list[dict[str, Any]] = []
            for task in _mapping_records(batch):
                paired.append(task)
                counter = dict(task)
                counter["task_id"] = f"{_safe_text(task.get('task_id'))}_counter"
                counter["query"] = f"{_safe_text(task.get('query'))} 否认 澄清 未供货 终止"
                counter["purpose"] = "counter"
                counter["query_purpose"] = "counter"
                counter["status"] = "pending"
                paired.append(counter)
            runs, documents = self._execute_search_tasks_batch(paired)
            counters = [item for item in paired if item.get("query_purpose") == "counter"]
            counter_succeeded = bool(counters) and all(
                str(item.get("status", "completed")).lower() not in {"failed", "error", "timeout"}
                for item in counters
            )
            return runs, documents, counter_succeeded

        execution = orchestrator.execute(
            tiered_tasks,
            run_tier,
            total_budget=max(1, self.max_execute_tasks // 2),
            required_targets=all_terms,
        )
        logger.info(
            "统一分层检索: tiers=%s decision=%s",
            execution.tiers_executed,
            execution.decision.reason if execution.decision else "无证据",
        )
        return execution.runs, execution.documents

    @staticmethod
    def _classify_tasks_by_tier(
        search_tasks: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """按 aquatrade_capability 把任务分组到质量阶梯。"""
        grouped: Dict[str, List[Dict[str, Any]]] = {tier: [] for tier in TIER_ORDER}
        for task in _mapping_records(search_tasks):
            capability = _safe_text(
                task.get("aquatrade_capability"), "semantic_search"
            )
            tier = CAPABILITY_TO_TIER.get(capability, "low")
            task["search_tier"] = tier
            grouped[tier].append(task)
        return grouped

    def _execute_search_tasks_batch(self, search_tasks: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], list[RawDocument]]:
        """执行一批搜索任务，保持 ProviderRegistry → ResearchEngine → Web 的降级链。

        三级降级：
        1. ProviderRegistry（问财语义搜索 / AKShare公告研报 / 巨潮公告等）
        2. ResearchEngine（内部路由）
        3. Bing/DuckDuckGo 网络搜索

        Args:
            search_tasks: 搜索任务列表

        Returns:
            tuple (runs, docs): 搜索执行记录列表和新文档列表
        """
        runs: list[dict[str, Any]] = []
        docs: list[RawDocument] = []

        # 第一优先级：ProviderRegistry 直接调用数据源
        registry = None
        try:
            from infrastructure.data_providers.provider_registry import ProviderRegistry
            registry = ProviderRegistry()
        except Exception as exc:
            logger.warning("ProviderRegistry 不可用: %s", exc)

        if registry is not None:
            for task in _mapping_records(search_tasks)[: self.max_execute_tasks]:
                started = datetime.now().isoformat()
                query = _safe_text(task.get("query"))
                capability = _safe_text(task.get("aquatrade_capability"), "semantic_search")
                keyword = _safe_text(task.get("keyword") or task.get("related_term"))
                task_docs: list[RawDocument] = []

                try:
                    # 根据能力路由到不同数据源方法
                    if capability == "semantic_search":
                        result = registry.semantic_search(query=query, count=10)
                        result = result if isinstance(result, dict) else {}
                        datas = _mapping_records(result.get("results"))
                        for item in datas:
                            stock_code = str(item.get("股票代码", "")).strip()
                            stock_name = str(item.get("股票简称", "")).strip()
                            title = f"{stock_name}({stock_code})" if stock_name else str(item)[:100]
                            task_docs.append(self._make_document(
                                source_id="wencai_semantic", source_type="semantic_search",
                                title=title, summary=str(item)[:200],
                                content_text=str(item),
                                related_companies=[stock_code] if stock_code else [],
                                related_themes=[keyword] if keyword else [],
                                published_at="", collected_at=datetime.now().isoformat(),
                                source_payload={"provider": result.get("source", "wencai"), "query": query},
                            ))

                    elif capability == "announcements":
                        symbols = _safe_text_list(task.get("symbols"))
                        for sym in symbols[:3]:
                            anns = _mapping_records(registry.get_announcements(code=sym, count=3))
                            if isinstance(anns, list):
                                for ann in anns:
                                    title = _safe_text(ann.get("title"))
                                    if title:
                                        task_docs.append(self._make_document(
                                            source_id="provider_announcement", source_type="announcement",
                                            title=title, summary=_safe_text(ann.get("digest")),
                                            content_text=title,
                                            related_companies=[sym], related_themes=[keyword] if keyword else [],
                                            published_at=_safe_text(ann.get("date")),
                                            collected_at=datetime.now().isoformat(),
                                            source_payload=ann if isinstance(ann, dict) else {},
                                        ))

                    elif capability == "reports":
                        symbols = _safe_text_list(task.get("symbols"))
                        for sym in symbols[:3]:
                            rpts = _mapping_records(registry.get_research_reports(code=sym, count=3))
                            if isinstance(rpts, list):
                                for rpt in rpts:
                                    title = _safe_text(rpt.get("title"))
                                    if title:
                                        task_docs.append(self._make_document(
                                            source_id="provider_report", source_type="research_report",
                                            title=title, summary=_safe_text(rpt.get("digest")),
                                            content_text=title,
                                            related_companies=[sym], related_themes=[keyword] if keyword else [],
                                            published_at=_safe_text(rpt.get("date")),
                                            collected_at=datetime.now().isoformat(),
                                            source_payload=rpt if isinstance(rpt, dict) else {},
                                        ))

                    elif capability == "periodic_reports":
                        symbols = _safe_text_list(task.get("symbols"))
                        for sym in symbols[:3]:
                            reports = _mapping_records(registry.get_periodic_reports(code=sym, count=3))
                            if isinstance(reports, list):
                                for rpt in reports:
                                    title = _safe_text(rpt.get("title"))
                                    if title:
                                        source_type = _safe_text(rpt.get("report_type"), "periodic_report") or "periodic_report"
                                        task_docs.append(self._make_document(
                                            source_id="provider_periodic_report", source_type=source_type,
                                            title=title, summary=_safe_text(rpt.get("digest")),
                                            content_text=title,
                                            related_companies=[sym], related_themes=[keyword] if keyword else [],
                                            published_at=_safe_text(rpt.get("date")),
                                            collected_at=datetime.now().isoformat(),
                                            source_payload=rpt if isinstance(rpt, dict) else {},
                                            url=_safe_text(rpt.get("pdf_url") or rpt.get("link")),
                                        ))

                    elif capability == "investor_interactions":
                        symbols = _safe_text_list(task.get("symbols"))
                        for sym in symbols[:3]:
                            interactions = _mapping_records(registry.get_investor_interactions(code=sym, count=5))
                            if isinstance(interactions, list):
                                for item in interactions:
                                    question = _safe_text(item.get("question"))
                                    answer = _safe_text(item.get("answer"))
                                    title = question[:80] or f"{sym} investor interaction"
                                    if question or answer:
                                        task_docs.append(self._make_document(
                                            source_id="provider_investor_interaction",
                                            source_type="investor_interaction",
                                            title=title, summary=answer[:200],
                                            content_text=f"{question}\n{answer}".strip(),
                                            related_companies=[sym], related_themes=[keyword] if keyword else [],
                                            published_at=_safe_text(item.get("date")),
                                            collected_at=datetime.now().isoformat(),
                                            source_payload=item if isinstance(item, dict) else {},
                                            url=_safe_text(item.get("url")),
                                        ))

                    elif capability == "news":
                        symbols = _safe_text_list(task.get("symbols"))
                        for sym in symbols[:3]:
                            try:
                                news = _mapping_records(registry.get_news(code=sym, count=3))
                                if news:
                                    for n in news:
                                        title = _safe_text(n.get("title"))
                                        if title:
                                            task_docs.append(self._make_document(
                                                source_id="provider_news", source_type="news",
                                                title=title, summary=_safe_text(n.get("digest")),
                                                content_text=title,
                                                related_companies=[sym], related_themes=[keyword] if keyword else [],
                                                published_at=_safe_text(n.get("date")),
                                                collected_at=datetime.now().isoformat(),
                                                source_payload=n if isinstance(n, dict) else {},
                                            ))
                            except Exception:
                                pass

                except Exception as exc:
                    logger.warning("ProviderRegistry 执行任务失败: %s", exc)

                if task_docs:
                    docs.extend(task_docs)
                    task["status"] = "completed"
                    task["last_run_at"] = started
                    runs.append({
                        "task_id": task.get("task_id", ""),
                        "query": query,
                        "status": "completed",
                        "event_count": len(task_docs),
                        "selected_provider": "ProviderRegistry",
                        "source_chain": [capability],
                        "errors": [],
                        "started_at": started,
                        "finished_at": datetime.now().isoformat(),
                    })
                    continue

                # ProviderRegistry 无结果，降级到 ResearchEngine / Web
                task["status"] = "pending_fallback"

        # 第二优先级：ResearchEngine
        engine = None
        try:
            from core.research.engine.research_engine import ResearchEngine
            engine = ResearchEngine()
        except Exception as exc:
            logger.warning("ResearchEngine 不可用，降级到网络搜索: %s", exc)

        # 收集需要降级的任务
        fallback_tasks = [
            t for t in _mapping_records(search_tasks)
            if _safe_text(t.get("status")) in ("pending", "pending_fallback")
        ]
        if not fallback_tasks:
            return runs, docs

        # 降级路径：ResearchEngine 不可用时使用网络搜索
        if engine is None:
            web_runs, web_docs = self._execute_search_tasks_via_web(fallback_tasks)
            runs.extend(web_runs)
            docs.extend(web_docs)
            return runs, docs

        for task in fallback_tasks[: self.max_execute_tasks]:
            started = datetime.now().isoformat()
            try:
                result = engine.query_source(
                    capability=_safe_text(task.get("aquatrade_capability"), "semantic_search"),
                    symbols=_safe_text_list(task.get("symbols")) or None,
                    query=_safe_text(task.get("query")),
                    keyword=_safe_text(task.get("keyword") or task.get("related_term")) or None,
                    limit=5,
                )
                result = result if isinstance(result, dict) else {}
                events = _mapping_records(result.get("events"))
                status = _safe_text(result.get("status"), "unknown") or "unknown"
                for event in events:
                    docs.append(self._document_from_event(event, task))
                task["status"] = "completed" if events else status
                task["last_run_at"] = started
                runs.append(
                    {
                        "task_id": task.get("task_id", ""),
                        "query": task.get("query", ""),
                        "status": status,
                        "event_count": len(events),
                        "selected_provider": _safe_text(result.get("selected_provider")),
                        "source_chain": _safe_text_list(result.get("source_chain")),
                        "errors": _safe_text_list(result.get("errors")),
                        "started_at": started,
                        "finished_at": datetime.now().isoformat(),
                    }
                )
            except Exception as exc:
                task["status"] = "failed"
                task["last_run_at"] = started
                runs.append(
                    {
                        "task_id": task.get("task_id", ""),
                        "query": task.get("query", ""),
                        "status": "failed",
                        "event_count": 0,
                        "errors": [str(exc)],
                        "started_at": started,
                        "finished_at": datetime.now().isoformat(),
                    }
                )
        return runs, docs

    def _execute_search_tasks_via_web(self, search_tasks: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], list[RawDocument]]:
        """通过 Bing/DuckDuckGo 网络搜索执行搜索任务（降级方案）。

        当 ResearchEngine 不可用时使用此方法，通过 PerillaLeafScreener 的
        _web_search() 方法执行网络搜索，将结果转换为 raw_documents。

        Args:
            search_tasks: 搜索任务列表

        Returns:
            tuple (runs, docs): 搜索执行记录列表和新文档列表
        """
        runs: list[dict[str, Any]] = []
        docs: list[RawDocument] = []

        try:
            from .perilla_leaf_screener import PerillaLeafScreener
            screener = PerillaLeafScreener()
        except Exception as exc:
            logger.error("PerillaLeafScreener 初始化失败: %s", exc)
            return ([{"status": "failed", "error": f"Web search init failed: {exc}", "created_at": datetime.now().isoformat()}], [])

        for task in _mapping_records(search_tasks)[: self.max_execute_tasks]:
            started = datetime.now().isoformat()
            query = _safe_text(task.get("query"))
            try:
                result_text = screener._web_search(query)
                event_count = 0
                if result_text and result_text.strip():
                    # 将搜索结果转换为一个文档
                    doc = self._make_document(
                        source_id="web_search_bing_ddg",
                        source_type="search_result",
                        title=query,
                        summary=result_text[:200],
                        content_text=result_text,
                        related_companies=[],
                        related_themes=[],
                        published_at="",
                        collected_at=datetime.now().isoformat(),
                        source_payload={"task": task, "search_method": screener._search_method},
                        url="",
                    )
                    if task.get("related_term"):
                        doc.metadata["query_context_terms"] = [str(task.get("related_term"))]
                    docs.append(doc)
                    event_count = 1
                    task["status"] = "completed"
                else:
                    task["status"] = "no_results"

                task["last_run_at"] = started
                runs.append(
                    {
                        "task_id": task.get("task_id", ""),
                        "query": query,
                        "status": task["status"],
                        "event_count": event_count,
                        "selected_provider": screener._search_method,
                        "source_chain": ["bing", "duckduckgo"],
                        "errors": [],
                        "started_at": started,
                        "finished_at": datetime.now().isoformat(),
                    }
                )
            except Exception as exc:
                task["status"] = "failed"
                task["last_run_at"] = started
                runs.append(
                    {
                        "task_id": task.get("task_id", ""),
                        "query": query,
                        "status": "failed",
                        "event_count": 0,
                        "errors": [str(exc)],
                        "started_at": started,
                        "finished_at": datetime.now().isoformat(),
                    }
                )
        return runs, docs

    def _select_cold_new_term(self, candidate_concepts: List[Dict[str, Any]]) -> Dict[str, Any]:
        existing_names = set()
        for signal in _mapping_records(self.loader.load_concept_signals()):
            name = _safe_text(signal.get("name"))
            if name:
                existing_names.add(name)
            existing_names.update(_safe_text_list(signal.get("aliases")))

        # 构建已知概念子串集合：seed 词、已录入概念、别名、父概念、通用排除词
        known_substrings: set[str] = set()
        known_substrings.update(self.taxonomy.seed_terms())
        known_substrings.update(self.taxonomy.candidate_terms())
        known_substrings.update(_GENERIC_COLD_EXCLUDE)
        known_substrings.update(_OLD_SEED_FRAGMENTS)

        eligible: list[dict[str, Any]] = []
        safe_concepts = _mapping_records(candidate_concepts)
        for concept in safe_concepts:
            name = _safe_text(concept.get("normalized_name") or concept.get("concept_name"))
            if not name:
                continue
            if name in existing_names:
                continue
            if self.taxonomy.is_known_term(name):
                continue
            if _safe_text(concept.get("root_theme")) not in ROOTS:
                continue
            if len(name) < 2 or any(fragment in name for fragment in _BAD_TERM_FRAGMENTS):
                continue
            if name in _GENERIC_COLD_EXCLUDE:
                continue
            if any(fragment in name for fragment in _OLD_SEED_FRAGMENTS):
                continue
            # 子串匹配：名字包含任何已知概念片段（如 "磷化铟衬底" 包含 "磷化铟"）则排除
            if any(known in name for known in known_substrings if known and len(known) >= 2):
                continue
            # 排除被识别为公司名的伪概念（如 "四川高纯材料" -> 四川高纯材料科技有限公司）
            related_companies = concept.get("related_companies") or []
            if any(
                name in company or company in name
                for company in related_companies
                if isinstance(company, str) and company.strip()
            ):
                continue
            if _safe_text(concept.get("term_type")) in {"material", "component", "equipment", "technology_route"}:
                eligible.append(concept)
        if eligible:
            priority = {"material": 4, "component": 3, "equipment": 2, "technology_route": 1}
            eligible.sort(
                key=lambda item: (
                    priority.get(item.get("term_type", ""), 0),
                    _safe_float(item.get("novelty_score")),
                    len(_safe_text_list(item.get("evidence_document_ids"))),
                    _safe_float(item.get("concept_score")),
                ),
                reverse=True,
            )
            return eligible[0]
        return safe_concepts[0] if safe_concepts else {}

    def _enrich_candidate_concepts_with_llm(
        self,
        candidate_concepts: List[Dict[str, Any]],
        raw_terms: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], list[RawDocument]]:
        safe_concepts = _mapping_records(candidate_concepts)
        safe_terms = _mapping_records(raw_terms)
        if not safe_concepts or self.max_llm_terms <= 0:
            return safe_concepts, []

        term_map: dict[str, dict[str, Any]] = {}
        for term in safe_terms:
            for key in (term.get("term"), term.get("normalized_term")):
                safe_key = _safe_text(key)
                if safe_key:
                    term_map[safe_key] = term

        selected = self._select_cold_new_term(candidate_concepts)
        priority_names: list[str] = []
        if selected:
            priority_names.append(str(selected.get("normalized_name") or ""))
        for concept in safe_concepts:
            name = _safe_text(concept.get("normalized_name"))
            if name and name not in priority_names:
                priority_names.append(name)
            if len(priority_names) >= self.max_llm_terms:
                break

        explanations: list[dict[str, Any]] = []
        concept_by_name = {
            _safe_text(item.get("normalized_name")): item
            for item in safe_concepts
            if _safe_text(item.get("normalized_name"))
        }
        if not self.llm_explainer or not self.llm_explainer.available:
            for name in priority_names:
                concept = concept_by_name.get(name)
                if concept is not None:
                    concept["llm_status"] = "fallback_rule_based"
                    concept["llm_error"] = "AquaTrader LLM endpoint is not configured or unavailable."
            return candidate_concepts, explanations

        for name in priority_names:
            concept = concept_by_name.get(name)
            raw_term = term_map.get(name)
            if not concept or not raw_term:
                continue
            try:
                explanation = self.llm_explainer.explain(raw_term)
            except Exception as exc:
                logger.warning("LLM 概念解释失败: %s", exc)
                explanation = None
            if not explanation:
                concept["llm_status"] = "fallback_rule_based"
                concept["llm_error"] = "LLM explanation failed or was not grounded in source quotes."
                continue

            concept["llm_explanation"] = explanation.explanation
            concept["llm_policy"] = "AquaTrader LLM evidence-bound; source quotes only"
            concept["llm_status"] = explanation.status
            concept["llm_model"] = explanation.model_name
            concept["llm_confidence"] = explanation.confidence
            concept["llm_grounded_quote"] = explanation.evidence_quote
            if explanation.chain_layer:
                concept["llm_chain_layer"] = explanation.chain_layer
            # Task 13.2: 规则与 LLM 类型冲突时标记 CONFLICT，不保留其一
            rule_type = str(concept.get("term_type") or "unclassified")
            llm_type = str(explanation.term_type or "unclassified")
            if rule_type == "unclassified" and llm_type != "unclassified":
                # 规则未识别，LLM 有判断：采纳 LLM 结果
                concept["term_type"] = llm_type
                concept["chain_layer"] = self._chain_layer_for(llm_type)
                concept.pop("type_conflict", None)
            elif (
                rule_type != "unclassified"
                and llm_type != "unclassified"
                and rule_type != llm_type
            ):
                # 规则与 LLM 均有判断但不一致：标记冲突，等待人工裁决
                concept["term_type"] = "CONFLICT"
                concept["chain_layer"] = "CONFLICT"
                concept["type_conflict"] = {
                    "rule_type": rule_type,
                    "llm_type": llm_type,
                }
                concept["needs_human_review"] = True

            explanations.append(
                {
                    "concept_name": name,
                    "status": explanation.status,
                    "term_type": explanation.term_type,
                    "root_theme": explanation.root_theme,
                    "chain_layer": explanation.chain_layer,
                    "explanation": explanation.explanation,
                    "evidence_quote": explanation.evidence_quote,
                    "confidence": explanation.confidence,
                    "model_name": explanation.model_name,
                }
            )
        return candidate_concepts, explanations

    def _concept_task_priority(self, concept: Dict[str, Any], preferred_terms: set[str]) -> float:
        if not isinstance(concept, dict):
            return 0.0
        name = _safe_text(concept.get("normalized_name"))
        if name in preferred_terms:
            return 1.0

        base = min(max(_safe_float(concept.get("concept_score")), 0.0), 1.0)
        term_type = _safe_text(concept.get("term_type"))
        is_known = self.taxonomy.is_known_term(name) or any(fragment in name for fragment in _OLD_SEED_FRAGMENTS)
        if is_known:
            return min(0.78, 0.45 + base * 0.28)
        if term_type in {"material", "component", "equipment", "technology_route"}:
            return min(0.95, 0.78 + base * 0.15)
        return min(0.86, 0.58 + base * 0.18)

    @staticmethod
    def _safe_write_text(path: Path, content: str) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return True
        except OSError as e:
            logger.exception("Failed to write research output %s: %s", path, e)
            return False

    @classmethod
    def _safe_write_json(cls, path: Path, payload: Any) -> bool:
        try:
            content = json.dumps(payload, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as e:
            logger.exception("Failed to serialize research output %s: %s", path, e)
            return False
        return cls._safe_write_text(path, content)

    def _write_outputs(
        self,
        sag_stats: Dict[str, Any],
        promotion_candidates: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Dict[str, str]:
        mapping = {
            "raw_sources": "raw_sources_latest.json",
            "raw_documents": "raw_documents_latest.json",
            "raw_terms": "raw_terms_latest.json",
            "candidate_concepts": "candidate_concepts_latest.json",
            "llm_explanations": "llm_concept_explanations_latest.json",
            "evidence_edges": "evidence_edges_latest.json",
            "search_tasks": "search_tasks_latest.json",
            "task_runs": "search_task_runs_latest.json",
        }
        outputs: dict[str, str] = {}
        for key, filename in mapping.items():
            path = self.output_dir / filename
            value = kwargs.get(key, [])
            # raw_documents 现为 RawDocument dataclass 列表，序列化前转为 dict
            if key == "raw_documents" and value:
                value = [asdict(item) for item in value if isinstance(item, RawDocument)]
            if self._safe_write_json(path, value):
                outputs[key] = str(path)

        # 写出 SAG 集成统计与晋升候选，供前端展示知识链最终结果
        sag_stats_path = self.output_dir / "sag_stats_latest.json"
        if self._safe_write_json(sag_stats_path, sag_stats):
            outputs["sag_stats"] = str(sag_stats_path)

        promotion_path = self.output_dir / "promotion_candidates_latest.json"
        if self._safe_write_json(promotion_path, promotion_candidates):
            outputs["promotion_candidates"] = str(promotion_path)

        # 写出真正的冷门新词，避免前端误用晋升候选第一个
        selected_path = self.output_dir / "selected_new_term_latest.json"
        if self._safe_write_json(selected_path, kwargs.get("selected_new_term", {})):
            outputs["selected_new_term"] = str(selected_path)

        summary_path = self.output_dir / "perilla_new_terms_today.md"
        if self._safe_write_text(summary_path, str(kwargs.get("summary", ""))):
            outputs["summary"] = str(summary_path)
        return outputs

    def _make_document(
        self,
        source_id: str,
        source_type: str,
        title: str,
        summary: str,
        content_text: str,
        related_companies: List[str],
        related_themes: List[str],
        published_at: str,
        collected_at: str,
        source_payload: Dict[str, Any],
        url: str = "",
    ) -> RawDocument:
        raw = f"{source_id}|{title}|{summary}|{content_text[:300]}"
        return RawDocument(
            document_id=f"doc_{_short_hash(raw)}",
            source_id=source_id,
            source_type=source_type,
            title=title,
            summary=summary,
            content=content_text,
            url=url,
            publish_date=published_at,
            collected_at=collected_at or datetime.now().isoformat(),
            related_companies=[item for item in related_companies if item],
            related_themes=[item for item in related_themes if item],
            raw_hash=_short_hash(raw),
            source_payload=source_payload,
        )

    def _document_from_event(self, event: Dict[str, Any], task: Dict[str, Any]) -> RawDocument:
        doc = self._make_document(
            source_id="aquatrade_research_engine",
            source_type=str(event.get("event_type") or task.get("aquatrade_capability") or "search_result"),
            title=str(event.get("title") or task.get("query") or ""),
            summary=str(event.get("summary") or ""),
            content_text=" ".join(_flatten_text(event)),
            related_companies=[str(event.get("stock_name") or event.get("symbol") or "")],
            related_themes=[],
            published_at=str(event.get("event_time") or event.get("trade_date") or ""),
            collected_at=str(event.get("fetched_at") or datetime.now().isoformat()),
            source_payload={"task": task, "event": event},
            url=str(event.get("source_url") or ""),
        )
        if task.get("related_term"):
            doc.metadata["query_context_terms"] = [str(task.get("related_term"))]
        return doc

    def _extract_terms_from_text(self, text: str) -> List[str]:
        terms = set()
        upper = text.upper()
        for seed in self.taxonomy.candidate_terms():
            if seed in text or seed.upper() in upper:
                terms.add(self.taxonomy.normalize(seed, text))
        terms.update(match.group(0) for match in _ABBR_RE.finditer(upper))
        terms.update(match.group(0) for match in _TECH_TERM_RE.finditer(text))
        return sorted(terms)

    @staticmethod
    def _is_valid_term(term: str) -> bool:
        term = str(term or "").strip()
        if len(term) < 2 or len(term) > 24:
            return False
        if any(fragment in term for fragment in _BAD_TERM_FRAGMENTS):
            return False
        if term.isdigit():
            return False
        upper = term.upper()
        if upper in _STOP_TERMS:
            return False
        if re.fullmatch(r"[A-Z]{2,12}", term) and upper not in _ALLOWED_ABBR:
            return False
        return True

    @staticmethod
    def _infer_term_type(term: str) -> str:
        # 产品平台优先识别（GB200/H100/H200/B200/NVL72/AI服务器 等）
        _PRODUCT_PLATFORM_ABBR = {"GB200", "H100", "H200", "B200", "NVL72"}
        if term.upper() in _PRODUCT_PLATFORM_ABBR:
            return "product_platform"
        if any(x in term for x in ("AI\u670d\u52a1\u5668", "AI\u82af\u7247")):
            return "product_platform"
        # 材料优先识别（PTFE/磷化铟/铜箔/树脂/衬底 等，需在 technology_route 之前）
        if any(x in term for x in ("\u6750\u6599", "\u6811\u8102", "\u94dc\u7b94", "\u886c\u5e95", "PTFE", "\u78f7\u5316\u94df")):
            return "material"
        if re.fullmatch(r"[A-Z0-9.]{2,10}", term):
            return "technology_route"
        if any(x in term for x in ("\u8bbe\u5907", "\u8d34\u7247\u673a")):
            return "equipment"
        if any(x in term for x in ("\u6a21\u5757", "\u82af\u7247", "\u63a2\u6d4b\u5668", "\u8fde\u63a5\u5668")):
            return "component"
        return "unclassified"

    @staticmethod
    def _chain_layer_for(term_type: str) -> str:
        return {
            "material": "\u6750\u6599/\u57fa\u6750",
            "equipment": "\u8bbe\u5907",
            "component": "\u6838\u5fc3\u5668\u4ef6",
            "technology_route": "\u6280\u672f\u8def\u7ebf",
            "product_platform": "\u4ea7\u54c1\u5e73\u53f0",
        }.get(term_type, "\u672a\u5206\u7c7b")

    @staticmethod
    def _evidence_bound_explanation(name: str, term: Dict[str, Any]) -> str:
        quote = (_safe_text_list(term.get("evidence_quotes")) or [""])[0]
        if not quote:
            return f"{name}: \u539f\u6587\u672a\u8bf4\u660e\uff0c\u4ec5\u4f5c\u5f85\u9a8c\u8bc1\u672f\u8bed\u3002"
        co_terms = _safe_text_list(term.get("co_terms"))[:3]
        return f"{name}: \u4ec5\u57fa\u4e8e\u539f\u6587\u7247\u6bb5\u5224\u65ad\uff0c\u5176\u4e0e {', '.join(co_terms)} \u5171\u73b0\u3002\u8bc1\u636e\uff1a{quote[:120]}"

    @staticmethod
    def _candidate_reason(name: str, term: Dict[str, Any], level: str) -> str:
        co_terms = "\u3001".join(_safe_text_list(term.get("co_terms"))[:5]) or "\u6682\u65e0"
        return (
            f"{name} \u5728 {_safe_nonnegative_int(term.get('source_count'), 0)} \u7bc7 raw_documents \u4e2d\u51fa\u73b0\uff0c"
            f"\u4e0e {co_terms} \u5171\u73b0\uff1b\u8bc1\u636e\u7b49\u7ea7 {level}\u3002"
        )

    @staticmethod
    def _append_edge(
        edges: List[Dict[str, Any]],
        seen: set[str],
        source_node: str,
        source_type: str,
        relation: str,
        target_node: str,
        target_type: str,
        concept: Dict[str, Any],
    ) -> None:
        key = f"{source_node}|{relation}|{target_node}"
        if key in seen or not target_node:
            return
        seen.add(key)
        evidence_ids = _safe_text_list(concept.get("evidence_document_ids"))[:8]
        evidence_quotes = _safe_text_list(concept.get("evidence_quotes"))[:3]
        edge_level = "weak_signal" if len(evidence_ids) >= 2 else "hypothesis"
        edges.append(
            {
                "edge_id": f"edge_{_short_hash(key)}",
                "source_node": source_node,
                "source_type": source_type,
                "relation": relation,
                "target_node": target_node,
                "target_type": target_type,
                "evidence_document_ids": evidence_ids,
                "evidence_quotes": evidence_quotes,
                "evidence_count": len(evidence_ids),
                "confidence_level": edge_level,
                "status": "pending" if edge_level == "hypothesis" else "weak_signal",
                "created_at": datetime.now().isoformat(),
                "last_verified_at": "",
            }
        )

    @staticmethod
    def _append_task(
        tasks: List[Dict[str, Any]],
        seen: set[str],
        query: str,
        query_type: str,
        related_term: str,
        related_edge_id: str,
        priority_score: float,
        reason: str,
        capability: str = "semantic_search",
        symbols: Optional[List[str]] = None,
        keyword: Optional[str] = None,
    ) -> None:
        query = " ".join(_safe_text(query).split())
        if not query or query in seen:
            return
        seen.add(query)
        clean_symbols = [symbol.upper() for symbol in _safe_text_list(symbols)]
        safe_priority = min(max(_safe_float(priority_score), 0.0), 1.0)
        safe_capability = _safe_text(capability, "semantic_search") or "semantic_search"
        safe_keyword = _safe_text(keyword or related_term)
        tasks.append(
            {
                "task_id": f"task_{_short_hash(query)}",
                "query": query,
                "query_type": query_type,
                "related_term": _safe_text(related_term),
                "related_edge_id": _safe_text(related_edge_id),
                "priority_score": round(safe_priority, 4),
                "reason": _safe_text(reason),
                "symbols": clean_symbols,
                "keyword": safe_keyword,
                "status": "pending",
                "created_at": datetime.now().isoformat(),
                "last_run_at": "",
                "execution_backend": "AquaTradeResearchEngine",
                "aquatrade_capability": safe_capability,
                "aquatrade_cli": _task_cli(safe_capability, query, clean_symbols, safe_keyword),
                "provider_policy": "AQUATRADE_ONLY",
                "direct_web_allowed": False,
            }
        )

    @staticmethod
    def _get_top_candidate_codes(docs: list[RawDocument], limit: int = 20) -> list[str]:
        """从已有文档中提取出现频率最高的股票代码，用于公告/研报查询。"""
        code_counter: Counter = Counter()
        for doc in docs if isinstance(docs, (list, tuple)) else []:
            if not isinstance(doc, RawDocument):
                continue
            for code in doc.related_companies or []:
                code = str(code).strip()
                if re.match(r"^\d{6}", code):
                    code_counter[code[:6]] += 1
        return [code for code, _ in code_counter.most_common(limit)]

    @staticmethod
    def _dedupe_documents(docs: list[RawDocument]) -> list[RawDocument]:
        seen: set[str] = set()
        out: list[RawDocument] = []
        for doc in docs if isinstance(docs, (list, tuple)) else []:
            if not isinstance(doc, RawDocument):
                continue
            key = str(doc.raw_hash or doc.document_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(doc)
        return out

    @staticmethod
    def _format_company(item: Dict[str, Any]) -> str:
        if not isinstance(item, dict):
            return ""
        company = _safe_text(item.get("company_name")).strip()
        symbol = _safe_text(item.get("symbol")).strip()
        if company and symbol and symbol not in company and symbol != "\u672a\u4e0a\u5e02":
            return f"{company}({symbol})"
        return company

    # ------------------------------------------------------------------
    # 下钻触发模块
    # ------------------------------------------------------------------

    def _execute_drill_down(
        self,
        candidate_concepts: List[Dict[str, Any]],
        evidence_edges: List[Dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """对候选概念中类型为 component/material 且非原子的概念触发递归下钻。

        从候选概念中选取最值得下钻的概念（出现频次高、类型为部件/材料），
        调用 DrillDownEngine 递归拆解，返回树形结构和图谱数据。

        Args:
            candidate_concepts: 候选概念列表
            evidence_edges: 证据边列表

        Returns:
            (tree, graph) 下钻树和图谱，如果无合适概念则返回 (None, None)
        """
        try:
            from .drill_down_engine import DrillDownEngine
        except ImportError:
            logger.warning("DrillDownEngine 不可用，跳过下钻")
            return None, None

        # 筛选可下钻的概念：类型为 component/material 且非原子
        drillable_types = {"component", "material", "device", "sub_component"}
        drill_candidates: list[dict[str, Any]] = []
        for concept in _mapping_records(candidate_concepts):
            term_type = _safe_text(concept.get("term_type"))
            if term_type in drillable_types:
                name = _safe_text(concept.get("normalized_name"))
                if name and len(name) >= 2:
                    drill_candidates.append(concept)

        if not drill_candidates:
            logger.info("无可下钻的候选概念")
            return None, None

        # 按频次排序，取 top-3 下钻
        drill_candidates.sort(key=lambda c: _safe_float(c.get("frequency")), reverse=True)
        top_concepts = drill_candidates[:3]

        logger.info("下钻触发: %d 个候选概念，选取 top-%d",
                     len(drill_candidates), len(top_concepts))

        engine = DrillDownEngine(
            max_depth=self.drill_down_depth,
            max_breadth=5,
        )

        # 对每个 top 概念执行下钻，合并结果
        all_trees: list[dict[str, Any]] = []
        for concept in top_concepts:
            name = _safe_text(concept.get("normalized_name"))
            if not name:
                continue
            logger.info("下钻: %s (depth=%d)", name, self.drill_down_depth)
            try:
                tree = engine.drill(name, max_depth=self.drill_down_depth)
            except Exception as exc:
                logger.warning("下钻失败 %s: %s", name, exc)
                continue
            if isinstance(tree, dict):
                all_trees.append(tree)

        if not all_trees:
            return None, None

        # 返回第一个树（主树）和合并后的图谱
        graph = engine.get_graph()
        logger.info("下钻完成: %d nodes, %d edges", len(engine.nodes), len(engine.edges))

        # 保存下钻图谱
        try:
            graph_path = self.output_dir / "drill_down_graph.json"
            graph_path.write_text(
                json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("保存下钻图谱失败: %s", e)

        return all_trees[0], graph

    @staticmethod
    def _render_summary(
        selected: Dict[str, Any],
        raw_terms: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
    ) -> str:
        selected = selected if isinstance(selected, dict) else {}
        safe_raw_terms = _mapping_records(raw_terms)
        safe_candidates = _mapping_records(candidates)
        safe_edges = _mapping_records(edges)
        safe_tasks = _mapping_records(tasks)
        name = _safe_text(selected.get("normalized_name"), "\u6682\u65e0") or "\u6682\u65e0"
        root = _safe_text(selected.get("root_theme"))
        level = _safe_text(selected.get("evidence_level"))
        reason = _safe_text(selected.get("discovery_reason"))
        _uncategorized = "\u672a\u5206\u7c7b"
        _llm_fallback = "\u89c4\u5219\u964d\u7ea7\uff0c\u672a\u83b7\u5f97\u53ef\u951a\u5b9a LLM \u89e3\u91ca"
        selected_edges = [edge for edge in safe_edges if _safe_text(edge.get("source_node")) == name]
        display_edges = selected_edges[:5] if selected_edges else safe_edges[:5]
        lines = [
            "# AquaTrader \u4eca\u65e5\u65b0\u8bcd\u95ed\u73af",
            "",
            "## \u4eca\u65e5\u65b0\u8bcd\u96f7\u8fbe",
            f"- {name}\uff5c{selected.get('chain_layer') or selected.get('term_type') or _uncategorized}\uff5c{root}\uff5c\u6587\u6863\u6570 {len(selected.get('evidence_document_ids') or [])}\uff5c\u8bc1\u636e {level}",
            f"- \u5171\u73b0: {_join_short(selected.get('co_terms', []), 6)}",
            f"- \u53d1\u73b0\u539f\u56e0: {reason}",
            f"- LLM\u89e3\u91ca: {selected.get('llm_explanation') or _llm_fallback}",
            "",
            "## \u4eca\u65e5\u5019\u9009\u6982\u5ff5",
        ]
        for concept in _summary_candidates(safe_candidates, name, 5):
            lines.append(
                f"- {concept.get('normalized_name')}\uff5c{concept.get('term_type')}\uff5c{concept.get('root_theme')}\uff5c"
                f"\u8bc1\u636e {concept.get('evidence_level')}\uff5c\u6587\u6863 {len(concept.get('evidence_document_ids') or [])}"
            )
        lines.extend(
            [
                "",
                "## \u4eca\u65e5\u65b0\u589e\u8bc1\u636e\u8fb9",
            ]
        )
        for edge in display_edges:
            lines.append(
                f"- {edge.get('source_node')} -> {edge.get('relation')} -> {edge.get('target_node')}"
                f"\uff5c{edge.get('confidence_level')}\uff5c\u8bc1\u636e {edge.get('evidence_count')}"
            )
        lines.extend(
            [
                "",
                "## \u5f85\u9a8c\u8bc1\u4efb\u52a1",
            ]
        )
        for task in safe_tasks[:6]:
            lines.append(
                f"- {task.get('query')}\uff5c{task.get('aquatrade_capability')}\uff5c\u4f18\u5148\u7ea7 {task.get('priority_score')}"
            )
        lines.extend(
            [
                "",
                "## \u8fd0\u884c\u7edf\u8ba1",
                f"- raw_terms: {len(safe_raw_terms)}",
                f"- candidate_concepts: {len(safe_candidates)}",
                f"- evidence_edges: {len(safe_edges)}",
                f"- search_tasks: {len(safe_tasks)}",
            ]
        )
        lines.extend(
            [
            "",
            "\u8bf4\u660e: \u672c\u95ed\u73af\u53ea\u57fa\u4e8e AquaTrader \u5185\u90e8 raw_documents \u548c ResearchEngine \u6570\u636e\u6e90\uff0c\u4e0d\u6784\u6210\u6295\u8d44\u5efa\u8bae\u3002",
            ]
        )
        return "\n".join(lines)


def _flatten_text(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, dict):
        parts: list[str] = []
        for child in value.values():
            parts.extend(_flatten_text(child))
        return parts
    if isinstance(value, list):
        parts: list[str] = []
        for child in value:
            parts.extend(_flatten_text(child))
        return parts
    return [str(value)]


def _quote_around(text: str, term: str, window: int = 80) -> str:
    idx = text.find(term)
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + len(term) + window)
    return text[start:end].strip()


def _join_short(items: Iterable[Any], limit: int = 5) -> str:
    values = [_safe_text(item) for item in items if _safe_text(item).strip()] if isinstance(items, (list, tuple, set)) else []
    return "\u3001".join(values[:limit]) if values else "\u6682\u65e0"


def _summary_candidates(
    candidates: List[Dict[str, Any]],
    selected_name: str,
    limit: int,
) -> List[Dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    for concept in _mapping_records(candidates):
        name = _safe_text(concept.get("normalized_name"))
        if not name:
            continue
        if name == selected_name:
            selected.append(concept)
            continue
        is_old_or_generic = (
            name in _GENERIC_COLD_EXCLUDE
            or any(fragment in name for fragment in _OLD_SEED_FRAGMENTS)
        )
        if not is_old_or_generic and concept.get("term_type") in {
            "material",
            "component",
            "equipment",
            "technology_route",
        }:
            fresh.append(concept)
        else:
            fallback.append(concept)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in (selected, fresh, fallback):
        for concept in group:
            name = str(concept.get("normalized_name") or "")
            if name in seen:
                continue
            out.append(concept)
            seen.add(name)
            if len(out) >= limit:
                return out
    return out


def _related_company_symbols(companies: Iterable[str]) -> List[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for company in companies if isinstance(companies, (list, tuple, set)) else []:
        text = _safe_text(company).strip()
        if not text:
            continue
        for match in _SYMBOL_RE.finditer(text):
            symbol = _normalize_symbol(match.group(0))
            if symbol and symbol not in seen:
                pairs.append((text, symbol))
                seen.add(symbol)
    return pairs


def _normalize_symbol(symbol: str) -> str:
    value = _safe_text(symbol).upper().strip()
    if re.fullmatch(r"\d{6}", value):
        if value.startswith(("0", "3")):
            return f"{value}.SZ"
        if value.startswith(("6", "9")):
            return f"{value}.SH"
        if value.startswith(("4", "8")):
            return f"{value}.BJ"
    if re.fullmatch(r"\d{6}\.(SZ|SH|BJ)", value):
        return value
    match = re.search(r"(\d{6})", value)
    if match:
        return _normalize_symbol(match.group(1))
    return ""


def _task_cli(capability: str, query: str, symbols: List[str], keyword: Optional[str]) -> List[str]:
    capability = _safe_text(capability, "semantic_search")
    query = _safe_text(query)
    keyword = _safe_text(keyword)
    symbols = _safe_text_list(symbols)
    command = {
        "semantic_search": "search",
        "reports": "report",
        "news": "news",
        "announcements": "announce",
        "periodic_reports": "periodic-report",
        "investor_interactions": "investor-interaction",
    }.get(capability, "search")
    cli = ["python", "scripts/research_data.py", command, "--limit", "5"]
    if symbols:
        cli.extend(["--symbols", ",".join(symbols)])
    if capability == "semantic_search":
        cli.extend(["--query", query])
    elif keyword:
        cli.extend(["--keyword", keyword])
    return cli


def _short_hash(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def run_local_concept_loop(
    output_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
    execute_search: bool = False,
    use_llm_explanations: bool = False,
) -> Dict[str, Any]:
    """运行 P0 本地概念闭环，返回 dict summary。

    封装 PerillaKnowledgeLoopEngine，供 P8 总调度直接 import 调用。

    Args:
        output_dir: 输出目录，默认为 data/reports/perilla_discovery/
        project_root: 项目根目录，默认为当前工作目录
        execute_search: 是否执行搜索任务（测试时设为 False）
        use_llm_explanations: 是否使用 LLM 解释（测试时设为 False）

    Returns:
        dict summary，含 module / success / outputs / counts
    """
    root = Path(project_root) if project_root else Path(".")
    out = Path(output_dir) if output_dir else root / "data" / "reports" / "perilla_discovery"

    try:
        engine = PerillaKnowledgeLoopEngine(
            project_root=root,
            output_dir=out,
            use_llm_explanations=use_llm_explanations,
        )
        result = engine.run(execute_search=execute_search)
        return {
            "module": "local_concept_loop",
            "success": True,
            "outputs": list(result.outputs.values()) if result.outputs else [],
            "counts": {
                "raw_sources": len(result.raw_sources),
                "raw_documents": len(result.raw_documents),
                "raw_terms": len(result.raw_terms),
                "candidate_concepts": len(result.candidate_concepts),
                "evidence_edges": len(result.evidence_edges),
                "search_tasks": len(result.search_tasks),
            },
        }
    except Exception as exc:
        logger.error("run_local_concept_loop 失败: %s", exc, exc_info=True)
        return {
            "module": "local_concept_loop",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }
