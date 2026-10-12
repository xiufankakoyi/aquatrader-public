"""P3.1 初始模型注册引导。

按 P3 计划 Task P3.1 第 2 条「先登记模型/数据/实验版本，再开始实现或运行」，
daemon 启动时调用 ``ensure_initial_models_registered`` 把已有的 shadow 阶段
研究模型族写入 ``data/research_control/p3_model_registry.json``。

设计原则：
- 幂等：重复调用安全，已存在的 (model_id, version) 跳过
- 严谨：初始登记只写入 SHADOW 状态，不主动升 ACTIVE；
  active 升级必须由独立验证人通过 workflow 完成并指定 rollback_version
- 可追溯：每个模型记录 code/config fingerprint，便于后续变更检测
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from core.research.model_governance.contracts import (
    InputContract,
    ModelStatus,
    ModelVersion,
    OutputContract,
)
from core.research.model_governance.registry import (
    ModelRegistry,
    ModelRegistryConflictError,
)

logger = logging.getLogger(__name__)


def _sha256_text(text: str) -> str:
    """对纯文本计算 SHA-256 摘要。

    Args:
        text: 待计算文本。

    Returns:
        16 进制摘要字符串。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_fingerprint(path: Path) -> str:
    """计算文件内容的 SHA-256 摘要；文件不存在时返回空串。

    Args:
        path: 文件路径。

    Returns:
        16 进制摘要字符串；读取失败返回 ""。
    """
    try:
        if not path.exists():
            return ""
        return _sha256_text(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("计算文件指纹失败 %s: %s", path, exc)
        return ""


def _build_shadow_learning_model(
    *,
    learning_bridge_path: Path,
    policy_version: str,
    effective_from: str,
) -> ModelVersion:
    """构造 shadow 阶段的研究学习闭环模型版本。

    Args:
        learning_bridge_path: learning_bridge.py 源文件路径，用于计算 code_fingerprint。
        policy_version: 学习策略版本字符串（来自 daemon 常量 LEARNING_POLICY_VERSION）。
        effective_from: 工程认证开始日（YYYY-MM-DD）。

    Returns:
        处于 SHADOW 状态的 ModelVersion 实例。
    """
    input_fields = {
        "symbol": "str",
        "status": "str(READY|WATCH)",
        "research_validated": "bool",
        "final_score": "float[0,1]",
        "mapping_confidence": "float[0,1]",
        "evidence_refs": "list[str]",
        "claim_ids": "list[str]",
        "source_snapshot_id": "str",
        "strategy_version": "str",
        "degradation_reasons": "list[str]",
    }
    output_fields = {
        "prediction_id": "str",
        "entity_id": "str",
        "predicted_value": "bool",
        "confidence": "float[0,1]",
        "as_of": "iso8601",
        "due_at": "iso8601",
        "model_version": "str",
        "policy_version": "str",
        "evidence_refs": "list[str]",
        "feature_snapshot_hash": "str",
    }
    input_contract = InputContract(
        fields=input_fields,
        data_source_refs=["daily_observations", "research_value_runs"],
        contract_hash="",
    )
    input_contract = InputContract(
        fields=input_contract.fields,
        data_source_refs=input_contract.data_source_refs,
        contract_hash=input_contract.compute_hash(),
    )
    output_contract = OutputContract(
        fields=output_fields,
        validation_rules={
            "confidence_range": "[0.0, 1.0]",
            "due_at_must_be_future_trading_day": "true",
            "evidence_refs_non_empty": "true",
            "no_trade_action_phrasing": "true",
        },
        contract_hash="",
    )
    output_contract = OutputContract(
        fields=output_contract.fields,
        validation_rules=output_contract.validation_rules,
        contract_hash=output_contract.compute_hash(),
    )
    code_fingerprint = _file_fingerprint(learning_bridge_path)
    config_fingerprint = _sha256_text(policy_version)

    return ModelVersion(
        model_id="research_learning_shadow_observations",
        model_family="research_learning_shadow",
        domain="forward_learning",
        version="template_v1",
        owner="system",
        reviewer="",  # 待独立验证人签署
        training_or_calibration_window=f"{effective_from}..",
        input_contract=input_contract,
        output_contract=output_contract,
        applicable_universe=["A_SHARE"],
        limitations=[
            "shadow 阶段，不参与正式决策，不可激活为 active",
            f"policy_version={policy_version}",
            f"code_fingerprint={code_fingerprint[:16] or 'missing'}，由 learning_bridge.py 内容生成",
            "_predictions 只接受同时满足 READY/WATCH + research_validated + 非空证据 + 合法分数的观察",
            "结算依赖 ForwardObservationBarReader 提供的 PIT 严格日线，缺失时保持 pending",
            "shadow 校准提案永远不会自动激活，必须经过独立审查",
        ],
        benchmark={},  # 待 4 个真实财报季度后填写
        validation_report={},  # 待独立验证签署后填写
        status=ModelStatus.SHADOW,
        effective_from=effective_from,
        effective_to="",  # 未退役
        rollback_version="",  # shadow 阶段无需 rollback
        code_fingerprint=code_fingerprint,
        config_fingerprint=config_fingerprint,
        data_fingerprint="",  # 数据指纹由运行时按 observations 计算
    )


def ensure_initial_models_registered(
    registry: ModelRegistry,
    *,
    learning_bridge_path: Path | None = None,
    policy_version: str = "research_monitor_shadow_learning_v1",
    effective_from: str = "2026-07-22",
) -> list[str]:
    """确保 P3 初始模型族已登记；幂等。

    Args:
        registry: ModelRegistry 实例。
        learning_bridge_path: learning_bridge.py 路径；None 时按项目相对路径推断。
        policy_version: 学习策略版本字符串。
        effective_from: 工程认证开始日。

    Returns:
        本次新登记的 model_id 列表（已存在的跳过）。
    """
    if learning_bridge_path is None:
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        learning_bridge_path = (
            project_root
            / "core"
            / "research"
            / "control_plane"
            / "learning_bridge.py"
        )

    registered: list[str] = []
    model = _build_shadow_learning_model(
        learning_bridge_path=learning_bridge_path,
        policy_version=policy_version,
        effective_from=effective_from,
    )
    existing = registry.get_version(model.model_id, model.version)
    if existing is not None:
        return registered
    try:
        registry.register(model)
        registered.append(model.model_id)
        logger.info(
            "已登记模型 model_id=%s version=%s status=%s",
            model.model_id,
            model.version,
            model.status.value,
        )
    except ModelRegistryConflictError as exc:
        # 并发场景下可能被其他 worker 先注册，记录后跳过
        logger.info("模型已存在跳过: %s", exc)
    return registered
