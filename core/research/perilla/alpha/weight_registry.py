"""WeightRegistry：评分权重的集中管理与热加载。

从 config/scoring_weights.yaml 加载所有权重，支持运行时热重载，
并提供默认值回退。所有评分模块通过此注册表获取权重，避免硬编码。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# YAML 可选依赖
try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    logger.warning("PyYAML not installed; using built-in default weights")


# 内置默认权重（与YAML一致，作为fallback）
_DEFAULT_WEIGHTS: dict[str, Any] = {
    "claim_scoring": {
        "positive_weights": {
            "source_weight": 0.30, "extractor_confidence": 0.20,
            "evidence_clarity": 0.20, "relation_clarity": 0.15,
            "recency_score": 0.10, "text_quality_score": 0.05,
        },
        "max_noise_penalty": 0.30,
        "max_contradiction_penalty": 0.50,
        "source_floor": {
            "high_confidence": {"min_weight": 0.95, "floor": 0.60},
            "medium_confidence": {"min_weight": 0.85, "floor": 0.50},
            "low_confidence": {"min_weight": 0.65, "floor": 0.38},
        },
        "status_thresholds": {"raw": 0.0, "candidate": 0.30, "supported": 0.55, "trusted": 0.75},
    },
    "node_scoring": {
        "positive_dimensions": {
            "upstream_position_score": {"weight": 1.0, "saturation": 0.8},
            "axis_score": {"weight": 1.2, "saturation": 0.9},
            "bottleneck_score": {"weight": 1.5, "saturation": 0.95},
            "irreplaceability_score": {"weight": 1.0, "saturation": 0.85},
            "evidence_score": {"weight": 1.3, "saturation": 0.95},
            "source_quality_score": {"weight": 1.0, "saturation": 0.9},
            "scarcity_score": {"weight": 0.8, "saturation": 0.7},
            "domestic_substitution_score": {"weight": 1.1, "saturation": 0.85},
            "obscurity_score": {"weight": 0.5, "saturation": 0.6},
            "valuation_score": {"weight": 0.4, "saturation": 0.5},
        },
        "penalty_dimensions": {
            "hype_penalty": {"max_decay": 0.30},
            "invalid_company_position_penalty": {"max_decay": 0.50},
            "conflict_penalty": {"max_decay": 0.40},
        },
        "veto": {"generic_penalty": True},
        "scarcity": {
            "few_sources_threshold": 2, "few_sources_score": 0.6,
            "some_sources_threshold": 4, "some_sources_score": 0.3,
            "min_evidence_for_scarcity": 0.5,
        },
        "hype": {
            "source_count_threshold": 10, "penalty_per_extra_source": 0.02, "max_penalty": 0.4,
        },
    },
    "path_scoring": {
        "node_aggregation": {"avg_node_weight": 0.45, "max_bottleneck_weight": 0.20},
        "bonuses": {
            "tail_company_bonus": 0.10, "tail_specific_node_bonus": 0.20,
            "direction_bonus_per_node": 0.07, "direction_bonus_max": 0.20,
            "diversity_bonus_per_axis": 0.03, "diversity_bonus_max": 0.15,
            "preferred_axis_extra_bonus": 0.05,
            "multi_source_per_type": 0.06, "multi_source_max": 0.12,
        },
        "length_factors": {"too_short_penalty": 0.7, "ideal_max": 6, "too_long_penalty": 0.8},
        "event_adjustment": {
            "boost_scale": 0.25, "risk_scale": 0.35, "contradiction_scale": 0.40,
            "freshness_scale": 0.05, "promotion_pressure_scale": 0.10,
        },
        "category_thresholds": {
            "high_confidence": 0.65, "contradiction_pressure": 0.15, "event_risk": 0.15,
            "promotion_pressure": 0.15, "high_potential": 0.40, "weak": 0.25,
            "evidence_gap_high": 0.3,
        },
    },
    "path_quality": {
        "aggregation_weights": {
            "structural_score": 0.15, "evidence_score": 0.25, "source_score": 0.10,
            "consistency_score": 0.15, "freshness_score": 0.05,
            "bottleneck_score": 0.20, "diversity_score": 0.10,
        },
        "quality_labels": {
            "STRONG_ALPHA": {"min_score": 0.80, "min_bottleneck": 0.55, "min_consistency": 0.70},
            "PROMOTION_READY": {"min_score": 0.70, "min_evidence": 0.55, "min_consistency": 0.65},
            "PROBE_FIRST": {"min_score": 0.45},
            "WEAK_EVIDENCE": {"min_score": 0.25},
        },
    },
    "flow_scoring": {
        "weights": {
            "root_relevance": 0.10, "axis_alignment": 0.20, "relation_reliability": 0.15,
            "edge_confidence_score": 0.15, "claim_support": 0.15,
            "evidence_availability": 0.10, "event_activation": 0.05, "source_reliability": 0.10,
        },
        "min_flow_score": 0.15, "redundancy_threshold": 0.85,
        "axis_weights": {"MATERIAL": 0.9, "EQUIPMENT": 0.9, "PROCESS": 0.85, "CERTIFICATION": 0.85, "VALUATION": 0.3},
    },
    "evidentiality": {
        "weights": {"entity_hit": 0.40, "stance_alignment": 0.30, "temporal_proximity": 0.15, "quantification": 0.15},
        "recency_window_days": 30, "quantification_cap": 3,
    },
    "source_weights": {
        "announcement": 0.95, "annual_report": 0.95, "prospectus": 0.95, "official_website": 0.95,
        "julang_report": 0.85, "broker_report": 0.85, "industry_whitepaper": 0.85, "whitepaper": 0.82,
        "patent": 0.75, "standard": 0.75,
        "wencai": 0.65, "eastmoney": 0.65, "cninfo_interaction": 0.65,
        "investor_relation": 0.65, "interaction_easy": 0.65,
        "news": 0.45, "exhibition": 0.45, "meeting_minutes": 0.45, "conference": 0.45,
        "web_search_snippet": 0.30, "web_search": 0.30,
        "forum": 0.20, "social_media": 0.20,
        "market_data": 0.90, "llm_inference": 0.20, "tdx_report": 0.50,
        "default": 0.50,
    },
    "temporal_kernel": {
        "half_lives": {
            "announcement": {"half_life_days": 90, "kernel_type": "exponential"},
            "annual_report": {"half_life_days": 180, "kernel_type": "exponential"},
            "broker_report": {"half_life_days": 30, "kernel_type": "exponential"},
            "industry_whitepaper": {"half_life_days": 120, "kernel_type": "exponential"},
            "patent": {"half_life_days": 365, "kernel_type": "exponential"},
            "news": {"half_life_days": 7, "kernel_type": "exponential"},
            "forum": {"half_life_days": 1, "kernel_type": "exponential"},
            "social_media": {"half_life_days": 1, "kernel_type": "exponential"},
            "default": {"half_life_days": 30, "kernel_type": "exponential"},
        },
        "hawkes": {
            "self_excitation_alpha": 0.1, "decay_beta": 0.05, "max_excitation": 0.3,
        },
    },
    "bayesian_calibration": {
        "prior_strength": 10.0,
        "walk_forward": {"train_window_days": 365, "test_window_days": 90, "min_train_samples": 50},
        "shrinkage": {"min_effective_samples": 20, "max_weight_deviation": 0.15},
    },
    "performance": {
        "cache": {"node_depth_cache_size": 10000, "node_score_cache_ttl_seconds": 300},
        "wal": {"enabled": True, "flush_interval_seconds": 5, "max_wal_entries_before_rotate": 1000},
    },
}


class WeightRegistry:
    """集中管理所有评分权重，支持YAML配置热加载。"""

    _instance: "WeightRegistry | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, config_path: str | Path | None = None) -> None:
        """初始化权重注册表。

        Args:
            config_path: YAML配置文件路径，None则自动查找
        """
        self._lock = threading.RLock()
        self._weights: dict[str, Any] = {}
        self._config_path: Path | None = None
        self._last_load_time: float = 0.0
        self._file_mtime: float = 0.0

        # 确定配置文件路径
        if config_path:
            self._config_path = Path(config_path)
        else:
            # 默认查找路径
            candidates = [
                Path(__file__).resolve().parents[4] / "config" / "scoring_weights.yaml",
                Path(__file__).resolve().parents[3] / "config" / "scoring_weights.yaml",
            ]
            for p in candidates:
                if p.exists():
                    self._config_path = p
                    break

        self._load()

    @classmethod
    def get_instance(cls, config_path: str | Path | None = None) -> "WeightRegistry":
        """获取单例实例。"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(config_path)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（测试用）。"""
        with cls._instance_lock:
            cls._instance = None

    def _load(self) -> None:
        """加载权重配置，合并默认值。"""
        with self._lock:
            weights = deepcopy(_DEFAULT_WEIGHTS)
            if self._config_path and self._config_path.exists() and _HAS_YAML:
                try:
                    with open(self._config_path, "r", encoding="utf-8") as f:
                        custom = yaml.safe_load(f) or {}
                    # 递归合并
                    if isinstance(custom, Mapping):
                        self._deep_merge(weights, custom)
                    else:
                        logger.warning("Ignoring non-mapping weights config from %s", self._config_path)
                    self._file_mtime = self._config_path.stat().st_mtime
                    logger.info("Loaded scoring weights from %s", self._config_path)
                except Exception as e:
                    logger.warning("Failed to load weights from %s: %s, using defaults", self._config_path, e)
            elif self._config_path and not _HAS_YAML:
                logger.info("PyYAML not available, using default weights")
            self._weights = weights
            self._last_load_time = time.time()

    @staticmethod
    def _deep_merge(base: dict, override: Mapping[str, Any]) -> None:
        """递归合并override到base（原地修改）。"""
        for k, v in override.items():
            if not isinstance(k, str):
                continue
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                WeightRegistry._deep_merge(base[k], v)
            else:
                base[k] = v

    def reload_if_changed(self) -> bool:
        """检查文件是否更新，如有则重新加载。

        Returns:
            是否发生了重载
        """
        if self._config_path and self._config_path.exists():
            try:
                mtime = self._config_path.stat().st_mtime
                if mtime != self._file_mtime:
                    self._load()
                    return True
            except OSError:
                pass
        return False

    def get(self, section: str, key: str | None = None, default: Any = None) -> Any:
        """获取配置值。

        Args:
            section: 顶级配置节名（如 'node_scoring'）
            key: 键名，None则返回整个section
            default: 默认值

        Returns:
            配置值
        """
        with self._lock:
            self.reload_if_changed()
            section_data = self._weights.get(section, {})
            if key is None:
                return deepcopy(section_data)
            if not isinstance(section_data, Mapping):
                return default
            return deepcopy(section_data.get(key, default))

    def get_source_weight(self, source_type: str) -> float:
        """获取来源权重，未知来源返回default。"""
        with self._lock:
            self.reload_if_changed()
            sw = self._weights.get("source_weights", {})
            if not isinstance(sw, Mapping):
                return 0.50
            try:
                weight = float(sw.get(source_type, sw.get("default", 0.50)))
            except (TypeError, ValueError, OverflowError):
                return 0.50
            return max(0.0, min(1.0, weight)) if math.isfinite(weight) else 0.50

    def get_node_weights(self) -> dict[str, dict]:
        """获取节点正向维度权重配置。"""
        return self.get("node_scoring", "positive_dimensions", {})

    def get_node_penalties(self) -> dict[str, dict]:
        """获取节点惩罚配置。"""
        return self.get("node_scoring", "penalty_dimensions", {})

    def get_path_weights(self) -> dict[str, Any]:
        """获取路径评分配置。"""
        return self.get("path_scoring")

    def get_quality_weights(self) -> dict[str, float]:
        """获取路径质量聚合权重。"""
        return self.get("path_quality", "aggregation_weights", {})

    def get_claim_weights(self) -> dict[str, float]:
        """获取Claim正向权重。"""
        return self.get("claim_scoring", "positive_weights", {})

    def get_status_thresholds(self) -> dict[str, float]:
        """获取状态晋升阈值。"""
        return self.get("claim_scoring", "status_thresholds", {})

    def get_temporal_config(self) -> dict[str, Any]:
        """获取时间核配置。"""
        return self.get("temporal_kernel")

    def get_performance_config(self) -> dict[str, Any]:
        """获取性能配置。"""
        return self.get("performance")

    def get_bayesian_config(self) -> dict[str, Any]:
        """获取贝叶斯校准配置。"""
        return self.get("bayesian_calibration")
