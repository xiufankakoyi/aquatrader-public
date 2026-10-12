"""另类数据注册表反序列化与写入边界回归测试。"""

from __future__ import annotations

import json
import math

import pytest

from core.research.alternative_data.contracts import (
    QualityMetric,
)
from core.research.alternative_data.governance import (
    AlternativeDataRegistry,
    AlternativeDataRegistryError,
    _dict_to_source,
)


def test_registry_wraps_overflow_in_malformed_source(tmp_path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "sources": {
                    "src": {
                        "source_id": "src",
                        "source_name": "source",
                        "source_type": "other",
                        "authorization_scope": {
                            "authorization_status": "authorized",
                            "license_reference": "lic",
                            "usage_scope": [],
                            "authorized_personnel": [],
                        },
                        "retention_policy": {
                            "fetch_method": "api",
                            "retention_days": math.inf,
                            "deletion_rule": "delete",
                            "pii_handling": "mask",
                        },
                        "update_frequency": "daily",
                        "known_biases": [],
                        "entity_mapping_method": "direct",
                        "timestamp_semantics": "event_time",
                        "quality_monitoring": {},
                        "mnpi_isolation": {},
                        "status": "active",
                    }
                },
                "quality_history": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AlternativeDataRegistryError):
        AlternativeDataRegistry(path)


def test_update_field_rejects_unknown_schema_fields(tmp_path) -> None:
    registry = AlternativeDataRegistry(tmp_path / "registry.json")
    registry._records["src"] = {"source_id": "src"}  # type: ignore[attr-defined]

    with pytest.raises(AlternativeDataRegistryError):
        registry.update_field("src", "unexpected", "value")


def test_source_rehydration_parses_boolean_and_list_boundaries() -> None:
    source = _dict_to_source(
        {
            "source_type": "other",
            "known_biases": "not-a-list",
            "quality_monitoring": {"auto_suspend_on_breach": "false"},
        }
    )

    assert source.known_biases == []
    assert source.quality_monitoring.auto_suspend_on_breach is False
