"""P4A 多智能体对抗式审查 — Skeptic / EvidenceReview / PortfolioTranslator。

hybrid 模式：
- SkepticAgent：LLM 优先用 AquaLLM 生成反方审查，失败时降级到六类规则攻击
- EvidenceReviewAgent：skepticism_score 始终用规则计算（确定性，不接 LLM）
- PortfolioTranslator：LLM 优先用 AquaLLM 翻译，失败时降级到规则模板

watchlist_action 限定 ignore/monitor/research_required/watchlist_candidate。
禁止输出买入/卖出/强烈推荐。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.llm_processing.llm_cache import LLMCache
from core.llm_processing.prompt_templates import (
    P4A_PROMPT_VERSION,
    build_p4a_portfolio_prompt,
    build_p4a_skeptic_prompt,
    check_compliance,
    get_p4a_portfolio_system_prompt,
    get_p4a_skeptic_system_prompt,
)

logger = logging.getLogger(__name__)

_DEFAULT_INPUT_DIR = Path("data/reports/perilla_discovery")

# watchlist_action 限定
_ALLOWED_WATCHLIST_ACTIONS: Set[str] = {
    "ignore", "monitor", "research_required", "watchlist_candidate",
}

# 禁用词
_FORBIDDEN_WORDS = ["买入", "卖出", "强烈推荐"]

_DEFAULT_CACHE_DIR = Path("data/llm_cache/extractions")


def _env_nonnegative_int(name: str, default: int) -> int:
    try:
        return max(0, int(float(os.getenv(name, str(default)) or default)))
    except (TypeError, ValueError, OverflowError):
        return default


_MAX_LLM_ITEMS = _env_nonnegative_int("AQUARESEARCH_LLM_MAX_ITEMS", 20)

# 六类风险攻击模板
_RISK_TEMPLATES = [
    {
        "challenge_type": "single_source_risk",
        "challenge_text": "该假设的证据来源单一，缺少多来源交叉验证。",
        "severity": "medium",
        "suggested_validation_task": "搜索其他研报/公告/新闻是否支持该假设",
    },
    {
        "challenge_type": "weak_relation",
        "challenge_text": "该假设仅基于共现关系，缺少实质性的产业链上下游关系证据。",
        "severity": "high",
        "suggested_validation_task": "搜索产业链关系、供应商名单、客户名单",
    },
    {
        "challenge_type": "lack_official_evidence",
        "challenge_text": "缺少公告、官网、客户认证等官方证据支持。",
        "severity": "high",
        "suggested_validation_task": "搜索公司公告、官网信息、客户认证进展",
    },
    {
        "challenge_type": "crowding",
        "challenge_text": "市场可能已经交易该逻辑，需评估预期差。",
        "severity": "medium",
        "suggested_validation_task": "搜索相关股票涨幅、机构持仓、研报覆盖度",
    },
    {
        "challenge_type": "tech_substitution",
        "challenge_text": "技术路线可能被替代方案取代。",
        "severity": "medium",
        "suggested_validation_task": "搜索替代技术路线的进展和对比分析",
    },
    {
        "challenge_type": "missing_company_mapping",
        "challenge_text": "假设未明确映射到具体受益公司。",
        "severity": "low",
        "suggested_validation_task": "搜索相关产业链公司名单和业务占比",
    },
]


def _short_hash(text: str) -> str:
    """计算短哈希。"""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    """当前时间 ISO 格式。"""
    return datetime.now().isoformat()


def _contains_forbidden(text: str) -> bool:
    """检查文本是否含禁用词。"""
    return any(word in str(text) for word in _FORBIDDEN_WORDS)


def _local_llm_enabled() -> bool:
    """环境变量层面的 LLM 开关。"""
    return os.getenv("LOCAL_LLM_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _mapping_records(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) and not isinstance(value, complex):
        return str(value)
    return ""


def _safe_text_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := _safe_text(item)).strip()]


def _safe_float(
    value: Any,
    default: float = 0.0,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return min(max(number, minimum), maximum)


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


def _call_aqua_llm_json(llm: Any, user_prompt: str, system_prompt: str) -> Optional[Dict[str, Any]]:
    """复用 AquaLLM 的 report 接口调用 JSON LLM。"""
    if llm is None:
        return None
    if hasattr(llm, "generate_json"):
        parsed = llm.generate_json(system_prompt=system_prompt, user_prompt=user_prompt)
        return parsed if isinstance(parsed, dict) else None
    content = llm.generate_report(user_prompt=user_prompt, system_prompt=system_prompt)
    return _parse_json_object(content)


def _cache_get_json(cache: LLMCache, raw_hash: str, prompt_suffix: str, model_name: str) -> Optional[Dict[str, Any]]:
    """读取 JSON LLM 缓存。"""
    cache_key = LLMCache._compute_cache_key(raw_hash, prompt_suffix, P4A_PROMPT_VERSION, model_name)
    cache_file = cache._cache_path(cache_key)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if data.get("prompt_version") != P4A_PROMPT_VERSION:
            return None
        parsed = data.get("parsed_result")
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.debug("P4A LLM 缓存读取失败: %s", exc)
        return None


def _cache_set_json(
    cache: LLMCache,
    raw_hash: str,
    prompt_suffix: str,
    model_name: str,
    parsed: Dict[str, Any],
) -> None:
    """写入 JSON LLM 缓存。"""
    try:
        cache_key = LLMCache._compute_cache_key(raw_hash, prompt_suffix, P4A_PROMPT_VERSION, model_name)
        cache_file = cache._cache_path(cache_key)
        cache_file.write_text(
            json.dumps({
                "raw_hash": raw_hash,
                "chunk_text_hash": prompt_suffix,
                "prompt_version": P4A_PROMPT_VERSION,
                "model_name": model_name,
                "parsed_result": parsed,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("P4A LLM 缓存写入失败: %s", exc)


def _llm_model_name(llm: Any) -> str:
    """尽量从 AquaLLM 或 mock 中取得模型名。"""
    if hasattr(llm, "model_name"):
        return str(getattr(llm, "model_name") or "")
    try:
        from config.config import Config
        return str(getattr(Config, "LLM_MODEL_NAME", "") or "")
    except Exception:
        return ""


class SkepticAgent:
    """反方审查 Agent — hybrid 模式。

    LLM 优先用 AquaLLM 生成反方审查，失败时降级到六类规则攻击。
    """

    def __init__(
        self,
        use_llm: bool = True,
        llm: Any | None = None,
        cache: LLMCache | None = None,
    ) -> None:
        """初始化 SkepticAgent。

        Args:
            use_llm: 是否尝试使用 LLM
            llm: 可选的 AquaLLM/mock 实例
            cache: 可选的 LLMCache
        """
        self.use_llm = use_llm and _local_llm_enabled()
        self.cache = cache or LLMCache(str(_DEFAULT_CACHE_DIR))
        self.llm_available = False
        self.llm = llm
        if self.use_llm and self.llm is not None:
            self.llm_available = True
        elif self.use_llm:
            try:
                from core.utils.llm_client import AquaLLM
                self.llm = AquaLLM()
                self.llm_available = True
            except Exception as exc:
                logger.info("AquaLLM 不可用，SkepticAgent 降级到规则模式: %s", exc)
                self.llm = None

    def review(self, hypotheses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """对假设列表进行反方审查。

        Args:
            hypotheses: 假设列表

        Returns:
            审查列表
        """
        reviews: List[Dict[str, Any]] = []

        for idx, hyp in enumerate(_mapping_records(hypotheses)):
            hyp_id = _safe_text(hyp.get("hypothesis_id"))
            llm_reviews: List[Dict[str, Any]] = []
            if self.llm_available and idx < _MAX_LLM_ITEMS:
                llm_reviews = self._review_with_llm(hyp)

            if llm_reviews:
                reviews.extend(llm_reviews)

            # 规则模式：六类风险攻击；LLM 成功时也补齐缺失风险类型
            covered = {
                _safe_text(r.get("challenge_type"))
                for r in llm_reviews
                if isinstance(r, dict)
            }
            reviews.extend(self._review_by_rules(hyp, covered))

        return reviews

    def _review_by_rules(
        self,
        hyp: Dict[str, Any],
        covered: Set[str] | None = None,
    ) -> List[Dict[str, Any]]:
        """规则模式生成六类风险攻击，可跳过已由 LLM 覆盖的类型。"""
        covered = covered or set()
        hyp_id = _safe_text(hyp.get("hypothesis_id"))
        rule_reviews: List[Dict[str, Any]] = []
        for template in _RISK_TEMPLATES:
            if template["challenge_type"] in covered:
                continue
            review_text = template["challenge_text"]
            if _contains_forbidden(review_text):
                continue

            rule_reviews.append({
                "review_id": f"review_{_short_hash(hyp_id + template['challenge_type'])}",
                "hypothesis_id": hyp_id,
                "challenge_type": template["challenge_type"],
                "challenge_text": review_text,
                "severity": template["severity"],
                "suggested_validation_task": template["suggested_validation_task"],
                "llm_status": "rule_fallback",
            })
        return rule_reviews

    def _review_with_llm(self, hyp: Dict[str, Any]) -> List[Dict[str, Any]]:
        """用 AquaLLM 生成反方审查，失败返回空列表。"""
        if self.llm is None:
            return []
        hyp_id = _safe_text(hyp.get("hypothesis_id"))
        hyp_text = _safe_text(hyp.get("hypothesis_text"))
        evidence_quotes = _safe_text_list(hyp.get("evidence_quotes"))
        prompt = build_p4a_skeptic_prompt(
            hyp_text,
            json.dumps(evidence_quotes, ensure_ascii=False),
        )
        system_prompt = get_p4a_skeptic_system_prompt()
        model_name = _llm_model_name(self.llm)
        raw_hash = _short_hash(hyp_id + hyp_text)
        prompt_suffix = _short_hash("skeptic|" + prompt)

        try:
            parsed = _cache_get_json(self.cache, raw_hash, prompt_suffix, model_name)
            if parsed is None:
                parsed = _call_aqua_llm_json(self.llm, prompt, system_prompt)
                if parsed:
                    _cache_set_json(self.cache, raw_hash, prompt_suffix, model_name, parsed)
            if not parsed or not check_compliance(json.dumps(parsed, ensure_ascii=False)):
                return []

            raw_reviews = parsed.get("reviews", [])
            if isinstance(raw_reviews, dict):
                raw_reviews = [raw_reviews]
            if not isinstance(raw_reviews, list):
                return []

            reviews: List[Dict[str, Any]] = []
            allowed_types = {t["challenge_type"] for t in _RISK_TEMPLATES}
            allowed_severity = {"high", "medium", "low"}
            seen: Set[str] = set()
            for item in raw_reviews:
                if not isinstance(item, dict):
                    continue
                ctype = _safe_text(item.get("challenge_type")).strip()
                text = _safe_text(item.get("challenge_text")).strip()
                suggested = _safe_text(item.get("suggested_validation_task")).strip()
                severity = _safe_text(item.get("severity")) or "medium"
                if ctype not in allowed_types or ctype in seen:
                    continue
                if not text or _contains_forbidden(text) or not check_compliance(text):
                    continue
                if _contains_forbidden(suggested) or not check_compliance(suggested):
                    continue
                if severity not in allowed_severity:
                    severity = "medium"
                seen.add(ctype)
                reviews.append({
                    "review_id": f"review_{_short_hash(hyp_id + ctype + text)}",
                    "hypothesis_id": hyp_id,
                    "challenge_type": ctype,
                    "challenge_text": text,
                    "severity": severity,
                    "suggested_validation_task": suggested,
                    "llm_status": "llm",
                    "llm_model": model_name,
                })
            return reviews
        except Exception as exc:
            logger.warning("P4A Skeptic LLM 审查失败，降级到规则模式: %s", exc)
            return []


class EvidenceReviewAgent:
    """证据审查 Agent — 计算 skepticism_score（始终用规则，确定性）。

    skepticism_score = 0.3*single_source_risk + 0.25*weak_relation_risk +
                       0.2*lack_official_evidence_risk + 0.15*crowding_risk +
                       0.1*missing_company_mapping_risk
    """

    def review(self, hypotheses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """计算每个假设的 skepticism_score。

        Args:
            hypotheses: 假设列表

        Returns:
            skepticism_scores 列表
        """
        scores: List[Dict[str, Any]] = []

        for hyp in _mapping_records(hypotheses):
            hyp_id = _safe_text(hyp.get("hypothesis_id"))
            evidence_quotes = _safe_text_list(hyp.get("evidence_quotes"))
            evidence_docs = _safe_text_list(hyp.get("evidence_document_ids"))
            source_nodes = _safe_text_list(hyp.get("source_nodes"))
            crowding_score = _safe_float(hyp.get("crowding_score"))

            # single_source_risk: 证据来源单一风险（0-1）
            source_count = len(set(evidence_docs))
            single_source_risk = round(max(0.0, 1.0 - source_count / 3.0), 4)

            # weak_relation_risk: 关系薄弱风险（0-1）
            # 证据引用少则风险高
            quote_count = len(evidence_quotes)
            weak_relation_risk = round(max(0.0, 1.0 - quote_count / 3.0), 4)

            # lack_official_evidence_risk: 缺少官方证据风险（0-1）
            # 默认高风险，除非有公告类证据
            lack_official_risk = 0.8  # 默认高风险

            # crowding_risk: 拥挤风险（0-1）
            crowding_risk = round(min(1.0, crowding_score), 4)

            # missing_company_mapping_risk: 公司映射缺失风险（0-1）
            # 如果 source_nodes 中没有公司名，风险高
            has_company = any(
                len(node) > 2 and not node.isupper()
                for node in source_nodes
            )
            missing_company_risk = 0.3 if has_company else 0.8

            # skepticism_score = 0.3*single + 0.25*weak + 0.2*lack_official +
            #                     0.15*crowding + 0.1*missing_company
            skepticism_score = round(
                0.3 * single_source_risk +
                0.25 * weak_relation_risk +
                0.2 * lack_official_risk +
                0.15 * crowding_risk +
                0.1 * missing_company_risk,
                4,
            )

            scores.append({
                "hypothesis_id": hyp_id,
                "skepticism_score": skepticism_score,
                "single_source_risk": single_source_risk,
                "weak_relation_risk": weak_relation_risk,
                "lack_official_evidence_risk": lack_official_risk,
                "crowding_risk": crowding_risk,
                "missing_company_mapping_risk": missing_company_risk,
                "reason": (
                    f"single_source={single_source_risk}, "
                    f"weak_relation={weak_relation_risk}, "
                    f"lack_official={lack_official_risk}, "
                    f"crowding={crowding_risk}, "
                    f"missing_company={missing_company_risk}"
                ),
            })

        return scores


class PortfolioTranslator:
    """组合翻译 Agent — hybrid 模式。

    LLM 优先用 AquaLLM 翻译，失败时降级到规则模板。
    watchlist_action 限定 ignore/monitor/research_required/watchlist_candidate。
    """

    def __init__(
        self,
        use_llm: bool = True,
        llm: Any | None = None,
        cache: LLMCache | None = None,
    ) -> None:
        """初始化 PortfolioTranslator。

        Args:
            use_llm: 是否尝试使用 LLM
            llm: 可选的 AquaLLM/mock 实例
            cache: 可选的 LLMCache
        """
        self.use_llm = use_llm and _local_llm_enabled()
        self.cache = cache or LLMCache(str(_DEFAULT_CACHE_DIR))
        self.llm_available = False
        self.llm = llm
        if self.use_llm and self.llm is not None:
            self.llm_available = True
        elif self.use_llm:
            try:
                from core.utils.llm_client import AquaLLM
                self.llm = AquaLLM()
                self.llm_available = True
            except Exception as exc:
                logger.info("AquaLLM 不可用，PortfolioTranslator 降级到规则模式: %s", exc)
                self.llm = None

    def translate(
        self,
        hypotheses: List[Dict[str, Any]],
        skepticism_scores: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """将假设翻译为组合观察名单动作。

        Args:
            hypotheses: 假设列表
            skepticism_scores: skepticism_scores 列表

        Returns:
            portfolio_translation 列表
        """
        # 建立 hypothesis_id -> skepticism_score 映射
        score_map = {
            _safe_text(score.get("hypothesis_id")): _safe_float(
                score.get("skepticism_score"), default=0.5
            )
            for score in _mapping_records(skepticism_scores)
        }

        translations: List[Dict[str, Any]] = []

        for idx, hyp in enumerate(_mapping_records(hypotheses)):
            hyp_id = _safe_text(hyp.get("hypothesis_id"))
            evidence_score = _safe_float(hyp.get("evidence_score"))
            novelty_score = _safe_float(hyp.get("novelty_score"))
            skepticism = score_map.get(hyp_id, 0.5)

            llm_translation: Optional[Dict[str, Any]] = None
            if self.llm_available and idx < _MAX_LLM_ITEMS:
                llm_translation = self._translate_with_llm(
                    hyp,
                    skepticism,
                    evidence_score,
                    novelty_score,
                )
            if llm_translation:
                translations.append(llm_translation)
                continue

            # 规则模式：基于 evidence_score 和 skepticism_score 决定 watchlist_action
            if skepticism > 0.7:
                action = "ignore"
                trade_meaning = "风险过高，暂不关注"
            elif skepticism > 0.5:
                action = "research_required"
                trade_meaning = "需要更多研究证据"
            elif evidence_score > 0.6 and novelty_score > 0.5:
                action = "watchlist_candidate"
                trade_meaning = "证据较强且新颖度高，列入观察候选"
            elif evidence_score > 0.3:
                action = "monitor"
                trade_meaning = "持续监测证据变化"
            else:
                action = "ignore"
                trade_meaning = "证据不足，暂不关注"

            # 强制限定 watchlist_action 枚举
            if action not in _ALLOWED_WATCHLIST_ACTIONS:
                action = "ignore"

            translations.append({
                "hypothesis_id": hyp_id,
                "watchlist_action": action,
                "trade_meaning": trade_meaning,
                "trigger_conditions": f"skepticism_score < {0.5:.2f} 且 evidence_score > {0.3:.2f}",
                "risk_notes": f"当前 skepticism_score = {skepticism:.4f}",
                "next_research_tasks": "补充官方证据和产业链关系验证",
                "llm_status": "rule_fallback",
            })

        return translations

    def _translate_with_llm(
        self,
        hyp: Dict[str, Any],
        skepticism: float,
        evidence_score: float,
        novelty_score: float,
    ) -> Optional[Dict[str, Any]]:
        """用 AquaLLM 翻译组合观察语言，失败返回 None。"""
        if self.llm is None:
            return None
        hyp_id = _safe_text(hyp.get("hypothesis_id"))
        hyp_text = _safe_text(hyp.get("hypothesis_text"))
        prompt = build_p4a_portfolio_prompt(
            hypothesis_text=hyp_text,
            skepticism_score=f"{skepticism:.4f}",
            evidence_score=f"{evidence_score:.4f}",
            novelty_score=f"{novelty_score:.4f}",
        )
        system_prompt = get_p4a_portfolio_system_prompt()
        model_name = _llm_model_name(self.llm)
        raw_hash = _short_hash(hyp_id + hyp_text)
        prompt_suffix = _short_hash("portfolio|" + prompt)

        try:
            parsed = _cache_get_json(self.cache, raw_hash, prompt_suffix, model_name)
            if parsed is None:
                parsed = _call_aqua_llm_json(self.llm, prompt, system_prompt)
                if parsed:
                    _cache_set_json(self.cache, raw_hash, prompt_suffix, model_name, parsed)
            if not parsed or not check_compliance(json.dumps(parsed, ensure_ascii=False)):
                return None

            payload = parsed.get("portfolio_translation", parsed)
            if not isinstance(payload, dict):
                return None
            action = str(payload.get("watchlist_action", "research_required"))
            if action not in _ALLOWED_WATCHLIST_ACTIONS:
                action = "research_required"

            trade_meaning = _safe_text(payload.get("trade_meaning")).strip()
            trigger_conditions = _safe_text(payload.get("trigger_conditions")).strip()
            risk_notes = _safe_text(payload.get("risk_notes")).strip()
            next_tasks = _safe_text(payload.get("next_research_tasks")).strip()
            text_blob = " ".join([trade_meaning, trigger_conditions, risk_notes, next_tasks])
            if _contains_forbidden(text_blob) or not check_compliance(text_blob):
                return None

            return {
                "hypothesis_id": hyp_id,
                "watchlist_action": action,
                "trade_meaning": trade_meaning or "需要更多研究证据",
                "trigger_conditions": trigger_conditions or "补充官方证据后再评估",
                "risk_notes": risk_notes or f"当前 skepticism_score = {skepticism:.4f}",
                "next_research_tasks": next_tasks or "补充官方证据和产业链关系验证",
                "llm_status": "llm",
                "llm_model": model_name,
            }
        except Exception as exc:
            logger.warning("P4A Portfolio LLM 翻译失败，降级到规则模式: %s", exc)
            return None


def run_adversarial_research_agents(
    input_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """运行 P4A 对抗式审查全流程。

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
        # 加载假设
        hyp_path = base / "research_hypotheses_latest.json"
        if not hyp_path.exists():
            return {
                "module": "adversarial_research_agents",
                "success": False,
                "outputs": [],
                "counts": {},
                "error": "research_hypotheses_latest.json 不存在",
            }

        raw_hypotheses = json.loads(hyp_path.read_text(encoding="utf-8"))
        hypotheses = raw_hypotheses if isinstance(raw_hypotheses, list) else []

        # 1. SkepticAgent 审查
        skeptic = SkepticAgent(use_llm=use_llm)
        reviews = skeptic.review(hypotheses)

        # 2. EvidenceReviewAgent 计算 skepticism_score
        evidence_reviewer = EvidenceReviewAgent()
        skepticism_scores = evidence_reviewer.review(hypotheses)

        # 3. PortfolioTranslator 翻译
        translator = PortfolioTranslator(use_llm=use_llm)
        translations = translator.translate(hypotheses, skepticism_scores)

        # 输出文件
        out.mkdir(parents=True, exist_ok=True)
        outputs: List[str] = []

        reviews_path = out / "adversarial_reviews_latest.json"
        reviews_path.write_text(
            json.dumps(reviews, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(reviews_path))

        scores_path = out / "skepticism_scores_latest.json"
        scores_path.write_text(
            json.dumps(skepticism_scores, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(scores_path))

        translation_path = out / "portfolio_translation_latest.json"
        translation_path.write_text(
            json.dumps(translations, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        outputs.append(str(translation_path))

        return {
            "module": "adversarial_research_agents",
            "success": True,
            "outputs": outputs,
            "counts": {
                "adversarial_reviews": len(reviews),
                "skepticism_scores": len(skepticism_scores),
                "portfolio_translation": len(translations),
            },
        }
    except Exception as exc:
        logger.error("run_adversarial_research_agents 失败: %s", exc, exc_info=True)
        return {
            "module": "adversarial_research_agents",
            "success": False,
            "outputs": [],
            "counts": {},
            "error": str(exc),
        }
