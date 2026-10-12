"""
信号生成引擎

功能：
- 根据规则生成买入/卖出/观察信号
- 支持右侧因子（技术指标）和左侧因子（估值指标）
- 信号强度评估

存储：使用 ArcticDB 替代 SQLite
"""

import json
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from config.config import Config
from core.quant.strategies.utils.factor_compute import FactorCompute


@dataclass
class Signal:
    """信号数据结构"""
    stock_code: str
    stock_name: str
    signal_date: str
    signal_type: str
    signal_name: str
    signal_strength: float = 0.0
    price_at_signal: float = 0.0
    details: str = ""


class SignalEngine:
    """
    信号生成引擎
    
    根据配置的规则生成交易信号，使用 ArcticDB 数据源
    """
    
    DEFAULT_RULES = {
        "buy_signals": {
            "right_side": {
                "ma20_breakout_with_bias": {
                    "enabled": True,
                    "description": "站上20日线 + 乖离率在0%~3%",
                    "bias_min": 0,
                    "bias_max": 3
                },
                "macd_golden_cross": {
                    "enabled": True,
                    "description": "MACD金叉"
                },
                "cup_handle": {
                    "enabled": True,
                    "description": "杯柄形态突破"
                }
            }
        },
        "sell_signals": {
            "right_side": {
                "ma5_breakdown": {
                    "enabled": True,
                    "description": "跌破5日线"
                },
                "macd_death_cross": {
                    "enabled": True,
                    "description": "MACD死叉"
                },
                "ma_trend_down": {
                    "enabled": True,
                    "description": "60日向下且现价<5日线<10日线"
                }
            }
        },
        "watch_signals": {
            "left_side": {
                "high_dividend": {
                    "enabled": True,
                    "description": "股息率 > 4%",
                    "threshold": 4.0
                },
                "low_pe_percentile": {
                    "enabled": True,
                    "description": "PE分位 < 20%",
                    "threshold": 20.0
                },
                "oversold_bias": {
                    "enabled": True,
                    "description": "乖离率 < -5%（超跌）",
                    "threshold": -5.0
                }
            }
        }
    }
    
    def __init__(self, rules_path: Optional[str] = None):
        self.rules_path = rules_path or str(Path(Config.DATA_DIR) / "signal_rules.json")
        self.rules = self._load_rules()
        self._data_adapter = None
    
    def _get_data_adapter(self):
        """Lazy load data adapter"""
        if self._data_adapter is None:
            from data_svc.unified_data_query import UnifiedDataQuery
            self._data_adapter = UnifiedDataQuery()
        return self._data_adapter
    
    def _load_rules(self) -> Dict[str, Any]:
        """加载信号规则"""
        try:
            if Path(self.rules_path).exists():
                with open(self.rules_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
        return self.DEFAULT_RULES
    
    def save_rules(self, rules: Dict[str, Any]):
        """保存信号规则"""
        Path(self.rules_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.rules_path, 'w', encoding='utf-8') as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
        self.rules = rules
    
    def get_stock_data(
        self,
        stock_code: str,
        days: int = 100,
        end_date: Optional[str] = None,
    ) -> Dict[str, np.ndarray]:
        """
        获取股票历史数据
        
        Args:
            stock_code: 股票代码
            days: 回溯天数
        
        Returns:
            历史数据字典
        """
        adapter = self._get_data_adapter()
        if adapter is None:
            return {}
        
        end_date = end_date or datetime.now().strftime("%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_date = (end_dt - timedelta(days=days * 2)).strftime("%Y-%m-%d")

        try:
            from infrastructure.data_providers.base import normalize_symbol

            symbol = normalize_symbol(stock_code)["symbol"]
            df = adapter.get_stock_history(symbol, start_date, end_date)
            if df is None or df.empty:
                return {}

            if "trade_date" in df.columns:
                df = df.sort_values("trade_date").tail(days)
                trade_dates = (
                    pd.to_datetime(df["trade_date"], errors="coerce")
                    .dt.strftime("%Y%m%d")
                    .fillna("")
                    .tolist()
                )
            else:
                df = df.sort_index().tail(days)
                trade_dates = [
                    d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d)[:10].replace("-", "")
                    for d in df.index
                ]
            
            data = {
                'trade_date': np.array(trade_dates),
                'open': df['open'].values.astype(np.float32) if 'open' in df.columns else np.array([]),
                'high': df['high'].values.astype(np.float32) if 'high' in df.columns else np.array([]),
                'low': df['low'].values.astype(np.float32) if 'low' in df.columns else np.array([]),
                'close': df['close'].values.astype(np.float32) if 'close' in df.columns else np.array([]),
                'volume': df['volume'].values.astype(np.float32) if 'volume' in df.columns else (
                    df['vol'].values.astype(np.float32) if 'vol' in df.columns else np.array([])
                ),
                'amount': df['amount'].values.astype(np.float32) if 'amount' in df.columns else np.array([]),
                'pe_ttm': df['pe_ttm'].values.astype(np.float32) if 'pe_ttm' in df.columns else np.array([]),
                'pb': df['pb'].values.astype(np.float32) if 'pb' in df.columns else np.array([]),
                'dividend_yield': df['dv_ratio'].values.astype(np.float32) if 'dv_ratio' in df.columns else np.array([]),
                'turnover_rate': df['turnover_rate'].values.astype(np.float32) if 'turnover_rate' in df.columns else np.array([]),
                'total_mv': df['total_mv'].values.astype(np.float32) if 'total_mv' in df.columns else np.array([]),
            }
            
            return data
        except Exception as e:
            print(f"[SignalEngine] Error getting stock data for {stock_code}: {e}")
            return {}
    
    def get_stock_name(self, stock_code: str) -> str:
        """获取股票名称"""
        names = self.get_stock_names([stock_code])
        return names.get(stock_code, stock_code)
    
    def get_stock_names(self, stock_codes: List[str]) -> Dict[str, str]:
        """批量获取股票名称"""
        if not stock_codes:
            return {}
        
        adapter = self._get_data_adapter()
        if adapter is None:
            return {c: c for c in stock_codes}
        
        try:
            df = adapter.get_stock_basic()
            if df is None or df.empty:
                return {c: c for c in stock_codes}
            
            result = {}
            for code in stock_codes:
                norm_code = code.split('.')[0]
                match = None
                if 'code' in df.columns:
                    match = df[df['code'] == norm_code]
                elif 'stock_code' in df.columns:
                    match = df[df['stock_code'] == norm_code]
                elif 'ts_code' in df.columns:
                    match = df[df['ts_code'].str.startswith(norm_code + '.')]
                
                if match is not None and not match.empty:
                    name = match.iloc[0].get('name', match.iloc[0].get('stock_name', code))
                    result[code] = name if name else code
                else:
                    result[code] = code
            return result
        except Exception as e:
            print(f"[SignalEngine] Error getting stock names: {e}")
            return {c: c for c in stock_codes}
    
    def generate_signals(self, stock_codes: List[str], signal_date: Optional[str] = None) -> Dict[str, List[Signal]]:
        """
        生成信号
        
        Args:
            stock_codes: 股票代码列表
            signal_date: 信号日期，默认为最新交易日
        
        Returns:
            信号分类 {'buy': [...], 'sell': [...], 'watch': [...]}
        """
        signals = {'buy': [], 'sell': [], 'watch': []}
        
        if not stock_codes:
            return signals
        
        stock_names = self.get_stock_names(stock_codes)
        
        for code in stock_codes:
            data = self.get_stock_data(code, end_date=signal_date)
            if not data or len(data.get('close', [])) < 30:
                continue
            
            stock_name = stock_names.get(code, code)
            latest_price = float(data['close'][-1])
            latest_date = str(data['trade_date'][-1])
            
            buy_signals = self._check_buy_signals(code, stock_name, data, latest_price, latest_date)
            sell_signals = self._check_sell_signals(code, stock_name, data, latest_price, latest_date)
            watch_signals = self._check_watch_signals(code, stock_name, data, latest_price, latest_date)
            
            signals['buy'].extend(buy_signals)
            signals['sell'].extend(sell_signals)
            signals['watch'].extend(watch_signals)
        
        return signals
    
    def _check_buy_signals(self, code: str, name: str, data: Dict, price: float, date_str: str) -> List[Signal]:
        """检查买入信号"""
        signals = []
        rules = self.rules.get('buy_signals', {}).get('right_side', {})
        
        close = data['close']
        
        bias_20 = FactorCompute.calc_bias(close, 20)
        macd_result = FactorCompute.calc_macd(close)
        ma5_breakout = FactorCompute.calc_ma_breakout(close, 5)
        ma20_breakout = FactorCompute.calc_ma_breakout(close, 20)
        
        latest_bias = float(bias_20[-1]) if not np.isnan(bias_20[-1]) else None
        is_ma20_breakout = bool(ma20_breakout[-1])
        is_golden_cross = bool(macd_result['golden_cross'][-1])
        
        rule = rules.get('ma20_breakout_with_bias', {})
        if rule.get('enabled', False):
            bias_min = rule.get('bias_min', 0)
            bias_max = rule.get('bias_max', 3)
            
            if is_ma20_breakout and latest_bias is not None and bias_min <= latest_bias <= bias_max:
                signals.append(Signal(
                    stock_code=code,
                    stock_name=name,
                    signal_date=date_str,
                    signal_type='buy',
                    signal_name='ma20_breakout_with_bias',
                    signal_strength=0.8,
                    price_at_signal=price,
                    details=f"站上20日线，乖离率 {latest_bias:.2f}%"
                ))
        
        rule = rules.get('macd_golden_cross', {})
        if rule.get('enabled', False) and is_golden_cross:
            signals.append(Signal(
                stock_code=code,
                stock_name=name,
                signal_date=date_str,
                signal_type='buy',
                signal_name='macd_golden_cross',
                signal_strength=0.7,
                price_at_signal=price,
                details="MACD金叉"
            ))
        
        rule = rules.get('cup_handle', {})
        if rule.get('enabled', False) and len(close) >= 60:
            cup_signal = self._detect_cup_handle(code, name, close, price, date_str)
            if cup_signal:
                signals.append(cup_signal)
        
        return signals
    
    def _detect_cup_handle(self, code: str, name: str, close: np.ndarray, price: float, date_str: str) -> Optional[Signal]:
        """
        检测杯柄形态
        
        杯柄形态特征：
        1. 先有一波上涨（杯子的左边）
        2. 然后回调形成圆弧底（杯子的底部）
        3. 再次上涨到前期高点附近（杯子的右边）
        4. 小幅回调形成柄部
        5. 突破柄部上沿
        """
        if len(close) < 60:
            return None
        
        recent_close = close[-60:]
        high_60 = np.max(recent_close)
        low_60 = np.min(recent_close)
        current_price = recent_close[-1]
        
        left_high = np.max(recent_close[:20])
        left_low = np.min(recent_close[:20])
        if (left_high - left_low) / left_low < 0.10:
            return None
        
        mid_low = np.min(recent_close[20:40])
        if mid_low > left_low * 1.05:
            return None
        
        right_high = np.max(recent_close[40:55])
        if right_high < left_high * 0.95:
            return None
        
        handle_high = np.max(recent_close[-10:-2])
        handle_low = np.min(recent_close[-10:-2])
        handle_depth = (handle_high - handle_low) / handle_high
        
        if handle_depth < 0.03 or handle_depth > 0.15:
            return None
        
        if current_price < handle_high * 1.01:
            return None
        
        return Signal(
            stock_code=code,
            stock_name=name,
            signal_date=date_str,
            signal_type='buy',
            signal_name='cup_handle',
            signal_strength=0.85,
            price_at_signal=price,
            details=f"杯柄形态突破，柄部回调{handle_depth*100:.1f}%"
        )
    
    def _check_sell_signals(self, code: str, name: str, data: Dict, price: float, date_str: str) -> List[Signal]:
        """检查卖出信号"""
        signals = []
        rules = self.rules.get('sell_signals', {}).get('right_side', {})
        
        close = data['close']
        
        ma5_breakout = FactorCompute.calc_ma_breakout(close, 5)
        macd_result = FactorCompute.calc_macd(close)
        
        is_ma5_breakdown = not bool(ma5_breakout[-1])
        is_death_cross = bool(macd_result['death_cross'][-1])
        
        rule = rules.get('ma5_breakdown', {})
        if rule.get('enabled', False) and is_ma5_breakdown:
            signals.append(Signal(
                stock_code=code,
                stock_name=name,
                signal_date=date_str,
                signal_type='sell',
                signal_name='ma5_breakdown',
                signal_strength=0.6,
                price_at_signal=price,
                details="跌破5日线"
            ))
        
        rule = rules.get('macd_death_cross', {})
        if rule.get('enabled', False) and is_death_cross:
            signals.append(Signal(
                stock_code=code,
                stock_name=name,
                signal_date=date_str,
                signal_type='sell',
                signal_name='macd_death_cross',
                signal_strength=0.7,
                price_at_signal=price,
                details="MACD死叉"
            ))
        
        rule = rules.get('ma_trend_down', {})
        if rule.get('enabled', False) and len(close) >= 60:
            ma5 = np.mean(close[-5:])
            ma10 = np.mean(close[-10:])
            ma60 = np.mean(close[-60:])
            ma60_prev = np.mean(close[-61:-1])
            
            is_ma60_down = ma60 < ma60_prev
            is_price_below_ma = price < ma5 < ma10
            
            if is_ma60_down and is_price_below_ma:
                signals.append(Signal(
                    stock_code=code,
                    stock_name=name,
                    signal_date=date_str,
                    signal_type='sell',
                    signal_name='ma_trend_down',
                    signal_strength=0.8,
                    price_at_signal=price,
                    details=f"60日线向下，现价{price:.2f}<5日线{ma5:.2f}<10日线{ma10:.2f}"
                ))
        
        return signals
    
    def _check_watch_signals(self, code: str, name: str, data: Dict, price: float, date_str: str) -> List[Signal]:
        """检查观察信号（左侧因子）"""
        signals = []
        rules = self.rules.get('watch_signals', {}).get('left_side', {})
        
        pe_ttm = data.get('pe_ttm', np.array([]))
        pb = data.get('pb', np.array([]))
        dividend_yield = data.get('dividend_yield', np.array([]))
        
        if len(pe_ttm) > 0:
            latest_pe = float(pe_ttm[-1]) if not np.isnan(pe_ttm[-1]) else None
            
            if latest_pe is not None:
                rule = rules.get('low_pe_percentile', {})
                if rule.get('enabled', False):
                    threshold = rule.get('threshold', 20.0)
                    pe_percentile = self._calc_percentile(pe_ttm, latest_pe)
                    
                    if pe_percentile is not None and pe_percentile < threshold:
                        signals.append(Signal(
                            stock_code=code,
                            stock_name=name,
                            signal_date=date_str,
                            signal_type='watch',
                            signal_name='low_pe_percentile',
                            signal_strength=0.6,
                            price_at_signal=price,
                            details=f"PE分位 {pe_percentile:.1f}% < {threshold}%"
                        ))
        
        if len(dividend_yield) > 0:
            latest_div = float(dividend_yield[-1]) if not np.isnan(dividend_yield[-1]) else None
            
            if latest_div is not None:
                rule = rules.get('high_dividend', {})
                if rule.get('enabled', False):
                    threshold = rule.get('threshold', 4.0)
                    
                    if latest_div > threshold:
                        signals.append(Signal(
                            stock_code=code,
                            stock_name=name,
                            signal_date=date_str,
                            signal_type='watch',
                            signal_name='high_dividend',
                            signal_strength=0.5,
                            price_at_signal=price,
                            details=f"股息率 {latest_div:.2f}% > {threshold}%"
                        ))
        
        close = data['close']
        bias_20 = FactorCompute.calc_bias(close, 20)
        latest_bias = float(bias_20[-1]) if not np.isnan(bias_20[-1]) else None
        
        if latest_bias is not None:
            rule = rules.get('oversold_bias', {})
            if rule.get('enabled', False):
                threshold = rule.get('threshold', -5.0)
                
                if latest_bias < threshold:
                    signals.append(Signal(
                        stock_code=code,
                        stock_name=name,
                        signal_date=date_str,
                        signal_type='watch',
                        signal_name='oversold_bias',
                        signal_strength=0.7,
                        price_at_signal=price,
                        details=f"乖离率 {latest_bias:.2f}% < {threshold}%（超跌）"
                    ))
        
        return signals
    
    def _calc_percentile(self, data: np.ndarray, value: float) -> Optional[float]:
        """计算分位数"""
        valid_data = data[~np.isnan(data)]
        if len(valid_data) == 0:
            return None
        
        return float(np.sum(valid_data < value) / len(valid_data) * 100)
    
    def get_latest_prices(self, stock_codes: List[str]) -> Dict[str, float]:
        """
        获取最新价格（批量）
        
        Args:
            stock_codes: 股票代码列表
        
        Returns:
            {stock_code: latest_price}
        """
        if not stock_codes:
            return {}
        
        adapter = self._get_data_adapter()
        if adapter is None:
            return {}
        
        try:
            return adapter.get_latest_prices(stock_codes)
        except Exception as e:
            print(f"[SignalEngine] Error getting latest prices: {e}")
            return {}
