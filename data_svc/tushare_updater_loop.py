"""Tushare 数据定时循环更新守护进程。

每小时检查一次 LanceDB 中 daily_ohlcv 的最新日期，如果落后于当前交易日
（非周末且为交易日），则触发 UnifiedDataUpdater 增量更新。

启动方式:
    python data_svc/tushare_updater_loop.py

环境变量:
    TUSHARE_LOOP_INTERVAL_SECONDS: 循环间隔秒数，默认 3600（1小时）
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta

# 确保项目根目录在 sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from loguru import logger

from data_svc.storage.unified_updater import UnifiedDataUpdater
from data_svc.unified_data_query import get_unified_data_query


def _is_weekend(date: datetime) -> bool:
    return date.weekday() >= 5


def _is_trading_day(query_date: datetime) -> bool:
    """通过 Tushare 交易日历判断是否为交易日。"""
    import tushare as ts
    from config.config import Config

    pro = ts.pro_api(Config.TUSHARE_TOKEN)
    date_str = query_date.strftime("%Y%m%d")
    try:
        cal_df = pro.trade_cal(exchange="SSE", start_date=date_str, end_date=date_str)
        if cal_df is None or cal_df.empty:
            return False
        return int(cal_df.iloc[0]["is_open"]) == 1
    except Exception as exc:
        logger.warning("查询交易日历失败 {}: {}", date_str, exc)
        return not _is_weekend(query_date)


def _latest_trade_date(query) -> datetime | None:
    stats = query.get_db_stats()
    latest = stats.get("latest_date")
    if not latest:
        return None
    try:
        return datetime.strptime(str(latest)[:10], "%Y-%m-%d")
    except ValueError:
        try:
            return datetime.strptime(str(latest)[:8], "%Y%m%d")
        except ValueError:
            return None


def run_loop() -> None:
    interval = int(os.environ.get("TUSHARE_LOOP_INTERVAL_SECONDS", "3600"))
    logger.info("Tushare 循环更新守护进程启动，间隔 {} 秒", interval)

    updater = UnifiedDataUpdater()
    query = get_unified_data_query()

    while True:
        now = datetime.now()
        logger.info("开始新一轮检查: {}", now.isoformat())

        latest = _latest_trade_date(query)
        today = now.date()

        if latest is None:
            logger.info("LanceDB 为空，执行全量更新")
            updater.run_full_update(skip_dragon=True)
        else:
            latest_date = latest.date()
            if latest_date >= today:
                logger.info("数据已最新: {}", latest_date)
            elif _is_trading_day(datetime.combine(today, datetime.min.time())):
                logger.info("需要更新: {} -> {}", latest_date, today)
                updater.run_full_update(skip_dragon=True)
            else:
                logger.info("今天不是交易日: {}", today)

        logger.info("本轮检查完成，{} 秒后再次检查", interval)
        time.sleep(interval)


if __name__ == "__main__":
    run_loop()
