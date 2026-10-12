from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# 全局 LLM 调用速率限制器(线程安全)
# deepseek API 限流策略:RPM 限制,短时间高频请求会触发 ConnectionReset
# 最小间隔 0.3s = 每秒不超过 3 次,留足余量
_LLM_RATE_LIMIT_LOCK = threading.Lock()
_LLM_LAST_CALL_TS: float = 0.0
_LLM_MIN_INTERVAL: float = float(os.environ.get("LLM_MIN_INTERVAL", "0.3"))


def _rate_limit_wait() -> None:
    """速率限制等待,确保两次 LLM 调用间隔不小于 _LLM_MIN_INTERVAL 秒。"""
    global _LLM_LAST_CALL_TS
    with _LLM_RATE_LIMIT_LOCK:
        now = time.monotonic()
        elapsed = now - _LLM_LAST_CALL_TS
        if elapsed < _LLM_MIN_INTERVAL:
            time.sleep(_LLM_MIN_INTERVAL - elapsed)
        _LLM_LAST_CALL_TS = time.monotonic()


@dataclass(frozen=True)
class LLMProviderConfig:
    name: str
    api_base: str
    api_key: str
    model_name: str
    # 模型分层:"flash" 用于高 token 经济任务(介入点1 证据抽取),
    # "pro" 用于高智商任务(介入点2/3/4 突触方向/种子图/反证问题)。
    # 默认空字符串表示不分层,按原顺序降级使用。
    tier: str = ""


