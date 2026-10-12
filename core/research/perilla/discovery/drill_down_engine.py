"""递归下钻引擎：从部件到矿产的自动拆解。

核心逻辑：每发现一个实体，就问"它由什么构成/用什么材料"，
直到答案变成原子级（矿产/基础化学品/能源/自然物/基础工艺）为止。

关系类型：
  contains  — 父部件包含子部件（激光发射器 → 泵浦源）
  made_of   — 器件/部件使用材料（钛宝石激光器 → 蓝宝石晶体）

递归控制：
  - 最大深度 10 层
  - 连续 2 次无新发现则暂停
  - 原子级概念立即停止

用法：
  from core.research.perilla.discovery.drill_down_engine import DrillDownEngine
  engine = DrillDownEngine()
  tree = engine.drill("光模块", max_depth=6)
  print(engine.render_tree(tree))
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from core.utils.search_throttle import SearchThrottlePool
from core.utils.llm_usage import (
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    record_llm_usage,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ATOMIC_TERMS_PATH = _PROJECT_ROOT / "data" / "perilla_taxonomy" / "atomic_terms.json"


def _short_hash(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now().isoformat()


def _bounded_int(value: Any, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    parsed = max(minimum, parsed)
    return min(parsed, maximum) if maximum is not None else parsed


def _bounded_float(value: Any, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    parsed = max(minimum, parsed)
    return min(parsed, maximum) if maximum is not None else parsed


class DrillDownEngine:
    """递归下钻引擎：从部件到矿产的自动拆解。

    对每个非原子概念，自动生成下钻查询，
    通过问财/AKShare/百度AI搜索获取组成和材料信息，
    用规则+LLM提取子项，建立 contains/made_of 关系边，
    然后对新子项递归执行，直到触达原子级或最大深度。
    """

    def __init__(
        self,
        atomic_terms_path: Path | str | None = None,
        max_depth: int = 10,
        max_breadth: int = 8,
        max_no_new_discovery: int = 2,
        llm_batch_size: int | None = None,
        extraction_confidence_threshold: float | None = None,
    ) -> None:
        """初始化下钻引擎。

        Args:
            atomic_terms_path: 原子级终止词典路径
            max_depth: 最大下钻深度
            max_breadth: 每层最大子项数
            max_no_new_discovery: 连续无新发现的最大次数
        """
        self.max_depth = _bounded_int(max_depth, 10, minimum=0)
        self.max_breadth = _bounded_int(max_breadth, 8, minimum=1)
        self.max_no_new_discovery = _bounded_int(max_no_new_discovery, 2, minimum=0)
        self.llm_batch_size = _bounded_int(
            llm_batch_size if llm_batch_size is not None else os.environ.get("DRILL_LLM_BATCH_SIZE", "8"),
            8,
            minimum=1,
            maximum=10,
        )
        self.extraction_confidence_threshold = _bounded_float(
            extraction_confidence_threshold
            if extraction_confidence_threshold is not None
            else os.environ.get("DRILL_EXTRACTION_CONFIDENCE_THRESHOLD", "0.65"),
            0.65,
            minimum=0.0,
            maximum=1.0,
        )
        self.important_max_depth = _bounded_int(
            os.environ.get("DRILL_IMPORTANT_MAX_DEPTH", "1"), 1, minimum=0
        )
        self.atomic_terms: set[str] = set()
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self._seen_nodes: set[str] = set()
        self._seen_edges: set[str] = set()

        # 加载原子级终止词典
        try:
            path = Path(atomic_terms_path) if atomic_terms_path else _ATOMIC_TERMS_PATH
        except (TypeError, ValueError):
            path = _ATOMIC_TERMS_PATH
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                categories = data.get("categories", {}) if isinstance(data, dict) else {}
                for category in categories.values() if isinstance(categories, dict) else []:
                    if not isinstance(category, dict):
                        continue
                    for term in category.get("terms", []):
                        if isinstance(term, str) and term.strip():
                            self.atomic_terms.add(term.strip())
                logger.info("加载原子级终止词典: %d 个术语", len(self.atomic_terms))
            except Exception as e:
                logger.warning("加载原子级词典失败: %s", e)

        # 初始化数据源
        self._registry = None
        self._llm_explainer = None
        self._search_discovery = None
        self._search_discovery_initialized = False
        self._ddg_throttle = SearchThrottlePool(
            max_concurrency=_bounded_int(os.environ.get("SEARCH_DDG_MAX_CONCURRENCY", "3"), 3, minimum=1),
            min_interval=_bounded_float(os.environ.get("SEARCH_DDG_MIN_INTERVAL", "1.0"), 1.0),
            failure_cooldown=_bounded_float(os.environ.get("SEARCH_DDG_FAILURE_COOLDOWN", "1200"), 1200.0),
            burst_capacity=_bounded_int(os.environ.get("SEARCH_DDG_BURST", "3"), 3, minimum=1),
        )
        self._runtime_stats = {
            "search_query_count": 0,
            "fast_path_count": 0,
            "retry_path_count": 0,
            "hard_path_count": 0,
            "llm_batch_call_count": 0,
            "llm_concept_count": 0,
        }

    def _ensure_registry(self) -> Any:
        """延迟初始化 ProviderRegistry。"""
        if self._registry is None:
            try:
                from infrastructure.data_providers.provider_registry import ProviderRegistry
                self._registry = ProviderRegistry()
            except Exception as e:
                logger.warning("ProviderRegistry 不可用: %s", e)
        return self._registry

    def _ensure_llm(self) -> Any:
        """延迟初始化 LLM 解释器。"""
        if self._llm_explainer is None:
            try:
                from core.research.perilla.discovery.concept_llm_explainer import ConceptLLMExplainer
                self._llm_explainer = ConceptLLMExplainer(
                    _PROJECT_ROOT / "data" / "llm_cache" / "drill_down"
                )
            except Exception as e:
                logger.warning("LLM 解释器不可用: %s", e)
        return self._llm_explainer

    def _ensure_search_discovery(self) -> Any:
        """按配置延迟初始化 SearXNG 搜索客户端。"""
        if self._search_discovery_initialized:
            return self._search_discovery
        self._search_discovery_initialized = True
        try:
            from config.secrets import Secrets
            from core.research.perilla.discovery.search_discovery import SearchDiscovery

            base_url = str(
                os.environ.get("SEARXNG_BASE_URL")
                or getattr(Secrets, "SEARXNG_BASE_URL", "")
                or ""
            ).strip()
            if not base_url:
                return None
            engines = str(
                os.environ.get("SEARXNG_ENGINES")
                or getattr(
                    Secrets,
                    "SEARXNG_ENGINES",
                    "duckduckgo,bing,brave,wikipedia,wikidata,baidu,baidu kaifa",
                )
                or ""
            ).strip()
            self._search_discovery = SearchDiscovery(
                searxng_base_url=base_url,
                max_results=5,
                timeout=15,
                engines=engines,
            )
        except Exception as exc:
            logger.warning("SearXNG 搜索客户端不可用: %s", exc)
        return self._search_discovery

    # ----------------------------------------------------------
    # 原子级判断
    # ----------------------------------------------------------

    def is_atomic(self, term: str) -> bool:
        """判断术语是否为原子级（不可再分）。"""
        if not isinstance(term, str) or not term:
            return False
        if term in self.atomic_terms:
            return True
        # 模糊匹配：术语包含原子级关键词
        for atomic in self.atomic_terms:
            if atomic in term or term in atomic:
                return True
        return False

    # ----------------------------------------------------------
    # 节点/边管理
    # ----------------------------------------------------------

    def _add_node(self, name: str, node_type: str, layer: str,
                  description: str = "", source: str = "drill_down") -> str:
        """添加节点，去重。"""
        if name in self._seen_nodes:
            return f"node_{_short_hash(name)}"
        self._seen_nodes.add(name)
        node_id = f"node_{_short_hash(name)}"
        self.nodes.append({
            "node_id": node_id,
            "node_name": name,
            "node_type": node_type,
            "layer": layer,
            "root_theme": "光",
            "description": description,
            "source": source,
            "is_atomic": self.is_atomic(name),
            "status": "active",
            "created_at": _now_iso(),
        })
        return node_id

    def _add_edge(self, source_name: str, relation: str, target_name: str,
                  confidence: str = "hypothesis", evidence: str = "") -> None:
        """添加边，去重。"""
        key = f"{source_name}|{relation}|{target_name}"
        if key in self._seen_edges:
            return
        self._seen_edges.add(key)
        self.edges.append({
            "edge_id": f"edge_{_short_hash(key)}",
            "source_node": source_name,
            "relation": relation,
            "target_node": target_name,
            "confidence_level": confidence,
            "evidence": evidence,
            "status": "pending",
            "created_at": _now_iso(),
        })

    # ----------------------------------------------------------
    # 下钻搜索
    # ----------------------------------------------------------

    @staticmethod
    def _query_plan(concept_name: str) -> list[str]:
        """Return the ordered 1→2→3 adaptive query plan."""
        return [
            f"{concept_name} 组成 结构 上游 材料 核心部件",
            f"{concept_name} 材料 上游 原料",
            f"{concept_name} 核心部件 子部件 组成",
        ]

    def _search_queries(
        self,
        concept_name: str,
        queries: list[str],
    ) -> list[dict[str, Any]]:
        """Execute only the supplied queries through one fallback chain."""
        docs: list[dict[str, Any]] = []
        if not queries:
            return docs
        self._runtime_stats["search_query_count"] += len(queries)

        search_discovery = self._ensure_search_discovery()
        if search_discovery:
            result_map = search_discovery.search_many(queries)
            for query, results in result_map.items():
                for result in results:
                    text = f"{result.title} {result.content_text}".strip()
                    if not text:
                        continue
                    docs.append({
                        "source": f"searxng:{result.site_name or 'unknown'}",
                        "query": query,
                        "data": {
                            "title": result.title,
                            "url": result.url,
                            "snippet": result.content_text,
                            "metadata": result.metadata,
                        },
                        "text": text,
                    })
            if docs:
                return docs

        registry = self._ensure_registry()
        if registry:
            for query in queries:
                try:
                    result = registry.semantic_search(query=query, count=10)
                    datas = result.get("results", [])
                    source = result.get("source", "unknown")
                    if datas:
                        for item in datas:
                            # 根据数据源类型拼接 text
                            if isinstance(item, dict):
                                # DuckDuckGo/百度返回 {title, snippet/url, ...}
                                title = str(item.get("title", ""))
                                snippet = str(item.get("snippet", "") or item.get("body", ""))
                                text = f"{title} {snippet}".strip() if title or snippet else str(item)
                            else:
                                text = str(item)
                            docs.append({
                                "source": source,
                                "query": query,
                                "data": item,
                                "text": text,
                            })
                except Exception as e:
                    logger.warning("ProviderRegistry 下钻搜索失败 (%s): %s", query, e)

        return docs

    def _drill_search(self, concept_name: str) -> list[dict[str, Any]]:
        """Compatibility wrapper: run the fast-path query only."""
        return self._search_queries(concept_name, self._query_plan(concept_name)[:1])

    @staticmethod
    def _merge_docs(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in groups:
            if not isinstance(group, (list, tuple)):
                continue
            for doc in group:
                if not isinstance(doc, dict):
                    continue
                data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
                key = "|".join(
                    [
                        str(doc.get("source", "")),
                        str(data.get("url", "")),
                        str(doc.get("text", ""))[:200],
                    ]
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(doc)
        return merged

    def _extraction_score(
        self,
        items: list[dict[str, Any]],
    ) -> float:
        """Score grounded extraction quality without using model confidence."""
        items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if not items:
            return 0.0
        grounded = sum(bool(str(item.get("evidence", "")).strip()) for item in items)
        sources = {
            str(item.get("source", "")).strip()
            for item in items
            if str(item.get("source", "")).strip()
        }
        item_score = min(1.0, len(items) / 3.0) * 0.55
        grounding_score = grounded / len(items) * 0.30
        source_score = min(1.0, len(sources) / 2.0) * 0.15
        return round(item_score + grounding_score + source_score, 4)

    def _adaptive_search(
        self,
        concept_name: str,
        *,
        important: bool,
    ) -> dict[str, Any]:
        """Spend one query by default and escalate only on weak extraction."""
        fast_query, material_query, component_query = self._query_plan(concept_name)
        docs: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        score = 0.0
        used_queries: list[str] = []
        query_queue = [fast_query]

        for index in range(3):
            if index >= len(query_queue):
                has_material = any(
                    item.get("type") == "used_material"
                    for item in items
                )
                has_component = any(
                    item.get("type") == "sub_component"
                    for item in items
                )
                if not has_component and component_query not in query_queue:
                    query_queue.append(component_query)
                elif not has_material and material_query not in query_queue:
                    query_queue.append(material_query)
                else:
                    remaining = [
                        query
                        for query in (material_query, component_query)
                        if query not in query_queue
                    ]
                    if remaining:
                        query_queue.append(remaining[0])
            if index >= len(query_queue):
                break
            if index == 2 and not important:
                break
            query = query_queue[index]
            fresh_docs = self._search_queries(concept_name, [query])
            docs = self._merge_docs(docs, fresh_docs)
            used_queries.append(query)
            items = self._rule_extract(concept_name, docs)
            score = self._extraction_score(items)

            if index == 0:
                self._runtime_stats["fast_path_count"] += 1
            elif index == 1:
                self._runtime_stats["retry_path_count"] += 1
            else:
                self._runtime_stats["hard_path_count"] += 1

            if len(items) >= 2 and score >= self.extraction_confidence_threshold:
                break

        return {
            "concept_name": concept_name,
            "docs": docs,
            "rule_items": items,
            "score": score,
            "queries": used_queries,
            "needs_llm": (
                important
                and score < self.extraction_confidence_threshold
            ),
        }

    def _duckduckgo_search(self, query: str) -> list[dict[str, Any]]:
        """DuckDuckGo/Bing 直接搜索（兜底路径）。"""
        results: list[dict[str, Any]] = []
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore[no-redef]
            except ImportError:
                logger.warning("ddgs/duckduckgo_search 均未安装，无法兜底搜索")
                return results

        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=5):
                    title = r.get("title", "")
                    body = r.get("body", "")
                    text = f"{title} {body}".strip()
                    if text:
                        results.append({
                            "source": "duckduckgo",
                            "query": query,
                            "data": r,
                            "text": text,
                        })
        except Exception as e:
            logger.warning("DuckDuckGo 搜索失败: %s", e)
        return results

    # ----------------------------------------------------------
    # 规则抽取
    # ----------------------------------------------------------

    def _rule_extract(self, concept_name: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """从搜索结果中用规则提取子项和材料。

        匹配模式（宽松版，适配百度/问财等多种返回格式）：
          X 由 Y 组成 / X 包括 Y 和 Z / X 的关键材料是 Y /
          X 采用 Y 作为... / X 使用 Y / X 包含 Y / X 主要由Y
        """
        items: list[dict[str, Any]] = []
        seen_items: set[str] = set()
        concept_name = concept_name if isinstance(concept_name, str) else ""
        docs = [doc for doc in docs if isinstance(doc, dict)] if isinstance(docs, list) else []
        if not concept_name:
            return items

        # 简化概念名（去掉括号和后缀）
        clean_name = re.sub(r"[（(].+?[）)]", "", concept_name).strip()

        patterns = [
            # X 由 Y 组成 / X 主要由 Y 组成
            re.compile(
                rf"{re.escape(clean_name)}"
                r"(?:主要)?[由用采以]"
                r"([^，。、；\n]{2,30})"
                r"[组成构成制造作为制备]"
            ),
            # X 的关键材料/核心部件/主要原料是 Y
            re.compile(
                rf"{re.escape(clean_name)}"
                r"的?(?:关键|核心|主要|重要)?(?:材料|部件|原料|组件|零件|元器件)"
                r"[是为有:]([^，。；\n]{2,20})"
            ),
            # X 包括/包含 Y
            re.compile(
                rf"{re.escape(clean_name)}"
                r"[包括包含含有]([^，。；\n]{2,40})"
            ),
            # 通用：在文本中直接匹配"Y是X的组成部分/核心部件/关键材料"
            re.compile(
                r"([^，。、；\n\s]{2,15})"
                rf"(?:是{re.escape(clean_name)}的)"
                r"(?:核心|关键|主要|重要)?(?:组成|部件|材料|原料|组件)"
            ),
        ]

        for doc in docs:
            text = doc.get("text", "")
            if not text or len(text) < 5:
                continue

            for pattern in patterns:
                for match in pattern.finditer(text):
                    raw = match.group(1).strip()
                    # 拆分"和"、"、"、"、"
                    parts = re.split(r"[、，,和与及以及以及]", raw)
                    for part in parts:
                        part = part.strip()
                        # 清理后缀
                        part = re.sub(r"(等|等等|组成|构成|制造|作为|制备|包括|包含)$", "", part).strip()
                        if not part or len(part) < 2 or len(part) > 15:
                            continue
                        if part == clean_name or part == concept_name or part in seen_items:
                            continue
                        # 过滤无意义词
                        skip_words = ["的", "了", "是", "在", "有", "和", "与", "及", "等",
                                      "其他", "相关", "各种", "多种", "若干", "部分", "一些"]
                        if part in skip_words:
                            continue
                        # 过滤垃圾词：含引号/标点/过长/含动词/含新闻标题特征
                        if any(c in part for c in "'\"《》【】「」''""…—–"):
                            continue
                        if len(part) > 10:
                            continue
                        # 过滤非中文词（日语假名/韩文/纯英文等）
                        chinese_chars = sum(1 for c in part if '\u4e00' <= c <= '\u9fff')
                        if chinese_chars == 0:
                            continue
                        # 过滤含日语假名
                        if any('\u3040' <= c <= '\u30ff' for c in part):
                            continue
                        # 过滤含新闻标题特征词
                        news_kw = ["地位", "关键", "蓄势", "待发", "突破", "崛起", "迎来",
                                   "爆发", "增长", "龙头", "概念", "板块", "上市", "公司",
                                   "国产化", "国产", "替代", "自主", "可控", "重要",
                                   "放量", "持续", "有望", "预计", "分析师", "机构",
                                   "投资", "涨跌", "收益", "利润", "营收", "市值"]
                        if any(kw in part for kw in news_kw):
                            continue
                        seen_items.add(part)

                        # 判断类型
                        material_kw = ["材料", "衬底", "晶圆", "晶体", "矿石", "矿", "原料",
                                       "树脂", "玻璃", "陶瓷", "金属", "合金", "纤维", "薄膜",
                                       "芯片", "IC", "激光器", "探测器", "二极管"]
                        if any(kw in part for kw in material_kw):
                            item_type = "used_material"
                        else:
                            item_type = "sub_component"

                        items.append({
                            "item": part,
                            "type": item_type,
                            "evidence": match.group(0)[:100],
                            "source": doc.get("source", "unknown"),
                        })

        return items[:self.max_breadth]

    # ----------------------------------------------------------
    # LLM 精抽
    # ----------------------------------------------------------

    @staticmethod
    def _normalize_evidence(text: str) -> str:
        return re.sub(r"\s+", "", str(text or ""))

    @staticmethod
    def _valid_item_name(name: str, concept_name: str) -> bool:
        name = str(name or "").strip()
        if not name or name == "未说明" or len(name) < 2 or name == concept_name:
            return False
        if len(name) > 10 or any(c in name for c in "'\"《》【】「」''""…—–"):
            return False
        if not any('\u4e00' <= c <= '\u9fff' for c in name):
            return False
        if any('\u3040' <= c <= '\u30ff' for c in name):
            return False
        news_kw = [
            "地位", "关键", "蓄势", "待发", "突破", "崛起", "迎来",
            "爆发", "增长", "龙头", "概念", "板块", "上市", "公司",
            "国产化", "国产", "替代", "自主", "可控", "重要",
            "放量", "持续", "有望", "预计", "分析师", "机构",
            "投资", "涨跌", "收益", "利润", "营收", "市值",
        ]
        if any(keyword in name for keyword in news_kw):
            return False
        return not re.match(
            r"^[望将可应需会能正已被让使把给对从向为与和或但而且则虽若如因故"
            r"所之其此这那该某各每任何无未不非否勿莫休别]",
            name,
        )

    @staticmethod
    def _llm_settings() -> tuple[str, str, str]:
        try:
            from config.config import Config

            return (
                str(getattr(Config, "LLM_API_BASE", "") or "").rstrip("/"),
                str(getattr(Config, "LLM_MODEL_NAME", "") or ""),
                str(getattr(Config, "LLM_API_KEY", "") or ""),
            )
        except Exception:
            return "", "", ""

    def _llm_extract_batch(
        self,
        cases: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Extract 5–10 low-confidence concepts in one grounded LLM call."""
        cases = [case for case in cases if isinstance(case, dict)] if isinstance(cases, list) else []
        output = {
            str(case.get("concept_name", "")): []
            for case in cases
            if str(case.get("concept_name", "")).strip()
        }
        if not output:
            return output

        evidence_catalog: dict[str, dict[str, str]] = {}
        prompt_cases: list[dict[str, Any]] = []
        for case_index, case in enumerate(cases[:self.llm_batch_size], start=1):
            concept_name = str(case.get("concept_name", "")).strip()
            snippets: list[dict[str, str]] = []
            docs = case.get("docs", [])
            docs = docs if isinstance(docs, list) else []
            for doc_index, doc in enumerate(
                (doc for doc in docs[:4] if isinstance(doc, dict)), start=1
            ):
                text = str(doc.get("text", "")).strip()[:350]
                if not text:
                    continue
                evidence_id = f"C{case_index}-E{doc_index}"
                evidence_catalog[evidence_id] = {
                    "concept_name": concept_name,
                    "text": text,
                    "source": str(doc.get("source", "unknown")),
                }
                snippets.append({"evidence_id": evidence_id, "text": text})
            prompt_cases.append({"concept_name": concept_name, "evidence": snippets})

        if not evidence_catalog:
            return output

        base_url, model, api_key = self._llm_settings()
        if not base_url or not model:
            return output

        prompt = (
            "请从以下多个概念及其搜索证据中抽取 contains / made_of 关系。\n"
            "硬性规则：\n"
            "1. 只能输出证据中明确出现的子项，不得根据常识补全或自由扩写。\n"
            "2. 每个子项必须绑定 evidence_id 和证据中的原文短句 evidence_snippet。\n"
            "3. relation_type 只能是 contains 或 made_of。\n"
            "4. 没有明确证据的概念必须返回空 extracted_items。\n"
            "5. evidence_snippet 必须逐字来自对应 evidence_id，不得改写。\n"
            "输出 JSON："
            '{"results":[{"source_concept":"概念","extracted_items":['
            '{"item":"子项","relation_type":"contains或made_of",'
            '"evidence_id":"C1-E1","evidence_snippet":"原文短句"}]}]}'
            "\n\n输入：\n"
            + json.dumps(prompt_cases, ensure_ascii=False)
        )
        llm_messages = [
            {"role": "system", "content": "evidence-constrained relation extractor"},
            {"role": "user", "content": prompt},
        ]
        prompt_tokens = estimate_messages_tokens(llm_messages)
        max_tokens = 2500

        try:
            import requests

            self._runtime_stats["llm_batch_call_count"] += 1
            self._runtime_stats["llm_concept_count"] += len(prompt_cases)
            check_llm_budget_or_raise(
                prompt_tokens,
                max_tokens,
                source="DrillDownEngine._llm_extract_batch",
            )
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            response = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是证据约束关系抽取器，只做精确抽取。",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 2500,
                },
                timeout=60,
            )
            if response.status_code != 200:
                record_llm_usage(
                    source="DrillDownEngine._llm_extract_batch",
                    provider="primary",
                    model=model,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=f"HTTP {response.status_code}",
                )
                return output
            content = (
                response.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            record_llm_usage(
                source="DrillDownEngine._llm_extract_batch",
                provider="primary",
                model=model,
                prompt_tokens_est=prompt_tokens,
                completion_tokens_est=estimate_tokens(str(content or "")),
                success=True,
            )
            text = str(content or "").strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                return output
        except Exception as exc:
            logger.warning("LLM 批量下钻提取失败: %s", exc)
            return output

        for result in parsed.get("results", []):
            if not isinstance(result, dict):
                continue
            concept_name = str(result.get("source_concept", "")).strip()
            if concept_name not in output:
                continue
            seen_names: set[str] = set()
            for item in result.get("extracted_items", []):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("item", "")).strip()
                evidence_id = str(item.get("evidence_id", "")).strip()
                evidence = str(item.get("evidence_snippet", "")).strip()
                relation = str(item.get("relation_type", "")).strip()
                catalog_item = evidence_catalog.get(evidence_id)
                if (
                    not catalog_item
                    or catalog_item["concept_name"] != concept_name
                    or relation not in {"contains", "made_of"}
                    or not self._valid_item_name(name, concept_name)
                    or name in seen_names
                ):
                    continue
                normalized_quote = self._normalize_evidence(evidence)
                normalized_source = self._normalize_evidence(catalog_item["text"])
                if len(normalized_quote) < 4 or normalized_quote not in normalized_source:
                    continue
                seen_names.add(name)
                output[concept_name].append(
                    {
                        "item": name,
                        "type": (
                            "used_material"
                            if relation == "made_of"
                            else "sub_component"
                        ),
                        "relation_type": relation,
                        "evidence": evidence,
                        "evidence_id": evidence_id,
                        "source": f"llm_batch:{catalog_item['source']}",
                    }
                )
                if len(output[concept_name]) >= self.max_breadth:
                    break
        return output

    def _llm_extract(
        self,
        concept_name: str,
        docs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper around the grounded batch extractor."""
        return self._llm_extract_batch(
            [{"concept_name": concept_name, "docs": docs}]
        ).get(concept_name, [])

    # ----------------------------------------------------------
    # 递归下钻主循环
    # ----------------------------------------------------------

    def drill(self, concept_name: str, max_depth: int | None = None) -> dict[str, Any]:
        """递归下钻：从概念拆解到原子级。

        Args:
            concept_name: 起始概念名
            max_depth: 最大深度（覆盖默认值）

        Returns:
            树形结构 {name, type, is_atomic, children: [...]}
        """
        depth = max_depth or self.max_depth
        logger.info("开始下钻: %s (max_depth=%d)", concept_name, depth)
        for key in self._runtime_stats:
            self._runtime_stats[key] = 0

        # 添加根节点
        self._add_node(concept_name, "component", "root", source="drill_down_start")

        # BFS 队列: (concept_name, current_depth)
        queue: list[tuple[str, int]] = [(concept_name, 0)]
        visited: set[str] = {concept_name}
        no_new_count: dict[str, int] = {}

        tree_root: dict[str, Any] = {
            "name": concept_name,
            "type": "component",
            "is_atomic": self.is_atomic(concept_name),
            "depth": 0,
            "children": [],
        }
        # 树节点索引（按名称）
        tree_index: dict[str, dict[str, Any]] = {concept_name: tree_root}

        while queue:
            batch = queue[:self.llm_batch_size]
            del queue[:self.llm_batch_size]
            prepared: list[dict[str, Any]] = []

            for current_name, current_depth in batch:
                if current_depth >= depth:
                    continue
                if self.is_atomic(current_name):
                    if current_name in tree_index:
                        tree_index[current_name]["is_atomic"] = True
                    continue

                no_new = no_new_count.get(current_name, 0)
                if no_new >= self.max_no_new_discovery:
                    continue

                logger.info(
                    "  下钻 [%d/%d]: %s",
                    current_depth,
                    depth,
                    current_name,
                )
                outcome = self._adaptive_search(
                    current_name,
                    important=current_depth <= self.important_max_depth,
                )
                if current_name in tree_index:
                    tree_index[current_name]["search_meta"] = {
                        "query_count": len(outcome["queries"]),
                        "queries": outcome["queries"],
                        "rule_item_count": len(outcome["rule_items"]),
                        "extraction_score": outcome["score"],
                        "llm_required": outcome["needs_llm"],
                    }
                prepared.append(
                    {
                        "current_name": current_name,
                        "current_depth": current_depth,
                        "no_new": no_new,
                        **outcome,
                    }
                )

            llm_cases = [
                {
                    "concept_name": item["current_name"],
                    "docs": item["docs"],
                }
                for item in prepared
                if item["needs_llm"]
            ]
            llm_results: dict[str, list[dict[str, Any]]] = {}
            for start in range(0, len(llm_cases), self.llm_batch_size):
                llm_results.update(
                    self._llm_extract_batch(
                        llm_cases[start:start + self.llm_batch_size]
                    )
                )

            for entry in prepared:
                current_name = entry["current_name"]
                current_depth = entry["current_depth"]
                no_new = entry["no_new"]
                llm_items = llm_results.get(current_name, [])

                all_items: list[dict[str, Any]] = []
                seen_item_names: set[str] = set()
                for item in entry["rule_items"] + llm_items:
                    name = str(item.get("item", "")).strip()
                    if not name or name in seen_item_names:
                        continue
                    seen_item_names.add(name)
                    all_items.append(item)
                    if len(all_items) >= self.max_breadth:
                        break

                if current_name in tree_index:
                    tree_index[current_name]["search_meta"]["llm_item_count"] = len(
                        llm_items
                    )

                if not all_items:
                    no_new_count[current_name] = no_new + 1
                    continue

                new_discovered = 0
                for item in all_items:
                    child_name = item["item"]
                    child_type = item["type"]
                    evidence = item.get("evidence", "")

                    if child_type == "used_material":
                        node_type = "material"
                        relation = "made_of"
                    else:
                        node_type = "component"
                        relation = "contains"

                    self._add_node(
                        child_name,
                        node_type,
                        f"L{current_depth + 1}",
                        description=evidence[:100],
                        source=item.get("source", "drill_down"),
                    )
                    self._add_edge(
                        current_name,
                        relation,
                        child_name,
                        "hypothesis",
                        evidence[:200],
                    )

                    child_tree = {
                        "name": child_name,
                        "type": node_type,
                        "is_atomic": self.is_atomic(child_name),
                        "depth": current_depth + 1,
                        "relation": relation,
                        "evidence": evidence[:100],
                        "evidence_id": item.get("evidence_id", ""),
                        "children": [],
                    }
                    if current_name in tree_index:
                        tree_index[current_name]["children"].append(child_tree)
                    tree_index[child_name] = child_tree

                    if child_name not in visited:
                        visited.add(child_name)
                        queue.append((child_name, current_depth + 1))
                        new_discovered += 1

                if new_discovered == 0:
                    no_new_count[current_name] = no_new + 1

        logger.info("下钻完成: %d nodes, %d edges", len(self.nodes), len(self.edges))
        tree_root["runtime_stats"] = dict(self._runtime_stats)
        return tree_root

    # ----------------------------------------------------------
    # 树形展示
    # ----------------------------------------------------------

    @staticmethod
    def render_tree(tree: dict[str, Any], indent: int = 0, prefix: str = "") -> str:
        """将树形结构渲染为文本。

        Args:
            tree: 树形结构
            indent: 缩进层级
            prefix: 前缀符号

        Returns:
            树形文本
        """
        if not isinstance(tree, dict):
            return "?"
        lines: list[str] = []
        name = tree.get("name", "?")
        node_type = tree.get("type", "?")
        is_atomic = tree.get("is_atomic", False)
        relation = tree.get("relation", "")
        evidence = tree.get("evidence", "")

        # 关系标签
        rel_label = ""
        if relation == "made_of":
            rel_label = " [材料]"
        elif relation == "contains":
            rel_label = " [包含]"

        # 原子级标记
        atomic_label = " (原子级)" if is_atomic else ""

        # 类型标签
        type_label = ""
        if node_type == "material":
            type_label = " 🪨"
        elif node_type == "component":
            type_label = " ⚙️"

        line = f"{prefix}{name}{rel_label}{type_label}{atomic_label}"
        if evidence and indent > 0:
            line += f"  ← {evidence[:50]}"
        lines.append(line)

        children = tree.get("children", [])
        if not isinstance(children, list):
            children = []
        for i, child in enumerate(children):
            is_last = (i == len(children) - 1)
            child_prefix = prefix + ("    " if is_last else "│   ")
            connector = "└── " if is_last else "├── "
            lines.append(DrillDownEngine.render_tree(child, indent + 1, prefix + connector))

        return "\n".join(lines)

    def get_graph(self) -> dict[str, Any]:
        """返回完整的图谱数据（nodes + edges）。"""
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "metadata": {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "atomic_terms_count": len(self.atomic_terms),
                "query_budget": "adaptive_1_to_3",
                "runtime_stats": dict(self._runtime_stats),
                "generated_at": _now_iso(),
            },
        }
