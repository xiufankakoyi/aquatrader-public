"""
持仓管理模块

功能：
- 持仓的增删改查
- 盈亏计算
- 仓位占比分析
- 行业分布统计

存储：使用 ArcticDB 持久化，Polars 读取分析
"""

import json
import os
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict, field
import pandas as pd
import numpy as np

from config.config import Config


@dataclass
class Position:
    """持仓数据结构

    持仓数量字段统一以 ``quantity`` 为准；``shares`` 仍可读以兼容历史数据。
    访问时请优先使用 :py:meth:`quantity` 获取真实持仓数量。
    """

    id: Optional[int] = None
    stock_code: str = ""
    stock_name: str = ""
    buy_price: float = 0.0
    shares: float = 0.0
    quantity: Optional[float] = None
    cost: float = 0.0
    buy_date: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    notes: str = ""
    is_active: bool = True
    mode: str = "demo"  # demo / paper / real
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    current_price: Optional[float] = None
    market_value: Optional[float] = None
    profit_loss: Optional[float] = None
    profit_loss_pct: Optional[float] = None
    weight: Optional[float] = None

    def effective_quantity(self) -> float:
        """统一返回持仓数量：优先 quantity，再 fallback shares。"""

        if self.quantity is not None and self.quantity > 0:
            return float(self.quantity)
        return float(self.shares or 0.0)


