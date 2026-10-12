"""ReportGraphBuilder：从可信文本来源抽取 claim，构建 SAG。

输入：TDX 研报、券商报告、公告、年报、招股书、官网文本、专利、互动易、问财、东财、网页搜索等。
输出：结构化 claim 列表，写入 SAGKnowledgeBase。

原则：
- 不直接写死结论，先写 claim。
- 每条 claim 必须绑定 evidence。
- LLM 只负责从原文中抽取，不做无证据补全。
- 如果没有文本输入且无法获取行情数据，直接报错停止（不降级）。
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from core.research.perilla.alpha.graph_schema import (
    GENERIC_SUBSTRINGS,
    NodeType,
    RelationType,
    SourceType,
    StructuralAxis,
    get_source_weight,
    infer_axis,
)
from core.research.perilla.shared.identity.entity_resolver import EntityResolver
from core.research.perilla.shared.ontology.ontology_registry import OntologyRegistry
from core.research.perilla.alpha.sag_knowledge_base import SAGKnowledgeBase
from core.utils.llm_usage import (
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    load_llm_providers,
    record_llm_usage,
)

logger = logging.getLogger(__name__)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
            return result if isinstance(result, Mapping) else {}
        except (AttributeError, TypeError, ValueError):
            return {}
    return {}


def _mapping_get(value: Any, key: Any, default: Any = None) -> Any:
    if not isinstance(value, Mapping):
        return default
    try:
        return value.get(key, default)
    except (TypeError, ValueError):
        return default


class ReportGraphBuilder:
    """从可信来源文本中抽取 claim 并写入 SAG。"""

    def __init__(
        self,
        kb: SAGKnowledgeBase | None = None,
        llm_fallback: bool = True,
        entity_resolver: EntityResolver | None = None,
    ) -> None:
        """初始化。

        Args:
            kb: SAG 知识库实例，None 则新建内存版
            llm_fallback: LLM 不可用时是否启用规则提取兜底
            entity_resolver: 实体解析器，None 则自动新建
        """
        self.kb = kb or SAGKnowledgeBase()
        self.llm_fallback = llm_fallback
        self.entity_resolver = entity_resolver or EntityResolver()
        self.ontology_registry: OntologyRegistry = self.kb.ontology_registry

    @staticmethod
    def _llm_settings() -> tuple[str, str, str]:
        """读取 LLM 配置。"""
        try:
            from config.config import Config

            return (
                str(getattr(Config, "LLM_API_BASE", "") or "").rstrip("/"),
                str(getattr(Config, "LLM_MODEL_NAME", "") or ""),
                str(getattr(Config, "LLM_API_KEY", "") or ""),
            )
        except Exception:
            return "", "", ""

    def _call_llm(self, prompt: str, max_tokens: int = 3000) -> str:
        """Call configured providers with fallback and rough token accounting."""
        prompt = _text(prompt)
        max_tokens = max(1, min(16000, int(_finite(max_tokens, 3000))))
        base_url, model, api_key = self._llm_settings()
        if not base_url or not model:
            raise RuntimeError("LLM config is missing")

        import requests

        messages = [
            {"role": "system", "content": "你是产业链证据抽取器，只根据原文抽取 claim，不做无证据补全。"},
            {"role": "user", "content": prompt},
        ]
        prompt_tokens = estimate_messages_tokens(messages)
        check_llm_budget_or_raise(prompt_tokens, max_tokens, source="ReportGraphBuilder._call_llm")
        last_error: Exception | None = None
        for provider in load_llm_providers(base_url, api_key, model):
            headers = {"Content-Type": "application/json"}
            if provider.api_key:
                headers["Authorization"] = f"Bearer {provider.api_key}"
            try:
                resp = requests.post(
                    f"{provider.api_base}/chat/completions",
                    headers=headers,
                    json={
                        "model": provider.model_name,
                        "messages": messages,
                        "temperature": 0,
                        "max_tokens": max_tokens,
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                payload = resp.json()
                choices = payload.get("choices") if isinstance(payload, dict) else None
                if not isinstance(choices, list) or not choices:
                    raise RuntimeError("LLM response missing choices")
                first_choice = choices[0]
                message = first_choice.get("message") if isinstance(first_choice, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if content is None:
                    raise RuntimeError("LLM response missing message content")
                text = str(content or "").strip()
                record_llm_usage(
                    source="ReportGraphBuilder._call_llm",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=estimate_tokens(text),
                    success=True,
                )
                return text
            except Exception as exc:
                last_error = exc
                record_llm_usage(
                    source="ReportGraphBuilder._call_llm",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=str(exc),
                )
        raise RuntimeError(last_error or "all LLM providers failed")

    @staticmethod
    def _clean_llm_json(text: str) -> str:
        """清理 LLM 返回的 JSON 代码块。"""
        text = _text(text)
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return text.strip()

    @staticmethod
    def _llm_available() -> bool:
        """检查 LLM 服务是否可达。"""
        base_url, model, _ = ReportGraphBuilder._llm_settings()
        if not base_url or not model:
            return False
        try:
            import requests

            resp = requests.get(f"{base_url}/models", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def _rule_based_extract_claims(
        self,
        text: str,
        source_type: str,
        source_title: str,
        timestamp: str,
        root_concept: str | None = None,
    ) -> list[dict[str, Any]]:
        """规则提取 claim（LLM 不可用时兜底）。

        基于关键词、句式模板从搜索摘要/文本中抽取产业链关系。
        结果质量低于 LLM，但保证不依赖外部模型即可跑通真实数据。
        """
        text = _text(text)
        source_type = _text(source_type) or SourceType.WEB_SEARCH.value
        source_title = _text(source_title, "untitled")
        timestamp = _text(timestamp)
        root_concept = _text(root_concept) or None
        results: list[dict[str, Any]] = []
        sentences = re.split(r"[。；!！?？\n]", text)

        # 候选关系词映射：relation -> (说明, 触发词, 是否提取 tail)
        relation_patterns: list[tuple[str, str, list[str], bool]] = [
            ("made_of", "由...构成/采用", ["由", "采用", "使用", "包含", "内有", "核心部件", "主要材料", "关键材料"], True),
            ("supplied_by", "供应商", ["供应商", "供货", "供应", "采购自", "来自"], True),
            ("import_dependent_on", "进口依赖", ["进口", "依赖进口", "海外依赖", "来自海外", "受制于人"], True),
            ("bottleneck_of", "瓶颈", ["瓶颈", "卡脖子", "难以替代", "核心难点", "关键制约"], True),
            ("substitute_for", "替代", ["国产替代", "进口替代", "替代"], True),
            ("constrained_by", "受...约束", ["受限于", "受制于", "依赖", "短缺"], True),
            ("processed_by", "加工工艺", ["加工", "制造", "生产", "工艺"], True),
            ("tested_by", "测试", ["测试", "检测", "量测"], True),
            ("certified_by", "认证", ["认证", "车规", "军规", "AEC-Q"], True),
            ("cost_driver_of", "成本驱动", ["成本", "占成本", "BOM"], True),
            ("performance_driver_of", "性能驱动", ["决定性能", "影响性能", "性能关键", "速率", "带宽"], True),
        ]

        # 从文本中识别可能的实体：root、具体材料/设备/公司
        known_products: dict[str, list[str]] = {
            "光模块": ["光芯片", "光器件", "激光器", "探测器", "DSP", "电芯片", "PCB", "光纤", "交换机", "光引擎", "铌酸锂调制器", "陶瓷外壳", "半导体硅片", "高端铜箔", "光通信测试仪器", "光芯片进口"],
            "液冷服务器": ["冷板", "冷却液", "快接头", "CDU", "液冷泵", "换热器", "浸没式液冷", "冷媒", "氟化液", "水冷", "管路", "温控系统", "散热模组", "真空钎焊"],
            "PCB": ["覆铜板", "铜箔", "树脂", "玻纤布", "PCB", "IC载板", "HDI板", "高频高速板", "沉铜", "电镀", "蚀刻", "钻孔", "阻焊", "激光钻孔机"],
        }

        # 常见中国/海外公司、机构名后缀
        company_suffixes = ["股份", "科技", "电子", "材料", "有限公司", "集团", " Corp", " Inc", " Ltd", "博通", "迈威尔", "史陶比尔", "建滔", "沪电", "鹏鼎", "诺德", "中核", "德福", "逸豪", "中一", "圣泉", "宏昌", "东材", "宏和科技", "生益"]

        def _extract_tail(after_text: str) -> str | None:
            """从关键词后的文本提取名词短语实体。"""
            # 去掉前导虚词/标点
            after_text = after_text.strip("，、；。 \n的之是为")
            if not after_text or len(after_text) < 2:
                return None
            # 优先匹配：2-10 个连续中文字符，或已知公司名（含$、英文缩写）
            m = re.match(r"[\u4e00-\u9fa5A-Za-z0-9\$\-]{2,10}", after_text)
            if not m:
                return None
            tail = m.group(0).rstrip("的之是为，、；。")
            return tail if len(tail) >= 2 else None

        def _is_bad_tail(tail: str) -> bool:
            """判断 tail 是否为无效实体。"""
            if not tail or len(tail) > 20 or len(tail) < 2:
                return True
            # 过滤纯数字/纯符号
            if re.match(r"^[0-9\$%\.\,]+$", tail):
                return True
            bad_starts = ["的", "是", "为", "和", "与", "及", "在", "对", "从", "被", "将", "以", "把", "向", "让", "给", "随", "因", "而", "但", "如", "若", "即", "则", "又", "也", "都", "却"]
            if any(tail.startswith(b) for b in bad_starts):
                return True
            bad_tail_keywords = [
                "限制", "动作", "方案", "精度", "效率", "稳定性", "可靠性", "能力", "水平", "趋势",
                "影响", "发展", "表现", "结果", "前景", "进程", "加速", "放缓", "提升", "下降",
                "增强", "减弱", "之一", "主要", "部分", "核心", "关键", "重要",
                # 拦截从搜索摘要中误抽的谓词性/泛化短语
                "绑定", "依赖", "突破", "实现", "环节", "领域", "方面", "产业链", "上游", "下游",
            ]
            if any(bk in tail for bk in bad_tail_keywords):
                return True
            # 拦截泛概念子串黑名单
            if any(gs in tail for gs in GENERIC_SUBSTRINGS):
                return True
            # tail 不能是单个虚词+名词的模糊结构（如"产品用于客户A"）
            if re.match(r"^[A-Za-z0-9]+用于", tail):
                return True
            return False

        for sentence in sentences:
            sentence = sentence.strip()
            if len(sentence) < 8:
                continue

            # 尝试匹配 root + relation + tail 模式
            for relation, _, keywords, extract_tail_flag in relation_patterns:
                for kw in keywords:
                    if kw not in sentence:
                        continue
                    head: str | None = None
                    tail: str | None = None

                    # 查找 root_concept 或已知产品作为 head
                    candidates: list[str] = [root_concept] if root_concept else []
                    for prod, subs in known_products.items():
                        candidates.extend([prod] + subs)
                    candidates = [c for c in candidates if c and c in sentence]
                    # 优先匹配较长的 head（避免“光”匹配到“光模块”的片段）
                    candidates.sort(key=len, reverse=True)

                    kw_pos = sentence.find(kw)
                    for cand in candidates:
                        cand_pos = sentence.find(cand)
                        if cand_pos < 0:
                            continue
                        # head 应在关键词之前，或至少不显著在关键词之后
                        if cand_pos <= kw_pos + len(kw):
                            head = cand
                            if extract_tail_flag:
                                after = sentence[kw_pos + len(kw):kw_pos + len(kw) + 16]
                                tail = _extract_tail(after)
                                # 如果 tail 与 head 相同，尝试从关键词前找 tail（supplied_by/import 等）
                                if tail == head or _is_bad_tail(tail):
                                    before = sentence[max(0, kw_pos - 16):kw_pos]
                                    # 从 before 末尾提取最后一个候选实体
                                    for sub in sorted(known_products.get(root_concept or "", []), key=len, reverse=True):
                                        if sub in before:
                                            tail = sub
                                            break
                            break

                    if _is_bad_tail(tail):
                        tail = None

                    if head and tail and head != tail:
                        head_type = self._infer_node_type(head, relation, "head")
                        tail_type = self._infer_node_type(tail, relation, "tail")
                        axis_candidates = infer_axis(tail, relation)
                        axis = list(axis_candidates)[0] if axis_candidates else StructuralAxis.MATERIAL.value
                        if head in ("液冷服务器", "光模块", "PCB"):
                            head_type = NodeType.PRODUCT.value
                        # 公司尾节点优先识别为公司
                        if any(s in tail for s in company_suffixes):
                            tail_type = NodeType.COMPANY.value
                        results.append({
                            "head": head,
                            "relation": relation,
                            "tail": tail,
                            "evidence": sentence,
                            "source_type": source_type,
                            "source_title": source_title,
                            "timestamp": timestamp,
                            "confidence": get_source_weight(source_type) * 0.7,
                            "head_type": head_type,
                            "tail_type": tail_type,
                            "structure_axis": axis,
                        })
                        break
                if results and results[-1]["head"] in (sentence[:20]):
                    break

        # 去重
        seen: set[tuple[str, str, str]] = set()
        unique: list[dict[str, Any]] = []
        for c in results:
            key = (c["head"], c["relation"], c["tail"])
            if key not in seen:
                seen.add(key)
                unique.append(c)
        return unique

    def _infer_node_type(self, name: str, relation: str, role: str) -> str:
        """推断节点类型，优先使用 EntityResolver，fallback 到启发式规则。"""
        name = _text(name)
        relation = _text(relation)
        role = _text(role)
        try:
            resolved = self.entity_resolver.resolve(name)
            confidence = _finite(getattr(resolved, "resolver_confidence", 0.0))
            node_type = _text(getattr(resolved, "node_type", ""))
            if confidence >= 0.5 and self.kb.ontology_registry.is_valid_node_type(node_type):
                return node_type
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.warning("实体类型解析失败: %s", name, exc_info=True)
        return self._heuristic_infer_node_type(name, relation, role)

    @staticmethod
    def _heuristic_infer_node_type(name: str, relation: str, role: str) -> str:
        """根据节点名和关系推断节点类型（启发式 fallback）。"""
        name = _text(name)
        name_lower = name.lower()
        relation = _text(relation)

        # 明确上游材料/耗材/零部件清单（优先识别为 MATERIAL）
        material_keywords = [
            "矿", "材料", "化学", "树脂", "金属", "晶圆", "衬底", "气体", "液", "芯片", "箔", "板",
            "外壳", "调制器", "连接器", "接头", "冷板", "冷却液", "氟化液", "快接头", "cdu",
            "光芯片", "光器件", "铌酸锂", "陶瓷外壳", "高端铜箔", "覆铜板", "铜箔", "玻纤",
            "环氧树脂", "光引擎", "激光器", "探测器", "dsp", "电芯片", "pcb", "光纤",
            "阻焊", "沉铜", "钻孔", "电镀", "蚀刻", "阻焊",
        ]
        # 明确设备/仪器清单
        equipment_keywords = [
            "设备", "机台", "光刻机", "刻蚀机", "钻孔机", "激光钻孔机", "加工中心",
            "测试设备", "检测设备", "量测设备", "封装设备", "探针台", "光通信测试仪器",
        ]
        # 明确工艺/流程清单
        process_keywords = [
            "工艺", "制程", "封装", "光刻", "蚀刻", "电镀", "钻孔", "钎焊", "切割", "抛光",
            "沉铜", "阻焊", "加工", "真空钎焊", "制造", "生产",
        ]
        # 明确测试/检测清单
        testing_keywords = ["测试", "检测", "量测", "探针", "检验"]
        # 公司识别（含常见外资/台资名）
        company_keywords = [
            "股份", "科技", "电子", "材料", "有限公司", "集团", " Corp", " Inc", " Ltd",
            "博通", "迈威尔", "史陶比尔", "建滔", "沪电", "鹏鼎", "诺德", "中核", "德福",
            "逸豪", "中一", "圣泉", "宏昌", "东材", "宏和科技", "生益",
        ]

        # 公司优先
        if any(kw in name for kw in company_keywords):
            return NodeType.COMPANY.value

        # 设备优先
        if any(kw in name_lower for kw in equipment_keywords):
            return NodeType.EQUIPMENT.value

        # 测试优先
        if any(kw in name_lower for kw in testing_keywords):
            return NodeType.TESTING.value

        # 工艺优先（但避免将“光芯片”等误判为 process）
        if any(kw in name_lower for kw in process_keywords):
            # 如果同时是材料关键词，按关系决定
            if any(kw in name_lower for kw in material_keywords):
                if relation in (RelationType.PROCESSED_BY.value, RelationType.TESTED_BY.value):
                    return NodeType.PROCESS.value
                return NodeType.MATERIAL.value
            return NodeType.PROCESS.value

        # 材料：包含核心上游零部件与原材料
        if any(kw in name_lower for kw in material_keywords):
            return NodeType.MATERIAL.value

        # 产品/系统级根概念识别
        if any(suffix in name for suffix in ["服务器", "模块", "PCB", "板"]):
            if role == "head":
                return NodeType.PRODUCT.value
            if relation in (RelationType.MADE_OF.value, RelationType.CONTAINS.value):
                return NodeType.MATERIAL.value

        # 根据关系兜底
        if relation in (RelationType.MADE_OF.value, RelationType.COST_DRIVER_OF.value):
            return NodeType.MATERIAL.value
        if relation in (RelationType.PROCESSED_BY.value, RelationType.TESTED_BY.value):
            return NodeType.PROCESS.value
        if relation in (RelationType.SUPPLIED_BY.value,):
            return NodeType.SUPPLIER.value
        if relation in (RelationType.CERTIFIED_BY.value,):
            return NodeType.CERTIFICATION.value

        if role == "head":
            return NodeType.COMPONENT.value
        return NodeType.MATERIAL.value

    def extract_claims_from_text(
        self,
        text: str,
        source_type: str,
        source_title: str,
        timestamp: str | None = None,
        root_concept: str | None = None,
    ) -> list[dict[str, Any]]:
        """从单段文本中抽取 claim。

        Args:
            text: 原文
            source_type: 来源类型
            source_title: 来源标题
            timestamp: 时间戳
            root_concept: 根概念提示

        Returns:
            claim dict 列表
        """
        text = _text(text)
        source_type = _text(source_type) or SourceType.WEB_SEARCH.value
        source_title = _text(source_title, "untitled")
        root_concept = _text(root_concept) or None
        if not text:
            return []

        max_len = 4000
        if len(text) > max_len:
            text = text[:max_len] + "\n...[截断]"

        try:
            relation_list = ", ".join(sorted(_text(item) for item in self.ontology_registry.all_relations() if _text(item)))
        except (AttributeError, TypeError, ValueError):
            relation_list = ", ".join(item.value for item in RelationType)
        axis_list = ", ".join(sorted([a.value for a in StructuralAxis]))

        prompt = (
            "请从以下产业链文本中抽取结构化 claim。\n"
            "硬性规则：\n"
            "1. 只能输出原文中明确支持的 claim，不得根据常识补全。\n"
            "2. 每条 claim 必须包含 head、relation、tail、evidence。\n"
            "3. evidence 必须是原文中的短句，不能改写。\n"
            "4. relation 只能从以下列表选择：" + relation_list + "\n"
            "5. 为每个 tail 判断其最主要的 structure_axis（可选：" + axis_list + "）。\n"
            "6. 如果原文没有明确关系，不要输出。\n"
            "7. head 和 tail 必须是具体实体，不能是'上游'、'下游'、'产业链'等泛概念。\n\n"
            "输出 JSON 数组：\n"
            '[{"head":"光模块","relation":"made_of","tail":"光芯片",'
            '"evidence":"光芯片是光模块的核心元器件","structure_axis":"material_axis"}]\n\n'
            f"根概念: {root_concept or '无'}\n\n文本:\n{text}\n"
        )

        now = _text(timestamp) or datetime.now().isoformat()
        claims: list[dict[str, Any]] = []
        if self._llm_available():
            try:
                raw = self._call_llm(prompt)
                raw = self._clean_llm_json(raw)
                claims = json.loads(raw)
                if not isinstance(claims, list):
                    claims = [claims]
            except Exception as e:
                logger.warning("LLM claim 抽取失败 (%s): %s", source_title, e)
                claims = []

        if not claims and self.llm_fallback:
            logger.info("LLM 不可用，对 %s 启用规则提取兜底", source_title)
            claims = self._rule_based_extract_claims(
                text=text,
                source_type=source_type,
                source_title=source_title,
                timestamp=now,
                root_concept=root_concept,
            )

        results: list[dict[str, Any]] = []
        for item in claims if isinstance(claims, list) else []:
            if not isinstance(item, Mapping):
                continue
            head = _text(item.get("head"))
            tail = _text(item.get("tail"))
            relation = _text(item.get("relation"))
            evidence = _text(item.get("evidence"))
            axis = _text(item.get("structure_axis"), StructuralAxis.MATERIAL.value)
            if axis not in {axis_item.value for axis_item in StructuralAxis}:
                axis = StructuralAxis.MATERIAL.value

            if not head or not tail or not relation or not evidence:
                continue
            try:
                relation_valid = self.ontology_registry.is_valid_relation(relation)
            except (AttributeError, TypeError, ValueError):
                relation_valid = False
            if not relation_valid:
                continue

            results.append({
                "head": head,
                "relation": relation,
                "tail": tail,
                "evidence": evidence,
                "source_type": source_type,
                "source_title": source_title,
                "timestamp": now,
                "confidence": get_source_weight(source_type),
                "head_type": self._infer_node_type(head, relation, "head"),
                "tail_type": self._infer_node_type(tail, relation, "tail"),
                "structure_axis": axis,
            })

        return results

    def ingest_text_documents(
        self,
        documents: list[dict[str, Any]],
        root_concept: str | None = None,
    ) -> int:
        """批量摄入文本文档，抽取 claim 并写入 SAG。

        Args:
            documents: [{"text": str, "source_type": str, "source_title": str, "timestamp": str}]
            root_concept: 根概念提示

        Returns:
            成功添加的 claim 数量
        """
        documents = list(documents) if isinstance(documents, (list, tuple)) else []
        if not documents:
            return 0

        added = 0
        for doc in documents:
            if not isinstance(doc, Mapping):
                continue
            try:
                claims = self.extract_claims_from_text(
                    text=doc.get("text"),
                    source_type=doc.get("source_type", SourceType.WEB_SEARCH.value),
                    source_title=doc.get("source_title", "untitled"),
                    timestamp=doc.get("timestamp"),
                    root_concept=root_concept,
                )
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.warning("文档 claim 抽取失败，跳过: %s", doc.get("source_title"), exc_info=True)
                continue
            for c in claims:
                try:
                    head_resolved = self.entity_resolver.resolve(c["head"])
                    tail_resolved = self.entity_resolver.resolve(c["tail"])
                except (AttributeError, KeyError, TypeError, ValueError):
                    head_resolved = None
                    tail_resolved = None
                metadata: dict[str, Any] = {
                    "structure_axis": c.get("structure_axis"),
                }
                if _finite(getattr(head_resolved, "resolver_confidence", 0.0)) >= 0.5:
                    metadata["head_canonical_name"] = head_resolved.canonical_name
                    metadata["head_resolver_confidence"] = head_resolved.resolver_confidence
                    metadata["head_resolver_reason"] = head_resolved.resolver_reason
                if _finite(getattr(tail_resolved, "resolver_confidence", 0.0)) >= 0.5:
                    metadata["tail_canonical_name"] = tail_resolved.canonical_name
                    metadata["tail_resolver_confidence"] = tail_resolved.resolver_confidence
                    metadata["tail_resolver_reason"] = tail_resolved.resolver_reason

                try:
                    self.kb.add_claim(
                        head=c["head"],
                        relation=c["relation"],
                        tail=c["tail"],
                        evidence=c["evidence"],
                        source_type=c["source_type"],
                        source_title=c["source_title"],
                        timestamp=c["timestamp"],
                        confidence=c["confidence"],
                        head_type=c.get("head_type"),
                        tail_type=c.get("tail_type"),
                        metadata=metadata,
                    )
                    added += 1
                except (AttributeError, KeyError, TypeError, ValueError):
                    logger.warning("写入 claim 失败，跳过: %s", c.get("head"), exc_info=True)

        logger.info("ReportGraphBuilder 摄入 %d 文档，新增 %d claims", len(documents), added)
        return added

    def ingest_tdx_market_data(
        self,
        symbols: list[str],
        source_title: str = "TDX 行情数据",
    ) -> int:
        """从 TDX 获取行情数据，生成 market_data claim。

        Args:
            symbols: 股票代码列表
            source_title: 来源标题

        Returns:
            成功添加的 claim 数量
        """
        symbols = [
            _text(symbol)
            for symbol in symbols
            if symbol is not None and _text(symbol)
        ] if isinstance(symbols, (list, tuple, set)) else []
        if not symbols:
            return 0

        try:
            from infrastructure.data_providers.market.tdx_provider import TdxProvider
            tdx = TdxProvider()
            if not tdx.is_available():
                raise RuntimeError("TDX provider 不可用（pytdx 未安装）")

            df = tdx.get_realtime_quotes(symbols)
            if df is None or getattr(df, "empty", False):
                raise RuntimeError("TDX 行情返回空")
        except Exception as e:
            raise RuntimeError(f"TDX 行情获取失败: {e}") from e

        added = 0
        timestamp = datetime.now().isoformat()
        iterrows = getattr(df, "iterrows", None)
        if not callable(iterrows):
            raise RuntimeError("TDX 行情结果不支持行迭代")
        for _, row in iterrows():
            row = _mapping(row)
            symbol = _text(row.get("symbol"))
            name = _text(row.get("name"))
            if not symbol or not name:
                continue

            # 生成 market_data claim：当前价格和涨跌幅
            price = _finite(row.get("price"))
            change_pct = _finite(row.get("change_pct"))
            if price > 0:
                try:
                    self.kb.add_claim(
                        head=name,
                        relation=RelationType.PERFORMANCE_DRIVER_OF.value,
                        tail=f"市值/估值({symbol})",
                        evidence=f"TDX 实时行情: 价格 {price}, 涨跌幅 {change_pct}%",
                        source_type=SourceType.MARKET_DATA.value,
                        source_title=source_title,
                        timestamp=timestamp,
                        head_type=NodeType.COMPANY.value,
                        tail_type=NodeType.MARKET.value,
                        metadata={"symbol": symbol, "price": price, "change_pct": change_pct},
                    )
                    added += 1
                except (AttributeError, KeyError, TypeError, ValueError):
                    logger.warning("写入 TDX claim 失败，跳过: %s", symbol, exc_info=True)

        logger.info("TDX market data 摄入 %d 条 claim", added)
        return added

    def build_from_sources(
        self,
        documents: list[dict[str, Any]] | None = None,
        symbols: list[str] | None = None,
        root_concept: str | None = None,
    ) -> dict[str, Any]:
        """从多个来源构建 SAG。

        Args:
            documents: 文本文档列表
            symbols: TDX 股票代码列表
            root_concept: 根概念

        Returns:
            {"documents_claims": int, "market_claims": int, "nodes": int, "edges": int}

        Raises:
            RuntimeError: 没有任何可用输入时停止（不降级）
        """
        if not documents and not symbols:
            raise RuntimeError(
                "ReportGraphBuilder: 没有文本输入且没有 TDX 代码，停止运行（不降级做基础版）。"
            )

        doc_claims = 0
        if documents:
            doc_claims = self.ingest_text_documents(documents, root_concept)

        market_claims = 0
        if symbols:
            market_claims = self.ingest_tdx_market_data(symbols)

        if not self.kb.save():
            logger.error("ReportGraphBuilder 持久化失败: %s", self.kb.storage_path)

        return {
            "documents_claims": doc_claims,
            "market_claims": market_claims,
            "nodes": self.kb.node_count(),
            "edges": self.kb.edge_count(),
            "claims": self.kb.claim_count(),
        }
