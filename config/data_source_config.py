"""
数据源配置
==========

配置默认使用的数据源：LanceDB / Parquet
"""

import os
from pathlib import Path

DEFAULT_DATA_SOURCE = os.getenv('DEFAULT_DATA_SOURCE', 'lancedb')

_project_root = Path(__file__).parent.parent
_default_lancedb_path = _project_root / 'data' / 'lancedb'
LANCEDB_URI = os.getenv('LANCEDB_URI', str(_default_lancedb_path))


def get_data_manager():
    """
    获取数据管理器实例

    根据 DEFAULT_DATA_SOURCE 配置返回对应的数据管理器
    """
    source = DEFAULT_DATA_SOURCE.lower()

    if source == 'lancedb':
        from data_svc.storage.lancedb_manager import get_lancedb_manager
        return get_lancedb_manager()
    elif source == 'parquet':
        from data_svc.database.parquet_updater import ParquetUpdater
        return ParquetUpdater()
    else:
        raise ValueError(f"Unsupported data source: {source}")
