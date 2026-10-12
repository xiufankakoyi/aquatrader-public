# server/asgi_entry.py
"""
ASGI 入口点 - 使用 python-socketio AsyncServer

解决 Flask-SocketIO 与 ASGI 不兼容的问题
使用原生 python-socketio 的 AsyncServer 以获得完整的 ASGI 支持
"""
import os
import sys
import threading
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore", 
    message="async_to_sync was passed a non-async-marked callable", 
    module="asgiref.wsgi"
)

_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

os.environ['USE_GRANIAN'] = 'true'

from server.app import app as flask_app

import socketio

sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',
    logger=False,
    engineio_logger=False,
    ping_interval=25000,
    ping_timeout=60000,
    per_message_deflate=True  # 启用消息压缩，节省 70-80% 带宽
)

print("\n" + "="*80)
print("[ASGI] Importing asgi_socketio_handlers...")
try:
    from server.asgi_socketio_handlers import register_handlers
    print("[ASGI] Calling register_handlers(sio)...")
    register_handlers(sio)
    print("[ASGI] OK - Socket.IO event handlers registered!")
except Exception as e:
    print(f"[ASGI] ERROR - Failed to register Socket.IO handlers: {e}")
    import traceback
    traceback.print_exc()
    raise
print("="*80 + "\n")

from asgiref.wsgi import WsgiToAsgi
flask_asgi_app = WsgiToAsgi(flask_app)

app_asgi = socketio.ASGIApp(sio, other_asgi_app=flask_asgi_app, socketio_path='socket.io')

_startup_lock = threading.Lock()
_startup_tasks_started = False

def background_preload():
    """后台预加载线程：不阻塞服务器启动"""
    import time
    time.sleep(1)

    try:
        from data_svc.unified_data_manager import get_unified_manager

        manager = get_unified_manager()

        print("[BackgroundPreload] Starting data preload (3 months)...")
        t0 = time.time()
        manager.preload_to_memory(years=0.25)
        elapsed = time.time() - t0
        cache_info = manager._cache_loaded
        print(f"[BackgroundPreload] Completed in {elapsed:.1f}s - cache_loaded={cache_info}")
    except Exception as e:
        print(f"[BackgroundPreload] Failed: {e}")
        import traceback
        traceback.print_exc()

def background_sync_data():
    """
    后台线程：检查数据是否过期
    用 Tushare API 获取最新交易日，与数据库对比
    """
    import time
    time.sleep(5)

    try:
        from datetime import datetime, timedelta
        from data_svc.unified_data_manager import get_unified_manager

        manager = get_unified_manager()

        try:
            stats = manager.get_stats()
            row_count = stats.get('row_count', 0)
            print(f"[BackgroundSync] Stock data: {row_count} rows")

            if row_count == 0:
                print("[BackgroundSync] WARNING: No data found in LanceDB!")
                print("[BackgroundSync] Please run data update: python -m data_svc.storage.unified_updater")
                return

            dates = manager.get_trading_dates()
            if not dates:
                print("[BackgroundSync] WARNING: No trading dates found!")
                return

            db_latest_date = max(dates)

            # 用 Tushare API 获取最近交易日
            try:
                import tushare as ts
                from config.config import Config

                pro = ts.pro_api(Config.TUSHARE_TOKEN)
                today_str = datetime.now().strftime('%Y%m%d')

                # 获取最近几个交易日期
                cal_df = pro.trade_cal(
                    exchange='SSE',
                    start_date=(datetime.now() - timedelta(days=10)).strftime('%Y%m%d'),
                    end_date=today_str
                )

                if cal_df is not None and not cal_df.empty:
                    trading_days = cal_df[cal_df['is_open'] == 1]['cal_date'].tolist()
                    if trading_days:
                        api_latest_date = trading_days[-1]  # 最近一个交易日
                        # 格式化
                        api_latest_date_fmt = f"{api_latest_date[:4]}-{api_latest_date[4:6]}-{api_latest_date[6:8]}"

                        # 比较
                        days_behind = 0
                        if db_latest_date < api_latest_date_fmt:
                            # 计算差距
                            db_dt = datetime.strptime(db_latest_date, '%Y-%m-%d')
                            api_dt = datetime.strptime(api_latest_date_fmt, '%Y-%m-%d')
                            days_behind = (api_dt - db_dt).days

                        print(f"[BackgroundSync] DB latest: {db_latest_date}, API latest: {api_latest_date_fmt}")
                        print(f"[BackgroundSync] Days behind: {days_behind}")

                        if days_behind > 0:
                            print(f"[BackgroundSync] WARNING: Data is {days_behind} day(s) behind!")
                            print(f"[BackgroundSync] Please update: python -m data_svc.storage.unified_updater")

                        if days_behind > 7:
                            print(f"[BackgroundSync] CRITICAL: Data is more than 7 days behind!")
                    else:
                        print("[BackgroundSync] No trading days found in recent period")
                else:
                    print("[BackgroundSync] WARNING: Failed to get trading calendar from API")

            except Exception as e:
                print(f"[BackgroundSync] API check failed: {e}, falling back to simple check")
                # 回退：简单用数据库最新日期
                print(f"[BackgroundSync] DB latest date: {db_latest_date}")

            print("[BackgroundSync] Data check completed!")

        except Exception as e:
            print(f"[BackgroundSync] Stats check failed: {e}")
            import traceback
            traceback.print_exc()

    except Exception as e:
        print(f"[BackgroundSync] Failed: {e}")
        import traceback
        traceback.print_exc()

def _start_post_startup_tasks():
    """Start non-critical services after the ASGI server enters startup."""
    global _startup_tasks_started

    with _startup_lock:
        if _startup_tasks_started:
            return
        _startup_tasks_started = True

    print("\n" + "="*80)
    print("[Startup] Starting post-startup background services...")

    try:
        from core.quant.strategies.hot_reload import get_watcher
        watcher = get_watcher()
        watcher.start()
        print("[OK] Strategy file watcher started (Hot-reload enabled)")
    except Exception as e:
        print(f"[WARNING] File watcher startup failed: {e}")

    preload_thread = threading.Thread(
        target=background_preload,
        name="aquatrade-background-preload",
        daemon=True,
    )
    preload_thread.start()
    print("[Startup] Background preload thread started")

    sync_thread = threading.Thread(
        target=background_sync_data,
        name="aquatrade-background-sync",
        daemon=True,
    )
    sync_thread.start()
    print("[Startup] Background sync thread started")

    try:
        from server.industry_chain.auto_update_scheduler import get_industry_auto_update_scheduler

        get_industry_auto_update_scheduler().start()
        print("[Startup] IndustryChainRadar auto update scheduler started")
    except Exception as e:
        print(f"[WARNING] IndustryChainRadar auto update scheduler failed: {e}")

    try:
        from server.strategy.perilla_daily_scheduler import get_perilla_daily_scheduler

        get_perilla_daily_scheduler().start()
        print("[Startup] Perilla daily scheduler started")
    except Exception as e:
        print(f"[WARNING] Perilla daily scheduler failed: {e}")

    print("="*80 + "\n")


async def _lifespan_app(scope, receive, send):
    if scope["type"] != "lifespan":
        await app_asgi(scope, receive, send)
        return

    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            _start_post_startup_tasks()
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


asgi_app = _lifespan_app
