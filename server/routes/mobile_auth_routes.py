"""移动端认证路由。

提供简单的 PIN 码认证机制，用于移动端日报访问。
"""
from __future__ import annotations

import os
from functools import wraps

from flask import Blueprint, jsonify, request

mobile_auth_bp = Blueprint("mobile_auth", __name__, url_prefix="/api/mobile/auth")

# 从环境变量读取 PIN 码，默认 123456
_MOBILE_PIN = os.environ.get("MOBILE_PIN", "123456")

# 简单的 token 存储（生产环境应使用 Redis 或数据库）
_valid_tokens: set[str] = set()


def _generate_token() -> str:
    """生成简单 token。"""
    import secrets
    return secrets.token_hex(16)


def require_mobile_auth(f):
    """移动端认证装饰器。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            token = request.args.get("token", "")
        
        if token not in _valid_tokens:
            return jsonify({"error": "Unauthorized", "message": "请先登录"}), 401
        
        return f(*args, **kwargs)
    return decorated


@mobile_auth_bp.route("/login", methods=["POST"])
def login():
    """PIN 码登录。
    
    请求体:
        {"pin": "123456"} 或 {"token": "xxx"} (直接验证 token)
    
    返回:
        {"success": true, "token": "xxx"}
    """
    data = request.get_json() or {}
    
    # 支持直接 token 验证
    if "token" in data:
        token = data["token"]
        if token in _valid_tokens:
            return jsonify({"success": True, "token": token})
        return jsonify({"success": False, "error": "Invalid token"}), 401
    
    # PIN 码登录
    pin = data.get("pin", "")
    if pin == _MOBILE_PIN:
        token = _generate_token()
        _valid_tokens.add(token)
        return jsonify({"success": True, "token": token})
    
    return jsonify({"success": False, "error": "Invalid PIN"}), 401


@mobile_auth_bp.route("/logout", methods=["POST"])
def logout():
    """登出。"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    _valid_tokens.discard(token)
    return jsonify({"success": True})


@mobile_auth_bp.route("/status", methods=["GET"])
def status():
    """检查认证状态。"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        token = request.args.get("token", "")
    
    if token in _valid_tokens:
        return jsonify({"authenticated": True, "token": token})
    
    return jsonify({"authenticated": False}), 401


@mobile_auth_bp.route("/pin", methods=["GET"])
def get_pin_hint():
    """获取 PIN 提示（仅开发环境）。"""
    from config.config import Config
    if Config.ENV == "development":
        return jsonify({"hint": f"PIN: {_MOBILE_PIN}"})
    return jsonify({"error": "Not available"}), 404