@dataclass
class LLMUsageStats:
    call_count: int = 0
    success: int = 0
    failure: int = 0
    prompt_tokens_est: int = 0
    completion_tokens_est: int = 0
    total_tokens_est: int = 0

    def add(
        self,
        prompt_tokens_est: int,
        completion_tokens_est: int,
        success: bool,
    ) -> None:
        self.call_count += 1
        if success:
            self.success += 1
        else:
            self.failure += 1
        self.prompt_tokens_est += max(0, int(prompt_tokens_est))
        self.completion_tokens_est += max(0, int(completion_tokens_est))
        self.total_tokens_est = self.prompt_tokens_est + self.completion_tokens_est

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def estimate_tokens(text: str) -> int:
    """Rough multilingual token estimate for budgeting, not billing."""
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def estimate_messages_tokens(messages: Iterable[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += 4
        total += estimate_tokens(str(message.get("role", "")))
        total += estimate_tokens(str(message.get("content", "")))
    return total + 2


def load_llm_providers(
    primary_base: str = "",
    primary_key: str = "",
    primary_model: str = "",
) -> list[LLMProviderConfig]:
    primary_base = primary_base or _config_value("LLM_API_BASE")
    primary_key = primary_key or _config_value("LLM_API_KEY")
    primary_model = primary_model or _config_value("LLM_MODEL_NAME")
    providers: list[LLMProviderConfig] = []
    if primary_base and primary_model:
        providers.append(
            LLMProviderConfig(
                name="primary",
                api_base=primary_base.rstrip("/"),
                api_key=primary_key,
                model_name=primary_model,
                tier="flash",
            )
        )

    backup_base = _config_value("LLM_BACKUP_API_BASE")
    backup_key = _config_value("LLM_BACKUP_API_KEY")
    backup_model = _config_value("LLM_BACKUP_MODEL_NAME") or primary_model
    if backup_base and backup_model:
        providers.append(
            LLMProviderConfig(
                name="backup",
                api_base=backup_base.rstrip("/"),
                api_key=backup_key,
                model_name=backup_model,
                tier="flash" if "flash" in backup_model.lower() else "pro",
            )
        )
    return providers


def load_llm_providers_by_tier(tier: str) -> list[LLMProviderConfig]:
    """按分层返回 provider 列表,优先返回指定 tier,降级返回其他 tier。

    分层策略:
    - tier="flash":高 token 经济任务(介入点1 证据抽取),优先 flash 模型
    - tier="pro":高智商任务(介入点2/3/4 突触方向/种子图/反证问题),优先 pro 模型

    Args:
        tier: "flash" 或 "pro"。

    Returns:
        provider 列表,指定 tier 优先,其他 tier 作为降级。
    """
    all_providers = load_llm_providers()
    if not all_providers:
        return []
    preferred = [p for p in all_providers if p.tier == tier]
    fallback = [p for p in all_providers if p.tier != tier]
    return preferred + fallback


def check_llm_budget_or_raise(
    prompt_tokens_est: int,
    max_completion_tokens: int = 0,
    source: str = "",
) -> None:
    budget = _int_config("LLM_DAILY_TOKEN_BUDGET", 0)
    if budget <= 0:
        return
    used = read_today_total_tokens()
    projected = used + max(0, prompt_tokens_est) + max(0, max_completion_tokens)
    if projected > budget:
        raise RuntimeError(
            f"LLM rough token budget exceeded: used={used}, "
            f"projected={projected}, budget={budget}, source={source}"
        )


def record_llm_usage(
    *,
    source: str,
    provider: str,
    model: str,
    prompt_tokens_est: int,
    completion_tokens_est: int,
    success: bool,
    cached: bool = False,
    error: str = "",
) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "provider": provider,
        "model": model,
        "prompt_tokens_est": max(0, int(prompt_tokens_est)),
        "completion_tokens_est": max(0, int(completion_tokens_est)),
        "total_tokens_est": max(0, int(prompt_tokens_est)) + max(0, int(completion_tokens_est)),
        "success": bool(success),
        "cached": bool(cached),
        "error": str(error)[:300],
    }
    path = _usage_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:
        logger.warning("failed to write LLM usage log: %s", exc)


def read_today_total_tokens() -> int:
    path = _usage_log_path()
    if not path.exists():
        return 0
    total = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            total += int(json.loads(line).get("total_tokens_est") or 0)
    except Exception as exc:
        logger.warning("failed to read LLM usage log: %s", exc)
    return total


def _usage_log_path() -> Path:
    configured = _config_value("LLM_USAGE_LOG_PATH")
    if configured:
        return Path(configured)
    day = datetime.now().strftime("%Y%m%d")
    return Path("logs") / "llm_usage" / f"{day}.jsonl"


def _config_value(key: str, default: str = "") -> str:
    try:
        from config.setting import Setting

        value = getattr(Setting, key, "")
        if value:
            return str(value)
    except Exception:
        pass
    return os.getenv(key, default)


def _int_config(key: str, default: int) -> int:
    raw = _config_value(key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def call_llm_with_budget(
    messages: list[dict[str, Any]],
    *,
    source: str,
    providers: list[LLMProviderConfig] | None = None,
    temperature: float = 0.0,
    max_completion_tokens: int = 1024,
    response_format_json: bool = False,
    timeout: int = 30,
    allow_no_provider: bool = False,
    tier: str = "",
) -> str:
    """统一的 LLM 调用入口(带预算检查 + 多 provider 降级 + 记账)。

    抽象自 ConceptLLMExplainer._call_llm / AquaLLM._chat_completion_text 的公共模板,
    供 neuron engine / perilla adapter / cold_start 等新接入点复用。

    Args:
        messages: OpenAI Chat Completions 格式的消息列表。
        source: 调用来源标识(用于记账与审计),如 "neuron.target_draft"。
        providers: provider 列表,默认按 tier 从 config 加载。
        temperature: 采样温度,默认 0(确定性输出)。
        max_completion_tokens: 最大 completion tokens,用于预算预估。
        response_format_json: 是否强制 JSON 输出(response_format={"type":"json_object"})。
            不支持 JSON mode 的端点会自动降级重试一次。
        timeout: HTTP 超时秒数。
        allow_no_provider: True 时,无 provider 配置不抛异常而是返回空字符串(用于离线 demo)。
        tier: 模型分层,"flash" 用于高 token 经济任务(介入点1 证据抽取),
            "pro" 用于高智商任务(介入点2/3/4 突触方向/种子图/反证问题)。
            空字符串表示不分层,按原顺序降级。

    Returns:
        LLM 生成的文本内容。

    Raises:
        RuntimeError: 预算超限或所有 provider 失败。
    """
    import requests

    if providers is None:
        if tier:
            providers = load_llm_providers_by_tier(tier)
        else:
            providers = load_llm_providers()
    if not providers:
        if allow_no_provider:
            logger.warning("call_llm_with_budget: no providers configured, returning empty (source=%s)", source)
            return ""
        raise RuntimeError(f"no LLM providers configured (source={source})")

    prompt_tokens = estimate_messages_tokens(messages)
    check_llm_budget_or_raise(prompt_tokens, max_completion_tokens, source=source)

    last_error: Exception | None = None
    for provider in providers:
        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        body: dict[str, Any] = {
            "model": provider.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_completion_tokens,
        }
        if response_format_json:
            body["response_format"] = {"type": "json_object"}

        # 网络错误重试(ConnectionReset/Timeout),指数退避 1s/2s
        max_net_retries = 2
        for attempt in range(max_net_retries + 1):
            try:
                _rate_limit_wait()  # 速率限制,避免触发 deepseek ConnectionReset
                resp = requests.post(
                    f"{provider.api_base}/chat/completions",
                    json=body, headers=headers, timeout=timeout,
                )
                # JSON mode 不支持时降级重试
                if resp.status_code >= 400 and "response_format" in body:
                    body.pop("response_format", None)
                    _rate_limit_wait()
                    resp = requests.post(
                        f"{provider.api_base}/chat/completions",
                        json=body, headers=headers, timeout=timeout,
                    )
                resp.raise_for_status()
                data = resp.json()
                msg = data["choices"][0]["message"]
                content = str(msg.get("content") or "")
                # 推理模型兼容:content 为空时读取 reasoning_content(deepseek-v4-pro/flash)
                if not content:
                    content = str(msg.get("reasoning_content") or "")
                # 记账:completion_tokens 优先用 response.usage,无则用估算
                usage = data.get("usage") or {}
                completion_tokens = int(usage.get("completion_tokens") or estimate_tokens(content))
                record_llm_usage(
                    source=source,
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=completion_tokens,
                    success=True,
                )
                return content
            except (requests.ConnectionError, requests.Timeout) as net_exc:
                # 网络错误重试,指数退避
                last_error = net_exc
                if attempt < max_net_retries:
                    backoff = 1.0 * (2 ** attempt)
                    logger.warning(
                        "call_llm_with_budget: provider %s network error (attempt %d/%d), "
                        "retry in %.1fs: %s",
                        provider.name, attempt + 1, max_net_retries, backoff, net_exc,
                    )
                    time.sleep(backoff)
                    continue
                record_llm_usage(
                    source=source,
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=f"net_retry_exhausted: {net_exc}",
                )
                break  # 网络重试耗尽,跳到下一个 provider
            except Exception as exc:
                last_error = exc
                record_llm_usage(
                    source=source,
                    provider=provider.name,
                    model=provider.model_name,
                    prompt_tokens_est=prompt_tokens,
                    completion_tokens_est=0,
                    success=False,
                    error=str(exc),
                )
                logger.warning("call_llm_with_budget: provider %s failed: %s", provider.name, exc)
                break  # 非网络错误,跳到下一个 provider

    raise RuntimeError(f"all LLM providers failed (source={source}, last_error={last_error})")

