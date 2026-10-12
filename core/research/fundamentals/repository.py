"""P1.1 标准化三表仓储：支持重述版本，旧版本不可覆盖。

按 P1 计划：
- 支持重述版本（restatement_version 严格递增）
- 旧版本不可覆盖（同三元组重复保存抛异常）
- 任何时点只能看到 available_at <= as_of 的报表版本

实现为内存版（生产可换 SQLite/PG）。本仓储不依赖外部存储，
保持与 P0 一致的「不可覆盖」语义。
"""
from __future__ import annotations

from typing import Callable

from .contracts import NormalizedFinancialStatement
from .normalizer import _normalize_period


class StatementRepository:
    """标准化三表仓储。

    不变量：
    - 同 (symbol, period, restatement_version) 三元组唯一
    - 已存在的版本不可被覆盖，只能插入更高版本号
    - PIT 查询时只能看到 available_at <= as_of
    """

    def __init__(self) -> None:
        # key: (symbol, period_normalized, restatement_version)
        self._store: dict[tuple[str, str, int], NormalizedFinancialStatement] = {}

    def save(self, statement: NormalizedFinancialStatement) -> None:
        """保存报表。同 (symbol, period, version) 已存在则抛异常。

        Args:
            statement: 待保存的标准化报表

        Raises:
            ValueError: 当同版本号已存在（旧版本不可覆盖）
        """
        if not isinstance(statement, NormalizedFinancialStatement):
            raise TypeError("statement must be a NormalizedFinancialStatement")
        key = (
            statement.symbol,
            _normalize_period(statement.period),
            statement.restatement_version,
        )
        if key in self._store:
            raise ValueError(
                f"报表已存在，不可覆盖: symbol={statement.symbol} "
                f"period={statement.period} version={statement.restatement_version}"
            )
        self._store[key] = statement

    def save_restatement(
        self,
        symbol: str,
        period: str,
        new_version: int,
        factory: Callable[[NormalizedFinancialStatement | None], NormalizedFinancialStatement],
    ) -> NormalizedFinancialStatement:
        """保存重述版本，校验版本号严格递增。

        Args:
            symbol: 公司代码
            period: 期间（YYYY-MM-DD 或 YYYYMMDD）
            new_version: 新版本号（必须 > 现有最大版本号）
            factory: 接收旧 statement（最大版本），返回新 statement 的函数

        Returns:
            新版本报表

        Raises:
            ValueError: 当 new_version 不严格大于现有最大版本号
        """
        period_norm = _normalize_period(period)
        existing_versions = [
            (ver, stmt) for (sym, per, ver), stmt in self._store.items()
            if sym == symbol and per == period_norm
        ]
        max_version = max([v[0] for v in existing_versions], default=0)
        try:
            new_version = int(new_version)
        except (TypeError, ValueError):
            raise ValueError("new_version must be an integer") from None
        if new_version <= max_version:
            raise ValueError(
                f"重述版本必须严格递增: 现有最大={max_version}, 传入={new_version}"
            )
        previous = next(
            (stmt for ver, stmt in existing_versions if ver == max_version), None
        )
        new_statement = factory(previous)
        if new_statement is None:
            raise ValueError("factory 必须返回非空报表")
        new_statement.restatement_version = new_version
        # 显式 save 触发唯一性校验（防止 factory 写坏 symbol/period）
        new_statement.symbol = symbol
        new_statement.period = period_norm
        self.save(new_statement)
        return new_statement

    def get_latest(
        self,
        symbol: str,
        period: str,
        as_of: str | None = None,
    ) -> NormalizedFinancialStatement | None:
        """获取某期间最新可见版本。

        Args:
            symbol: 公司代码
            period: 期间 YYYY-MM-DD
            as_of: PIT 时点；None 表示无 PIT 限制

        Returns:
            最新版本报表；无可见版本时返回 None
        """
        period_norm = _normalize_period(period)
        cutoff = _normalize_period(as_of) if as_of else None
        candidates = [
            stmt for (sym, per, _ver), stmt in self._store.items()
            if sym == symbol and per == period_norm
            and (
                cutoff is None
                or (stmt.available_at and _normalize_period(stmt.available_at) <= cutoff)
            )
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.restatement_version)

    def list_periods(
        self,
        symbol: str,
        as_of: str | None = None,
    ) -> list[str]:
        """列出某公司可见期间列表（按时间升序）。

        Args:
            symbol: 公司代码
            as_of: PIT 时点；None 表示无 PIT 限制

        Returns:
            可见期间列表，按时间升序排序
        """
        cutoff = _normalize_period(as_of) if as_of else None
        periods = {
            stmt.period for (_sym, _per, _ver), stmt in self._store.items()
            if _sym == symbol
            and (
                cutoff is None
                or (stmt.available_at and _normalize_period(stmt.available_at) <= cutoff)
            )
        }
        return sorted(periods)

    def get_latest_statement(
        self,
        symbol: str,
        as_of: str | None = None,
    ) -> NormalizedFinancialStatement | None:
        """获取公司最新可见报表（不限期间，P1.10 桥接层适配方法）。

        与 ``get_latest(symbol, period, as_of)`` 不同，本方法自动选择最新可见期间，
        供 CfaReportBuilder 等"按 as_of 取最新"语义的调用方使用。

        Args:
            symbol: 公司代码
            as_of: PIT 时点；None 表示无 PIT 限制

        Returns:
            最新可见报表；无可见报表返回 None
        """
        periods = self.list_periods(symbol, as_of=as_of)
        if not periods:
            return None
        latest_period = periods[-1]
        return self.get_latest(symbol, latest_period, as_of=as_of)

    def list_versions(
        self,
        symbol: str,
        period: str,
    ) -> list[int]:
        """列出某期间的所有版本号（升序）。

        Args:
            symbol: 公司代码
            period: 期间 YYYY-MM-DD

        Returns:
            版本号列表
        """
        period_norm = _normalize_period(period)
        versions = [
            ver for (sym, per, ver) in self._store.keys()
            if sym == symbol and per == period_norm
        ]
        return sorted(versions)

    def count(self) -> int:
        """返回仓储中报表总数（用于测试与诊断）。"""
        return len(self._store)
