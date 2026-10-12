"""
持仓历史管理模块

功能：
- 记录每次买入、卖出、加仓、减仓操作
- 支持查询持仓历史
- 统计交易数据

存储：ArcticDB + Parquet
"""

import json
import os
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
import pandas as pd
import numpy as np

from config.config import Config


@dataclass
class PositionHistory:
    """持仓历史记录数据结构"""
    id: Optional[int] = None
    position_id: int = 0
    stock_code: str = ""
    stock_name: str = ""
    action: str = ""  # 'buy', 'sell', 'add', 'reduce'
    shares: float = 0.0
    price: float = 0.0
    amount: float = 0.0  # 正数表示买入/加仓，负数表示卖出/减仓
    date: str = ""
    notes: str = ""
    created_at: Optional[str] = None


@dataclass
class PositionHistoryStats:
    """持仓历史统计"""
    total_trades: int = 0
    buy_count: int = 0
    sell_count: int = 0
    add_count: int = 0
    reduce_count: int = 0
    total_buy_amount: float = 0.0
    total_sell_amount: float = 0.0


class PositionHistoryManager:
    """
    持仓历史管理器
    """

    LIBRARY_NAME = "portfolio"
    SYMBOL_NAME = "position_history"
    PARQUET_FILE = "position_history.parquet"

    def __init__(self):
        self.parquet_path = os.path.join(Config.PARQUET_DIR, self.PARQUET_FILE)
        os.makedirs(Config.PARQUET_DIR, exist_ok=True)

    def _get_history_df(self) -> pd.DataFrame:
        """获取历史记录 DataFrame"""
        try:
            if os.path.exists(self.parquet_path):
                import polars as pl
                df = pl.read_parquet(self.parquet_path).to_pandas()
                return df
        except Exception as e:
            print(f"[PositionHistoryManager] Polars read error: {e}")
        return pd.DataFrame()

    def _save_history_df(self, df: pd.DataFrame):
        """保存历史记录 DataFrame"""
        try:
            df_to_save = df.copy()
            df_to_save.to_parquet(self.parquet_path, index=False)
            print(f"[PositionHistoryManager] Saved: {len(df)} records to Parquet")
        except Exception as e:
            print(f"[PositionHistoryManager] Error saving history: {e}")
            import traceback
            traceback.print_exc()
            raise

    def _get_next_id(self, df: pd.DataFrame) -> int:
        """获取下一个 ID"""
        if df.empty or 'id' not in df.columns:
            return 1
        return int(df['id'].max()) + 1

    def add_history(self, history: PositionHistory) -> int:
        """
        添加历史记录

        Args:
            history: 历史记录信息

        Returns:
            新记录的 ID
        """
        df = self._get_history_df()
        new_id = self._get_next_id(df)

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        history.id = new_id
        history.created_at = now

        new_row = {
            'id': new_id,
            'position_id': history.position_id,
            'stock_code': history.stock_code,
            'stock_name': history.stock_name,
            'action': history.action,
            'shares': history.shares,
            'price': history.price,
            'amount': history.amount,
            'date': history.date,
            'notes': history.notes or '',
            'created_at': now
        }

        if df.empty:
            df = pd.DataFrame([new_row])
        else:
            for column in new_row:
                if column not in df.columns:
                    df[column] = None
            df.loc[len(df), df.columns] = [new_row.get(column) for column in df.columns]

        self._save_history_df(df)
        print(f"[PositionHistoryManager] 添加历史记录成功: {history.stock_code} {history.action}, ID={new_id}")
        return new_id

    def get_history(self, position_id: Optional[int] = None) -> List[PositionHistory]:
        """
        获取历史记录

        Args:
            position_id: 持仓ID，为None则返回所有记录

        Returns:
            历史记录列表
        """
        df = self._get_history_df()
        if df.empty:
            return []

        if position_id is not None:
            df = df[df['position_id'] == position_id]

        df = df.sort_values('date', ascending=False)
        return [self._row_to_history(row) for _, row in df.iterrows()]

    def get_stats(self, position_id: Optional[int] = None) -> PositionHistoryStats:
        """
        获取统计信息

        Args:
            position_id: 持仓ID，为None则统计所有记录

        Returns:
            统计信息
        """
        df = self._get_history_df()
        if df.empty:
            return PositionHistoryStats()

        if position_id is not None:
            df = df[df['position_id'] == position_id]

        stats = PositionHistoryStats()
        stats.total_trades = len(df)
        stats.buy_count = len(df[df['action'] == 'buy'])
        stats.sell_count = len(df[df['action'] == 'sell'])
        stats.add_count = len(df[df['action'] == 'add'])
        stats.reduce_count = len(df[df['action'] == 'reduce'])
        
        # 计算买入/卖出金额
        buy_df = df[df['action'].isin(['buy', 'add'])]
        sell_df = df[df['action'].isin(['sell', 'reduce'])]
        
        stats.total_buy_amount = buy_df['amount'].sum() if not buy_df.empty else 0
        stats.total_sell_amount = abs(sell_df['amount'].sum()) if not sell_df.empty else 0

        return stats

    def delete_history(self, history_id: int) -> bool:
        """
        删除单条历史记录

        Args:
            history_id: 历史记录ID

        Returns:
            是否删除成功
        """
        df = self._get_history_df()
        if df.empty:
            return False

        if 'id' not in df.columns:
            return False

        original_len = len(df)
        df = df[df['id'] != history_id]

        if len(df) == original_len:
            return False

        self._save_history_df(df)
        print(f"[PositionHistoryManager] 删除历史记录成功: id={history_id}")
        return True

    def delete_history_by_position(self, position_id: int) -> bool:
        """
        删除指定持仓的所有历史记录

        Args:
            position_id: 持仓ID

        Returns:
            是否删除成功
        """
        df = self._get_history_df()
        if df.empty:
            return False

        df = df[df['position_id'] != position_id]
        self._save_history_df(df)
        print(f"[PositionHistoryManager] 删除持仓历史成功: position_id={position_id}")
        return True

    def _row_to_history(self, row: pd.Series) -> PositionHistory:
        """将 DataFrame 行转换为 PositionHistory 对象"""
        return PositionHistory(
            id=int(row.get('id', 0)) if pd.notna(row.get('id')) else None,
            position_id=int(row.get('position_id', 0)) if pd.notna(row.get('position_id')) else 0,
            stock_code=str(row.get('stock_code', '')),
            stock_name=str(row.get('stock_name', '')),
            action=str(row.get('action', '')),
            shares=float(row.get('shares', 0)) if pd.notna(row.get('shares')) else 0.0,
            price=float(row.get('price', 0)) if pd.notna(row.get('price')) else 0.0,
            amount=float(row.get('amount', 0)) if pd.notna(row.get('amount')) else 0.0,
            date=str(row.get('date', '')),
            notes=str(row.get('notes', '')),
            created_at=str(row.get('created_at', '')) if pd.notna(row.get('created_at')) else None
        )
