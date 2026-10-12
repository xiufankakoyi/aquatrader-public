"""
数据路由

提供数据状态查询和数据更新接口
"""
import os
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Any

from loguru import logger
from flask import Blueprint, jsonify, request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from config.config import Config

data_bp = Blueprint('data', __name__, url_prefix='/api/db')


def json_response(data: Dict[str, Any], status: int = 200):
    """返回 JSON 响应"""
    response = jsonify(data)
    response.status_code = status
    return response


@data_bp.route('/status', methods=['GET'])
def get_data_status():
    """
    获取数据状态（简化为快速返回，不调用外部API）

    Returns:
        {
            "success": True,
            "data": {
                "stock": {...},
                "dragon_eye": {...},
                "overall_status": "OK" | "WARNING" | "CRITICAL",
                "overall_message": "..."
            }
        }
    """
    try:
        from data_svc.query.fast_market_query import get_fast_market_query

        reader = get_fast_market_query()._reader
        result = {
            "success": True,
            "data": {
                "stock": {
                    "db_latest_date": None,
                    "api_latest_date": None,
                    "days_behind": 0,
                    "row_count": 0,
                    "status": "OK",
                    "message": "数据已是最新"
                },
                "dragon_eye": {
                    "latest_date": None,
                    "days_behind": 0,
                    "status": "WARNING",
                    "message": "检查中..."
                },
                "overall_status": "WARNING",
                "overall_message": "检查中..."
            }
        }

        try:
            stats = reader.get_db_stats()
            result["data"]["stock"]["db_latest_date"] = stats.get("latest_date")
            result["data"]["stock"]["row_count"] = stats.get("row_count", 0)
            if stats.get("row_count", 0) == 0:
                result["data"]["stock"]["status"] = "WARNING"
                result["data"]["stock"]["message"] = "数据库为空"
        except Exception as e:
            logger.warning(f"[DataStatus] LanceDB check failed: {e}")

        try:
            dragon_status = _get_dragon_eye_status()
            result["data"]["dragon_eye"] = dragon_status
        except Exception as e:
            logger.warning(f"[DataStatus] DragonEye check failed: {e}")

        stock_status = result["data"]["stock"]["status"]
        dragon_status_val = result["data"]["dragon_eye"]["status"]

        if stock_status == "OK" and dragon_status_val in ["OK", "WARNING"]:
            result["data"]["overall_status"] = "OK"
            result["data"]["overall_message"] = "所有数据已是最新"
        elif stock_status == "CRITICAL" or dragon_status_val == "CRITICAL":
            result["data"]["overall_status"] = "CRITICAL"
            result["data"]["overall_message"] = "存在严重落后的数据，请立即更新"
        else:
            result["data"]["overall_status"] = "WARNING"
            result["data"]["overall_message"] = "部分数据落后，建议更新"

        return json_response(result)

    except Exception as e:
        logger.error(f"[DataStatus] Error: {e}")
        return json_response({"success": False, "error": str(e)}, 500)


def _get_dragon_eye_status() -> Dict[str, Any]:
    """
    获取 DragonEye 爬虫数据状态

    检查爬虫数据目录获取最新日期
    爬虫数据保存在 quant/data_lake 目录
    """
    try:
        from pathlib import Path

        # 可能的爬虫数据目录
        possible_base_dirs = [
            Path(Config.BASE_DIR) / "quant" / "data",           # aquatrade/quant/data
            Path(r"C:/Users/USER\Desktop\projects\quant") / "data",  # projects/quant/data
        ]

        latest_date_str = None
        data_dir = None

        for base_dir in possible_base_dirs:
            for subdir in ["data_lake", "cleaned_data"]:
                check_dir = base_dir / subdir
                if check_dir.exists():
                    date_dirs = [d for d in check_dir.iterdir() if d.is_dir()]
                    if date_dirs:
                        current_latest = max(d.name for d in date_dirs)
                        if latest_date_str is None or current_latest > latest_date_str:
                            latest_date_str = current_latest
                            data_dir = check_dir

        if not latest_date_str or not data_dir:
            return {
                "latest_date": None,
                "days_behind": 0,
                "status": "WARNING",
                "message": "暂无爬虫数据"
            }

        latest_date = datetime.strptime(latest_date_str, '%Y-%m-%d')

        # 使用日历天数估算（Tushare API 调用已移除以避免超时）
        today = datetime.now().date()
        days_behind = (today - latest_date.date()).days

        if days_behind == 0:
            return {
                "latest_date": latest_date_str,
                "days_behind": 0,
                "status": "OK",
                "message": "爬虫数据已是最新"
            }
        elif days_behind <= 3:
            return {
                "latest_date": latest_date_str,
                "days_behind": days_behind,
                "status": "WARNING",
                "message": f"爬虫数据落后约 {days_behind} 天"
            }
        else:
            return {
                "latest_date": latest_date_str,
                "days_behind": days_behind,
                "status": "CRITICAL",
                "message": f"爬虫数据严重落后约 {days_behind} 天"
            }

    except Exception as e:
        return {
            "latest_date": None,
            "days_behind": 0,
            "status": "WARNING",
            "message": f"检查失败: {str(e)}"
        }


@data_bp.route('/update', methods=['POST'])
def trigger_update():
    """
    触发数据更新（后台异步执行）

    Returns:
        {"success": True, "message": "更新任务已启动"}
    """
    try:
        from data_svc.storage.unified_updater import UnifiedDataUpdater

        def background_update():
            """后台更新线程"""
            try:
                updater = UnifiedDataUpdater()
                result = updater.run_full_update()

                # 通过 Socket.IO 广播进度（如果可用）
                try:
                    from server.asgi_socketio_handlers import sio
                    if sio:
                        sio.emit('db_update_progress', {
                            "status": "COMPLETED" if result.success else "FAILED",
                            "progress": 100,
                            "message": result.message
                        })
                except:
                    pass

                logger.info(f"[DataUpdate] Background update completed: {result.message}")
            except Exception as e:
                logger.error(f"[DataUpdate] Background update failed: {e}")

        # 启动后台更新线程
        update_thread = threading.Thread(target=background_update, daemon=True)
        update_thread.start()

        return json_response({
            "success": True,
            "message": "数据更新任务已在后台启动",
            "job_id": f"update_{int(time.time())}"
        })

    except Exception as e:
        logger.error(f"[DataUpdate] Failed to start update: {e}")
        return json_response({"success": False, "error": str(e)}, 500)


@data_bp.route('/update/dragon', methods=['POST'])
def trigger_dragon_update():
    """
    触发 DragonEye 爬虫数据更新（后台异步执行）

    Request Body:
        - date: 目标日期 (YYYY-MM-DD)，默认为今天
        - backfill: 是否补爬最近缺失交易日，默认为 false

    Returns:
        {"success": True, "message": "爬虫任务已启动", "job_id": "xxx"}
    """
    try:
        data = request.get_json(silent=True) or {}
        target_date = data.get('date') or datetime.now().strftime("%Y-%m-%d")
        backfill = bool(data.get('backfill', False))

        def background_crawl():
            """后台爬虫线程"""
            try:
                from data_svc.storage.unified_updater import UnifiedDataUpdater

                result = UnifiedDataUpdater().update_dragon_eye(target_date, backfill=backfill)
                logger.info(f"[DragonUpdate] Crawl completed: {result}")
            except Exception as e:
                logger.error(f"[DragonUpdate] Crawl failed: {e}")

        crawl_thread = threading.Thread(target=background_crawl, daemon=True)
        crawl_thread.start()

        return json_response({
            "success": True,
            "message": f"爬虫任务已在后台启动，目标日期: {target_date}",
            "job_id": f"dragon_{int(time.time())}"
        })

    except Exception as e:
        logger.error(f"[DragonUpdate] Failed to start crawl: {e}")
        return json_response({"success": False, "error": str(e)}, 500)


@data_bp.route('/update/all', methods=['POST'])
def trigger_full_update():
    """
    触发全部数据更新（股票数据 + DragonEye 爬虫）

    Request Body:
        - date: DragonEye 目标日期 (YYYY-MM-DD)，默认为今天
        - backfill: 是否补爬最近缺失交易日，默认为 false

    Returns:
        {"success": True, "message": "全部更新任务已在后台启动"}
    """
    try:
        data = request.get_json(silent=True) or {}
        dragon_date = data.get('date') or datetime.now().strftime("%Y-%m-%d")
        dragon_backfill = bool(data.get('backfill', False))

        def background_full_update():
            """后台完整更新线程"""
            try:
                # 1. 更新股票数据
                from data_svc.storage.unified_updater import UnifiedDataUpdater
                updater = UnifiedDataUpdater()
                update_result = updater.run_full_update(
                    dragon_target_date=dragon_date,
                    dragon_backfill=dragon_backfill,
                )
                logger.info(f"[FullUpdate] Update result: {update_result.message}")

                # 广播完成
                try:
                    from server.asgi_socketio_handlers import sio
                    if sio:
                        sio.emit('db_update_progress', {
                            "status": "COMPLETED" if update_result.success else "FAILED",
                            "progress": 100,
                            "message": update_result.message,
                        })
                except:
                    pass

            except Exception as e:
                logger.error(f"[FullUpdate] Failed: {e}")

        update_thread = threading.Thread(target=background_full_update, daemon=True)
        update_thread.start()

        return json_response({
            "success": True,
            "message": "全部数据更新任务已在后台启动",
            "job_id": f"full_{int(time.time())}"
        })

    except Exception as e:
        logger.error(f"[FullUpdate] Failed to start: {e}")
        return json_response({"success": False, "error": str(e)}, 500)


@data_bp.route('/update/progress', methods=['GET'])
def get_update_progress():
    """
    获取更新进度（轮询接口，备用）

    Returns:
        {"success": True, "progress": 50, "status": "UPDATING", "message": "..."}
    """
    # 这个接口被 Socket.IO 替代，这里仅作为备用
    return json_response({
        "success": True,
        "progress": 0,
        "status": "IDLE",
        "message": "请使用 Socket.IO 监听 db_update_progress 事件"
    })


# ============== 废弃的 ArcticDB 路由（已移除）==============

# 以下路由已被移除：
# - GET  /arcticdb/info    -> 使用 /db/status
# - POST /arcticdb/query   -> 使用 LanceDB 原生查询
# - POST /arcticdb/update   -> 使用 /db/update
