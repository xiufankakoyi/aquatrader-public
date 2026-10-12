"""
AI 策略生成器的 Prompt 模板

包含系统提示词、硬性约束、代码模板等，用于将用户的自然语言描述转换为符合规范的策略代码。
"""

# 系统提示词
SYSTEM_PROMPT = """你是一个专业的量化策略代码生成器。你的任务是将用户的自然语言描述转化为符合规范的 Python 策略代码。

你的输出必须是完整的、可运行的 Python 代码，严格遵循以下规范。"""

# 硬性约束
HARD_CONSTRAINTS = """
【硬性约束 - 必须严格遵守】

1. 必须继承 AIStrategyBase
   - 导入: from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig
   - 类定义: class YourStrategy(AIStrategyBase):

2. 必须实现 get_required_indicators() 方法
   - 声明策略用到的所有技术指标（如 RSI、MA、EMA、MACD 等）
   - 参数必须从 self.config.params 获取，不能硬编码
   - 返回格式: [{"name": "RSI", "period": 14}, {"name": "MA", "period": 20}]

3. 必须实现 _generate_signals_impl() 方法
   - 这是核心逻辑，只做 if/else 判断，不计算指标
   - 输入的 stock_pool 已经包含了 get_required_indicators() 声明的所有指标列
   - 禁止使用 talib、pandas.rolling() 等计算指标的函数
   - 只能使用 stock_pool 中已有的列（如 row["RSI"], row["MA"]）

4. 所有阈值和参数必须提取到 AIStrategyConfig 中
   - 在 __init__ 中创建 config = AIStrategyConfig(params={...})
   - 在代码中使用 self.config.params.get("param_name", default_value)
   - 禁止硬编码数字（如 if row["RSI"] < 70，应该改为 if row["RSI"] < self.config.params.get("rsi_sell_threshold", 70)）

5. 状态管理
   - 使用 self.holding_state 记录持仓信息（如持仓天数）
   - 格式: {stock_code: {"holding_days": int, "buy_price": float}}
   - 在 _generate_signals_impl() 中可以使用 self.holding_state[code]["holding_days"] 等

6. 返回值格式
   - 返回 Dict[str, str]: {stock_code: "buy"/"sell"/"hold"}
   - 或返回 Dict[str, dict]: {stock_code: {"action": "buy", "weight": 0.2}}
"""

# 代码模板
CODE_TEMPLATE = """
【代码模板 - 参考结构】

from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig

class {StrategyName}(AIStrategyBase):
    strategy_name = "{StrategyName}"
    
    def __init__(self, config: AIStrategyConfig = None):
        # 如果未提供 config，使用默认参数
        if config is None:
            config = AIStrategyConfig(params={{
                # 在这里定义所有参数，不能硬编码
                "ma_period": 20,
                "rsi_period": 14,
                "rsi_buy_threshold": 30,
                "rsi_sell_threshold": 70,
                "max_holding_days": 5,
                # ... 其他参数
            }})
        super().__init__(config)
    
    def get_required_indicators(self):
        \"\"\"声明需要的技术指标\"\"\"
        return [
            {{"name": "MA", "period": self.config.params.get("ma_period", 20), "column": "close"}},
            {{"name": "RSI", "period": self.config.params.get("rsi_period", 14), "column": "close"}},
            # ... 其他指标
        ]
    
    def _generate_signals_impl(self, current_date: str, stock_pool):
        \"\"\"核心逻辑：只做判断，不计算指标\"\"\"
        signals = {{}}
        
        # 从 config 获取参数
        ma_period = self.config.params.get("ma_period", 20)
        rsi_sell_threshold = self.config.params.get("rsi_sell_threshold", 70)
        max_holding_days = self.config.params.get("max_holding_days", 5)
        
        for _, row in stock_pool.iterrows():
            code = row["stock_code"]
            
            # 卖出逻辑：检查持仓状态
            if code in self.current_portfolio:
                # 检查持仓天数
                holding_days = self.holding_state.get(code, {{}}).get("holding_days", 0)
                if holding_days >= max_holding_days:
                    signals[code] = "sell"
                    continue
                
                # 检查 RSI 卖出条件
                if row["RSI"] > rsi_sell_threshold:
                    signals[code] = "sell"
                    continue
            
            # 买入逻辑：使用已有的指标列
            # 注意：不能计算指标，只能使用 stock_pool 中已有的列
            # 指标列名可能带周期后缀，如 "MA_20", "MA_20_close" 等
            # 需要尝试多种可能的列名格式
            ma_column_variants = [
                f"MA_{{ma_period}}",
                f"MA_{{ma_period}}_close",
                "MA",
                f"ma_{{ma_period}}",
            ]
            ma_value = None
            for col in ma_column_variants:
                if col in row.index:
                    ma_value = row[col]
                    break
            
            if ma_value is not None and row["close"] > ma_value:
                signals[code] = "buy"
            else:
                signals[code] = "hold"
        
        return signals
"""

