"""
SQLite 数据库工具
=================

集中管理 SQLite 表结构初始化与迁移，供后端启动时调用。
"""
import sqlite3


def ensure_tables(conn: sqlite3.Connection) -> None:
    """
    确保应用运行所需的核心表已创建。

    Args:
        conn: SQLite 数据库连接
    """
    cursor = conn.cursor()

    # 回测结果表
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            trade_count INTEGER DEFAULT 0,
            initial_capital REAL,
            final_capital REAL,
            total_return REAL,
            annual_return REAL,
            max_drawdown REAL,
            sharpe_ratio REAL,
            params TEXT,
            metrics TEXT
        )
        """
    )

    # 交易记录表
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            action TEXT NOT NULL,
            price REAL NOT NULL,
            shares INTEGER NOT NULL,
            amount REAL NOT NULL,
            profit_loss REAL DEFAULT 0,
            FOREIGN KEY (backtest_id) REFERENCES backtest_results(id) ON DELETE CASCADE
        )
        """
    )

    # 常用索引
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_backtest_results_strategy_name
        ON backtest_results(strategy_name)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_backtest_results_created_at
        ON backtest_results(created_at DESC)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trade_records_backtest_id
        ON trade_records(backtest_id)
        """
    )

    conn.commit()
