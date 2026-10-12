"""
自选股监控管理模块

功能：
- 管理自选股列表
- 支持多种监控条件（价格、MA、MACD、RSI等）
- 信号检测与飞书推送
- 推送状态追踪（避免重复推送）

存储：使用 Parquet 文件存储
"""

import os
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict, field
import pandas as pd
import numpy as np

from config.config import Config
from config.logger import get_logger

logger = get_logger(__name__)


SUPPORTED_CONDITIONS = {
    'price': {
        'name': '价格条件',
        'conditions': [
            {'key': 'price_above', 'name': '价格突破', 'desc': '价格涨破目标价', 'params': ['target_price']},
            {'key': 'price_below', 'name': '价格跌破', 'desc': '价格跌破目标价', 'params': ['target_price']},
        ]
    },
    'ma': {
        'name': '均线条件',
        'conditions': [
            {'key': 'ma5_break_up', 'name': '突破5日线', 'desc': '收盘价突破MA5', 'params': []},
            {'key': 'ma5_break_down', 'name': '跌破5日线', 'desc': '收盘价跌破MA5', 'params': []},
            {'key': 'ma10_break_up', 'name': '突破10日线', 'desc': '收盘价突破MA10', 'params': []},
            {'key': 'ma10_break_down', 'name': '跌破10日线', 'desc': '收盘价跌破MA10', 'params': []},
            {'key': 'ma20_break_up', 'name': '突破20日线', 'desc': '收盘价突破MA20', 'params': []},
            {'key': 'ma20_break_down', 'name': '跌破20日线', 'desc': '收盘价跌破MA20', 'params': []},
            {'key': 'ma30_break_up', 'name': '突破30日线', 'desc': '收盘价突破MA30', 'params': []},
            {'key': 'ma30_break_down', 'name': '跌破30日线', 'desc': '收盘价跌破MA30', 'params': []},
            {'key': 'ma60_break_up', 'name': '突破60日线', 'desc': '收盘价突破MA60', 'params': []},
            {'key': 'ma60_break_down', 'name': '跌破60日线', 'desc': '收盘价跌破MA60', 'params': []},
            {'key': 'ma120_break_up', 'name': '突破120日线', 'desc': '收盘价突破MA120', 'params': []},
            {'key': 'ma120_break_down', 'name': '跌破120日线', 'desc': '收盘价跌破MA120', 'params': []},
            {'key': 'ma_bull_alignment', 'name': '均线多头排列', 'desc': 'MA5>MA10>MA20', 'params': []},
            {'key': 'ma_bear_alignment', 'name': '均线空头排列', 'desc': 'MA5<MA10<MA20', 'params': []},
            {'key': 'ma_golden_cross', 'name': '均线金叉', 'desc': '短期均线上穿长期均线', 'params': ['short_period', 'long_period']},
            {'key': 'ma_death_cross', 'name': '均线死叉', 'desc': '短期均线下穿长期均线', 'params': ['short_period', 'long_period']},
        ]
    },
    'macd': {
        'name': 'MACD条件',
        'conditions': [
            {'key': 'macd_golden_cross', 'name': 'MACD金叉', 'desc': 'DIF上穿DEA', 'params': []},
            {'key': 'macd_death_cross', 'name': 'MACD死叉', 'desc': 'DIF下穿DEA', 'params': []},
            {'key': 'macd_bar_positive', 'name': 'MACD红柱', 'desc': 'MACD柱状线由负转正', 'params': []},
            {'key': 'macd_bar_negative', 'name': 'MACD绿柱', 'desc': 'MACD柱状线由正转负', 'params': []},
        ]
    },
    'rsi': {
        'name': 'RSI条件',
        'conditions': [
            {'key': 'rsi_oversold', 'name': 'RSI超卖', 'desc': 'RSI低于超卖线', 'params': ['threshold']},
            {'key': 'rsi_overbought', 'name': 'RSI超买', 'desc': 'RSI高于超买线', 'params': ['threshold']},
            {'key': 'rsi_cross_up', 'name': 'RSI上穿', 'desc': 'RSI上穿指定值', 'params': ['threshold']},
            {'key': 'rsi_cross_down', 'name': 'RSI下穿', 'desc': 'RSI下穿指定值', 'params': ['threshold']},
        ]
    },
    'kdj': {
        'name': 'KDJ条件',
        'conditions': [
            {'key': 'kdj_golden_cross', 'name': 'KDJ金叉', 'desc': 'K线上穿D线', 'params': []},
            {'key': 'kdj_death_cross', 'name': 'KDJ死叉', 'desc': 'K线下穿D线', 'params': []},
            {'key': 'kdj_oversold', 'name': 'KDJ超卖', 'desc': 'J值低于超卖线', 'params': ['threshold']},
        ]
    },
    'boll': {
        'name': '布林带条件',
        'conditions': [
            {'key': 'boll_break_upper', 'name': '突破布林上轨', 'desc': '价格突破布林上轨', 'params': []},
            {'key': 'boll_break_lower', 'name': '跌破布林下轨', 'desc': '价格跌破布林下轨', 'params': []},
            {'key': 'boll_squeeze', 'name': '布林收口', 'desc': '布林带宽度收窄', 'params': ['threshold']},
        ]
    },
    'volume': {
        'name': '成交量条件',
        'conditions': [
            {'key': 'volume_surge', 'name': '放量', 'desc': '成交量超过均量倍数', 'params': ['multiplier']},
            {'key': 'volume_shrink', 'name': '缩量', 'desc': '成交量低于均量比例', 'params': ['ratio']},
        ]
    }
}


@dataclass
class MonitorCondition:
    """监控条件"""
    key: str
    category: str
    params: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class WatchItem:
    """自选股数据结构"""
    id: Optional[int] = None
    stock_code: str = ""
    stock_name: str = ""
    
    buy_target_price: Optional[float] = None
    sell_target_price: Optional[float] = None
    
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    
    notes: str = ""
    tags: List[str] = field(default_factory=list)
    
    is_active: bool = True
    feishu_notify: bool = True
    
    last_trigger_time: Optional[str] = None
    last_trigger_condition: Optional[str] = None
    last_notify_time: Optional[str] = None
    
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    current_price: Optional[float] = None


class WatchlistManager:
    """
    自选股管理器
    
    管理用户关注的股票列表，支持多种监控条件
    """
    
    PARQUET_FILE = "watchlist.parquet"
    
    def __init__(self):
        self.parquet_path = os.path.join(Config.PARQUET_DIR, self.PARQUET_FILE)
        os.makedirs(Config.PARQUET_DIR, exist_ok=True)
    
    def _get_watchlist_df(self) -> pd.DataFrame:
        """获取自选股列表 DataFrame"""
        try:
            if os.path.exists(self.parquet_path):
                import polars as pl
                df = pl.read_parquet(self.parquet_path).to_pandas()
                return df
        except Exception as e:
            logger.warning(f"Polars read error: {e}")
        return pd.DataFrame()
    
    def _save_watchlist_df(self, df: pd.DataFrame):
        """保存自选股列表 DataFrame"""
        try:
            df_to_save = df.copy()
            
            for col in ['is_active', 'feishu_notify']:
                if col in df_to_save.columns:
                    df_to_save[col] = df_to_save[col].apply(lambda x: 1 if bool(x) else 0).astype('int64')
            
            for col in ['buy_target_price', 'sell_target_price']:
                if col in df_to_save.columns:
                    df_to_save[col] = df_to_save[col].astype('float64')
            
            if 'conditions' in df_to_save.columns:
                df_to_save['conditions'] = df_to_save['conditions'].apply(
                    lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, (list, dict)) else x
                )
            
            if 'tags' in df_to_save.columns:
                df_to_save['tags'] = df_to_save['tags'].apply(
                    lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, list) else x
                )
            
            df_to_save.to_parquet(self.parquet_path, index=False)
            logger.info(f"保存自选股列表成功: {len(df)} 条记录")
        except Exception as e:
            logger.error(f"保存自选股列表失败: {e}")
            raise
    
    def _get_next_id(self, df: pd.DataFrame) -> int:
        """获取下一个 ID"""
        if df.empty or 'id' not in df.columns:
            return 1
        return int(df['id'].max()) + 1
    
    def add_item(self, item: WatchItem) -> int:
        """添加自选股"""
        df = self._get_watchlist_df()
        
        existing = df[df['stock_code'] == item.stock_code] if not df.empty else pd.DataFrame()
        if not existing.empty:
            logger.info(f"股票 {item.stock_code} 已在自选列表中，更新数据")
            item.id = int(existing.iloc[0]['id'])
            return self.update_item(item)
        
        new_id = self._get_next_id(df)
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        item.id = new_id
        item.created_at = now
        item.updated_at = now
        
        new_row = pd.DataFrame([{
            'id': new_id,
            'stock_code': item.stock_code,
            'stock_name': item.stock_name,
            'buy_target_price': item.buy_target_price,
            'sell_target_price': item.sell_target_price,
            'conditions': json.dumps(item.conditions, ensure_ascii=False),
            'notes': item.notes or '',
            'tags': json.dumps(item.tags, ensure_ascii=False),
            'is_active': 1 if item.is_active else 0,
            'feishu_notify': 1 if item.feishu_notify else 0,
            'last_trigger_time': None,
            'last_trigger_condition': None,
            'last_notify_time': None,
            'created_at': now,
            'updated_at': now
        }])
        
        if df.empty:
            df = new_row
        else:
            df = pd.concat([df, new_row], ignore_index=True)
        
        self._save_watchlist_df(df)
        logger.info(f"添加自选股成功: {item.stock_code} {item.stock_name}, ID={new_id}")
        return new_id
    
    def update_item(self, item: WatchItem) -> int:
        """更新自选股"""
        if item.id is None:
            return self.add_item(item)
        
        df = self._get_watchlist_df()
        if df.empty:
            return self.add_item(item)
        
        mask = df['id'] == item.id
        if not mask.any():
            return self.add_item(item)
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        df.loc[mask, 'stock_code'] = item.stock_code
        df.loc[mask, 'stock_name'] = item.stock_name
        df.loc[mask, 'buy_target_price'] = item.buy_target_price
        df.loc[mask, 'sell_target_price'] = item.sell_target_price
        df.loc[mask, 'conditions'] = json.dumps(item.conditions, ensure_ascii=False)
        df.loc[mask, 'notes'] = item.notes or ''
        df.loc[mask, 'tags'] = json.dumps(item.tags, ensure_ascii=False)
        df.loc[mask, 'is_active'] = 1 if item.is_active else 0
        df.loc[mask, 'feishu_notify'] = 1 if item.feishu_notify else 0
        df.loc[mask, 'updated_at'] = now
        
        self._save_watchlist_df(df)
        logger.info(f"更新自选股成功: ID={item.id}")
        return item.id
    
    def delete_item(self, item_id: int) -> bool:
        """删除自选股"""
        df = self._get_watchlist_df()
        if df.empty:
            return False
        
        mask = df['id'] == item_id
        if not mask.any():
            return False
        
        df = df[~mask]
        self._save_watchlist_df(df)
        logger.info(f"删除自选股成功: ID={item_id}")
        return True
    
    def get_item(self, item_id: int) -> Optional[WatchItem]:
        """获取单个自选股"""
        df = self._get_watchlist_df()
        if df.empty:
            return None
        
        match = df[df['id'] == item_id]
        if match.empty:
            return None
        
        return self._row_to_item(match.iloc[0])
    
    def get_item_by_code(self, stock_code: str) -> Optional[WatchItem]:
        """根据股票代码获取自选股"""
        df = self._get_watchlist_df()
        if df.empty:
            return None
        
        match = df[df['stock_code'] == stock_code]
        if match.empty:
            return None
        
        return self._row_to_item(match.iloc[0])
    
    def get_all_items(self, active_only: bool = True) -> List[WatchItem]:
        """获取所有自选股"""
        df = self._get_watchlist_df()
        if df.empty:
            return []
        
        if active_only:
            df = df[df['is_active'].astype(bool) == True]
        
        df = df.sort_values('created_at', ascending=False)
        return [self._row_to_item(row) for _, row in df.iterrows()]
    
    def _row_to_item(self, row: pd.Series) -> WatchItem:
        """将 DataFrame 行转换为 WatchItem 对象"""
        conditions_raw = row.get('conditions', '[]')
        if isinstance(conditions_raw, str):
            try:
                conditions = json.loads(conditions_raw)
            except:
                conditions = []
        else:
            conditions = conditions_raw if conditions_raw else []
        
        tags_raw = row.get('tags', '[]')
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
            except:
                tags = []
        else:
            tags = tags_raw if tags_raw else []
        
        return WatchItem(
            id=int(row.get('id', 0)) if pd.notna(row.get('id')) else None,
            stock_code=str(row.get('stock_code', '')),
            stock_name=str(row.get('stock_name', '')),
            buy_target_price=float(row.get('buy_target_price')) if pd.notna(row.get('buy_target_price')) else None,
            sell_target_price=float(row.get('sell_target_price')) if pd.notna(row.get('sell_target_price')) else None,
            conditions=conditions,
            notes=str(row.get('notes', '')),
            tags=tags,
            is_active=bool(int(row.get('is_active', 1))) if pd.notna(row.get('is_active')) else True,
            feishu_notify=bool(int(row.get('feishu_notify', 1))) if pd.notna(row.get('feishu_notify')) else True,
            last_trigger_time=str(row.get('last_trigger_time', '')) if pd.notna(row.get('last_trigger_time')) else None,
            last_trigger_condition=str(row.get('last_trigger_condition', '')) if pd.notna(row.get('last_trigger_condition')) else None,
            last_notify_time=str(row.get('last_notify_time', '')) if pd.notna(row.get('last_notify_time')) else None,
            created_at=str(row.get('created_at', '')) if pd.notna(row.get('created_at')) else None,
            updated_at=str(row.get('updated_at', '')) if pd.notna(row.get('updated_at')) else None
        )
    
    def update_trigger_time(self, item_id: int, condition_key: str, notify: bool = False):
        """更新触发时间和通知时间"""
        df = self._get_watchlist_df()
        if df.empty:
            return
        
        mask = df['id'] == item_id
        if not mask.any():
            return
        
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        df.loc[mask, 'last_trigger_time'] = now
        df.loc[mask, 'last_trigger_condition'] = condition_key
        
        if notify:
            df.loc[mask, 'last_notify_time'] = now
        
        self._save_watchlist_df(df)
    
    def get_stock_data(self, stock_code: str) -> Optional[pd.DataFrame]:
        """获取股票最新数据（从 ArcticDB）"""
        try:
            from data_svc.database.optimized_data_query import OptimizedStockDataQuery
            
            query = OptimizedStockDataQuery()
            # 获取最近10个交易日的数据
            from datetime import datetime, timedelta
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            
            df = query.get_stock_history(stock_code, start_date, end_date)
            
            if df is not None and not df.empty:
                # 按日期降序排列，取最近10条
                df = df.sort_values('trade_date', ascending=False).head(10)
                return df
            return None
        except Exception as e:
            logger.error(f"获取股票数据失败 {stock_code}: {e}")
            return None
    
    def check_condition(self, item: WatchItem, stock_data: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        检查单个自选股的监控条件
        
        Returns:
            触发的条件列表
        """
        triggered = []
        
        if stock_data is None or stock_data.empty:
            return triggered
        
        latest = stock_data.iloc[0]
        prev = stock_data.iloc[1] if len(stock_data) > 1 else latest
        
        current_price = float(latest.get('close', 0))
        
        if item.buy_target_price and current_price <= item.buy_target_price:
            triggered.append({
                'key': 'buy_target',
                'name': '买入目标价',
                'message': f"当前价 {current_price:.2f} <= 目标买入价 {item.buy_target_price:.2f}",
                'severity': 'buy'
            })
        
        if item.sell_target_price and current_price <= item.sell_target_price:
            triggered.append({
                'key': 'sell_target',
                'name': '止盈/止损价',
                'message': f"当前价 {current_price:.2f} <= 止盈/止损价 {item.sell_target_price:.2f}",
                'severity': 'sell'
            })
        
        for cond in item.conditions:
            if not cond.get('enabled', True):
                continue
            
            key = cond.get('key', '')
            params = cond.get('params', {})
            result = self._check_technical_condition(key, params, latest, prev)
            if result:
                triggered.append(result)
        
        return triggered
    
    def _check_technical_condition(
        self, 
        key: str, 
        params: Dict[str, Any], 
        latest: pd.Series, 
        prev: pd.Series
    ) -> Optional[Dict[str, Any]]:
        """检查技术指标条件"""
        
        if key == 'ma5_break_up':
            if latest.get('close', 0) > latest.get('ma5', 0) and prev.get('close', 0) <= prev.get('ma5', 0):
                return {'key': key, 'name': '突破5日线', 'message': f"收盘价 {latest['close']:.2f} 突破MA5 {latest['ma5']:.2f}", 'severity': 'info'}
        
        elif key == 'ma5_break_down':
            if latest.get('close', 0) < latest.get('ma5', 0) and prev.get('close', 0) >= prev.get('ma5', 0):
                return {'key': key, 'name': '动态止盈(跌破5日线)', 'message': f"收盘价 {latest['close']:.2f} 跌破MA5 {latest['ma5']:.2f}，建议卖出", 'severity': 'sell'}
        
        elif key == 'ma10_break_up':
            if latest.get('close', 0) > latest.get('ma10', 0) and prev.get('close', 0) <= prev.get('ma10', 0):
                return {'key': key, 'name': '突破10日线', 'message': f"收盘价 {latest['close']:.2f} 突破MA10 {latest['ma10']:.2f}", 'severity': 'info'}
        
        elif key == 'ma10_break_down':
            if latest.get('close', 0) < latest.get('ma10', 0) and prev.get('close', 0) >= prev.get('ma10', 0):
                return {'key': key, 'name': '动态止盈(跌破10日线)', 'message': f"收盘价 {latest['close']:.2f} 跌破MA10 {latest['ma10']:.2f}，建议卖出", 'severity': 'sell'}
        
        elif key == 'ma20_break_up':
            if latest.get('close', 0) > latest.get('ma20', 0) and prev.get('close', 0) <= prev.get('ma20', 0):
                return {'key': key, 'name': '突破20日线', 'message': f"收盘价 {latest['close']:.2f} 突破MA20 {latest['ma20']:.2f}", 'severity': 'info'}
        
        elif key == 'ma20_break_down':
            if latest.get('close', 0) < latest.get('ma20', 0) and prev.get('close', 0) >= prev.get('ma20', 0):
                return {'key': key, 'name': '跌破20日线', 'message': f"收盘价 {latest['close']:.2f} 跌破MA20 {latest['ma20']:.2f}", 'severity': 'warning'}
        
        elif key == 'ma30_break_up':
            if latest.get('close', 0) > latest.get('ma30', 0) and prev.get('close', 0) <= prev.get('ma30', 0):
                return {'key': key, 'name': '突破30日线', 'message': f"收盘价 {latest['close']:.2f} 突破MA30 {latest['ma30']:.2f}", 'severity': 'info'}
        
        elif key == 'ma30_break_down':
            if latest.get('close', 0) < latest.get('ma30', 0) and prev.get('close', 0) >= prev.get('ma30', 0):
                return {'key': key, 'name': '跌破30日线', 'message': f"收盘价 {latest['close']:.2f} 跌破MA30 {latest['ma30']:.2f}", 'severity': 'warning'}
        
        elif key == 'ma60_break_up':
            if latest.get('close', 0) > latest.get('ma60', 0) and prev.get('close', 0) <= prev.get('ma60', 0):
                return {'key': key, 'name': '突破60日线', 'message': f"收盘价 {latest['close']:.2f} 突破MA60 {latest['ma60']:.2f}", 'severity': 'info'}
        
        elif key == 'ma60_break_down':
            if latest.get('close', 0) < latest.get('ma60', 0) and prev.get('close', 0) >= prev.get('ma60', 0):
                return {'key': key, 'name': '跌破60日线', 'message': f"收盘价 {latest['close']:.2f} 跌破MA60 {latest['ma60']:.2f}", 'severity': 'warning'}
        
        elif key == 'ma120_break_up':
            if latest.get('close', 0) > latest.get('ma120', 0) and prev.get('close', 0) <= prev.get('ma120', 0):
                return {'key': key, 'name': '突破120日线', 'message': f"收盘价 {latest['close']:.2f} 突破MA120 {latest['ma120']:.2f}", 'severity': 'info'}
        
        elif key == 'ma120_break_down':
            if latest.get('close', 0) < latest.get('ma120', 0) and prev.get('close', 0) >= prev.get('ma120', 0):
                return {'key': key, 'name': '跌破120日线', 'message': f"收盘价 {latest['close']:.2f} 跌破MA120 {latest['ma120']:.2f}", 'severity': 'warning'}
        
        elif key == 'ma_golden_cross':
            short_period = params.get('short_period', 5)
            long_period = params.get('long_period', 20)
            short_ma = latest.get(f'ma{short_period}', 0)
            long_ma = latest.get(f'ma{long_period}', 0)
            prev_short_ma = prev.get(f'ma{short_period}', 0)
            prev_long_ma = prev.get(f'ma{long_period}', 0)
            if short_ma > long_ma and prev_short_ma <= prev_long_ma:
                return {'key': key, 'name': '均线金叉', 'message': f"MA{short_period}({short_ma:.2f}) 上穿 MA{long_period}({long_ma:.2f})", 'severity': 'buy'}
        
        elif key == 'ma_death_cross':
            short_period = params.get('short_period', 5)
            long_period = params.get('long_period', 20)
            short_ma = latest.get(f'ma{short_period}', 0)
            long_ma = latest.get(f'ma{long_period}', 0)
            prev_short_ma = prev.get(f'ma{short_period}', 0)
            prev_long_ma = prev.get(f'ma{long_period}', 0)
            if short_ma < long_ma and prev_short_ma >= prev_long_ma:
                return {'key': key, 'name': '均线死叉', 'message': f"MA{short_period}({short_ma:.2f}) 下穿 MA{long_period}({long_ma:.2f})", 'severity': 'sell'}
        
        elif key == 'ma_bull_alignment':
            ma5 = latest.get('ma5', 0)
            ma10 = latest.get('ma10', 0)
            ma20 = latest.get('ma20', 0)
            if ma5 > ma10 > ma20:
                return {'key': key, 'name': '均线多头排列', 'message': f"MA5({ma5:.2f}) > MA10({ma10:.2f}) > MA20({ma20:.2f})", 'severity': 'info'}
        
        elif key == 'macd_golden_cross':
            if latest.get('macd_golden_cross', False):
                return {'key': key, 'name': 'MACD金叉', 'message': f"DIF {latest.get('macd_dif', 0):.4f} 上穿 DEA {latest.get('macd_dea', 0):.4f}", 'severity': 'buy'}
        
        elif key == 'macd_death_cross':
            if latest.get('macd_death_cross', False):
                return {'key': key, 'name': 'MACD死叉', 'message': f"DIF {latest.get('macd_dif', 0):.4f} 下穿 DEA {latest.get('macd_dea', 0):.4f}", 'severity': 'sell'}
        
        elif key == 'rsi_oversold':
            threshold = params.get('threshold', 30)
            rsi = latest.get('rsi_6', 0)
            if rsi < threshold:
                return {'key': key, 'name': 'RSI超卖', 'message': f"RSI(6) = {rsi:.2f} < {threshold}", 'severity': 'buy'}
        
        elif key == 'rsi_overbought':
            threshold = params.get('threshold', 70)
            rsi = latest.get('rsi_6', 0)
            if rsi > threshold:
                return {'key': key, 'name': 'RSI超买', 'message': f"RSI(6) = {rsi:.2f} > {threshold}", 'severity': 'sell'}
        
        elif key == 'kdj_golden_cross':
            k = latest.get('kdj_k', 0)
            d = latest.get('kdj_d', 0)
            prev_k = prev.get('kdj_k', 0)
            prev_d = prev.get('kdj_d', 0)
            if k > d and prev_k <= prev_d:
                return {'key': key, 'name': 'KDJ金叉', 'message': f"K({k:.2f}) 上穿 D({d:.2f})", 'severity': 'buy'}
        
        elif key == 'kdj_death_cross':
            k = latest.get('kdj_k', 0)
            d = latest.get('kdj_d', 0)
            prev_k = prev.get('kdj_k', 0)
            prev_d = prev.get('kdj_d', 0)
            if k < d and prev_k >= prev_d:
                return {'key': key, 'name': 'KDJ死叉', 'message': f"K({k:.2f}) 下穿 D({d:.2f})", 'severity': 'sell'}
        
        elif key == 'boll_break_upper':
            close = latest.get('close', 0)
            upper = latest.get('boll_upper', 0)
            if close > upper:
                return {'key': key, 'name': '突破布林上轨', 'message': f"收盘价 {close:.2f} > 上轨 {upper:.2f}", 'severity': 'info'}
        
        elif key == 'boll_break_lower':
            close = latest.get('close', 0)
            lower = latest.get('boll_lower', 0)
            if close < lower:
                return {'key': key, 'name': '跌破布林下轨', 'message': f"收盘价 {close:.2f} < 下轨 {lower:.2f}", 'severity': 'warning'}
        
        elif key == 'volume_surge':
            multiplier = params.get('multiplier', 2.0)
            vol = latest.get('volume', 0)
            vol_ma5 = latest.get('volume_ma5', vol)
            if vol_ma5 > 0 and vol > vol_ma5 * multiplier:
                return {'key': key, 'name': '放量', 'message': f"成交量 {vol/10000:.0f}万 > 5日均量 {vol_ma5/10000:.0f}万 × {multiplier}", 'severity': 'info'}
        
        return None
    
    def check_all_signals(
        self,
        feishu_webhook: Optional[str] = None,
        cooldown_hours: int = 4
    ) -> Dict[str, Any]:
        """
        检查所有自选股的信号并发送飞书通知
        
        Args:
            feishu_webhook: 飞书 webhook URL
            cooldown_hours: 通知冷却时间（小时）
        
        Returns:
            检测结果
        """
        items = self.get_all_items(active_only=True)
        if not items:
            return {'signals': [], 'notified': False, 'notification_count': 0}
        
        all_signals = []
        notifications = []
        
        now = datetime.now()
        cooldown_threshold = now - timedelta(hours=cooldown_hours)
        
        for item in items:
            if not item.feishu_notify:
                continue
            
            stock_data = self.get_stock_data(item.stock_code)
            if stock_data is None or stock_data.empty:
                continue
            
            item.current_price = float(stock_data.iloc[0].get('close', 0))
            
            triggered = self.check_condition(item, stock_data)
            
            if not triggered:
                continue
            
            if item.last_notify_time:
                try:
                    last_notify = datetime.strptime(item.last_notify_time, '%Y-%m-%d %H:%M:%S')
                    if last_notify > cooldown_threshold:
                        logger.debug(f"{item.stock_code} 在冷却期内，跳过通知")
                        continue
                except ValueError:
                    pass
            
            for t in triggered:
                signal_info = {
                    'stock_code': item.stock_code,
                    'stock_name': item.stock_name,
                    'current_price': item.current_price,
                    'condition_key': t['key'],
                    'condition_name': t['name'],
                    'message': t['message'],
                    'severity': t['severity']
                }
                all_signals.append(signal_info)
                
                severity_emoji = {
                    'buy': '🟢',
                    'sell': '🔴',
                    'warning': '🟡',
                    'info': '🔵'
                }.get(t['severity'], '⚪')
                
                notifications.append({
                    'item_id': item.id,
                    'condition_key': t['key'],
                    'message': f"{severity_emoji} **{item.stock_name}({item.stock_code})**\n"
                              f"条件: {t['name']}\n"
                              f"详情: {t['message']}\n"
                              f"当前价: {item.current_price:.2f}"
                })
        
        notified = False
        if notifications and feishu_webhook:
            from core.notifications.feishu_push import FeishuPush
            feishu = FeishuPush(feishu_webhook)
            
            all_messages = "\n\n---\n\n".join([n['message'] for n in notifications])
            title = f"📈 自选股信号提醒 ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
            
            notified = feishu.push_markdown(all_messages, title)
            
            if notified:
                for n in notifications:
                    self.update_trigger_time(n['item_id'], n['condition_key'], notify=True)
        
        return {
            'signals': all_signals,
            'notified': notified,
            'notification_count': len(notifications)
        }
    
    def batch_add(self, items: List[WatchItem]) -> int:
        """批量添加自选股"""
        count = 0
        for item in items:
            try:
                self.add_item(item)
                count += 1
            except Exception as e:
                logger.error(f"批量添加失败: {item.stock_code} - {e}")
        return count
    
    @staticmethod
    def get_supported_conditions() -> Dict[str, Any]:
        """获取支持的监控条件列表"""
        return SUPPORTED_CONDITIONS