# 指标映射表（帮助 AI 理解用户描述中的指标）
INDICATOR_MAPPING = """
【支持的指标类型】

1. MA / 均线 / 移动平均线
   - 格式: {"name": "MA", "period": 20, "column": "close"}
   - 列名: 可能是 "MA", "MA_20", "ma_20" 等

2. EMA / 指数均线
   - 格式: {"name": "EMA", "period": 12, "column": "close"}

3. RSI / 相对强弱指标
   - 格式: {"name": "RSI", "period": 14, "column": "close"}
   - 列名: 可能是 "RSI", "RSI_14", "rsi_14" 等

4. MACD
   - 格式: {"name": "MACD", "fast": 12, "slow": 26, "signal": 9, "column": "close"}
   - 列名: 可能是 "MACD", "MACD_DIF", "MACD_DEA", "MACD_MACD" 等

5. BOLL / 布林带
   - 格式: {"name": "BOLL", "period": 20, "column": "close"}
   - 列名: 可能是 "BOLL_UPPER", "BOLL_MIDDLE", "BOLL_LOWER" 等

6. 成交量相关
   - 格式: {"name": "VOLUME_MA", "period": 5, "column": "volume"}
"""

# 常见策略模式
STRATEGY_PATTERNS = """
【常见策略模式 - 参考实现】

1. 突破策略
   - "股价突破N日均线买入"
   - 实现: 
     ma_value = row.get(f"MA_{{period}}") or row.get("MA")
     if ma_value and row["close"] > ma_value: signals[code] = "buy"

2. RSI 超买超卖
   - "RSI小于30买入，大于70卖出"
   - 实现: 
     if row["RSI"] < rsi_buy_threshold: signals[code] = "buy"
     elif row["RSI"] > rsi_sell_threshold: signals[code] = "sell"

3. 持仓天数限制
   - "最多持仓N天"
   - 实现:
     if code in self.current_portfolio:
         holding_days = self.holding_state.get(code, {}).get("holding_days", 0)
         if holding_days >= max_holding_days: signals[code] = "sell"

4. 止损止盈
   - "亏损超过10%卖出" / "盈利超过20%卖出"
   - 实现:
     if code in self.current_portfolio:
         buy_price = self.holding_state[code]["buy_price"]
         profit_pct = (row["close"] - buy_price) / buy_price * 100
         if profit_pct < -10: signals[code] = "sell"  # 止损
         elif profit_pct > 20: signals[code] = "sell"  # 止盈
"""

# 完整 Prompt 构建函数
def build_prompt(user_description: str) -> str:
    """
    构建完整的 Prompt，用于生成策略代码
    
    参数：
        user_description: 用户的自然语言描述
    
    返回：
        str: 完整的 Prompt 文本
    """
    prompt = f"""{SYSTEM_PROMPT}

{HARD_CONSTRAINTS}

{CODE_TEMPLATE}

{INDICATOR_MAPPING}

{STRATEGY_PATTERNS}

【用户需求】
{user_description}

【任务】
请根据用户需求，生成完整的 Python 策略代码。确保：
1. 所有参数都提取到 AIStrategyConfig 中
2. 所有指标都在 get_required_indicators() 中声明
3. 在 _generate_signals_impl() 中只做逻辑判断，不计算指标
4. 代码可以直接运行，无需修改

【输出格式】
只输出 Python 代码，不要包含任何解释文字、markdown 标记或其他内容。
代码应该可以直接保存为 .py 文件并运行。
"""
    return prompt


# 示例 Prompt（用于测试）
EXAMPLE_PROMPT = build_prompt(
    "写一个策略：股价突破20日均线买入，RSI大于70卖出，最多持仓5天。"
)

