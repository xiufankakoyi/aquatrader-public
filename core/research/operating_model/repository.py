"""P1.3 经营驱动模型仓储。

支持多版本快照，按 symbol/period/version 存储，PIT 过滤。
"""
from __future__ import annotations

from .contracts import OperatingModel


class OperatingModelRepository:
    """经营驱动模型仓储（内存版，便于测试）。

    生产环境应替换为持久化存储（如 SQLite/Postgres）。
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, int], OperatingModel] = {}

    def save(self, model: OperatingModel, version: int = 1) -> None:
        """保存模型快照。

        Args:
            model: 模型实例
            version: 快照版本号（必须 > 0）

        Raises:
            ValueError: 当 version <= 0 或同 (symbol, period, version) 已存在
        """
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValueError(f"version 必须 > 0，收到 {version}")
        if not isinstance(model, OperatingModel):
            raise TypeError("model must be an OperatingModel")
        key = (model.symbol, model.period, version)
        if key in self._store:
            raise ValueError(
                f"同 (symbol, period, version)={key} 已存在，不可覆盖；"
                "请使用新 version"
            )
        self._store[key] = model

    def get_latest(
        self,
        symbol: str,
        period: str,
        as_of: str | None = None,
    ) -> OperatingModel | None:
        """获取最新版本快照。

        Args:
            symbol: 公司代码
            period: 期间 YYYY-MM-DD
            as_of: PIT 过滤时点（目前未使用，仅用于接口对齐）

        Returns:
            最新版本模型；无则 None
        """
        latest_key = max(
            (
                key
                for key in self._store
                if key[0] == symbol and key[1] == period
            ),
            key=lambda key: key[2],
            default=None,
        )
        if latest_key is None:
            return None
        return self._store[latest_key]

    def list_periods(self, symbol: str) -> list[str]:
        """列出某公司的所有期间。"""
        return sorted({per for sym, per, _ in self._store if sym == symbol})

    def get_latest_model(
        self,
        symbol: str,
        as_of: str | None = None,
    ) -> OperatingModel | None:
        """获取公司最新可见模型（不限期间，P1.10 桥接层适配方法）。

        与 ``get_latest(symbol, period, as_of)`` 不同，本方法自动选择最新期间，
        供 CfaReportBuilder 等"按 as_of 取最新"语义的调用方使用。

        Args:
            symbol: 公司代码
            as_of: PIT 时点（目前未用于过滤，仅用于接口对齐）

        Returns:
            最新可见模型；无返回 None
        """
        latest_period = max(
            (per for sym, per, _ in self._store if sym == symbol),
            default=None,
        )
        if latest_period is None:
            return None
        return self.get_latest(symbol, latest_period, as_of=as_of)

    def list_versions(self, symbol: str, period: str) -> list[int]:
        """列出某公司某期间的所有版本号。"""
        return sorted(
            ver for sym, per, ver in self._store if sym == symbol and per == period
        )

    def count(self) -> int:
        """返回快照总数。"""
        return len(self._store)
