"""
策略分析服务

负责将回测引擎返回的结果字典清洗成 LLM 能读懂的 Prompt，并生成策略分析报告。
"""

import json
import logging
from typing import Dict, List, Any, Optional
from core.utils.llm_client import AquaLLM


class AnalysisService:
    """
    策略分析服务
    
    使用示例:
        service = AnalysisService()
        review = service.generate_review(
            strategy_name="双均线策略",
            backtest_result=result_dict,
            strategy_code="..."
        )
    """
    
    def __init__(self):
        """初始化分析服务"""
        self.llm = AquaLLM()
        self.logger = logging.getLogger("AnalysisService")
    
    def generate_review(
        self, 
        strategy_name: str, 
        backtest_result: Dict[str, Any], 
        strategy_code: str = ""
    ) -> str:
        """
        生成策略分析报告 (非流式)
        """
        try:
            summary_data = self._prepare_data_for_llm(backtest_result)
            user_prompt = self._build_user_prompt(strategy_name, summary_data, strategy_code)
            system_prompt = self._get_system_prompt()
            
            self.logger.info(f"正在让 AI 分析策略 (同步): {strategy_name} ...")
            return self.llm.generate_report(user_prompt, system_prompt)
            
        except Exception as e:
            self.logger.error(f"生成策略分析报告失败: {e}", exc_info=True)
            return self._get_fallback_report(backtest_result, str(e))

    def generate_review_stream(
        self, 
        strategy_name: str, 
        backtest_result: Dict[str, Any], 
        strategy_code: str = ""
    ):
        """
        生成策略分析报告 (流式)
        """
        try:
            summary_data = self._prepare_data_for_llm(backtest_result)
            user_prompt = self._build_user_prompt(strategy_name, summary_data, strategy_code)
            system_prompt = self._get_system_prompt()
            
            self.logger.info(f"正在让 AI 分析策略 (流式): {strategy_name} ...")
            return self.llm.generate_report_stream(user_prompt, system_prompt)
            
        except Exception as e:
            self.logger.error(f"生成策略分析流失败: {e}", exc_info=True)
            yield f"\n[ERROR] 生成分析流失败: {str(e)}"

    def _get_system_prompt(self) -> str:
        return """你是一位拥有20年经验的量化基金风控总监。
你的任务是阅读用户的【策略代码】和【回测数据摘要】，生成一份犀利的【投资分析报告】。

要求：
1. 风格：客观、严谨、直击痛点。不要说客套话。
2. 分析维度：
   - 收益风险比：夏普比率是否合理？回撤是否过大？
   - 稳定性：是否存在某个月赚了绝大部分钱（幸存者偏差）？
   - 改进建议：给出 2-3 条具体的优化方向（如止损、仓位、过滤条件）。
3. 格式：使用 Markdown，包含【综合评分(0-10分)】、【核心风险】、【改进建议】。
"""

    def _build_user_prompt(self, strategy_name: str, summary_data: Dict, strategy_code: str) -> str:
        return f"""请分析以下策略表现：

【策略名称】: {strategy_name}

【核心数据摘要】:
{json.dumps(summary_data, indent=2, ensure_ascii=False)}

【策略逻辑代码】:
```python
{strategy_code[:2000] if strategy_code else "# 策略代码未提供"}
```
"""

    def _get_fallback_report(self, backtest_result: Dict, error_msg: str) -> str:
        return f"""# 策略分析报告 (自动降级)

> [!WARNING]
> AI 分析服务暂时不可用（可能由于本地模型显存不足或连接断开）。
> 
> **系统捕获的错误信息**：
> `{error_msg}`

## 建议操作
1. **减少回测数据量**：尝试缩短回测时间范围。
2. **检查本地模型**：确认 LM Studio / Ollama 是否正常运行。
3. **切换模型**：尝试使用更小的模型参数（如 Qwen-1.8B）。

## 基础指标概览
- **总收益**: {backtest_result.get('metrics', {}).get('totalReturn', 'N/A')}%
- **夏普比率**: {backtest_result.get('metrics', {}).get('sharpeRatio', 'N/A')}
- **最大回撤**: {backtest_result.get('metrics', {}).get('maxDrawdown', 'N/A')}%
"""
    
                # 计算交易统计
    def _prepare_data_for_llm(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        将庞大的回测结果精简为特征数据
        
        参数:
            result: 回测引擎返回的完整结果字典
        
        返回:
            dict: 精简后的数据摘要
        """
        # 从 result 中提取 metrics 字段
        metrics = result.get('metrics', {}) or result.get('performanceMetrics', {})
        trades = result.get('trades', []) or result.get('tradeRecords', [])
        
        # 1. 核心指标 (保留2位小数)
        summary: Dict[str, Any] = {
            "metrics": {
                "total_return": round(float(metrics.get('totalReturn') or metrics.get('total_return', 0)), 2),
                "annualized_return": round(float(metrics.get('annualizedReturn') or metrics.get('annualized_return', 0)), 2),
                "max_drawdown": round(float(metrics.get('maxDrawdown') or metrics.get('max_drawdown', 0)), 2),
                "sharpe_ratio": round(float(metrics.get('sharpeRatio') or metrics.get('sharpe_ratio', 0)), 2),
                "win_rate": round(float(metrics.get('winRate') or metrics.get('win_rate', 0)), 2),
                "profit_factor": round(float(metrics.get('profitFactor') or metrics.get('profit_factor', 0)), 2),
                "trade_count": len(trades) if trades else 0,
            }
        }
        
        # 2. 月度收益趋势 (保留最近6个月，减少长度)
        monthly_returns = result.get('monthlyReturns', [])
        if monthly_returns:
            recent_months = monthly_returns[-6:] if len(monthly_returns) > 6 else monthly_returns
            # 精简月度数据格式
            summary['recent_months'] = [
                {
                    "date": m.get('date', '') or m.get('month', ''),
                    "return": round(float(m.get('return', 0)), 2)
                }
                for m in recent_months
            ]
        
        # 3. 极端交易 (减少到各2笔，且只保留关键指标)
        if trades:
            trades_with_pnl = []
            for trade in trades:
                pnl = float(trade.get('profit_loss') or trade.get('pnl') or 0)
                
                # 深度精简：只传递该笔交易的核心上下文
                simple_trade = {
                    'code': trade.get('symbol') or trade.get('symbol_code', ''),
                    'date': trade.get('entry_date') or trade.get('entryDate', ''),
                    'pnl': round(pnl, 2),
                    'pct': round(float(trade.get('return_pct') or trade.get('returnPct', 0)), 2)
                }
                
                # 只有在存在指标时才添加，且只保留关键指标并降低精度
                if 'indicators' in trade and trade['indicators']:
                    inds = trade['indicators']
                    simple_inds = {}
                    if 'volume_ratio' in inds:
                        simple_inds['vr'] = round(float(inds['volume_ratio']), 2)
                    if 'gain_3d' in inds:
                        simple_inds['gain3d'] = round(float(inds['gain_3d']), 2)
                    if 'turnover_rate' in inds:
                        simple_inds['turnover'] = round(float(inds['turnover_rate']), 2)
                    if simple_inds:
                        simple_trade['inds'] = simple_inds
                        
                trades_with_pnl.append(simple_trade)
            
            # 排序并提取
            if trades_with_pnl:
                sorted_trades = sorted(trades_with_pnl, key=lambda x: x['pnl'])
                summary['worst_trades'] = sorted_trades[:3]  # 用户要求：取前3最差
                summary['best_trades'] = sorted_trades[-3:]  # 用户要求：取前3最好
        
        # 4. 回测期间信息
        strategy_info = result.get('strategyInfo', {})
        if strategy_info:
            summary['period'] = strategy_info.get('period', '')
        
        # 5. 权益曲线摘要（只保留关键点）
        equity_curve = result.get('equityCurve', [])
        if equity_curve and len(equity_curve) > 0:
            # 只保留开始、结束、最高点、最低点
            equity_values = [point.get('equity', 0) for point in equity_curve if point.get('equity')]
            if equity_values:
                summary['equity_summary'] = {
                    "initial": equity_values[0],
                    "final": equity_values[-1],
                    "peak": max(equity_values),
                    "trough": min(equity_values),
                    "data_points": len(equity_values)
                }
        
        return summary
    
    def generate_quick_review(
        self, 
        strategy_name: str, 
        metrics: Dict[str, Any]
    ) -> str:
        """
        生成快速分析报告（仅基于指标，不需要完整回测结果）
        
        参数:
            strategy_name: 策略名称
            metrics: 指标字典
        
        返回:
            str: 简要分析报告
        """
        try:
            system_prompt = """你是一位量化策略分析师。请根据提供的策略指标，给出简要的分析评价（100字以内）。"""
            
            user_prompt = f"""策略名称: {strategy_name}

指标数据:
{json.dumps(metrics, indent=2, ensure_ascii=False)}

请给出简要评价。"""
            
            review = self.llm.generate_code(user_prompt, system_prompt)
            return review
            
        except Exception as e:
            self.logger.error(f"生成快速分析失败: {e}", exc_info=True)
            return f"分析失败: {str(e)}"


# 测试入口
if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 示例测试数据
    test_result = {
        "metrics": {
            "totalReturn": 15.5,
            "annualizedReturn": 12.3,
            "maxDrawdown": -8.2,
            "sharpeRatio": 1.5,
            "winRate": 55.0,
            "profitFactor": 1.8
        },
        "trades": [
            {"symbol": "000001", "pnl": 1000, "entry_date": "2024-01-01", "exit_date": "2024-01-05"},
            {"symbol": "000002", "pnl": -500, "entry_date": "2024-01-10", "exit_date": "2024-01-15"},
        ],
        "monthlyReturns": [
            {"month": "2024-01", "return": 2.5},
            {"month": "2024-02", "return": -1.2},
        ]
    }
    
    service = AnalysisService()
    
    try:
        review = service.generate_review(
            strategy_name="测试策略",
            backtest_result=test_result,
            strategy_code="# 测试代码\nprint('hello')"
        )
        print("\n生成的报告：")
        print(review)
    except Exception as e:
        print(f"测试失败: {e}")
        import traceback
        traceback.print_exc()




