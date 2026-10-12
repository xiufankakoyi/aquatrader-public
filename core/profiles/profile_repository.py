"""
策略参数预设存储模块

使用 Parquet 文件存储用户配置。
"""

import os
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import polars as pl
from pathlib import Path

_profile_cache = {}
_profile_dir = Path(__file__).parent.parent.parent / "data" / "profiles"
_profile_dir.mkdir(parents=True, exist_ok=True)
_profile_file = _profile_dir / "strategy_profiles.parquet"


def _get_profiles_df() -> pl.DataFrame:
    """获取所有预设 DataFrame"""
    try:
        if _profile_file.exists():
            return pl.read_parquet(_profile_file)
    except Exception:
        pass
    return pl.DataFrame(schema={
        "id": pl.Int64,
        "strategy_name": pl.Utf8,
        "profile_name": pl.Utf8,
        "description": pl.Utf8,
        "params": pl.Utf8,
        "source": pl.Utf8,
        "created_at": pl.Datetime,
    })


def _save_profiles_df(df: pl.DataFrame):
    """保存预设 DataFrame"""
    df.write_parquet(_profile_file)


def _next_profile_id(df: pl.DataFrame) -> int:
    """生成下一个自增 id"""
    if df.is_empty():
        return 1
    return int(df["id"].max()) + 1


def create_profile(
    strategy_name: str,
    profile_name: str,
    description: Optional[str],
    params_dict: Dict[str, Any],
    source: str = "optimization",
) -> Dict[str, Any]:
    """
    创建一个新的参数预设（Profile）。
    """
    df = _get_profiles_df()
    new_id = _next_profile_id(df)
    created_at = datetime.utcnow()
    params_json = json.dumps(params_dict, ensure_ascii=False)

    new_row = pl.DataFrame([{
        "id": new_id,
        "strategy_name": strategy_name,
        "profile_name": profile_name,
        "description": description or "",
        "params": params_json,
        "source": source,
        "created_at": created_at,
    }])

    if df.is_empty():
        df = new_row
    else:
        df = df.vstack(new_row)

    _save_profiles_df(df)

    return {
        "id": new_id,
        "strategy_name": strategy_name,
        "profile_name": profile_name,
        "description": description,
        "params": params_dict,
        "source": source,
        "created_at": created_at.isoformat(),
    }


def list_profiles(strategy_name: str) -> List[Dict[str, Any]]:
    """
    列出某个策略下的所有预设，按创建时间倒序。
    """
    df = _get_profiles_df()
    if df.is_empty():
        return []

    filtered = df.filter(pl.col("strategy_name") == strategy_name)
    sorted_df = filtered.sort("created_at", descending=True)

    profiles: List[Dict[str, Any]] = []
    for row in sorted_df.iter_rows(named=True):
        profiles.append({
            "id": row["id"],
            "strategy_name": row["strategy_name"],
            "profile_name": row["profile_name"],
            "description": row["description"],
            "params": json.loads(row["params"]) if row["params"] else {},
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        })
    return profiles


def get_profile(profile_id: int) -> Optional[Dict[str, Any]]:
    """
    获取单个预设详情。
    """
    df = _get_profiles_df()
    if df.is_empty():
        return None

    filtered = df.filter(pl.col("id") == profile_id)
    if filtered.is_empty():
        return None

    row = filtered.row(0, named=True)
    return {
        "id": row["id"],
        "strategy_name": row["strategy_name"],
        "profile_name": row["profile_name"],
        "description": row["description"],
        "params": json.loads(row["params"]) if row["params"] else {},
        "source": row["source"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }
