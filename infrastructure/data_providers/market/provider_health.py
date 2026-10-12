"""数据源健康状态与熔断机制。

每个数据源维护一个状态（active / rate_limited / quota_exceeded / network_blocked），
一旦失败，在熔断时间内不再重试，避免重复支付失败成本。

熔断时间：
  - quota_exceeded（额度用完）：24h
  - rate_limited（限频 429）：1h
  - network_blocked（网络拦截）：30min
  - auth_failed（认证失败）：24h

状态持久化到 JSON 文件，重启后仍有效。

用法：
  from infrastructure.data_providers.market.provider_health import provider_health

  # 检查是否可用
  if provider_health.is_available("wencai"):
      result = wencai.semantic_search(...)

  # 报告失败
  provider_health.report_failure("wencai", "quota_exceeded", "每日额度用完")

  # 报告成功
  provider_health.report_success("wencai")
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_HEALTH_FILE = _PROJECT_ROOT / "data" / "provider_health.json"
_LEGACY_HEALTH_FILE = Path(__file__).resolve().parents[2] / "data" / "provider_health.json"

# 熔断时间配置（秒）
CIRCUIT_BREAKER_SECONDS: dict[str, int] = {
    "quota_exceeded": 24 * 3600,     # 24h
    "rate_limited": 3600,            # 1h
    "network_blocked": 1800,         # 30min
    "auth_failed": 24 * 3600,        # 24h
    "server_error": 600,             # 10min
    "timeout": 300,                  # 5min
}

# 失败类型自动识别关键词
_FAILURE_KEYWORDS: dict[str, list[str]] = {
    "quota_exceeded": ["次数已用完", "额度", "quota", "limit exceeded", "每日限制"],
    "rate_limited": ["429", "Too Many Requests", "rate limit", "限频", "频率限制"],
    "network_blocked": ["ProxyError", "Max retries exceeded", "ConnectionError",
                        "RemoteDisconnected", "被代理拦截", "网络不可用"],
    "auth_failed": ["401", "认证失败", "Unauthorized", "Skill 版本过低"],
    "server_error": ["500", "502", "503", "Internal Server Error"],
    "timeout": ["timeout", "Timeout", "超时"],
}


class ProviderHealth:
    """数据源健康状态与熔断管理器。"""

    def __init__(self, health_file: Path | str | None = None) -> None:
        """初始化熔断管理器。

        Args:
            health_file: 状态持久化文件路径
        """
        self._file = Path(health_file) if health_file else _HEALTH_FILE
        # get_status() calls is_available(); use a re-entrant lock so health
        # inspection cannot deadlock the whole provider registry.
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        if health_file is None:
            self._migrate_legacy_state()
        self._load()

    def _migrate_legacy_state(self) -> None:
        """Merge the historical wrong-root state into the canonical file."""
        if _LEGACY_HEALTH_FILE == self._file or not _LEGACY_HEALTH_FILE.exists():
            return
        merged: dict[str, Any] = {"providers": {}}
        for candidate in (self._file, _LEGACY_HEALTH_FILE):
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                for name, state in (payload.get("providers") or {}).items():
                    if not isinstance(state, dict):
                        continue
                    existing = merged["providers"].get(name)
                    candidate_time = str(
                        state.get("failed_at")
                        or state.get("last_success_at")
                        or state.get("recovered_at")
                        or ""
                    )
                    existing_time = str(
                        (existing or {}).get("failed_at")
                        or (existing or {}).get("last_success_at")
                        or (existing or {}).get("recovered_at")
                        or ""
                    )
                    if existing is None or candidate_time >= existing_time:
                        merged["providers"][name] = state
            except Exception as exc:
                logger.warning("读取旧 provider health 状态失败(%s): %s", candidate, exc)
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            merged["updated_at"] = datetime.now().isoformat()
            temp = self._file.with_suffix(self._file.suffix + ".tmp")
            temp.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp.replace(self._file)
            _LEGACY_HEALTH_FILE.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("迁移 provider health 状态失败: %s", exc)

    def _load(self) -> None:
        """从文件加载状态。"""
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self._states = data.get("providers", {})
                logger.info("加载 provider health: %d 个数据源", len(self._states))
            except Exception as e:
                logger.warning("加载 provider health 失败: %s", e)
                self._states = {}

    def _save(self) -> None:
        """持久化状态到文件。"""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "updated_at": datetime.now().isoformat(),
                "providers": self._states,
            }
            temp = self._file.with_suffix(self._file.suffix + ".tmp")
            temp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temp.replace(self._file)
        except Exception as e:
            logger.warning("保存 provider health 失败: %s", e)

    def is_available(self, provider_name: str) -> bool:
        """检查数据源是否可用（未熔断）。

        Args:
            provider_name: 数据源名称

        Returns:
            True 表示可用，False 表示熔断中
        """
        with self._lock:
            state = self._states.get(provider_name)
            if not state:
                return True

            status = state.get("status", "active")
            if status == "active":
                return True

            # 检查熔断是否过期
            failed_at = state.get("failed_at", "")
            breaker_seconds = CIRCUIT_BREAKER_SECONDS.get(status, 600)
            if failed_at:
                try:
                    failed_time = datetime.fromisoformat(failed_at)
                    if datetime.now() - failed_time > timedelta(seconds=breaker_seconds):
                        # 熔断过期，恢复
                        logger.info("熔断过期，恢复 %s (status=%s)", provider_name, status)
                        state["status"] = "active"
                        state["recovered_at"] = datetime.now().isoformat()
                        self._save()
                        return True
                except (ValueError, TypeError):
                    pass

            return False

    def report_failure(self, provider_name: str, failure_type: str,
                       message: str = "") -> None:
        """报告数据源失败，触发熔断。

        Args:
            provider_name: 数据源名称
            failure_type: 失败类型（quota_exceeded/rate_limited/network_blocked/auth_failed/server_error/timeout）
            message: 失败信息
        """
        with self._lock:
            breaker_seconds = CIRCUIT_BREAKER_SECONDS.get(failure_type, 600)
            self._states[provider_name] = {
                "status": failure_type,
                "failed_at": datetime.now().isoformat(),
                "breaker_seconds": breaker_seconds,
                "message": message[:200],
                "success_count": self._states.get(provider_name, {}).get("success_count", 0),
                "failure_count": self._states.get(provider_name, {}).get("failure_count", 0) + 1,
            }
            logger.warning(
                "熔断 %s: status=%s, 熔断=%dh, 原因=%s",
                provider_name, failure_type,
                breaker_seconds // 3600, message[:50],
            )
            self._save()

    def report_success(self, provider_name: str) -> None:
        """报告数据源成功，恢复状态。

        Args:
            provider_name: 数据源名称
        """
        with self._lock:
            state = self._states.get(provider_name, {})
            state["status"] = "active"
            state["success_count"] = state.get("success_count", 0) + 1
            state["last_success_at"] = datetime.now().isoformat()
            self._states[provider_name] = state
            self._save()

    def auto_detect_failure(self, provider_name: str, error_message: str) -> None:
        """自动识别失败类型并触发熔断。

        Args:
            provider_name: 数据源名称
            error_message: 错误信息
        """
        for failure_type, keywords in _FAILURE_KEYWORDS.items():
            if any(kw in error_message for kw in keywords):
                self.report_failure(provider_name, failure_type, error_message)
                return
        # 未匹配到具体类型，按 server_error 处理
        self.report_failure(provider_name, "server_error", error_message)

    def get_status(self, provider_name: str) -> dict[str, Any]:
        """获取数据源状态。"""
        with self._lock:
            state = self._states.get(provider_name, {})
            return {
                "provider": provider_name,
                "status": state.get("status", "active"),
                "available": self.is_available(provider_name),
                "failed_at": state.get("failed_at", ""),
                "message": state.get("message", ""),
                "success_count": state.get("success_count", 0),
                "failure_count": state.get("failure_count", 0),
            }

    def get_all_status(self) -> dict[str, dict[str, Any]]:
        """获取所有数据源状态。"""
        all_providers = {"wencai", "ifind", "akshare", "baidu", "duckduckgo",
                         "tdx", "tencent", "efinance", "tushare", "baostock"}
        for name in list(self._states.keys()):
            all_providers.add(name)
        return {name: self.get_status(name) for name in sorted(all_providers)}

    def reset(self, provider_name: str | None = None) -> None:
        """重置熔断状态。

        Args:
            provider_name: 指定数据源名称，None 则重置全部
        """
        with self._lock:
            if provider_name:
                self._states.pop(provider_name, None)
            else:
                self._states.clear()
            self._save()


# 全局单例
provider_health = ProviderHealth()
