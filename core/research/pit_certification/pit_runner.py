"""P3.9 PIT 认证执行器。

按 P3.9 硬门槛：
- 按时间顺序遍历，每个时点只能用当时可见的信息
- 必须使用当时有效的模型版本（从 model_governance.registry 查询）
- 必须使用当时有效的 policy（不能事后回填）
- 支持断点续跑（checkpoint）
- 支持并行处理（多进程，CPU 八核）
- 不删除失败样本
- 同时记录成功和失败

设计要点：
- 通过 prediction_runner 注入预测执行逻辑，支持真实运行和测试 mock
- checkpoint 以 JSON 文件持久化已完成 (symbol, pit_time) 对
- 并行使用 concurrent.futures.ThreadPoolExecutor（兼容 mock 且跨平台）
- 失败样本与成功样本同等保留，不选择性删除
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Protocol

from core.research.pit_certification.contracts import (
    CertificationStatus,
    CompanyCertificationMember,
    PITCertificationResult,
    PITCertificationScope,
    PITErrorAttribution,
    PITLeakageCheck,
    MarketState,
)
from core.research.pit_certification.leakage_checker import check_future_leakage


class PredictionRunner(Protocol):
    """预测执行器协议。

    真实运行时由调用方实现，调用当时有效的模型版本和 policy 执行预测。
    """

    def run_single(
        self,
        member: CompanyCertificationMember,
        pit_time: str,
    ) -> PITCertificationResult:
        """执行单公司单时点预测。

        Args:
            member: 公司成员
            pit_time: PIT 时点

        Returns:
            PITCertificationResult: 认证结果（含成功或失败）
        """
        ...


def run_pit_certification(
    scope: PITCertificationScope,
    checkpoint_dir: Path,
    members: list[CompanyCertificationMember],
    pit_times: list[str],
    prediction_runner: Callable[[CompanyCertificationMember, str], PITCertificationResult],
    *,
    leakage_inputs_provider: Callable[[CompanyCertificationMember, str], dict[str, Any]] | None = None,
    model_version_provider: Callable[[str, str], dict[str, Any]] | None = None,
    max_workers: int | None = None,
    resume: bool = True,
) -> list[PITCertificationResult]:
    """执行大规模 PIT 认证。

    按时间顺序遍历，每个时点只能用当时可见的信息。
    支持断点续跑和并行处理。不删除失败样本。

    Args:
        scope: 认证范围
        checkpoint_dir: checkpoint 目录路径
        members: 公司成员列表
        pit_times: PIT 时点列表（按时间升序）
        prediction_runner: 预测执行器，接受 (member, pit_time) 返回认证结果
        leakage_inputs_provider: 可选的泄漏检查输入提供器
        model_version_provider: 可选的模型版本提供器，接受 (symbol, pit_time)
        max_workers: 并行工作线程数；None 时使用 min(8, os.cpu_count())
        resume: 是否从 checkpoint 续跑

    Returns:
        list[PITCertificationResult]: 全部认证结果（含成功和失败）

    Note:
        - 不选择性删除失败样本
        - 同时记录成功和失败
        - 每个时点只能用当时可见的信息
    """
    checkpoint_dir = Path(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_dir / "pit_certification_checkpoint.json"

    # 加载已完成的结果
    completed: dict[str, PITCertificationResult] = {}
    if resume and checkpoint_path.exists():
        completed = _load_checkpoint(checkpoint_path)

    # 构建任务列表：(member, pit_time)
    # 按时间顺序排列，确保时间顺序遍历
    tasks: list[tuple[CompanyCertificationMember, str]] = []
    for pit_time in sorted(pit_times):
        for member in members:
            task_key = _task_key(member.symbol, pit_time)
            if task_key not in completed:
                tasks.append((member, pit_time))

    if not tasks:
        # 全部已完成
        return list(completed.values())

    # 确定并行度
    try:
        workers = int(max_workers) if max_workers is not None else min(8, os.cpu_count() or 4)
    except (TypeError, ValueError, OverflowError):
        workers = min(8, os.cpu_count() or 4)
    workers = max(1, min(workers, len(tasks)))

    # 并行执行
    results: list[PITCertificationResult] = list(completed.values())

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_task = {
            executor.submit(
                _run_single_task,
                member,
                pit_time,
                prediction_runner,
                leakage_inputs_provider,
                model_version_provider,
            ): (member, pit_time)
            for member, pit_time in tasks
        }

        for future in as_completed(future_to_task):
            member, pit_time = future_to_task[future]
            try:
                result = future.result()
            except Exception as exc:
                # 异常不丢弃，记录为失败样本（不选择性删除）
                result = PITCertificationResult(
                    symbol=member.symbol,
                    company_name=member.company_name,
                    industry=member.industry,
                    pit_time=pit_time,
                    status=CertificationStatus.FAILURE,
                    notes=f"执行异常: {type(exc).__name__}: {exc}",
                )
            results.append(result)
            completed[_task_key(member.symbol, pit_time)] = result

            # 增量保存 checkpoint
            _save_checkpoint(checkpoint_path, completed)

    return results


def load_checkpoint_results(checkpoint_path: Path) -> list[PITCertificationResult]:
    """加载断点结果供正式认证预检复用。"""
    return sorted(
        _load_checkpoint(checkpoint_path).values(),
        key=lambda result: (result.pit_time, result.symbol),
    )


def _run_single_task(
    member: CompanyCertificationMember,
    pit_time: str,
    prediction_runner: Callable[[CompanyCertificationMember, str], PITCertificationResult],
    leakage_inputs_provider: Callable[[CompanyCertificationMember, str], dict[str, Any]] | None,
    model_version_provider: Callable[[str, str], dict[str, Any]] | None,
) -> PITCertificationResult:
    """执行单个认证任务（含泄漏检查）。

    Args:
        member: 公司成员
        pit_time: PIT 时点
        prediction_runner: 预测执行器
        leakage_inputs_provider: 泄漏检查输入提供器
        model_version_provider: 模型版本提供器

    Returns:
        PITCertificationResult: 认证结果
    """
    # 先执行泄漏检查
    leakage_check: PITLeakageCheck | None = None
    if leakage_inputs_provider is not None:
        inputs = leakage_inputs_provider(member, pit_time)
        model_version_info = None
        if model_version_provider is not None:
            model_version_info = model_version_provider(member.symbol, pit_time)
        leakage_check = check_future_leakage(
            symbol=member.symbol,
            pit_time=pit_time,
            inputs=inputs,
            model_version_info=model_version_info,
        )

        # 检测到泄漏时直接标记为 leakage_detected，不执行预测
        if leakage_check.has_leakage:
            return PITCertificationResult(
                symbol=member.symbol,
                company_name=member.company_name,
                industry=member.industry,
                pit_time=pit_time,
                status=CertificationStatus.LEAKAGE_DETECTED,
                leakage_check=leakage_check,
                notes="检测到未来信息泄漏，未执行预测",
            )

    # 执行预测
    result = prediction_runner(member, pit_time)
    if not isinstance(result, PITCertificationResult):
        raise TypeError("prediction_runner must return PITCertificationResult")

    # 附加泄漏检查结果
    if leakage_check is not None:
        result.leakage_check = leakage_check

    return result


def _task_key(symbol: str, pit_time: str) -> str:
    """生成任务唯一键。

    Args:
        symbol: 公司代码
        pit_time: PIT 时点

    Returns:
        str: 任务唯一键
    """
    return f"{symbol}|{pit_time}"


def _load_checkpoint(checkpoint_path: Path) -> dict[str, PITCertificationResult]:
    """加载 checkpoint。

    Args:
        checkpoint_path: checkpoint 文件路径

    Returns:
        dict: task_key -> PITCertificationResult
    """
    try:
        with checkpoint_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}

    completed: dict[str, PITCertificationResult] = {}
    if not isinstance(payload, dict):
        return completed
    items = payload.get("completed", [])
    if not isinstance(items, list):
        return completed
    for item in items:
        try:
            result = _dict_to_result(item)
            key = _task_key(result.symbol, result.pit_time)
            completed[key] = result
        except (KeyError, TypeError, ValueError):
            continue
    return completed


def _save_checkpoint(checkpoint_path: Path, completed: dict[str, PITCertificationResult]) -> None:
    """保存 checkpoint。

    Args:
        checkpoint_path: checkpoint 文件路径
        completed: 已完成的结果字典
    """
    payload = {
        "schema_version": 1,
        "total_completed": len(completed),
        "completed": [r.to_dict() for r in completed.values()],
    }
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
    temporary.replace(checkpoint_path)


def _dict_to_result(item: dict[str, Any]) -> PITCertificationResult:
    """将字典转换为 PITCertificationResult。

    Args:
        item: 字典形式的认证结果

    Returns:
        PITCertificationResult: 认证结果对象
    """
    if not isinstance(item, dict):
        raise TypeError("checkpoint result must be a mapping")

    leakage_check = None
    lc_data = item.get("leakage_check")
    if lc_data and isinstance(lc_data, dict):
        leakage_check = PITLeakageCheck(
            symbol=lc_data.get("symbol", ""),
            pit_time=lc_data.get("pit_time", ""),
            has_leakage=lc_data.get("has_leakage", False),
            leakage_items=lc_data.get("leakage_items", []),
            checked_count=lc_data.get("checked_count", 0),
            blocked_count=lc_data.get("blocked_count", 0),
        )

    error_attribution = None
    ea_value = item.get("error_attribution")
    if ea_value:
        try:
            error_attribution = PITErrorAttribution(ea_value)
        except ValueError:
            error_attribution = None

    market_state = MarketState.SIDEWAYS
    ms_value = item.get("market_state", "sideways")
    try:
        market_state = MarketState(ms_value)
    except ValueError:
        market_state = MarketState.SIDEWAYS

    status = CertificationStatus.INSUFFICIENT_DATA
    st_value = item.get("status", "insufficient_data")
    try:
        status = CertificationStatus(st_value)
    except ValueError:
        status = CertificationStatus.INSUFFICIENT_DATA

    return PITCertificationResult(
        symbol=item.get("symbol", ""),
        company_name=item.get("company_name", ""),
        industry=item.get("industry", ""),
        pit_time=item.get("pit_time", ""),
        status=status,
        prediction_value=item.get("prediction_value"),
        prediction_interval_low=item.get("prediction_interval_low"),
        prediction_interval_high=item.get("prediction_interval_high"),
        actual_value=item.get("actual_value"),
        consensus_value=item.get("consensus_value"),
        absolute_error=item.get("absolute_error"),
        percentage_error=item.get("percentage_error"),
        interval_covered=item.get("interval_covered"),
        consensus_relative_error=item.get("consensus_relative_error"),
        model_version=item.get("model_version", ""),
        policy_version=item.get("policy_version", ""),
        error_attribution=error_attribution,
        error_attribution_detail=item.get("error_attribution_detail", ""),
        leakage_check=leakage_check,
        market_state=market_state,
        vp_formed=item.get("vp_formed", False),
        vp_invalidation_reason=item.get("vp_invalidation_reason", ""),
        notes=item.get("notes", ""),
    )
