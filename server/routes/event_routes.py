"""
Event（占位实现）

完整实现需补充业务逻辑与数据接口，当前仅暴露蓝图避免路由注册失败。
"""
from flask import Blueprint, jsonify

event_bp = Blueprint('event', __name__, url_prefix='/api/research/event')


@event_bp.route('/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({'status': 'ok', 'module': 'event'})


@event_bp.route('/', methods=['GET'])
def list_items():
    """获取列表（占位）"""
    return jsonify({'data': [], 'module': 'event'})
