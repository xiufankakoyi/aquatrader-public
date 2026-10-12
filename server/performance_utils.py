# server/performance_utils.py
"""
高性能工具函数 - orjson + msgpack
"""
try:
    import orjson
    ORJSON_AVAILABLE = True
except ImportError:
    ORJSON_AVAILABLE = False
    orjson = None

try:
    import msgpack
    MSGPACK_AVAILABLE = True
except ImportError:
    MSGPACK_AVAILABLE = False
    msgpack = None

import json
from typing import Any, Dict


def json_response(data: Any, status_code: int = 200):
    """
    使用 orjson 创建 JSON 响应（比标准 json 快 2-3 倍）
    
    Returns:
        Flask Response 对象
    """
    from flask import Response
    
    if ORJSON_AVAILABLE:
        content = orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY)
        return Response(
            content,
            mimetype='application/json',
            status=status_code
        )
    else:
        # 回退到标准 json
        return Response(
            json.dumps(data),
            mimetype='application/json',
            status=status_code
        )


def pack_backtest_data(data: Dict[str, Any]) -> bytes:
    """
    使用 msgpack 压缩回测数据（比 JSON 小 30-50%，快 2-3 倍）
    
    Args:
        data: 回测结果字典
        
    Returns:
        压缩后的字节数据
    """
    if MSGPACK_AVAILABLE:
        return msgpack.packb(data, use_bin_type=True)
    else:
        # 回退到 JSON
        import json
        return json.dumps(data).encode('utf-8')


def unpack_backtest_data(data: bytes) -> Dict[str, Any]:
    """
    解压回测数据
    
    Args:
        data: 压缩的字节数据
        
    Returns:
        解压后的字典
    """
    if MSGPACK_AVAILABLE:
        return msgpack.unpackb(data, raw=False)
    else:
        # 回退到 JSON
        import json
        return json.loads(data.decode('utf-8'))

