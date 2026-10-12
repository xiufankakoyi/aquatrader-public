"""
News（占位实现）

完整实现需补充业务逻辑与数据接口，当前仅暴露蓝图避免路由注册失败。
"""
from flask import Blueprint, jsonify

news_bp = Blueprint('news', __name__, url_prefix='/api/research/news')


@news_bp.route('/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({'status': 'ok', 'module': 'news'})


@news_bp.route('/', methods=['GET'])
def list_items():
    """获取列表（占位）"""
    return jsonify({'data': [], 'module': 'news'})
