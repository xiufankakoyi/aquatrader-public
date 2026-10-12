"""Virtual Research Desk Agents — 五岗协作 Agent 实现。

DataOps / Research / QuantValidation / Risk / CIO 五个 Agent，
每个 Agent 读取本地结构化数据，输出统一 AgentOutput 结构。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.research.desk.models import AgentOutput, CIOOutput, StockCandidate, ThemeCandidate
from data_svc.query.fast_market_query import get_fast_market_query

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPORT_DIR = PROJECT_ROOT / "data" / "reports"


def _as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _mapping_list(value: Any) -> list[Dict[str, Any]]:
    return [dict(item) for item in _as_list(value) if isinstance(item, Mapping)]


def _safe_count(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return max(0, int(number)) if isfinite(number) else 0


def _read_json(filename: str) -> Dict[str, Any]:
    """读取 data/reports/ 下的 JSON 文件，失败返回空字典。"""
    if not isinstance(filename, str) or Path(filename).name != filename:
        return {}
    path = REPORT_DIR / filename
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _as_mapping(payload)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _get_global_latest_trade_date(quant_flow: Dict[str, Any]) -> str:
    """从 QuantFlow 报告中提取全局最新交易日。"""
    return str(quant_flow.get("global_latest_trade_date") or "")


class DataOpsAgent:
    """数据岗：判断行情、因子、DragonEye、Evidence Center 是否新鲜完整。"""

    def run(self, trade_date: str = "") -> AgentOutput:
        """执行数据新鲜度校验。

        Args:
            trade_date: 全局最新交易日（可选，默认从 QuantFlow 报告读取）

        Returns:
            AgentOutput 包含数据新鲜度判定结果
        """
        health = _read_json("data_health_latest.json")
        quant_flow = _read_json("quant_flow_latest.json")
        evidence = _read_json("evidence_center_latest.json")

        global_date = trade_date or _get_global_latest_trade_date(quant_flow)
        input_sources = [
            "data_health_latest.json",
            "quant_flow_latest.json",
            "evidence_center_latest.json",
        ]

        # 收集各模块 data_date
        module_dates: Dict[str, str] = {}
        for stage in _as_list(quant_flow.get("stages")):
            stage = _as_mapping(stage)
            name = stage.get("stage", "")
            dd = stage.get("data_date", "")
            if isinstance(name, str) and isinstance(dd, str) and name and dd:
                module_dates[name] = dd

        # 数据健康状态
        health_status = health.get("status", "unknown")

        # Evidence Center 状态
        total_evidence = _safe_count(evidence.get("total_evidence", 0))
        stale_evidence = min(_safe_count(evidence.get("stale_evidence", 0)), total_evidence)
        fresh_evidence = total_evidence - stale_evidence

        # 判定逻辑
        risk_flags: List[str] = []
        stale_modules: List[str] = []
        for name, dd in module_dates.items():
            if global_date and dd < global_date:
                stale_modules.append(f"{name}({dd})")
                risk_flags.append("stale_data")

        # Evidence Center 为空
        if total_evidence == 0:
            return AgentOutput(
                agent="DataOpsAgent",
                trade_date=global_date,
                input_sources=input_sources,
                summary="Evidence Center 为空，无法生成研究结论",
                evidence_ids=[],
                confidence="low",
                risk_flags=["evidence_center_empty"],
                decision="BLOCK",
                reason="Evidence Center 为空，无法生成研究结论",
                details={
                    "health_status": health_status,
                    "module_dates": module_dates,
                    "stale_modules": stale_modules,
                    "total_evidence": total_evidence,
                    "stale_evidence": stale_evidence,
                    "fresh_evidence": fresh_evidence,
                },
            )

        # Evidence Center 全部 stale
        if fresh_evidence == 0 and total_evidence > 0:
            return AgentOutput(
                agent="DataOpsAgent",
                trade_date=global_date,
                input_sources=input_sources,
                summary=f"Evidence Center {total_evidence} 条证据全部过时",
                evidence_ids=[],
                confidence="low",
                risk_flags=["evidence_all_stale"],
                decision="BLOCK",
                reason=f"Evidence Center {total_evidence} 条证据全部过时，无法生成有效研究结论",
                details={
                    "health_status": health_status,
                    "module_dates": module_dates,
                    "stale_modules": stale_modules,
                    "total_evidence": total_evidence,
                    "stale_evidence": stale_evidence,
                    "fresh_evidence": 0,
                },
            )

        # 有 stale 模块
        if stale_modules:
            return AgentOutput(
                agent="DataOpsAgent",
                trade_date=global_date,
                input_sources=input_sources,
                summary=f"数据部分过期: {', '.join(stale_modules)}",
                evidence_ids=[],
                confidence="medium",
                risk_flags=risk_flags,
                decision="WATCH",
                reason=f"以下模块数据过期: {', '.join(stale_modules)}",
                details={
                    "health_status": health_status,
                    "module_dates": module_dates,
                    "stale_modules": stale_modules,
                    "total_evidence": total_evidence,
                    "stale_evidence": stale_evidence,
                    "fresh_evidence": fresh_evidence,
                },
            )

        # 全部新鲜
        return AgentOutput(
            agent="DataOpsAgent",
            trade_date=global_date,
            input_sources=input_sources,
            summary="数据全部新鲜，可生成研究结论",
            evidence_ids=[],
            confidence="high",
            risk_flags=[],
            decision="PASS",
            reason="所有核心数据源 data_date 与全局最新交易日一致，Evidence Center 有有效证据",
            details={
                "health_status": health_status,
                "module_dates": module_dates,
                "stale_modules": [],
                "total_evidence": total_evidence,
                "stale_evidence": stale_evidence,
                "fresh_evidence": fresh_evidence,
            },
        )


class ResearchAgent:
    """行研岗：从 Evidence Center 读取证据，总结题材与股票。"""

    def run(self, trade_date: str = "") -> AgentOutput:
        """执行行研分析。

        Args:
            trade_date: 全局最新交易日

        Returns:
            AgentOutput 包含题材/股票摘要与证据引用
        """
        evidence_data = _read_json("evidence_center_latest.json")
        quant_flow = _read_json("quant_flow_latest.json")

        global_date = trade_date or _get_global_latest_trade_date(quant_flow)
        input_sources = [
            "evidence_center_latest.json",
            "quant_flow_latest.json",
        ]

        evidences = _mapping_list(evidence_data.get("evidences"))
        total_evidence = len(evidences)

        # 分离有效与 stale 证据
        fresh_evidences = [ev for ev in evidences if not ev.get("is_stale", False)]
        stale_count = total_evidence - len(fresh_evidences)

        # Evidence Center 为空
        if total_evidence == 0:
            return AgentOutput(
                agent="ResearchAgent",
                trade_date=global_date,
                input_sources=input_sources,
                summary="Evidence Center 为空，无法生成研究结论",
                evidence_ids=[],
                confidence="low",
                risk_flags=["evidence_center_empty"],
                decision="INSUFFICIENT_EVIDENCE",
                reason="Evidence Center 为空，行研岗无法输出任何结论",
                details={
                    "total_evidence": 0,
                    "fresh_evidence": 0,
                    "stale_evidence": 0,
                    "themes": [],
                    "stocks": [],
                },
            )

        # 全部 stale
        if not fresh_evidences:
            return AgentOutput(
                agent="ResearchAgent",
                trade_date=global_date,
                input_sources=input_sources,
                summary=f"所有 {total_evidence} 条证据已过时",
                evidence_ids=[],
                confidence="low",
                risk_flags=["evidence_all_stale"],
                decision="INSUFFICIENT_EVIDENCE",
                reason="所有证据已过时，无法生成有效研究结论",
                details={
                    "total_evidence": total_evidence,
                    "fresh_evidence": 0,
                    "stale_evidence": stale_count,
                    "themes": [],
                    "stocks": [],
                },
            )

        # 从有效证据中提取题材和股票
        themes: List[Dict[str, Any]] = []
        stocks: List[Dict[str, Any]] = []
        theme_evidence_ids: List[str] = []
        stock_evidence_ids: List[str] = []

        for ev in fresh_evidences:
            ev_id = ev.get("evidence_id", "")
            entity_type = ev.get("entity_type", "")
            entity_name = ev.get("entity_name", "")
            strength = ev.get("evidence_strength", "medium")
            symbols = [item for item in _as_list(ev.get("symbols")) if isinstance(item, str) and item]
            themes_list = [item for item in _as_list(ev.get("themes")) if isinstance(item, str) and item]

            if entity_type == "theme" or themes_list:
                theme_item = {
                    "name": entity_name,
                    "evidence_id": ev_id,
                    "strength": strength,
                    "themes": themes_list,
                }
                # 过滤无 evidence_id 的结论
                if ev_id:
                    themes.append(theme_item)
                    theme_evidence_ids.append(ev_id)

            if symbols:
                for symbol in symbols:
                    stock_item = {
                        "symbol": symbol,
                        "evidence_id": ev_id,
                        "strength": strength,
                        "source": ev.get("source_name", ""),
                    }
                    if ev_id:
                        stocks.append(stock_item)
                        stock_evidence_ids.append(ev_id)

        # 从 QuantFlow DragonEye 题材数据补充
        dragon_data = {}
        for stage in _as_list(quant_flow.get("stages")):
            stage = _as_mapping(stage)
            if stage.get("stage") == "dragon_eye_summary":
                dragon_data = _as_mapping(stage.get("data"))
                break

        direction_rows = []
        dragon_evidence = _as_mapping(dragon_data.get("evidence"))
        sector_heat = _as_mapping(dragon_evidence.get("sector_heat_stats"))
        if sector_heat:
            raw_data = sector_heat.get("data", [])
            for item in _as_list(raw_data)[:10]:
                item = _as_mapping(item)
                if item.get("name"):
                    direction_rows.append({
                        "name": item.get("name"),
                        "count": item.get("count"),
                    })

        # 证据强度分布
        strength_dist = {"strong": 0, "medium": 0, "weak": 0}
        for ev in fresh_evidences:
            s = ev.get("evidence_strength", "medium")
            if s not in strength_dist:
                s = "medium"
            strength_dist[s] = strength_dist.get(s, 0) + 1

        # ---- theme_candidates 聚合 ----
        # 聚合结构: theme_name -> {evidence_ids, related_symbols, strengths}
        theme_agg: Dict[str, Dict[str, Any]] = {}
        for ev in fresh_evidences:
            ev_id = ev.get("evidence_id", "")
            if not ev_id:
                continue
            entity_type = ev.get("entity_type", "")
            entity_name = ev.get("entity_name", "")
            themes_list = [item for item in _as_list(ev.get("themes")) if isinstance(item, str) and item]
            symbols = [item for item in _as_list(ev.get("symbols")) if isinstance(item, str) and item]
            strength = ev.get("evidence_strength", "medium")

            # 从 themes 字段中提取题材名
            for theme_name in themes_list:
                if not theme_name:
                    continue
                if theme_name not in theme_agg:
                    theme_agg[theme_name] = {
                        "evidence_ids": set(),
                        "related_symbols": set(),
                        "strengths": [],
                    }
                theme_agg[theme_name]["evidence_ids"].add(ev_id)
                theme_agg[theme_name]["related_symbols"].update(symbols)
                theme_agg[theme_name]["strengths"].append(strength)

            # entity_type == "theme" 且 entity_name 非空时也加入聚合组
            if entity_type == "theme" and entity_name:
                if entity_name not in theme_agg:
                    theme_agg[entity_name] = {
                        "evidence_ids": set(),
                        "related_symbols": set(),
                        "strengths": [],
                    }
                theme_agg[entity_name]["evidence_ids"].add(ev_id)
                theme_agg[entity_name]["related_symbols"].update(symbols)
                theme_agg[entity_name]["strengths"].append(strength)

        # 收集 stale 证据中的题材名，用于置信度降级
        stale_theme_names: set = set()
        stale_evidences = [ev for ev in evidences if ev.get("is_stale", False)]
        for ev in stale_evidences:
            for t in _as_list(ev.get("themes")):
                if t:
                    stale_theme_names.add(t)
            if ev.get("entity_type") == "theme" and ev.get("entity_name"):
                stale_theme_names.add(ev["entity_name"])

        # 置信度降级映射
        _confidence_downgrade = {"high": "medium", "medium": "low", "low": "low"}

        # 构建 ThemeCandidate 列表
        theme_candidates: List[ThemeCandidate] = []
        for theme_name, agg in theme_agg.items():
            ev_ids = sorted(agg["evidence_ids"])
            ev_count = len(ev_ids)
            related_syms = sorted(agg["related_symbols"])
            strengths = agg["strengths"]

            # 置信度计算
            strong_count = sum(1 for s in strengths if s == "strong")
            strong_ratio = strong_count / max(ev_count, 1)
            if strong_ratio >= 0.5:
                conf = "high"
            elif strong_ratio >= 0.2:
                conf = "medium"
            else:
                conf = "low"

            # stale 证据支撑该题材时置信度降一级
            if theme_name in stale_theme_names:
                conf = _confidence_downgrade[conf]

            # decision 判定
            if ev_count >= 5 and conf == "high":
                dec = "PASS"
            elif ev_count >= 3:
                dec = "WATCH"
            else:
                dec = "INSUFFICIENT_EVIDENCE"

            # reason 生成
            if dec == "PASS":
                reason = f"题材{theme_name}有{ev_count}条证据支撑，置信度高"
            elif dec == "WATCH":
                reason = f"题材{theme_name}有{ev_count}条证据支撑，需进一步验证"
            else:
                reason = f"题材{theme_name}仅有{ev_count}条证据，不足以形成结论"

            theme_candidates.append(ThemeCandidate(
                theme=theme_name,
                evidence_ids=ev_ids,
                evidence_count=ev_count,
                related_symbols=related_syms,
                confidence=conf,
                decision=dec,
                reason=reason,
            ))

        # 按 evidence_count 降序排列
        theme_candidates.sort(key=lambda tc: tc.evidence_count, reverse=True)

        # theme_candidates 的 evidence_ids 汇总
        tc_evidence_ids: List[str] = []
        for tc in theme_candidates:
            tc_evidence_ids.extend(tc.evidence_ids)

        # 置信度计算
        strong_ratio = strength_dist.get("strong", 0) / max(len(fresh_evidences), 1)
        if strong_ratio >= 0.5:
            confidence = "high"
        elif strong_ratio >= 0.2:
            confidence = "medium"
        else:
            confidence = "low"

        all_evidence_ids = list(set(theme_evidence_ids + stock_evidence_ids + tc_evidence_ids))
        summary_parts = []
        if themes:
            theme_names = [t["name"] for t in themes[:5]]
            summary_parts.append(f"主要题材: {', '.join(theme_names)}")
        if stocks:
            summary_parts.append(f"相关股票 {len(stocks)} 只")
        summary_parts.append(f"有效证据 {len(fresh_evidences)} 条")
        summary_parts.append(f"stale 证据 {stale_count} 条")

        return AgentOutput(
            agent="ResearchAgent",
            trade_date=global_date,
            input_sources=input_sources,
            summary="；".join(summary_parts),
            evidence_ids=all_evidence_ids,
            confidence=confidence,
            risk_flags=[],
            decision="PASS" if fresh_evidences else "INSUFFICIENT_EVIDENCE",
            reason=f"基于 {len(fresh_evidences)} 条有效证据生成研究结论，stale 证据 {stale_count} 条已排除",
            details={
                "total_evidence": total_evidence,
                "fresh_evidence": len(fresh_evidences),
                "stale_evidence": stale_count,
                "strength_distribution": strength_dist,
                "themes": themes[:10],
                "stocks": stocks[:20],
                "directions": direction_rows,
                "theme_candidates": [asdict(tc) for tc in theme_candidates],
            },
        )


class QuantValidationAgent:
    """量化验证岗：验证候选股票是否有趋势、量价、因子或策略信号确认。"""

    def get_factor_data(self, symbol: str, start_date: str, end_date: str, fields=None):
        """Get factor data for a symbol via FastMarketQuery.

        Args:
            symbol: Stock code
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            fields: Optional list of factor fields

        Returns:
            Polars DataFrame with factor data
        """
        try:
            return get_fast_market_query().get_stock_factors(symbol, start_date, end_date, fields=fields)
        except Exception:
            import polars as pl
            return pl.DataFrame()

    def run(self, trade_date: str = "") -> AgentOutput:
        """执行量化验证。

        Args:
            trade_date: 全局最新交易日

        Returns:
            AgentOutput 包含候选股票验证结果
        """
        quant_flow = _read_json("quant_flow_latest.json")
        global_date = trade_date or _get_global_latest_trade_date(quant_flow)
        if not global_date:
            try:
                global_date = get_fast_market_query().get_latest_trade_date() or ""
            except Exception:
                pass
        input_sources = ["quant_flow_latest.json"]

        # 提取 stock_screener_candidates 和 strategy_signal_scan 数据
        candidates_data: Dict[str, Any] = {}
        signals_data: Dict[str, Any] = {}
        for stage in _as_list(quant_flow.get("stages")):
            stage = _as_mapping(stage)
            if stage.get("stage") == "stock_screener_candidates":
                candidates_data = _as_mapping(stage.get("data"))
            elif stage.get("stage") == "strategy_signal_scan":
                signals_data = _as_mapping(stage.get("data"))

        candidates = _mapping_list(candidates_data.get("candidates"))
        no_candidates = candidates_data.get("no_candidates", True)

        # 策略信号中的股票集合
        buy_symbols: set = set()
        watch_symbols: set = set()
        for signal in _as_list(signals_data.get("buy")):
            signal = _as_mapping(signal)
            code = str(signal.get("stock_code") or "")
            if code:
                buy_symbols.add(code)
        for signal in _as_list(signals_data.get("watch")):
            signal = _as_mapping(signal)
            code = str(signal.get("stock_code") or "")
            if code:
                watch_symbols.add(code)
        signal_symbols = buy_symbols | watch_symbols

        # 无候选股票
        if no_candidates or not candidates:
            return AgentOutput(
                agent="QuantValidationAgent",
                trade_date=global_date,
                input_sources=input_sources,
                summary="无因子筛选候选",
                evidence_ids=[],
                confidence="low",
                risk_flags=["no_candidates"],
                decision="INSUFFICIENT_EVIDENCE",
                reason="无因子筛选候选，量化验证岗无法验证",
                details={
                    "candidate_count": 0,
                    "validation_results": [],
                    "stock_candidates": [],
                    "buy_signal_count": len(buy_symbols),
                    "watch_signal_count": len(watch_symbols),
                },
            )

        # 读取 Evidence Center，构建 symbol → evidence_ids 映射
        evidence_data = _read_json("evidence_center_latest.json")
        evidences = _mapping_list(evidence_data.get("evidences"))
        symbol_evidence_map: Dict[str, List[str]] = {}
        for ev in evidences:
            ev_id = ev.get("evidence_id", "")
            if not ev_id:
                continue
            for sym in _as_list(ev.get("symbols")):
                if not isinstance(sym, str) or not sym:
                    continue
                symbol_evidence_map.setdefault(sym, []).append(ev_id)
        input_sources.append("evidence_center_latest.json")

        # 逐只验证
        validation_results: List[Dict[str, Any]] = []
        stock_candidates: List[Dict[str, Any]] = []
        pass_count = 0
        watch_count = 0
        insufficient_count = 0
        all_evidence_ids: List[str] = []

        for candidate in candidates:
            symbol = str(candidate.get("symbol") or "")
            name = str(candidate.get("name") or "")

            # --- 原有 validation_results 逻辑（向后兼容） ---
            candidate_evidence_ids: List[str] = []
            # 引用因子筛选来源
            candidate_evidence_ids.append(
                f"factor_screener:{symbol}"
            )

            has_signal = symbol in signal_symbols
            has_buy_signal = symbol in buy_symbols

            if has_signal:
                stock_decision = "PASS"
                stock_reason = "因子筛选通过且有策略信号确认"
                if has_buy_signal:
                    candidate_evidence_ids.append(f"signal_buy:{symbol}")
                else:
                    candidate_evidence_ids.append(f"signal_watch:{symbol}")
                pass_count += 1
            else:
                stock_decision = "WATCH"
                stock_reason = "仅有因子筛选通过，无策略信号确认"
                watch_count += 1

            validation_results.append({
                "symbol": symbol,
                "name": name,
                "decision": stock_decision,
                "reason": stock_reason,
                "evidence_ids": candidate_evidence_ids,
                "has_factor_support": True,
                "has_signal_support": has_signal,
            })
            all_evidence_ids.extend(candidate_evidence_ids)

            # --- 新增 stock_candidates 结构化输出 ---
            # 从 Evidence Center 获取关联证据
            ev_ids = list(symbol_evidence_map.get(symbol, []))
            # 补充因子筛选和信号来源证据
            ev_ids.append(f"factor_screener:{symbol}")
            if has_buy_signal:
                ev_ids.append(f"signal_buy:{symbol}")
            elif has_signal:
                ev_ids.append(f"signal_watch:{symbol}")

            # 三重确认判定
            factor_confirmed = True  # 股票在 stock_screener_candidates 中
            signal_confirmed = has_signal

            # 趋势确认：因子筛选通过 + 20日收益为正 + MA 多头排列
            # 注意：pipeline 已过滤 ma5 >= ma10 >= ma20，候选股天然满足 MA 条件
            return_20d = candidate.get("return_20d") or 0
            try:
                return_20d = float(return_20d)
            except (TypeError, ValueError):
                return_20d = 0.0
            trend_confirmed = factor_confirmed and return_20d > 0

            # 风险标记
            risk_flags: List[str] = []
            volatility_20d = candidate.get("volatility_20d")
            try:
                if volatility_20d is not None and float(volatility_20d) > 0.3:
                    risk_flags.append("high_volatility")
            except (TypeError, ValueError):
                pass
            rsi_12 = candidate.get("rsi_12")
            try:
                if rsi_12 is not None and float(rsi_12) > 70:
                    risk_flags.append("overbought_rsi")
            except (TypeError, ValueError):
                pass

            # decision 判定
            if not ev_ids:
                # evidence_ids 为空 → INSUFFICIENT_EVIDENCE（不能 PASS）
                sc_decision = "INSUFFICIENT_EVIDENCE"
                sc_reason = "无关联证据，无法确认"
            elif factor_confirmed and signal_confirmed and trend_confirmed:
                # 三重确认全 True 且 evidence_ids 非空 → PASS
                if risk_flags:
                    # 有风险标记 → 降级为 WATCH
                    sc_decision = "WATCH"
                    sc_reason = (
                        f"因子+信号+趋势三重确认，但存在风险标记: "
                        f"{', '.join(risk_flags)}"
                    )
                else:
                    sc_decision = "PASS"
                    sc_reason = "因子+信号+趋势三重确认，无风险标记"
            elif factor_confirmed and not signal_confirmed:
                # 因子确认但无信号 → WATCH
                sc_decision = "WATCH"
                sc_reason = "因子筛选通过，无策略信号确认"
            else:
                sc_decision = "WATCH"
                sc_reason = "验证条件不充分"

            sc = StockCandidate(
                symbol=symbol,
                stock_name=name,
                evidence_ids=ev_ids,
                factor_confirmed=factor_confirmed,
                signal_confirmed=signal_confirmed,
                trend_confirmed=trend_confirmed,
                risk_flags=risk_flags,
                decision=sc_decision,
                reason=sc_reason,
            )
            stock_candidates.append(asdict(sc))

        # 整体判定
        if pass_count > 0:
            overall_decision = "PASS"
            overall_reason = f"候选 {len(candidates)} 只中 {pass_count} 只有因子+信号双重确认"
        elif watch_count > 0:
            overall_decision = "WATCH"
            overall_reason = f"候选 {len(candidates)} 只仅有因子筛选通过，无策略信号确认"
        else:
            overall_decision = "INSUFFICIENT_EVIDENCE"
            overall_reason = "无候选股票通过验证"

        return AgentOutput(
            agent="QuantValidationAgent",
            trade_date=global_date,
            input_sources=input_sources,
            summary=f"候选 {len(candidates)} 只: PASS={pass_count}, WATCH={watch_count}, INSUFFICIENT={insufficient_count}",
            evidence_ids=list(set(all_evidence_ids)),
            confidence="high" if pass_count > 0 else "medium" if watch_count > 0 else "low",
            risk_flags=[],
            decision=overall_decision,
            reason=overall_reason,
            details={
                "candidate_count": len(candidates),
                "pass_count": pass_count,
                "watch_count": watch_count,
                "insufficient_count": insufficient_count,
                "buy_signal_symbols": sorted(buy_symbols),
                "watch_signal_symbols": sorted(watch_symbols),
                "validation_results": validation_results[:50],
                "stock_candidates": stock_candidates[:50],
            },
        )


class RiskAgent:
    """风控岗：检查 stale 数据、无证据结论、持仓风险、策略风险。"""

    def run(
        self,
        dataops_output: AgentOutput,
        research_output: AgentOutput,
        quant_output: AgentOutput,
        trade_date: str = "",
        theme_candidates: List[Dict[str, Any]] | None = None,
        stock_candidates: List[Dict[str, Any]] | None = None,
    ) -> AgentOutput:
        """执行风控检查。

        Args:
            dataops_output: DataOps Agent 输出
            research_output: Research Agent 输出
            quant_output: QuantValidation Agent 输出
            trade_date: 全局最新交易日
            theme_candidates: 题材候选列表（v2，可选）
            stock_candidates: 股票候选列表（v2，可选）

        Returns:
            AgentOutput 包含风控判定结果
        """
        quant_flow = _read_json("quant_flow_latest.json")
        global_date = trade_date or _get_global_latest_trade_date(quant_flow)
        input_sources = [
            "quant_flow_latest.json",
            "DataOpsAgent",
            "ResearchAgent",
            "QuantValidationAgent",
        ]

        risk_flags: List[str] = []
        block_reasons: List[str] = []
        watch_reasons: List[str] = []

        # 1. DataOps BLOCK 传递
        if dataops_output.decision == "BLOCK":
            risk_flags.append("dataops_block")
            block_reasons.append(f"数据岗 BLOCK: {dataops_output.reason}")

        # 2. Research 无证据结论检测
        research_details = _as_mapping(research_output.details)
        research_themes = [_as_mapping(item) for item in _as_list(research_details.get("themes"))]
        research_stocks = [_as_mapping(item) for item in _as_list(research_details.get("stocks"))]
        no_evidence_themes = [
            t for t in research_themes if not t.get("evidence_id")
        ]
        no_evidence_stocks = [
            s for s in research_stocks if not s.get("evidence_id")
        ]
        if no_evidence_themes or no_evidence_stocks:
            risk_flags.append("conclusion_without_evidence")
            block_reasons.append(
                f"行研岗存在无证据结论: "
                f"{len(no_evidence_themes)} 个题材, "
                f"{len(no_evidence_stocks)} 只股票"
            )

        # 3. 持仓风险检查
        portfolio_data: Dict[str, Any] = {}
        for stage in _as_list(quant_flow.get("stages")):
            stage = _as_mapping(stage)
            if stage.get("stage") == "portfolio_risk_check":
                portfolio_data = _as_mapping(stage.get("data"))
                break

        valuation_complete = portfolio_data.get("valuation_complete", True)
        active_positions = _safe_count(portfolio_data.get("active_position_count", 0))
        if not valuation_complete and active_positions > 0:
            risk_flags.append("portfolio_valuation_incomplete")
            watch_reasons.append("持仓估值不完整")

        # 4. 策略信号 blocked_reason 检查
        signals_data: Dict[str, Any] = {}
        for stage in _as_list(quant_flow.get("stages")):
            stage = _as_mapping(stage)
            if stage.get("stage") == "strategy_signal_scan":
                signals_data = _as_mapping(stage.get("data"))
                break
        blocked_reason = signals_data.get("blocked_reason", "")
        if blocked_reason:
            risk_flags.append("strategy_blocked")
            watch_reasons.append(f"策略信号被阻塞: {blocked_reason}")

        # 5. stale 数据风险
        if "stale_data" in dataops_output.risk_flags:
            risk_flags.append("stale_data_risk")
            watch_reasons.append("存在过期数据")

        # ========== v2: 逐个候选判定 ==========

        # 检查 portfolio_risk_check 是否为 demo 模式
        portfolio_mode = portfolio_data.get("mode", "")
        is_demo_mode = portfolio_mode == "demo"

        # 读取 Evidence Center，用于 stock 风险标记查询
        evidence_data = _read_json("evidence_center_latest.json")
        all_evidences = [_as_mapping(item) for item in _as_list(evidence_data.get("evidences"))]

        # 构建 symbol → risk_flags 映射（从证据中提取风险标记）
        symbol_risk_flags_map: Dict[str, List[str]] = {}
        for ev in all_evidences:
            ev_risk_flags = ev.get("risk_flags", [])
            if not ev_risk_flags:
                continue
            for sym in _as_list(ev.get("symbols")):
                if not isinstance(sym, str) or not sym:
                    continue
                symbol_risk_flags_map.setdefault(sym, []).extend(ev_risk_flags)

        # 检查持仓中估值不完整的 symbol 集合
        incomplete_valuation_symbols: set = set()
        if not valuation_complete and active_positions > 0:
            positions = portfolio_data.get("positions", [])
            for pos in positions:
                if isinstance(pos, dict) and not pos.get("valuation_complete", True):
                    sym = str(pos.get("symbol", ""))
                    if sym:
                        incomplete_valuation_symbols.add(sym)

        # --- theme_verdicts ---
        theme_verdicts: List[Dict[str, Any]] = []
        if theme_candidates is not None:
            for tc in theme_candidates:
                original_decision = tc.get("decision", "INSUFFICIENT_EVIDENCE")
                final_decision = original_decision
                reason_parts: List[str] = []
                theme_name = tc.get("theme", "")

                # a. stale 降级
                if "stale_data" in dataops_output.risk_flags:
                    if original_decision == "PASS":
                        final_decision = "WATCH"
                        reason_parts.append("数据过期，PASS 降级为 WATCH")
                    elif original_decision == "WATCH":
                        final_decision = "INSUFFICIENT_EVIDENCE"
                        reason_parts.append("数据过期，WATCH 降级为 INSUFFICIENT_EVIDENCE")

                # b. demo_mode 降级
                if is_demo_mode and final_decision == "PASS":
                    final_decision = "WATCH"
                    reason_parts.append("portfolio_risk_check 为 demo 模式，PASS 降级为 WATCH")

                # c. 无 evidence_id BLOCK
                tc_evidence_ids = tc.get("evidence_ids", [])
                if not tc_evidence_ids:
                    final_decision = "BLOCK"
                    reason_parts.append("无 evidence_ids，直接 BLOCK")

                # d. 默认保持 original_decision
                if not reason_parts:
                    reason_parts.append(f"无额外风险，保持原判定 {original_decision}")

                theme_verdicts.append({
                    "theme": theme_name,
                    "original_decision": original_decision,
                    "final_decision": final_decision,
                    "reason": "；".join(reason_parts),
                })

        # --- stock_verdicts ---
        stock_verdicts: List[Dict[str, Any]] = []
        if stock_candidates is not None:
            for sc in stock_candidates:
                original_decision = sc.get("decision", "INSUFFICIENT_EVIDENCE")
                final_decision = original_decision
                reason_parts: List[str] = []
                symbol = str(sc.get("symbol", ""))

                # a. stale 降级
                if "stale_data" in dataops_output.risk_flags:
                    if original_decision == "PASS":
                        final_decision = "WATCH"
                        reason_parts.append("数据过期，PASS 降级为 WATCH")
                    elif original_decision == "WATCH":
                        final_decision = "INSUFFICIENT_EVIDENCE"
                        reason_parts.append("数据过期，WATCH 降级为 INSUFFICIENT_EVIDENCE")

                # b. demo_mode 降级
                if is_demo_mode and final_decision == "PASS":
                    final_decision = "WATCH"
                    reason_parts.append("portfolio_risk_check 为 demo 模式，PASS 降级为 WATCH")

                # c. 无 evidence_id BLOCK
                sc_evidence_ids = sc.get("evidence_ids", [])
                if not sc_evidence_ids:
                    final_decision = "BLOCK"
                    reason_parts.append("无 evidence_ids，直接 BLOCK")

                # d. ST/停牌/涨跌停 BLOCK
                sym_risk = symbol_risk_flags_map.get(symbol, [])
                block_risk_flags = {"st_stock", "suspended", "limit_up", "limit_down"}
                found_block_risks = [f for f in sym_risk if f in block_risk_flags]
                if found_block_risks:
                    final_decision = "BLOCK"
                    reason_parts.append(f"存在风险标记: {', '.join(found_block_risks)}，直接 BLOCK")

                # e. 持仓/仓位风险
                if symbol in incomplete_valuation_symbols:
                    if final_decision != "BLOCK":
                        final_decision = "WATCH"
                        reason_parts.append("该标的在持仓中且估值不完整，降级为 WATCH")

                # f. 默认保持 original_decision
                if not reason_parts:
                    reason_parts.append(f"无额外风险，保持原判定 {original_decision}")

                stock_verdicts.append({
                    "symbol": symbol,
                    "original_decision": original_decision,
                    "final_decision": final_decision,
                    "reason": "；".join(reason_parts),
                })

        # v2 BLOCK 汇入 block_reasons（仅来自 verdicts 的 BLOCK，非 v1 逻辑）
        for tv in theme_verdicts:
            if tv["final_decision"] == "BLOCK":
                block_reasons.append(f"题材 {tv['theme']} 风控 BLOCK: {tv['reason']}")
        for sv in stock_verdicts:
            if sv["final_decision"] == "BLOCK":
                block_reasons.append(f"股票 {sv['symbol']} 风控 BLOCK: {sv['reason']}")

        # ========== 综合判定：BLOCK 优先级最高 ==========
        if block_reasons:
            decision = "BLOCK"
            reason = "；".join(block_reasons)
            confidence = "high"
        elif watch_reasons:
            decision = "WATCH"
            reason = "；".join(watch_reasons)
            confidence = "medium"
        else:
            decision = "PASS"
            reason = "风控检查未发现阻断性风险"
            confidence = "high"

        details = {
            "block_reasons": block_reasons,
            "watch_reasons": watch_reasons,
            "no_evidence_themes_count": len(no_evidence_themes),
            "no_evidence_stocks_count": len(no_evidence_stocks),
            "valuation_complete": valuation_complete,
            "active_positions": active_positions,
            "strategy_blocked_reason": blocked_reason,
            "dataops_decision": dataops_output.decision,
            "research_decision": research_output.decision,
            "quant_decision": quant_output.decision,
            # v2 字段
            "theme_verdicts": theme_verdicts,
            "stock_verdicts": stock_verdicts,
        }

        return AgentOutput(
            agent="RiskAgent",
            trade_date=global_date,
            input_sources=input_sources,
            summary=f"风控判定: {decision}，风险标记 {len(risk_flags)} 项",
            evidence_ids=[],
            confidence=confidence,
            risk_flags=risk_flags,
            decision=decision,
            reason=reason,
            details=details,
        )


class CIOAgent:
    """CIO 投委会总结：汇总前四岗结果，输出最终结论。"""

    @staticmethod
    def _apply_debate_consensus(
        theme_watchlist: List[Dict[str, Any]],
        stock_watchlist: List[Dict[str, Any]],
        blocked_items: List[Dict[str, Any]],
        insufficient_evidence_items: List[Dict[str, Any]],
        debate_results: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """把 Agent 讨论共识反向写入 CIO 列表。

        规则：
          - 讨论共识为 BLOCK -> 从 watchlist 移除，加入 blocked_items
          - 讨论共识为 BUY_CANDIDATE -> 在 watchlist 中标记为 PASS
          - 讨论共识为 INSUFFICIENT_EVIDENCE -> 从 watchlist 移除，加入 insufficient_evidence_items
          - 讨论共识为 WATCH -> 保持观察
        """
        consensus_by_target: Dict[str, str] = {}
        for d in debate_results or []:
            target_type = d.get("target_type", "")
            target_key = d.get("target_key", "")
            if target_type and target_key:
                consensus_by_target[f"{target_type}:{target_key}"] = d.get("consensus_decision", "")

        def _key(item: Dict[str, Any], target_type: str) -> str:
            target_key = item.get("theme") if target_type == "theme" else item.get("symbol")
            return f"{target_type}:{target_key}"

        new_theme_watchlist: List[Dict[str, Any]] = []
        for item in theme_watchlist:
            decision = consensus_by_target.get(_key(item, "theme"), item.get("decision", ""))
            if decision == "BLOCK":
                blocked_items.append({
                    "item": item.get("theme", ""),
                    "reason": f"Agent 讨论共识 BLOCK: {item.get('reason', '')}",
                })
                continue
            if decision == "INSUFFICIENT_EVIDENCE":
                insufficient_evidence_items.append({
                    "item": item.get("theme", ""),
                    "reason": f"Agent 讨论共识证据不足: {item.get('reason', '')}",
                })
                continue
            item = dict(item)
            if decision == "BUY_CANDIDATE":
                item["decision"] = "PASS"
            new_theme_watchlist.append(item)

        new_stock_watchlist: List[Dict[str, Any]] = []
        for item in stock_watchlist:
            decision = consensus_by_target.get(_key(item, "stock"), item.get("decision", ""))
            if decision == "BLOCK":
                blocked_items.append({
                    "item": item.get("symbol", ""),
                    "reason": f"Agent 讨论共识 BLOCK: {item.get('reason', '')}",
                })
                continue
            if decision == "INSUFFICIENT_EVIDENCE":
                insufficient_evidence_items.append({
                    "item": item.get("symbol", ""),
                    "reason": f"Agent 讨论共识证据不足: {item.get('reason', '')}",
                })
                continue
            item = dict(item)
            if decision == "BUY_CANDIDATE":
                item["decision"] = "PASS"
            new_stock_watchlist.append(item)

        return new_theme_watchlist, new_stock_watchlist, blocked_items, insufficient_evidence_items

    def run(
        self,
        dataops_output: AgentOutput,
        research_output: AgentOutput,
        quant_output: AgentOutput,
        risk_output: AgentOutput,
        trade_date: str = "",
        theme_candidates: List[Dict[str, Any]] | None = None,
        stock_candidates: List[Dict[str, Any]] | None = None,
        debate_results: List[Dict[str, Any]] | None = None,
    ) -> CIOOutput:
        """执行 CIO 汇总。

        Args:
            dataops_output: DataOps Agent 输出
            research_output: Research Agent 输出
            quant_output: QuantValidation Agent 输出
            risk_output: Risk Agent 输出
            trade_date: 全局最新交易日
            theme_candidates: 题材候选列表（v2，可选）
            stock_candidates: 股票候选列表（v2，可选）
            debate_results: Agent 讨论结果列表（v2，可选）

        Returns:
            CIOOutput 包含最终投委会结论
        """
        global_date = trade_date or dataops_output.trade_date
        input_sources = [
            "DataOpsAgent",
            "ResearchAgent",
            "QuantValidationAgent",
            "RiskAgent",
        ]

        # 收集所有 evidence_ids
        all_evidence_ids = list(set(
            dataops_output.evidence_ids
            + research_output.evidence_ids
            + quant_output.evidence_ids
            + risk_output.evidence_ids
        ))

        # 最终结论判定逻辑（优先级：BLOCK > INSUFFICIENT_EVIDENCE > WATCH > BUY_CANDIDATE）
        no_trade_reasons: List[str] = []
        blocked_directions: List[Dict[str, Any]] = []
        watch_directions: List[Dict[str, Any]] = []
        candidate_stocks: List[Dict[str, Any]] = []

        # 1. Risk BLOCK → CIO BLOCK
        if risk_output.decision == "BLOCK":
            final_level = "BLOCK"
            no_trade_reasons.append(f"风控否决: {risk_output.reason}")
            blocked_directions.append({
                "source": "RiskAgent",
                "reason": risk_output.reason,
            })

        # 2. DataOps BLOCK → CIO BLOCK
        elif dataops_output.decision == "BLOCK":
            final_level = "BLOCK"
            no_trade_reasons.append(f"数据岗否决: {dataops_output.reason}")
            blocked_directions.append({
                "source": "DataOpsAgent",
                "reason": dataops_output.reason,
            })

        # 3. Research INSUFFICIENT_EVIDENCE → CIO INSUFFICIENT_EVIDENCE
        elif research_output.decision == "INSUFFICIENT_EVIDENCE":
            final_level = "INSUFFICIENT_EVIDENCE"
            no_trade_reasons.append(f"行研岗证据不足: {research_output.reason}")

        # 4. QuantValidation 全部 INSUFFICIENT_EVIDENCE
        elif quant_output.decision == "INSUFFICIENT_EVIDENCE":
            final_level = "INSUFFICIENT_EVIDENCE"
            no_trade_reasons.append(f"量化验证岗证据不足: {quant_output.reason}")

        # 5. 全部 PASS → BUY_CANDIDATE
        elif (
            dataops_output.decision == "PASS"
            and research_output.decision == "PASS"
            and quant_output.decision == "PASS"
            and risk_output.decision == "PASS"
        ):
            final_level = "BUY_CANDIDATE"

        # 6. 混合 PASS + WATCH → WATCH
        else:
            final_level = "WATCH"
            if dataops_output.decision == "WATCH":
                watch_directions.append({
                    "source": "DataOpsAgent",
                    "reason": dataops_output.reason,
                })
            if risk_output.decision == "WATCH":
                watch_directions.append({
                    "source": "RiskAgent",
                    "reason": risk_output.reason,
                })

        # 构建候选股票列表（只包含有 evidence_ids 的）
        quant_details = quant_output.details
        for result in quant_details.get("validation_results", []):
            if result.get("decision") == "PASS" and result.get("evidence_ids"):
                candidate_stocks.append({
                    "symbol": result.get("symbol", ""),
                    "name": result.get("name", ""),
                    "evidence_ids": result.get("evidence_ids", []),
                    "reason": result.get("reason", ""),
                })

        # 构建可观察方向（从 Research 提取，只包含有 evidence_id 的）
        research_details = research_output.details
        for theme in research_details.get("themes", []):
            if theme.get("evidence_id"):
                watch_directions.append({
                    "source": "ResearchAgent",
                    "name": theme.get("name", ""),
                    "evidence_id": theme.get("evidence_id", ""),
                    "strength": theme.get("strength", ""),
                })

        # 从 Research directions 补充
        for direction in research_details.get("directions", []):
            watch_directions.append({
                "source": "DragonEye",
                "name": direction.get("name", ""),
                "count": direction.get("count"),
            })

        # ========== v2: 从 risk_output.details 获取 verdicts ==========
        risk_details = risk_output.details
        theme_verdicts = risk_details.get("theme_verdicts", [])
        stock_verdicts = risk_details.get("stock_verdicts", [])

        # 构建 theme_watchlist：final_decision 为 PASS 或 WATCH 的题材
        theme_watchlist: List[Dict[str, Any]] = []
        for tv in theme_verdicts:
            if tv.get("final_decision") in ("PASS", "WATCH"):
                # 从 theme_candidates 中查找对应的 evidence_ids
                ev_ids: List[str] = []
                if theme_candidates is not None:
                    for tc in theme_candidates:
                        if tc.get("theme") == tv.get("theme"):
                            ev_ids = tc.get("evidence_ids", [])
                            break
                theme_watchlist.append({
                    "theme": tv.get("theme", ""),
                    "evidence_ids": ev_ids,
                    "decision": tv.get("final_decision", ""),
                    "reason": tv.get("reason", ""),
                })

        # 构建 stock_watchlist：final_decision 为 PASS 或 WATCH 的股票
        stock_watchlist: List[Dict[str, Any]] = []
        for sv in stock_verdicts:
            if sv.get("final_decision") in ("PASS", "WATCH"):
                # 从 stock_candidates 中查找对应的 evidence_ids 和 stock_name
                ev_ids: List[str] = []
                stock_name = ""
                if stock_candidates is not None:
                    for sc in stock_candidates:
                        if str(sc.get("symbol", "")) == sv.get("symbol", ""):
                            ev_ids = sc.get("evidence_ids", [])
                            stock_name = str(sc.get("stock_name", ""))
                            break
                stock_watchlist.append({
                    "symbol": sv.get("symbol", ""),
                    "stock_name": stock_name,
                    "evidence_ids": ev_ids,
                    "decision": sv.get("final_decision", ""),
                    "reason": sv.get("reason", ""),
                })

        # 构建 blocked_items：final_decision 为 BLOCK 的项
        blocked_items: List[Dict[str, Any]] = []
        for tv in theme_verdicts:
            if tv.get("final_decision") == "BLOCK":
                blocked_items.append({
                    "item": tv.get("theme", ""),
                    "reason": tv.get("reason", ""),
                })
        for sv in stock_verdicts:
            if sv.get("final_decision") == "BLOCK":
                blocked_items.append({
                    "item": sv.get("symbol", ""),
                    "reason": sv.get("reason", ""),
                })

        # 构建 insufficient_evidence_items：final_decision 为 INSUFFICIENT_EVIDENCE 的项
        insufficient_evidence_items: List[Dict[str, Any]] = []
        for tv in theme_verdicts:
            if tv.get("final_decision") == "INSUFFICIENT_EVIDENCE":
                insufficient_evidence_items.append({
                    "item": tv.get("theme", ""),
                    "reason": tv.get("reason", ""),
                })
        for sv in stock_verdicts:
            if sv.get("final_decision") == "INSUFFICIENT_EVIDENCE":
                insufficient_evidence_items.append({
                    "item": sv.get("symbol", ""),
                    "reason": sv.get("reason", ""),
                })

        # v2.5: 应用 Agent 讨论共识到 CIO 最终列表
        if debate_results:
            (
                theme_watchlist,
                stock_watchlist,
                blocked_items,
                insufficient_evidence_items,
            ) = self._apply_debate_consensus(
                theme_watchlist,
                stock_watchlist,
                blocked_items,
                insufficient_evidence_items,
                debate_results,
            )

        # 生成 market_view
        quant_flow = _read_json("quant_flow_latest.json")
        market_status = ""
        for stage in quant_flow.get("stages", []):
            if stage.get("stage") == "portfolio_risk_check":
                market_status = stage.get("data", {}).get("market_status", "")
                break
        market_view = (
            f"数据状态: {dataops_output.decision}; "
            f"研究状态: {research_output.decision}; "
            f"市场状态: {market_status or '未知'}"
        )

        # v2 final_decision 判定
        if risk_output.decision == "BLOCK":
            v2_final_decision = "BLOCK"
        elif dataops_output.decision == "BLOCK":
            v2_final_decision = "BLOCK"
        elif not theme_watchlist and not stock_watchlist:
            v2_final_decision = "INSUFFICIENT_EVIDENCE"
        elif all(
            item.get("decision") == "PASS"
            for item in theme_watchlist + stock_watchlist
        ):
            v2_final_decision = "BUY_CANDIDATE"
        else:
            v2_final_decision = "WATCH"

        # ========== v1 字段向后兼容转换 ==========
        # today_watch_directions: 从 theme_watchlist 转换
        v1_watch_directions = list(watch_directions)
        for tw in theme_watchlist:
            v1_watch_directions.append({
                "source": "RiskVerdict",
                "name": tw.get("theme", ""),
                "decision": tw.get("decision", ""),
                "reason": tw.get("reason", ""),
            })

        # today_blocked_directions: 从 blocked_items 转换
        v1_blocked_directions = list(blocked_directions)
        for bi in blocked_items:
            v1_blocked_directions.append({
                "source": "RiskVerdict",
                "item": bi.get("item", ""),
                "reason": bi.get("reason", ""),
            })

        # today_candidate_stocks: 从 stock_watchlist 中 decision=PASS 的转换
        v1_candidate_stocks = list(candidate_stocks)
        for sw in stock_watchlist:
            if sw.get("decision") == "PASS":
                v1_candidate_stocks.append({
                    "symbol": sw.get("symbol", ""),
                    "name": sw.get("stock_name", ""),
                    "evidence_ids": sw.get("evidence_ids", []),
                    "reason": sw.get("reason", ""),
                })

        # 摘要
        summary_parts = [
            f"最终结论: {final_level}",
            f"候选股票 {len(v1_candidate_stocks)} 只",
            f"可观察方向 {len(v1_watch_directions)} 个",
        ]
        if v1_blocked_directions:
            summary_parts.append(f"禁止方向 {len(v1_blocked_directions)} 个")
        if no_trade_reasons:
            summary_parts.append(f"不应交易原因 {len(no_trade_reasons)} 项")

        # reason
        if final_level == "BLOCK":
            reason = f"投委会否决: {'; '.join(no_trade_reasons)}"
        elif final_level == "INSUFFICIENT_EVIDENCE":
            reason = f"证据不足: {'; '.join(no_trade_reasons)}"
        elif final_level == "BUY_CANDIDATE":
            reason = "四岗全部通过，存在有证据支持的候选股票"
        else:
            reason = f"部分岗存在观望: {'; '.join(d.get('reason', '') for d in watch_directions[:3])}"

        return CIOOutput(
            agent="CIOAgent",
            trade_date=global_date,
            input_sources=input_sources,
            summary="；".join(summary_parts),
            evidence_ids=all_evidence_ids,
            confidence="high" if final_level in ("BUY_CANDIDATE", "BLOCK") else "medium" if final_level == "WATCH" else "low",
            risk_flags=risk_output.risk_flags,
            decision=final_level,
            reason=reason,
            details={
                "dataops_decision": dataops_output.decision,
                "research_decision": research_output.decision,
                "quant_decision": quant_output.decision,
                "risk_decision": risk_output.decision,
            },
            # v1 字段（向后兼容）
            today_watch_directions=v1_watch_directions,
            today_blocked_directions=v1_blocked_directions,
            today_candidate_stocks=v1_candidate_stocks,
            today_no_trade_reasons=no_trade_reasons,
            final_output_level=final_level,
            # v2 字段
            market_view=market_view,
            theme_watchlist=theme_watchlist,
            stock_watchlist=stock_watchlist,
            blocked_items=blocked_items,
            insufficient_evidence_items=insufficient_evidence_items,
            final_decision=v2_final_decision,
        )
