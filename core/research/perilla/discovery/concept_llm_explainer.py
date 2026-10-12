"""Evidence-bound LLM explanations for Perilla raw terms.

The explainer uses AquaTrader's configured OpenAI-compatible LLM endpoint.
It never searches the web and it only accepts evidence quotes supplied by the
caller. If the LLM is unavailable or returns ungrounded text, callers can keep
the rule-based explanation.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional
from core.utils.llm_usage import (
    check_llm_budget_or_raise,
    estimate_messages_tokens,
    estimate_tokens,
    load_llm_providers,
    record_llm_usage,
)

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _items(value: Any, limit: int | None = None) -> list[Any]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    return values if limit is None else values[:limit]

VALID_ROOTS = {"\u5149", "\u5b58", "\u7b97", "\u7535", "\u539f\u6587\u672a\u8bf4\u660e"}
VALID_TERM_TYPES = {
    "material",
    "component",
    "equipment",
    "technology_route",
    "platform",
    "process",
    "unclassified",
}


@dataclass
class ConceptLLMExplanation:
    """Grounded LLM explanation for one candidate concept."""

    explanation: str
    term_type: str
    root_theme: str
    chain_layer: str
    evidence_quote: str
    confidence: str
    raw_response: Dict[str, Any]
    model_name: str
    status: str = "ok"


class ConceptLLMExplainer:
    """Explain new terms with AquaTrader's internal LLM configuration."""

    def __init__(
        self,
        cache_dir: str | Path = "data/llm_cache/perilla_concept_explanations",
        timeout: int = 20,
    ) -> None:
        try:
            self.cache_dir = Path(cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_enabled = True
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Concept LLM cache disabled: %s", exc)
            self.cache_dir = Path(".")
            self._cache_enabled = False
        try:
            self.timeout = max(1, int(float(timeout)))
        except (TypeError, ValueError, OverflowError):
            self.timeout = 20
        self.api_base = ""
        self.api_key = ""
        self.model_name = ""
        self.providers = []
        self._load_config()

    @property
    def available(self) -> bool:
        return bool(self.providers)

    def explain(self, term: Dict[str, Any]) -> Optional[ConceptLLMExplanation]:
        """Return a grounded explanation, or None when unavailable/invalid."""
        if not self.available or not isinstance(term, Mapping):
            return None

        cache_key = self._cache_key(term)
        cache_path = self.cache_dir / f"{cache_key}.json"
        if self._cache_enabled and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                return self._from_validated(cached, term)
            except Exception as exc:
                logger.debug("Perilla LLM cache read failed: %s", exc)

        try:
            response = self._call_llm(term)
            parsed = self._parse_json(response)
            validated = self._validate(parsed, term)
            if validated:
                if self._cache_enabled:
                    try:
                        cache_path.write_text(
                            json.dumps(validated.raw_response, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                    except (OSError, TypeError, ValueError) as exc:
                        logger.debug("Perilla LLM cache write failed: %s", exc)
            return validated
        except Exception as exc:
            logger.warning("Perilla concept LLM explanation failed: %s", exc)
            return None

    def _load_config(self) -> None:
        try:
            from config.config import Config

            self.api_base = str(getattr(Config, "LLM_API_BASE", "") or "").rstrip("/")
            self.api_key = str(getattr(Config, "LLM_API_KEY", "") or "")
            self.model_name = str(getattr(Config, "LLM_MODEL_NAME", "") or "")
            self.providers = load_llm_providers(self.api_base, self.api_key, self.model_name)
        except Exception:
            self.api_base = ""
            self.api_key = ""
            self.model_name = ""
            self.providers = []

    def _call_llm(self, term: Dict[str, Any]) -> str:
        import requests

        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._user_prompt(term)},
        ]
        prompt_tokens = estimate_messages_tokens(messages)
        check_llm_budget_or_raise(prompt_tokens, 0, source="ConceptLLMExplainer._call_llm")
        last_error: Exception | None = None
        for provider in self.providers:
            headers = {"Content-Type": "application/json"}
            if provider.api_key:
                headers["Authorization"] = f"Bearer {provider.api_key}"
            body = {
                "model": provider.model_name,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            }
            try:
                resp = requests.post(
                    f"{provider.api_base}/chat/completions",
                    json=body,
                    headers=headers,
                    timeout=self.timeout,
                )
                if resp.status_code >= 400 and "response_format" in body:
                    body.pop("response_format", None)
                    resp = requests.post(
                        f"{provider.api_base}/chat/completions",
                        json=body,
                        headers=headers,
                        timeout=self.timeout,
                    )
                resp.raise_for_status()
                data = resp.json()
                content = str(data["choices"][0]["message"]["content"])
                record_llm_usage(
                    source="ConceptLLMExplainer._call_llm",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=estimate_tokens(content),
                    success=True,
                )
                return content
            except Exception as exc:
                last_error = exc
                record_llm_usage(
                    source="ConceptLLMExplainer._call_llm",
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=str(exc),
                )
        raise last_error or RuntimeError("all LLM providers failed")

    @staticmethod
    def _system_prompt() -> str:
        return (
            "\u4f60\u662f AquaTrader \u7684\u8bc1\u636e\u5316\u672f\u8bed\u89e3\u91ca\u5668\u3002"
            "\u53ea\u80fd\u57fa\u4e8e\u7528\u6237\u63d0\u4f9b\u7684\u539f\u6587\u7247\u6bb5\u5224\u65ad\uff0c"
            "\u4e0d\u5141\u8bb8\u4f7f\u7528\u4f60\u81ea\u5df1\u7684\u77e5\u8bc6\u5e93\uff0c"
            "\u4e0d\u5141\u8bb8\u7f16\u9020\u516c\u53f8\u4e1a\u52a1\u3001\u4f9b\u5e94\u5173\u7cfb\u6216\u6295\u8d44\u5efa\u8bae\u3002"
            "\u8f93\u51fa\u5fc5\u987b\u662f JSON\u3002"
        )

    @staticmethod
    def _user_prompt(term: Dict[str, Any]) -> str:
        term = term if isinstance(term, Mapping) else {}
        payload = {
            "term": _text(term.get("term")) or _text(term.get("normalized_term")),
            "normalized_term": _text(term.get("normalized_term")),
            "co_terms": _items(term.get("co_terms"), 8),
            "related_titles": _items(term.get("related_titles"), 5),
            "evidence_quotes": _items(term.get("evidence_quotes"), 5),
            "allowed_root_themes": ["\u5149", "\u5b58", "\u7b97", "\u7535", "\u539f\u6587\u672a\u8bf4\u660e"],
            "allowed_term_types": sorted(VALID_TERM_TYPES),
            "rules": [
                "\u53ea\u80fd\u4f7f\u7528 evidence_quotes \u548c related_titles",
                "evidence_quote \u5fc5\u987b\u4ece evidence_quotes \u4e2d\u9010\u5b57\u590d\u5236",
                "\u539f\u6587\u6ca1\u8bf4\u7684\u5199\u539f\u6587\u672a\u8bf4\u660e",
                "\u4e0d\u5141\u8bb8\u8f93\u51fa confirmed_supplier/probable_supplier",
            ],
            "output_schema": {
                "term_type": "material/component/equipment/technology_route/platform/process/unclassified",
                "root_theme": "\u5149/\u5b58/\u7b97/\u7535/\u539f\u6587\u672a\u8bf4\u660e",
                "chain_layer": "\u4e0d\u8d85\u8fc7 12 \u4e2a\u5b57",
                "explanation": "\u4e0d\u8d85\u8fc7 80 \u4e2a\u5b57\uff0c\u5fc5\u987b\u57fa\u4e8e\u539f\u6587",
                "evidence_quote": "\u5fc5\u987b\u662f evidence_quotes \u4e2d\u7684\u539f\u6587\u7247\u6bb5",
                "confidence": "low/medium/high",
            },
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _parse_json(content: str) -> Dict[str, Any]:
        text = _text(content).strip()
        if not text:
            raise ValueError("LLM response is not a JSON object")
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end])
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("LLM response is not a JSON object")

    def _validate(
        self,
        parsed: Dict[str, Any],
        term: Dict[str, Any],
    ) -> Optional[ConceptLLMExplanation]:
        if not isinstance(parsed, Mapping) or not isinstance(term, Mapping):
            return None
        quotes = [_text(item) for item in _items(term.get("evidence_quotes")) if _text(item)]
        quote = _text(parsed.get("evidence_quote"))
        if quote and not any(quote in original or original in quote for original in quotes):
            return None
        if not quote and quotes:
            quote = quotes[0]

        root = _text(parsed.get("root_theme")) or "\u539f\u6587\u672a\u8bf4\u660e"
        if root not in VALID_ROOTS:
            root = "\u539f\u6587\u672a\u8bf4\u660e"

        term_type = _text(parsed.get("term_type")) or "unclassified"
        if term_type not in VALID_TERM_TYPES:
            term_type = "unclassified"

        model_explanation = _text(parsed.get("explanation")).strip()
        if not model_explanation:
            return None
        blocked = (
            "CONFIRMED_SUPPLIER", "PROBABLE_SUPPLIER", "\u5efa\u8bae\u4e70\u5165", "\u5efa\u8bae\u5356\u51fa",
            "\u4e70\u5165", "\u5356\u51fa", "\u4ed3\u4f4d", "\u6b62\u76c8", "\u6b62\u635f",
        )
        if any(item in model_explanation for item in blocked):
            return None

        confidence = (_text(parsed.get("confidence")) or "low").lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"

        term_name = _text(term.get("normalized_term")) or _text(term.get("term"))
        layer = _text(parsed.get("chain_layer")).strip()[:24]
        layer_text = f"/{layer}" if layer else ""
        quote_text = quote[:110] if quote else "\u539f\u6587\u672a\u8bf4\u660e"
        explanation = (
            f"{term_name}: LLM\u4ec5\u57fa\u4e8e\u539f\u6587\u5c06\u5176\u5f52\u7c7b\u4e3a"
            f"{term_type}{layer_text}\uff0c\u4f9d\u636e\uff1a{quote_text}"
        )

        raw_response = dict(parsed)
        raw_response["_model_name"] = self.model_name
        raw_response["_model_explanation"] = model_explanation
        return ConceptLLMExplanation(
            explanation=explanation[:160],
            term_type=term_type,
            root_theme=root,
            chain_layer=layer,
            evidence_quote=quote,
            confidence=confidence,
            raw_response=raw_response,
            model_name=self.model_name,
        )

    def _from_validated(
        self,
        parsed: Dict[str, Any],
        term: Dict[str, Any],
    ) -> Optional[ConceptLLMExplanation]:
        result = self._validate(parsed, term)
        if result:
            result.status = "cache_hit"
        return result

    def _cache_key(self, term: Dict[str, Any]) -> str:
        term = term if isinstance(term, Mapping) else {}
        raw = {
            "model": self.model_name,
            "term": _text(term.get("normalized_term")) or _text(term.get("term")),
            "quotes": _items(term.get("evidence_quotes"), 5),
            "titles": _items(term.get("related_titles"), 5),
        }
        return hashlib.sha1(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