class PositionManager:
    """
    持仓管理器

    使用 ArcticDB 持久化，Polars 读取分析
    """

    LIBRARY_NAME = "portfolio"
    SYMBOL_NAME = "positions"
    PARQUET_FILE = "portfolio_positions.parquet"

    def __init__(self):
        self.parquet_path = os.path.join(Config.PARQUET_DIR, self.PARQUET_FILE)
        os.makedirs(Config.PARQUET_DIR, exist_ok=True)

    @property
    def library(self):
        """Lazy load - 使用 Parquet 文件存储（简单可靠）"""
        return None  # 不再使用 ArcticDB

    def _get_positions_df(self) -> pd.DataFrame:
        """
        获取持仓 DataFrame - 从 Parquet 读取
        """
        try:
            if os.path.exists(self.parquet_path):
                import polars as pl
                df = pl.read_parquet(self.parquet_path).to_pandas()
                if 'is_active' in df.columns:
                    df['is_active'] = df['is_active'].astype(bool)
                return df
        except Exception as e:
            print(f"[PositionManager] Polars read error: {e}")
            try:
                return pd.read_parquet(self.parquet_path)
            except Exception as e2:
                print(f"[PositionManager] Pandas read error: {e2}")
        return pd.DataFrame()

    def _save_positions_df(self, df: pd.DataFrame):
        """
        保存持仓 DataFrame - 使用 Parquet 文件
        """
        try:
            df_to_save = df.copy()
            
            if 'is_active' in df_to_save.columns:
                df_to_save['is_active'] = df_to_save['is_active'].apply(lambda x: 1 if bool(x) else 0).astype('int64')
            
            for col in ['stop_loss', 'take_profit']:
                if col in df_to_save.columns:
                    df_to_save[col] = df_to_save[col].astype('float64')
            
            df_to_save.to_parquet(self.parquet_path, index=False)
            print(f"[PositionManager] Saved: {len(df)} records to Parquet")
        except Exception as e:
            print(f"[PositionManager] Error saving positions: {e}")
            import traceback
            traceback.print_exc()
            raise

    def _get_next_id(self, df: pd.DataFrame) -> int:
        """获取下一个 ID"""
        if df.empty or 'id' not in df.columns:
            return 1
        return int(df['id'].max()) + 1

    def add_position(self, position: Position) -> int:
        """
        添加持仓

        Args:
            position: 持仓信息

        Returns:
            新持仓的 ID
        """
        df = self._get_positions_df()
        new_id = self._get_next_id(df)

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        position.id = new_id
        position.created_at = now
        position.updated_at = now

        new_row = {
            'id': new_id,
            'stock_code': position.stock_code,
            'stock_name': position.stock_name,
            'buy_price': position.buy_price,
            'shares': position.shares,
            'quantity': position.effective_quantity(),
            'cost': position.cost,
            'buy_date': position.buy_date,
            'stop_loss': position.stop_loss,
            'take_profit': position.take_profit,
            'notes': position.notes or '',
            'is_active': 1 if position.is_active else 0,
            'mode': position.mode,
            'created_at': now,
            'updated_at': now
        }

        if df.empty:
            df = pd.DataFrame([new_row])
        else:
            for column in new_row:
                if column not in df.columns:
                    df[column] = None
            df.loc[len(df), df.columns] = [new_row.get(column) for column in df.columns]

        self._save_positions_df(df)
        print(f"[PositionManager] 添加持仓成功: {position.stock_code} {position.stock_name}, ID={new_id}")
        return new_id

    def update_position(self, position: Position) -> bool:
        """
        更新持仓

        Args:
            position: 持仓信息（必须包含 id）

        Returns:
            是否更新成功
        """
        if position.id is None:
            return False

        df = self._get_positions_df()
        if df.empty:
            return False

        mask = df['id'] == position.id
        if not mask.any():
            return False

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        df.loc[mask, 'stock_code'] = position.stock_code
        df.loc[mask, 'stock_name'] = position.stock_name
        df.loc[mask, 'buy_price'] = position.buy_price
        df.loc[mask, 'shares'] = position.shares
        df.loc[mask, 'quantity'] = position.effective_quantity()
        df.loc[mask, 'cost'] = position.cost
        df.loc[mask, 'buy_date'] = position.buy_date
        df.loc[mask, 'stop_loss'] = position.stop_loss
        df.loc[mask, 'take_profit'] = position.take_profit
        df.loc[mask, 'notes'] = position.notes or ''
        df.loc[mask, 'is_active'] = 1 if position.is_active else 0
        df.loc[mask, 'mode'] = position.mode
        df.loc[mask, 'updated_at'] = now

        self._save_positions_df(df)
        print(f"[PositionManager] 更新持仓成功: ID={position.id}")
        return True

    def delete_position(self, position_id: int) -> bool:
        """
        删除持仓

        Args:
            position_id: 持仓 ID

        Returns:
            是否删除成功
        """
        df = self._get_positions_df()
        if df.empty:
            return False

        mask = df['id'] == position_id
        if not mask.any():
            return False

        df = df[~mask]
        self._save_positions_df(df)
        print(f"[PositionManager] 删除持仓成功: ID={position_id}")
        return True

    def get_position(self, position_id: int) -> Optional[Position]:
        """
        获取单个持仓

        Args:
            position_id: 持仓 ID

        Returns:
            持仓信息
        """
        df = self._get_positions_df()
        if df.empty or 'id' not in df.columns:
            return None

        match = df[df['id'] == position_id]
        if match.empty:
            return None

        return self._row_to_position(match.iloc[0])

    def get_all_positions(self, active_only: bool = True) -> List[Position]:
        """
        获取所有持仓

        Args:
            active_only: 是否只获取活跃持仓

        Returns:
            持仓列表
        """
        df = self._get_positions_df()
        if df.empty:
            return []

        if active_only:
            # is_active 可能是 int (0/1) 或 bool，需要兼容处理
            df = df[df['is_active'].astype(bool) == True]

        if df.empty or 'buy_date' not in df.columns:
            return []

        df = df.sort_values('buy_date', ascending=False)
        return [self._row_to_position(row) for _, row in df.iterrows()]

    def get_positions_by_codes(self, stock_codes: List[str]) -> List[Position]:
        """
        根据股票代码获取持仓

        Args:
            stock_codes: 股票代码列表

        Returns:
            持仓列表
        """
        if not stock_codes:
            return []

        df = self._get_positions_df()
        if df.empty or 'stock_code' not in df.columns or 'is_active' not in df.columns:
            return []

        df = df[(df['stock_code'].isin(stock_codes)) & (df['is_active'] == True)]
        return [self._row_to_position(row) for _, row in df.iterrows()]

    def _row_to_position(self, row: pd.Series) -> Position:
        """将 DataFrame 行转换为 Position 对象，兼容 quantity / shares。"""

        raw_quantity = row.get('quantity', None)
        raw_shares = row.get('shares', 0)
        if pd.notna(raw_quantity) and float(raw_quantity) > 0:
            quantity_value: Optional[float] = float(raw_quantity)
            shares_value = float(raw_shares) if pd.notna(raw_shares) else float(raw_quantity)
        else:
            quantity_value = None
            shares_value = float(raw_shares) if pd.notna(raw_shares) else 0.0
        return Position(
            id=int(row.get('id', 0)) if pd.notna(row.get('id')) else None,
            stock_code=str(row.get('stock_code', '')),
            stock_name=str(row.get('stock_name', '')),
            buy_price=float(row.get('buy_price', 0)) if pd.notna(row.get('buy_price')) else 0.0,
            shares=shares_value,
            quantity=quantity_value,
            cost=float(row.get('cost', 0)) if pd.notna(row.get('cost')) else 0.0,
            buy_date=str(row.get('buy_date', '')),
            stop_loss=float(row.get('stop_loss')) if pd.notna(row.get('stop_loss')) else None,
            take_profit=float(row.get('take_profit')) if pd.notna(row.get('take_profit')) else None,
            notes=str(row.get('notes', '')),
            is_active=bool(int(row.get('is_active', 1))) if pd.notna(row.get('is_active')) else True,
            mode=str(row.get('mode', 'demo')) if pd.notna(row.get('mode')) else 'demo',
            created_at=str(row.get('created_at', '')) if pd.notna(row.get('created_at')) else None,
            updated_at=str(row.get('updated_at', '')) if pd.notna(row.get('updated_at')) else None
        )

    def calculate_analysis(self, positions: List[Position], price_data: Dict[str, float]) -> Dict[str, Any]:
        """
        计算持仓分析

        Args:
            positions: 持仓列表
            price_data: 最新价格数据 {stock_code: current_price}

        Returns:
            分析结果
        """
        total_market_value = 0.0
        total_cost = 0.0
        total_profit_loss = 0.0

        analyzed_positions = []

        for pos in positions:
            current_price = price_data.get(pos.stock_code)
            held_quantity = pos.effective_quantity()

            if current_price and held_quantity > 0:
                pos.current_price = current_price
                pos.market_value = current_price * held_quantity
                if pos.cost and pos.cost > 0:
                    pos.profit_loss = pos.market_value - pos.cost
                    pos.profit_loss_pct = (pos.profit_loss / pos.cost * 100) if pos.cost > 0 else 0
                else:
                    pos.profit_loss = None
                    pos.profit_loss_pct = None

                total_market_value += pos.market_value
                total_cost += pos.cost
                if pos.profit_loss is not None:
                    total_profit_loss += pos.profit_loss

            analyzed_positions.append(pos)

        for pos in analyzed_positions:
            if pos.market_value and total_market_value > 0:
                pos.weight = pos.market_value / total_market_value * 100

        return {
            'positions': [asdict(p) for p in analyzed_positions],
            'summary': {
                'total_market_value': total_market_value,
                'total_cost': total_cost,
                'total_profit_loss': total_profit_loss,
                'total_profit_loss_pct': (total_profit_loss / total_cost * 100) if total_cost > 0 else 0,
                'position_count': len(analyzed_positions),
                'active_count': sum(1 for p in analyzed_positions if p.is_active)
            }
        }

    def get_industry_distribution(self, positions: List[Position]) -> Dict[str, float]:
        """
        获取行业分布

        Args:
            positions: 持仓列表（需已计算 market_value）

        Returns:
            行业分布 {industry: weight}
        """
        if not positions:
            return {}

        total_value = sum(p.market_value or 0 for p in positions)
        if total_value == 0:
            return {}

        stock_codes = [p.stock_code for p in positions if p.market_value]
        if not stock_codes:
            return {}

        code_to_industry = self._get_industry_info(stock_codes)

        industry_values: Dict[str, float] = {}
        for pos in positions:
            if not pos.market_value:
                continue
            industry = code_to_industry.get(pos.stock_code, '其他')
            if industry not in industry_values:
                industry_values[industry] = 0.0
            industry_values[industry] += pos.market_value

        return {
            industry: value / total_value * 100
            for industry, value in industry_values.items()
        }

    def get_industry_distribution_from_dict(self, positions: List[Dict]) -> Dict[str, float]:
        """
        从字典列表获取行业分布（用于 API 返回的数据）

        Args:
            positions: 持仓字典列表（需包含 stock_code 和 market_value）

        Returns:
            行业分布 {industry: weight}
        """
        if not positions:
            return {}

        total_value = sum(p.get('market_value') or 0 for p in positions)
        if total_value == 0:
            return {}

        stock_codes = [p.get('stock_code') for p in positions if p.get('market_value')]
        if not stock_codes:
            return {}

        code_to_industry = self._get_industry_info(stock_codes)

        industry_values: Dict[str, float] = {}
        for pos in positions:
            market_value = pos.get('market_value')
            if not market_value:
                continue
            stock_code = pos.get('stock_code')
            industry = code_to_industry.get(stock_code, '其他')
            if industry not in industry_values:
                industry_values[industry] = 0.0
            industry_values[industry] += market_value

        return {
            industry: value / total_value * 100
            for industry, value in industry_values.items()
        }

    def _get_industry_info(self, stock_codes: List[str]) -> Dict[str, str]:
        """
        获取股票行业信息（从 ArcticDB）

        Args:
            stock_codes: 股票代码列表

        Returns:
            {stock_code: industry}
        """
        code_to_industry = {}

        # 基金代码映射：易方达瑞锦C -> 债基，其他基金/ETF -> 基金
        fund_category_map = {
            '009690': '债基',  # 易方达瑞锦C
        }

        try:
            from data_svc.unified_data_query import get_stock_basic
            
            df = get_stock_basic()
            if df is not None and not df.empty:
                # get_stock_basic 返回的 DataFrame 可能使用 'code' 或 'symbol' 列
                code_col = 'code' if 'code' in df.columns else ('symbol' if 'symbol' in df.columns else None)
                if code_col:
                    df_filtered = df[df[code_col].isin(stock_codes)]
                    
                    for _, row in df_filtered.iterrows():
                        code = row.get(code_col)
                        if code:
                            industry = row.get('industry')
                            
                            # 如果没有行业信息，检查是否为基金
                            if not industry or industry == '其他':
                                # 检查是否在基金映射中
                                if code in fund_category_map:
                                    industry = fund_category_map[code]
                                # 检查是否为基金代码（以0开头的6位代码通常是基金）
                                elif code.startswith('0') and len(code) == 6:
                                    industry = '基金'
                                else:
                                    industry = '其他'
                            
                            code_to_industry[code] = industry

        except Exception as e:
            print(f"[PositionManager] Error getting industry: {e}")

        # 处理不在 stock_basic 中的代码（基金/ETF等）
        for code in stock_codes:
            if code not in code_to_industry:
                # 检查是否在基金映射中
                if code in fund_category_map:
                    code_to_industry[code] = fund_category_map[code]
                # ETF代码通常是5位或6位数字，以5开头
                elif code.startswith('5') and len(code) == 6:
                    code_to_industry[code] = '基金'
                # 基金代码通常以0开头
                elif code.startswith('0') and len(code) == 6:
                    code_to_industry[code] = '基金'
                else:
                    code_to_industry[code] = '其他'

        return code_to_industry

    def check_stop_loss_take_profit(self, positions: List[Position], price_data: Dict[str, float]) -> List[Dict[str, Any]]:
        """
        检查止损止盈触发

        Args:
            positions: 持仓列表
            price_data: 最新价格数据

        Returns:
            触发列表
        """
        alerts = []

        for pos in positions:
            current_price = price_data.get(pos.stock_code)
            if not current_price:
                continue

            if pos.stop_loss and current_price <= pos.stop_loss:
                alerts.append({
                    'stock_code': pos.stock_code,
                    'stock_name': pos.stock_name,
                    'alert_type': 'stop_loss',
                    'trigger_price': pos.stop_loss,
                    'current_price': current_price,
                    'message': f'{pos.stock_name}({pos.stock_code}) 触发止损：当前价 {current_price:.2f} <= 止损价 {pos.stop_loss:.2f}'
                })

            if pos.take_profit and current_price >= pos.take_profit:
                alerts.append({
                    'stock_code': pos.stock_code,
                    'stock_name': pos.stock_name,
                    'alert_type': 'take_profit',
                    'trigger_price': pos.take_profit,
                    'current_price': current_price,
                    'message': f'{pos.stock_name}({pos.stock_code}) 触发止盈：当前价 {current_price:.2f} >= 止盈价 {pos.take_profit:.2f}'
                })

        return alerts

    def import_from_excel(self, file_path: str) -> int:
        """
        从 Excel 导入持仓

        Args:
            file_path: Excel 文件路径

        Returns:
            导入的持仓数量
        """
        try:
            df = pd.read_excel(file_path)

            required_cols = ['stock_code', 'stock_name', 'buy_price', 'shares', 'cost', 'buy_date']
            missing = [col for col in required_cols if col not in df.columns]
            if missing:
                raise ValueError(f"Excel 缺少必要列: {missing}")

            count = 0
            for _, row in df.iterrows():
                position = Position(
                    stock_code=str(row['stock_code']).zfill(6),
                    stock_name=str(row['stock_name']),
                    buy_price=float(row['buy_price']),
                    shares=float(row['shares']),
                    cost=float(row['cost']),
                    buy_date=str(row['buy_date']),
                    stop_loss=row.get('stop_loss') if pd.notna(row.get('stop_loss')) else None,
                    take_profit=row.get('take_profit') if pd.notna(row.get('take_profit')) else None,
                    notes=str(row.get('notes', ''))
                )
                self.add_position(position)
                count += 1

            return count
        except ImportError:
            raise RuntimeError("需要安装 pandas 和 openpyxl: pip install pandas openpyxl")
