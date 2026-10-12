from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import json


DEFAULT_UNIVERSE = [
    "SOXS",
    "SQQQ",
    "SNXX",
    "TZA",
    "TSLL",
    "NVD",
    "MULL",
    "NVDL",
    "SPCH",
    "RAM",
    "FXI",
]


@dataclass(frozen=True)
class LiquidityThresholds:
    max_ask: float = 0.50
    preferred_max_ask: float = 0.35
    min_ask: float = 0.03
    min_bid: float = 0.01
    max_spread_pct: float = 0.50
    preferred_spread_pct: float = 0.30
    min_volume: int = 20
    preferred_volume: int = 100
    min_open_interest: int = 100
    preferred_open_interest: int = 500


@dataclass(frozen=True)
class ScanConfig:
    universe: list[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    budget: float = 50.0
    direction: str = "both"
    min_dte: int = 7
    max_dte: int = 45
    top_n: int = 10
    risk_free_rate: float = 0.045
    dividend_yield: float = 0.0
    data_source: str = "auto"
    data_quality: str = "delayed_or_unverified"
    output_root: str = "data/options_scans"
    push_feishu: bool = True
    liquidity: LiquidityThresholds = field(default_factory=LiquidityThresholds)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["universe"] = [symbol.upper() for symbol in self.universe]
        return data


def load_scan_config(path: str | Path | None = None, **overrides: Any) -> ScanConfig:
    data: dict[str, Any] = {}
    if path:
        config_path = Path(path)
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
    data.update({key: value for key, value in overrides.items() if value is not None})
    if "universe" in data:
        seen: set[str] = set()
        deduped: list[str] = []
        for symbol in data["universe"]:
            normalized = str(symbol).upper()
            if normalized and normalized not in seen:
                seen.add(normalized)
                deduped.append(normalized)
        data["universe"] = deduped
    if isinstance(data.get("liquidity"), dict):
        data["liquidity"] = LiquidityThresholds(**data["liquidity"])
    known_fields = {field.name for field in ScanConfig.__dataclass_fields__.values()}
    init_data = {key: value for key, value in data.items() if key in known_fields}
    return ScanConfig(**init_data)
