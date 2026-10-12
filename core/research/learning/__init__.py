"""P3.5 受治理的自校准学习闭环。

按 P3 计划 Task P3.5 实施：
- contracts: 校准参数与上线流程契约
- calibration_engine: 校准引擎
- promotion_policy: 升级策略与跨域防火墙

只允许校准 6 类目标；绝对禁止自动修改 6 类目标。
"""
from .contracts import (  # noqa: F401
    ALLOWED_CALIBRATION_DOMAINS,
    CalibrationOutcome,
    CalibrationParameter,
    CalibrationStatus,
    FORBIDDEN_MUTATION_TARGETS,
    ForbiddenMutationAttempt,
    LearningGovernanceError,
    assert_domain_is_calibratable,
    assert_target_not_forbidden,
    is_status_transition_allowed,
)
from .calibration_engine import CalibrationEngine  # noqa: F401
from .promotion_policy import (  # noqa: F401
    CrossDomainFirewall,
    DEFAULT_MINIMUM_SAMPLE_SIZE,
    DOMAIN_MINIMUM_SAMPLE_SIZE,
    PromotionPolicy,
)
