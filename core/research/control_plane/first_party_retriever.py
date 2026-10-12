"""第一方检索专线编排器。

Phase 3：统一编排 CNINFO 公告 + 上交所/深交所互动平台 + 公司官网投资者关系，
当 Claim 缺第一方来源时优先定向第一方渠道。

设计原则：
- KISS：聚合多个 provider，统一 search() 接口
- DRY：复用现有 CninfoProvider，扩展而非重写
- 防御性：每个 provider 独立超时/限流/健康状态，一个失败不阻断其他
- 不抓取非第一方来源：龙虎榜/K线/成交数据不进入此专线

来源状态机：
- unavailable: provider 不可用（网络/认证/robots 禁止）
- degraded: provider 可用但返回部分结果或限流
- ok: provider 正常返回
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from core.research.perilla.discovery.models import SourceDocument
from .contracts import _as_mapping, _safe_bool, _safe_float, _safe_int, _safe_text

logger = logging.getLogger(__name__)


@dataclass
class SourceStatus:
    """单个 provider 的来源状态。

    Attributes:
        name: provider 名称。
        status: unavailable / degraded / ok
        last_check_at: 上次健康检查时间。
        last_error: 上次错误信息（status=unavailable/degraded 时）。
        consecutive_failures: 连续失败次数。
    """

    name: str
    status: str = "unknown"
    last_check_at: str = ""
    last_error: str = ""
    consecutive_failures: int = 0
    error_code: str = ""
    robots_policy: str = "unknown"
    license_policy: str = "unknown"
    rate_limit_policy: str = "default"
    timeout_seconds: float = 0.0


@dataclass
class FirstPartyRetrievalResult:
    """第一方专线检索结果。

    Attributes:
        documents: 合并后的 SourceDocument 列表。
        source_status: 每个 provider 的状态。
        reason_code: stopped/failed 原因码：
            first_party_ok / first_party_degraded / first_party_unavailable /
            first_party_no_code / first_party_no_terms
        skipped_providers: 因不可用而跳过的 provider 列表。
    """

    documents: list[SourceDocument] = field(default_factory=list)
    source_status: dict[str, SourceStatus] = field(default_factory=dict)
    reason_code: str = "first_party_ok"
    skipped_providers: list[str] = field(default_factory=list)
    degradation_codes: list[str] = field(default_factory=list)


class FirstPartyRetriever:
    """第一方检索专线编排器。

    聚合多个第一方 provider，按 Claim 缺口优先定向：
    1. CNINFO 公告（含年报/半年报/季报）
    2. 上交所互动平台
    3. 深交所互动平台
    4. 公司官网投资者关系页面

    每个 provider 独立调用，独立超时，独立健康状态。
    一个 provider 失败不阻断其他 provider。
    """

    # 单 provider 调用超时（秒）
    DEFAULT_PROVIDER_TIMEOUT: float = 12.0
    # 连续失败 N 次后标记为 unavailable
    MAX_CONSECUTIVE_FAILURES: int = 3
    # unavailable 后的冷却时间（秒）
    COOLDOWN_SECONDS: int = 300

    def __init__(
        self,
        cninfo_pdf_retriever: Any | None = None,
        *,
        interactive_providers: list[Any] | None = None,
        company_site_provider: Any | None = None,
        timeout: float | None = None,
    ) -> None:
        """初始化第一方专线编排器。

        Args:
            cninfo_pdf_retriever: CninfoPdfRetriever 实例（公告 PDF 检索）。
            interactive_providers: 互动平台 provider 列表（上交所/深交所）。
            company_site_provider: 公司官网投资者关系 provider。
            timeout: 单 provider 超时覆盖。
        """
        self.cninfo_pdf_retriever = cninfo_pdf_retriever
        self.interactive_providers = list(interactive_providers) if isinstance(interactive_providers, (list, tuple)) else []
        self.company_site_provider = company_site_provider
        self.timeout = max(3.0, _safe_float(timeout, self.DEFAULT_PROVIDER_TIMEOUT))
        self._status: dict[str, SourceStatus] = {}
        self._lock = threading.RLock()
        # 初始化所有 provider 状态
        for provider in self._all_providers():
            name = self._provider_name(provider)
            self._status[name] = SourceStatus(name=name, status="ok")

    def _all_providers(self) -> list[Any]:
        """返回所有已配置的 provider（不含 None）。"""
        providers: list[Any] = []
        if self.cninfo_pdf_retriever is not None:
            providers.append(self.cninfo_pdf_retriever)
        providers.extend(self.interactive_providers)
        if self.company_site_provider is not None:
            providers.append(self.company_site_provider)
        unique: list[Any] = []
        seen: set[int] = set()
        for provider in providers:
            if provider is None or not callable(getattr(provider, "search", None)):
                continue
            marker = id(provider)
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(provider)
        return unique

    @staticmethod
    def _provider_name(provider: Any) -> str:
        """获取 provider 名称。"""
        name = _safe_text(getattr(provider, "name", None))
        return name or type(provider).__name__

    def search(
        self,
        query: str,
        *,
        tier: str = "first_party",
        limit: int = 5,
        context: dict[str, Any] | None = None,
    ) -> FirstPartyRetrievalResult:
        """第一方专线检索。

        Args:
            query: 检索查询（通常含股票代码）。
            tier: 检索层级，固定为 first_party，兼容现有 retriever 接口。
            limit: 最大返回文档数。

        Returns:
            FirstPartyRetrievalResult：合并后的文档 + 每个 provider 状态。
        """
        result = FirstPartyRetrievalResult(source_status=self.get_status())

        query = _safe_text(query)
        tier = _safe_text(tier, "first_party")
        limit = max(1, _safe_int(limit, 5))
        context = _as_mapping(context)
        if not query:
            result.reason_code = "first_party_no_terms"
            return result

        # 按 provider 独立调用，独立超时
        all_documents: list[SourceDocument] = []
        for provider in self._all_providers():
            name = self._provider_name(provider)
            self._refresh_health(name, provider)
            if not self._is_available(name):
                result.skipped_providers.append(name)
                result.degradation_codes.append(f"{name}:unavailable")
                continue

            try:
                # 调用 provider 的 search 方法
                docs = self._call_provider(
                    provider, query, tier=tier, limit=limit, context=context
                )
                if docs:
                    all_documents.extend(docs)
                    self._mark_success(name)
                else:
                    # 空结果不算失败，标记为 degraded
                    self._mark_degraded(name, "empty_result")
                    result.degradation_codes.append(f"{name}:empty_result")
            except Exception as exc:
                logger.warning(
                    "第一方 provider %s 调用失败: %s", name, exc
                )
                error_code = self._error_code(exc)
                self._mark_failure(name, str(exc), error_code)
                result.skipped_providers.append(name)
                result.degradation_codes.append(f"{name}:{error_code}")

        # URL 去重
        seen_documents: set[str] = set()
        deduped: list[SourceDocument] = []
        for doc in all_documents:
            url = _safe_text(getattr(doc, "url", None))
            document_key = url or _safe_text(getattr(doc, "document_id", None))
            if document_key and document_key in seen_documents:
                continue
            if document_key:
                seen_documents.add(document_key)
            deduped.append(doc)
            if len(deduped) >= limit:
                break

        result.documents = deduped
        result.source_status = self.get_status()

        # 综合 reason_code
        if not result.documents:
            if not result.skipped_providers:
                result.reason_code = "first_party_no_results"
            elif len(result.skipped_providers) == len(self._all_providers()):
                result.reason_code = "first_party_unavailable"
            else:
                result.reason_code = "first_party_degraded"
        elif result.skipped_providers:
            result.reason_code = "first_party_degraded"
        else:
            result.reason_code = "first_party_ok"

        return result

    def _call_provider(
        self,
        provider: Any,
        query: str,
        *,
        tier: str,
        limit: int,
        context: dict[str, Any] | None = None,
    ) -> list[SourceDocument]:
        result: dict[str, Any] = {}

        def invoke() -> None:
            try:
                result["documents"] = self._call_provider_direct(
                    provider, query, tier=tier, limit=limit, context=context
                )
            except Exception as exc:
                result["error"] = exc

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        timeout_seconds = max(
            0.01, _safe_float(self.timeout, self.DEFAULT_PROVIDER_TIMEOUT)
        )
        worker.join(timeout_seconds)
        if worker.is_alive():
            raise TimeoutError(f"provider timed out after {timeout_seconds:.2f}s")
        error = result.get("error")
        if isinstance(error, Exception):
            raise error
        documents = result.get("documents")
        return documents if isinstance(documents, list) else []

    def _call_provider_direct(
        self,
        provider: Any,
        query: str,
        *,
        tier: str,
        limit: int,
        context: dict[str, Any] | None = None,
    ) -> list[SourceDocument]:
        """调用单个 provider 的 search 方法。

        兼容 CninfoPdfRetriever.search(query, tier, limit) 和
        InteractiveProvider.search(query, limit) 两种签名。
        """
        import inspect

        sig = inspect.signature(provider.search)
        params = sig.parameters
        # 优先尝试 (query, tier, limit) 签名（CninfoPdfRetriever）
        kwargs: dict[str, Any] = {"limit": limit}
        if "tier" in params:
            kwargs["tier"] = tier
        if "context" in params:
            kwargs["context"] = context or {}
        if "timeout" in params:
            kwargs["timeout"] = self.timeout
        if "tier" in params or "context" in params or "timeout" in params:
            raw_result = provider.search(query, **kwargs) or []
        else:
            # 兼容 (query, limit) 签名
            raw_result = provider.search(query, limit=limit) or []
        if isinstance(raw_result, Mapping):
            raw_result = raw_result.get("documents", [])
        raw_documents = list(raw_result) if isinstance(raw_result, (list, tuple)) else []
        return [
            normalized
            for document in raw_documents
            if (normalized := _complete_source_document(
                document, provider=self._provider_name(provider)
            )) is not None
        ]

    def _refresh_health(self, name: str, provider: Any) -> None:
        """同步 provider 自身的健康/治理声明，不让单个健康检查阻断检索。"""
        health = getattr(provider, "health", None)
        if not callable(health):
            return
        try:
            payload = health() or {}
            status = self._status.setdefault(name, SourceStatus(name=name))
            status.last_check_at = _now_iso()
            if not isinstance(payload, Mapping):
                payload = {}
            if "available" in payload and not _safe_bool(payload.get("available")):
                status.status = "unavailable"
                status.error_code = _safe_text(
                    payload.get("error_code"), "provider_unavailable"
                )
                status.last_error = _safe_text(
                    payload.get("last_error"), status.error_code
                )
            elif status.status == "unknown":
                status.status = "ok"
            for field_name in (
                "robots_policy", "license_policy", "rate_limit_policy",
            ):
                if payload.get(field_name) is not None:
                    setattr(status, field_name, _safe_text(payload[field_name]))
            if payload.get("timeout_seconds") is not None:
                status.timeout_seconds = max(
                    0.0, _safe_float(payload["timeout_seconds"])
                )
        except Exception as exc:
            self._mark_failure(name, str(exc), self._error_code(exc))

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        """将 provider 异常归一为可审计的降级码。"""
        error_name = type(exc).__name__.lower()
        error_text = str(exc).lower()
        if isinstance(exc, TimeoutError) or "timeout" in error_name or "timed out" in error_text:
            return "provider_timeout"
        if "rate" in error_name or "rate limit" in error_text or "429" in error_text:
            return "provider_rate_limited"
        if "robot" in error_name or "robot" in error_text:
            return "robots_blocked"
        if "licen" in error_name or "licen" in error_text:
            return "license_denied"
        return "provider_error"

    def _is_available(self, name: str) -> bool:
        """检查 provider 是否可用（未被冷却）。"""
        with self._lock:
            status = self._status.get(name)
            if status is None:
                return True
            if status.status == "unavailable":
                # 检查冷却时间是否已过
                if status.last_check_at:
                    try:
                        from datetime import datetime, timezone

                        last = datetime.fromisoformat(
                            status.last_check_at.replace("Z", "+00:00")
                        )
                        elapsed = (
                            datetime.now(timezone.utc) - last
                        ).total_seconds()
                        if elapsed >= self.COOLDOWN_SECONDS:
                            # 冷却结束，恢复 ok 状态
                            status.status = "ok"
                            status.consecutive_failures = 0
                            status.last_error = ""
                            return True
                        return False
                    except Exception:
                        return False
                return False
            return True

    def _mark_success(self, name: str) -> None:
        """标记 provider 调用成功。"""
        with self._lock:
            status = self._status.get(name)
            if status is None:
                status = SourceStatus(name=name, status="ok")
                self._status[name] = status
            status.status = "ok"
            status.consecutive_failures = 0
            status.last_error = ""
            status.error_code = ""
            status.last_check_at = _now_iso()

    def _mark_degraded(self, name: str, reason: str) -> None:
        """标记 provider 降级（部分结果或限流）。"""
        with self._lock:
            status = self._status.get(name)
            if status is None:
                status = SourceStatus(name=name, status="degraded")
                self._status[name] = status
            status.status = "degraded"
            status.last_error = reason
            status.error_code = reason
            status.last_check_at = _now_iso()

    def _mark_failure(
        self, name: str, error: str, error_code: str = "provider_error"
    ) -> None:
        """标记 provider 调用失败，连续失败超过阈值后进入 unavailable。"""
        with self._lock:
            status = self._status.get(name)
            if status is None:
                status = SourceStatus(name=name, status="degraded")
                self._status[name] = status
            status.consecutive_failures += 1
            status.last_error = error
            status.error_code = error_code or "provider_error"
            status.last_check_at = _now_iso()
            if status.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                status.status = "unavailable"

    def get_status(self) -> dict[str, SourceStatus]:
        """返回所有 provider 的当前状态快照。"""
        with self._lock:
            return {name: replace(status) for name, status in self._status.items()}


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _complete_source_document(
    document: Any,
    *,
    provider: str,
    source_type: str | None = None,
    fallback_url: str = "",
) -> SourceDocument | None:
    """将 provider 返回值补齐为可追溯的 ``SourceDocument``。

    Provider 适配器经常只返回正文和 URL。这里补齐稳定 ID、抓取时间、
    文本指纹及 lineage；缺失字段只触发安全降级，不阻断其它 provider。
    """
    if isinstance(document, SourceDocument):
        normalized = document
    elif isinstance(document, Mapping):
        try:
            normalized = SourceDocument(
                document_id=str(document.get("document_id") or ""),
                source_type=str(document.get("source_type") or "first_party"),
                url=str(document.get("url") or ""),
                title=str(document.get("title") or ""),
                site_name=str(document.get("site_name") or ""),
                published_at=document.get("published_at"),
                fetched_at=str(document.get("fetched_at") or ""),
                content_text=str(document.get("content_text") or document.get("text") or ""),
                raw_html_path=document.get("raw_html_path"),
                text_hash=str(document.get("text_hash") or ""),
                metadata=_as_mapping(document.get("metadata")),
                credibility_hint=str(document.get("credibility_hint") or "unknown"),
            )
        except Exception:
            return None
    else:
        # 不接受任意对象直接透传，避免后续 Extractor 看到不完整的伪文档。
        try:
            normalized = SourceDocument(
                document_id=str(getattr(document, "document_id", "") or ""),
                source_type=str(getattr(document, "source_type", "") or "first_party"),
                url=str(getattr(document, "url", "") or ""),
                title=str(getattr(document, "title", "") or ""),
                site_name=str(getattr(document, "site_name", "") or ""),
                published_at=getattr(document, "published_at", None),
                fetched_at=str(getattr(document, "fetched_at", "") or ""),
                content_text=str(
                    getattr(document, "content_text", "")
                    or getattr(document, "text", "")
                    or ""
                ),
                raw_html_path=getattr(document, "raw_html_path", None),
                text_hash=str(getattr(document, "text_hash", "") or ""),
                metadata=_as_mapping(getattr(document, "metadata", {})),
                credibility_hint=str(
                    getattr(document, "credibility_hint", "unknown") or "unknown"
                ),
            )
        except Exception:
            return None

    normalized.url = _safe_text(normalized.url, _safe_text(fallback_url))
    normalized.content_text = _safe_text(normalized.content_text)
    normalized.title = _safe_text(normalized.title)
    normalized.document_id = _safe_text(normalized.document_id)
    normalized.fetched_at = _safe_text(normalized.fetched_at)
    normalized.text_hash = _safe_text(normalized.text_hash)
    normalized.published_at = _safe_text(normalized.published_at) or None
    if not normalized.content_text and not normalized.title:
        return None
    normalized.source_type = _safe_text(
        source_type if source_type is not None else normalized.source_type,
        "first_party",
    )
    normalized.site_name = _safe_text(normalized.site_name)
    normalized.credibility_hint = _safe_text(normalized.credibility_hint, "unknown")
    identity = "|".join((provider, normalized.url, normalized.title, normalized.content_text))
    if not normalized.document_id:
        normalized.document_id = f"{provider}_" + hashlib.sha1(
            identity.encode("utf-8")
        ).hexdigest()[:20]
    if not normalized.fetched_at:
        normalized.fetched_at = _now_iso()
    if not normalized.text_hash:
        normalized.text_hash = hashlib.sha256(
            normalized.content_text.encode("utf-8")
        ).hexdigest()
    metadata = _as_mapping(normalized.metadata)
    metadata.setdefault(
        "source_lineage_id",
        hashlib.sha1((normalized.url or normalized.document_id).encode("utf-8")).hexdigest()[:20],
    )
    metadata.setdefault("provider", provider)
    metadata.setdefault("availability_status", "ok")
    normalized.metadata = metadata
    return normalized


class InteractivePlatformProvider:
    """互动平台 provider 基类（上交所/深交所）。

    子类需实现 _fetch_questions / _fetch_answers 方法。
    当前为占位实现：返回空列表，标记为 degraded。
    实际接入时需补充具体 API 调用。
    """

    name: str = "interactive_base"

    def __init__(
        self,
        *,
        market_provider: Any | None = None,
        request_timeout: float = 10.0,
        rate_limit_per_minute: int = 20,
    ) -> None:
        """初始化互动平台 provider。

        Args:
            request_timeout: 单次请求超时（秒）。
            rate_limit_per_minute: 每分钟最大请求数。
        """
        self.market_provider = market_provider
        self.request_timeout = max(3.0, _safe_float(request_timeout, 10.0))
        self.rate_limit_per_minute = max(1, _safe_int(rate_limit_per_minute, 20))
        self._last_request_time: float = 0.0
        self._request_count: int = 0
        self._window_start: float = time.monotonic()

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        context: dict[str, Any] | None = None,
    ) -> list[SourceDocument]:
        """检索互动平台问答。

        Args:
            query: 检索查询（通常含股票代码或公司名）。
            limit: 最大返回文档数。

        Returns:
            SourceDocument 列表；当前为占位实现返回空列表。
        """
        # 占位实现：实际接入时需调用具体平台 API
        # 当前返回空列表，由 FirstPartyRetriever 标记为 degraded
        logger.info(
            "InteractivePlatformProvider %s search: 占位实现，返回空列表",
            self.name,
        )
        return []

    def health(self) -> dict[str, Any]:
        available = bool(
            self.market_provider is not None
            and callable(getattr(self.market_provider, "get_investor_interactions", None))
        )
        checker = getattr(self.market_provider, "is_available", None)
        if available and callable(checker):
            try:
                available = _safe_bool(checker(), False)
            except Exception:
                available = False
        return {
            "name": self.name,
            "available": available,
            "error_code": "provider_not_injected" if not available else "",
            "robots_policy": "allow",
            "license_policy": "official",
            "rate_limit_policy": f"{self.rate_limit_per_minute}/minute",
            "timeout_seconds": self.request_timeout,
        }

    def _search_interactions(
        self,
        query: str,
        *,
        expected_market: str,
        limit: int,
        context: dict[str, Any] | None = None,
    ) -> list[SourceDocument]:
        if self.market_provider is None:
            return []
        code_match = re.search(r"\d{6}", str(query or ""))
        if not code_match:
            return []
        self._respect_rate_limit()
        safe_limit = max(1, _safe_int(limit, 5))
        rows = self.market_provider.get_investor_interactions(
            code_match.group(0), count=safe_limit
        ) or []
        rows = rows if isinstance(rows, list) else []
        context = _as_mapping(context)
        as_of = _safe_text(context.get("as_of"))[:10]
        documents: list[SourceDocument] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            market = str(row.get("market") or expected_market).lower()
            if expected_market == "sse" and market not in {"sse", "sseinfo"}:
                continue
            if expected_market == "cninfo_irm" and market not in {
                "cninfo_irm", "szse", "szse_irm",
            }:
                continue
            published_at = str(row.get("date") or row.get("published_at") or "")
            if as_of and published_at[:10] and published_at[:10] > as_of:
                continue
            document = self._interaction_document(row, expected_market)
            if document is not None:
                documents.append(document)
        return documents[:safe_limit]

    def _interaction_document(
        self, row: dict[str, Any], expected_market: str
    ) -> SourceDocument | None:
        question = str(row.get("question") or row.get("content") or "").strip()
        answer = str(row.get("answer") or row.get("reply") or "").strip()
        if not question and not answer:
            return None
        url = str(row.get("url") or "").strip()
        question_id = str(row.get("question_id") or row.get("id") or "")
        published_at = str(row.get("date") or row.get("published_at") or "")
        raw_key = "|".join((expected_market, url, question_id, published_at, question, answer))
        lineage = hashlib.sha1((url or raw_key).encode("utf-8")).hexdigest()[:20]
        document_id = "interaction_" + hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:20]
        content = "\n".join(
            part for part in (
                f"投资者问题：{question}" if question else "",
                f"公司回复：{answer}" if answer else "",
            ) if part
        )
        return SourceDocument(
            document_id=document_id,
            source_type="investor_interaction",
            url=url,
            title="投资者互动问答",
            site_name=str(row.get("source") or expected_market),
            published_at=published_at or None,
            fetched_at=_now_iso(),
            content_text=content,
            text_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            credibility_hint="high",
            metadata={
                "market": expected_market,
                "question_id": question_id,
                "source_backend": str(row.get("source") or expected_market),
                "source_lineage_id": lineage,
                "provider": self.name,
                "robots_policy": "allow",
                "license_policy": "official",
                "rate_limit_policy": f"{self.rate_limit_per_minute}/minute",
                "availability_status": "ok",
            },
        )

    def _respect_rate_limit(self) -> None:
        """尊重限流策略，必要时等待。"""
        now = time.monotonic()
        elapsed = now - self._window_start
        if elapsed >= 60.0:
            self._request_count = 0
            self._window_start = now
        if self._request_count >= self.rate_limit_per_minute:
            sleep_seconds = 60.0 - elapsed
            if sleep_seconds > 0:
                time.sleep(min(sleep_seconds, 5.0))
            self._request_count = 0
            self._window_start = time.monotonic()
        self._request_count += 1


class SseInteractiveProvider(InteractivePlatformProvider):
    """上交所互动平台 provider。"""

    name = "sse_interactive"

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        context: dict[str, Any] | None = None,
    ) -> list[SourceDocument]:
        return self._search_interactions(
            query, expected_market="sse", limit=limit, context=context
        )


class SzseInteractiveProvider(InteractivePlatformProvider):
    """深交所互动平台 provider。"""

    name = "szse_interactive"

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        context: dict[str, Any] | None = None,
    ) -> list[SourceDocument]:
        return self._search_interactions(
            query, expected_market="cninfo_irm", limit=limit, context=context
        )


class CompanyIRSiteProvider(InteractivePlatformProvider):
    """公司官网投资者关系页面 provider。"""

    name = "company_ir_site"

    def __init__(
        self,
        *,
        crawler: Any | None = None,
        resolver: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.crawler = crawler
        self.resolver = resolver

    def health(self) -> dict[str, Any]:
        available = callable(getattr(self.crawler, "crawl_url", None)) or callable(self.resolver)
        return {
            "name": self.name,
            "available": available,
            "error_code": "explicit_ir_url_required" if not available else "",
            "robots_policy": "check_per_url",
            "license_policy": "official_site_only",
            "rate_limit_policy": f"{self.rate_limit_per_minute}/minute",
            "timeout_seconds": self.request_timeout,
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        context: dict[str, Any] | None = None,
    ) -> list[SourceDocument]:
        context = _as_mapping(context)
        urls = context.get("official_ir_urls") or context.get("company_ir_urls") or []
        if not urls and callable(self.resolver):
            try:
                resolved = self.resolver(query=query, context=context)
            except TypeError:
                try:
                    resolved = self.resolver(query)
                except Exception as exc:
                    logger.warning("解析公司官网地址失败: %s", exc)
                    resolved = []
            except Exception as exc:
                logger.warning("解析公司官网地址失败: %s", exc)
                resolved = []
            urls = resolved or []
        if isinstance(urls, str):
            urls = [urls]
        if not urls or self.crawler is None:
            return []
        documents: list[SourceDocument] = []
        safe_limit = max(1, _safe_int(limit, 5))
        for raw_url in list(urls)[:safe_limit]:
            url = str(raw_url or "").strip()
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            self._respect_rate_limit()
            try:
                document = self.crawler.crawl_url(url)
            except Exception as exc:
                logger.warning("公司官网页面抓取失败(%s): %s", url, exc)
                continue
            if document is None:
                continue
            document = _complete_source_document(
                document,
                provider=self.name,
                source_type="official_site",
                fallback_url=url,
            )
            if document is None:
                continue
            metadata = _as_mapping(document.metadata)
            lineage = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
            metadata.update({
                "source_lineage_id": metadata.get("source_lineage_id") or lineage,
                "provider": self.name,
                "source_backend": "explicit_company_ir_url",
                "robots_policy": metadata.get("robots_policy", "check_per_url"),
                "license_policy": metadata.get("license_policy", "official_site_only"),
                "rate_limit_policy": f"{self.rate_limit_per_minute}/minute",
                "availability_status": "ok",
            })
            document.metadata = metadata
            document.source_type = "official_site"
            documents.append(document)
        return documents
