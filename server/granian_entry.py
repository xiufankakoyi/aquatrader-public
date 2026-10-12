# server/granian_entry.py
"""
Granian ASGI 入口点 - 高性能服务器

使用 Granian 替代 Flask 开发服务器，提供更高的并发性能
"""
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# 导入 ASGI 应用（使用 python-socketio AsyncServer）
try:
    from server.asgi_entry import app_asgi
    asgi_app = app_asgi

    print("[OK] 使用 ASGI 模式（python-socketio AsyncServer）")
    print("   特性: 完整的 WebSocket 支持 + 高性能")
except ImportError as e:
    print(f"[WARN] ASGI 入口不可用: {e}")
    print("   回退到 WSGI 模式（HTTP API 可用，WebSocket 不可用）")
    # 回退：使用原始 Flask 应用（WSGI）
    from server.app import app
    try:
        from granian.wsgi import WSGIApplication
        asgi_app = WSGIApplication(app)
    except ImportError:
        raise ImportError("Granian is required: pip install granian")

try:
    from granian import Granian
except ImportError:
    raise ImportError("Granian is required: pip install granian")


def create_app():
    """创建 Granian 应用实例"""
    import platform

    # Windows 上 Granian 只支持 1 个 worker
    is_windows = platform.system().lower() == "windows"
    workers = 1 if is_windows else int(os.getenv("WORKERS", "4"))

    # Granian 参数：根据最新版本调整
    # 注意：某些版本可能不支持 threads 参数，需要逐步尝试
    try:
        # 尝试完整参数（包括 threads）
        server = Granian(
            "server.granian_entry:asgi_app",
            address="0.0.0.0",
            port=int(os.getenv("PORT", "5000")),
            workers=workers,
            threads=int(os.getenv("THREADS", "2")) if not is_windows else None,
            loop="auto",
            interface="asgi",
            log_level=os.getenv("LOG_LEVEL", "info"),
            access_log=os.getenv("ACCESS_LOG", "true").lower() == "true",
        )

        return server
    except TypeError as e:
        # 如果 threads 参数不支持，移除它
        if "threads" in str(e).lower() or "unexpected keyword" in str(e).lower():
            try:
                # 尝试不带 threads 的版本
                granian_params = {
                    "target": "server.granian_entry:asgi_app",
                    "address": "0.0.0.0",
                    "port": int(os.getenv("PORT", "5000")),
                    "workers": workers,
                    "loop": "auto",
                    "interface": "asgi",
                    "log_level": os.getenv("LOG_LEVEL", "info"),
                    "access_log": os.getenv("ACCESS_LOG", "true").lower() == "true",
                }
                # 移除 None 值
                granian_params = {k: v for k, v in granian_params.items() if v is not None}
                return Granian(**granian_params)
            except TypeError:
                # 最后回退：只使用基本参数
                return Granian(
                    "server.granian_entry:asgi_app",
                    address="0.0.0.0",
                    port=int(os.getenv("PORT", "5000")),
                    workers=workers,
                    interface="asgi",
                )
        else:
            raise


if __name__ == "__main__":
    # 直接运行
    server = create_app()
    server.serve()
