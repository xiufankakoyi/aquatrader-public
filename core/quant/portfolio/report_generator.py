"""
报告生成模块

功能：
- 生成每日持仓分析报告
- 生成操作建议报告
- 飞书推送
"""

import json
import polars as pl
from datetime import datetime, date
from typing import Optional, List, Dict, Any

from core.notifications.feishu_push import FeishuPush
from core.quant.portfolio.position_manager import PositionManager, Position
from core.quant.portfolio.signal_engine import SignalEngine, Signal


class ReportGenerator:
    """
    报告生成器
    
    生成持仓分析报告并推送到飞书
    """
    
    def __init__(
        self,
        position_manager: Optional[PositionManager] = None,
        signal_engine: Optional[SignalEngine] = None,
        feishu_webhook: Optional[str] = None
    ):
        self.position_manager = position_manager or PositionManager()
        self.signal_engine = signal_engine or SignalEngine()
        self.feishu = FeishuPush(feishu_webhook) if feishu_webhook else None
    
    def generate_daily_report(
        self,
        positions: Optional[List[Position]] = None,
        include_signals: bool = True,
        include_market_overview: bool = True
    ) -> str:
        """
        生成每日报告
        
        Args:
            positions: 持仓列表，默认获取所有活跃持仓
            include_signals: 是否包含信号
            include_market_overview: 是否包含大盘概览
        
        Returns:
            报告文本
        """
        if positions is None:
            positions = self.position_manager.get_all_positions(active_only=True)
        
        stock_codes = [p.stock_code for p in positions]
        latest_prices = self.signal_engine.get_latest_prices(stock_codes)
        analysis = self.position_manager.calculate_analysis(positions, latest_prices)
        
        report_lines = []
        report_lines.append(f"📅 日期：{datetime.now().strftime('%Y-%m-%d')}")
        report_lines.append("")
        
        if include_market_overview:
            market_summary = self._get_market_summary()
            report_lines.append("## 📊 持仓概览")
            report_lines.append("")
            report_lines.append(f"- 总市值：{analysis['summary']['total_market_value']:,.2f}")
            report_lines.append(f"- 总成本：{analysis['summary']['total_cost']:,.2f}")
            report_lines.append(f"- 总盈亏：{analysis['summary']['total_profit_loss']:,.2f} ({analysis['summary']['total_profit_loss_pct']:.2f}%)")
            report_lines.append(f"- 持仓数量：{analysis['summary']['position_count']}")
            report_lines.append("")
            if market_summary:
                report_lines.extend(market_summary)
                report_lines.append("")
        
        report_lines.append("## 📋 持仓明细")
        report_lines.append("")
        for pos in analysis['positions']:
            if pos.get('current_price'):
                pnl_emoji = "🟢" if pos['profit_loss'] >= 0 else "🔴"
                report_lines.append(
                    f"- **{pos['stock_name']}({pos['stock_code']})** "
                    f"成本 {pos['buy_price']:.2f} → 现价 {pos['current_price']:.2f} "
                    f"{pnl_emoji} {pos['profit_loss_pct']:.2f}% "
                    f"仓位 {pos['weight']:.1f}%"
                )
            else:
                report_lines.append(
                    f"- **{pos['stock_name']}({pos['stock_code']})** "
                    f"成本 {pos['buy_price']:.2f} (无最新价格)"
                )
        report_lines.append("")
        
        if include_signals and stock_codes:
            signals = self.signal_engine.generate_signals(stock_codes)
            
            if signals['buy']:
                report_lines.append("## 🟢 买入信号")
                report_lines.append("")
                for sig in signals['buy']:
                    report_lines.append(f"- **{sig.stock_name}({sig.stock_code})** {sig.details}")
                report_lines.append("")
            
            if signals['sell']:
                report_lines.append("## 🔴 卖出信号")
                report_lines.append("")
                for sig in signals['sell']:
                    report_lines.append(f"- **{sig.stock_name}({sig.stock_code})** {sig.details}")
                report_lines.append("")
            
            if signals['watch']:
                report_lines.append("## 👀 观察信号（左侧机会）")
                report_lines.append("")
                for sig in signals['watch']:
                    report_lines.append(f"- **{sig.stock_name}({sig.stock_code})** {sig.details}")
                report_lines.append("")
        
        alerts = self.position_manager.check_stop_loss_take_profit(positions, latest_prices)
        if alerts:
            report_lines.append("## ⚠️ 止损止盈预警")
            report_lines.append("")
            for alert in alerts:
                report_lines.append(f"- {alert['message']}")
            report_lines.append("")
        
        report_lines.append("---")
        report_lines.append("*报告由 AquaTrade 自动生成*")
        
        return "\n".join(report_lines)
    
    def generate_operation_suggestions(
        self,
        positions: Optional[List[Position]] = None,
        watchlist: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        生成操作建议
        
        Args:
            positions: 持仓列表
            watchlist: 观察列表股票代码
        
        Returns:
            操作建议
        """
        if positions is None:
            positions = self.position_manager.get_all_positions(active_only=True)
        
        position_codes = [p.stock_code for p in positions]
        all_codes = position_codes + (watchlist or [])
        
        signals = self.signal_engine.generate_signals(list(set(all_codes)))
        
        suggestions = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'positions_to_sell': [],
            'positions_to_hold': [],
            'stocks_to_buy': [],
            'stocks_to_watch': []
        }
        
        for sig in signals['sell']:
            suggestions['positions_to_sell'].append({
                'stock_code': sig.stock_code,
                'stock_name': sig.stock_name,
                'reason': sig.details,
                'signal_strength': sig.signal_strength
            })
        
        for pos in positions:
            sell_codes = [s['stock_code'] for s in suggestions['positions_to_sell']]
            if pos.stock_code not in sell_codes:
                suggestions['positions_to_hold'].append({
                    'stock_code': pos.stock_code,
                    'stock_name': pos.stock_name,
                    'action': 'hold'
                })
        
        for sig in signals['buy']:
            if sig.stock_code not in position_codes:
                suggestions['stocks_to_buy'].append({
                    'stock_code': sig.stock_code,
                    'stock_name': sig.stock_name,
                    'reason': sig.details,
                    'signal_strength': sig.signal_strength
                })
        
        for sig in signals['watch']:
            if sig.stock_code not in position_codes:
                suggestions['stocks_to_watch'].append({
                    'stock_code': sig.stock_code,
                    'stock_name': sig.stock_name,
                    'reason': sig.details,
                    'signal_strength': sig.signal_strength
                })
        
        return suggestions
    
    def _get_market_summary(self) -> List[str]:
        """获取大盘概览（从 ArcticDB）"""
        lines = []
        
        try:
            from data_svc.unified_data_query import get_stock_daily_latest_polars
            
            df = get_stock_daily_latest_polars()
            if df is None or df.is_empty():
                return lines
            
            up_count = df.filter(pl.col('change_pct') > 0).height
            down_count = df.filter(pl.col('change_pct') < 0).height
            limit_up = df.filter(pl.col('change_pct') > 9.5).height
            limit_down = df.filter(pl.col('change_pct') < -9.5).height
            
            lines.append(f"- 涨跌家数：{up_count} 涨 / {down_count} 跌")
            lines.append(f"- 涨跌停：{limit_up} 涨停 / {limit_down} 跌停")
        except Exception as e:
            print(f"[ReportGenerator] Error getting market summary: {e}")
        
        return lines
    
    def push_to_feishu(self, report: str, title: str = "每日持仓分析报告") -> bool:
        """
        推送报告到飞书
        
        Args:
            report: 报告内容
            title: 报告标题
        
        Returns:
            是否推送成功
        """
        if not self.feishu:
            return False
        
        markdown_content = self.feishu.txt_to_markdown(report)
        if markdown_content:
            return self.feishu.push_markdown(markdown_content, title)
        else:
            return self.feishu.push_text(report, title)
    
    def generate_and_push(self, webhook_url: Optional[str] = None) -> bool:
        """
        生成报告并推送到飞书
        
        Args:
            webhook_url: 飞书 webhook URL
        
        Returns:
            是否成功
        """
        if webhook_url:
            self.feishu = FeishuPush(webhook_url)
        
        report = self.generate_daily_report()
        return self.push_to_feishu(report)
