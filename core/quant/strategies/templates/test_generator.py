"""
测试 AI 策略生成器

演示完整的生成流程，包括：
1. 生成策略代码
2. 验证代码
3. 动态加载和执行
"""

import sys
import importlib.util
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from core.quant.strategies.templates import AIStrategyGenerator, build_prompt


def test_prompt_generation():
    """测试 Prompt 生成"""
    print("=" * 80)
    print("测试 1: Prompt 生成")
    print("=" * 80)
    
    user_description = "写一个策略：股价突破20日均线买入，RSI大于70卖出，最多持仓5天。"
    prompt = build_prompt(user_description)
    
    print(f"用户描述: {user_description}")
    print(f"\n生成的 Prompt 长度: {len(prompt)} 字符")
    print(f"Prompt 预览（前500字符）:\n{prompt[:500]}...")
    print("\n[OK] Prompt 生成成功")


def test_code_validation():
    """测试代码验证功能"""
    print("\n" + "=" * 80)
    print("测试 2: 代码验证")
    print("=" * 80)
    
    generator = AIStrategyGenerator()
    
    # 测试代码1: 正确的代码
    correct_code = '''
from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig

class TestStrategy(AIStrategyBase):
    strategy_name = "测试策略"
    
    def __init__(self, config: AIStrategyConfig = None):
        if config is None:
            config = AIStrategyConfig(params={"ma_period": 20})
        super().__init__(config)
    
    def get_required_indicators(self):
        return [{"name": "MA", "period": 20, "column": "close"}]
    
    def _generate_signals_impl(self, current_date: str, stock_pool):
        signals = {}
        for _, row in stock_pool.iterrows():
            signals[row["stock_code"]] = "hold"
        return signals
'''
    
    try:
        generator._validate_code(correct_code)
        print("[OK] 正确代码验证通过")
    except Exception as e:
        print(f"[ERROR] 验证失败: {e}")
    
    # 测试代码2: 缺少必需方法
    incorrect_code = '''
from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig

class TestStrategy(AIStrategyBase):
    pass
'''
    
    try:
        generator._validate_code(incorrect_code)
        print("[ERROR] 应该检测到错误，但验证通过了")
    except ValueError as e:
        print(f"[OK] 正确检测到错误: {e}")
    
    # 测试代码3: 使用了禁止的函数
    forbidden_code = '''
import talib
import pandas as pd

class TestStrategy(AIStrategyBase):
    def _generate_signals_impl(self, current_date: str, stock_pool):
        # 错误：使用了 talib
        rsi = talib.RSI(stock_pool["close"])
        return {}
'''
    
    try:
        generator._validate_code(forbidden_code)
        print("[ERROR] 应该检测到禁止的函数，但验证通过了")
    except ValueError as e:
        print(f"[OK] 正确检测到禁止的函数: {e}")


def test_code_cleaning():
    """测试代码清理功能"""
    print("\n" + "=" * 80)
    print("测试 3: 代码清理")
    print("=" * 80)
    
    generator = AIStrategyGenerator()
    
    # 模拟 LLM 返回的代码（包含 markdown 标记）
    raw_code = '''
```python
from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig

class TestStrategy(AIStrategyBase):
    def get_required_indicators(self):
        return []
```
'''
    
    cleaned = generator._clean_code(raw_code)
    print("原始代码:")
    print(raw_code)
    print("\n清理后的代码:")
    print(cleaned)
    
    # 检查是否移除了 markdown 标记
    if "```" not in cleaned:
        print("[OK] Markdown 标记已移除")
    else:
        print("[ERROR] Markdown 标记未完全移除")


def test_custom_llm():
    """测试自定义 LLM 函数"""
    print("\n" + "=" * 80)
    print("测试 4: 自定义 LLM 生成")
    print("=" * 80)
    
    generator = AIStrategyGenerator()
    
    # 模拟 LLM 函数
    def mock_llm(prompt: str) -> str:
        """模拟 LLM 返回"""
        return '''
from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig

class GeneratedStrategy(AIStrategyBase):
    strategy_name = "生成策略"
    
    def __init__(self, config: AIStrategyConfig = None):
        if config is None:
            config = AIStrategyConfig(params={
                "ma_period": 20,
                "rsi_threshold": 70,
            })
        super().__init__(config)
    
    def get_required_indicators(self):
        return [
            {"name": "MA", "period": self.config.params.get("ma_period", 20), "column": "close"},
            {"name": "RSI", "period": 14, "column": "close"},
        ]
    
    def _generate_signals_impl(self, current_date: str, stock_pool):
        signals = {}
        rsi_threshold = self.config.params.get("rsi_threshold", 70)
        
        for _, row in stock_pool.iterrows():
            code = row["stock_code"]
            if code in self.current_portfolio and row["RSI"] > rsi_threshold:
                signals[code] = "sell"
            elif row["close"] > row.get("MA_20", 0):
                signals[code] = "buy"
            else:
                signals[code] = "hold"
        
        return signals
'''
    
    generator.set_custom_llm(mock_llm)
    
    try:
        code = generator.generate("股价突破20日均线买入，RSI大于70卖出")
        print("[OK] 代码生成成功")
        print("\n生成的代码:")
        print(code)
        
        # 尝试动态加载
        print("\n尝试动态加载代码...")
        spec = importlib.util.spec_from_loader("generated_strategy", loader=None)
        module = importlib.util.module_from_spec(spec)
        exec(code, module.__dict__)
        
        # 检查类是否存在
        if hasattr(module, "GeneratedStrategy"):
            print("[OK] 策略类已成功加载")
            
            # 创建实例
            strategy = module.GeneratedStrategy()
            print(f"[OK] 策略实例创建成功: {strategy.strategy_name}")
            
            # 检查方法
            indicators = strategy.get_required_indicators()
            print(f"[OK] 指标声明: {indicators}")
        else:
            print("[ERROR] 策略类未找到")
            
    except Exception as e:
        print(f"[ERROR] 生成或加载失败: {e}")
        import traceback
        traceback.print_exc()


def test_full_workflow():
    """测试完整工作流程"""
    print("\n" + "=" * 80)
    print("测试 5: 完整工作流程")
    print("=" * 80)
    
    # 1. 创建生成器
    generator = AIStrategyGenerator()
    
    # 2. 设置模拟 LLM
    def simple_llm(prompt: str) -> str:
        return '''
from core.quant.strategies.templates.ai_base import AIStrategyBase, AIStrategyConfig

class SimpleStrategy(AIStrategyBase):
    strategy_name = "简单策略"
    
    def __init__(self, config: AIStrategyConfig = None):
        if config is None:
            config = AIStrategyConfig(params={"rsi_period": 14})
        super().__init__(config)
    
    def get_required_indicators(self):
        return [{"name": "RSI", "period": 14, "column": "close"}]
    
    def _generate_signals_impl(self, current_date: str, stock_pool):
        return {row["stock_code"]: "hold" for _, row in stock_pool.iterrows()}
'''
    
    generator.set_custom_llm(simple_llm)
    
    # 3. 生成代码
    print("步骤 1: 生成策略代码...")
    code = generator.generate("写一个简单的策略")
    print("[OK] 代码生成完成")
    
    # 4. 保存到文件
    print("\n步骤 2: 保存到文件...")
    output_path = Path(__file__).parent / "generated_test_strategy.py"
    generator.generate_and_save("写一个简单的策略", str(output_path))
    print(f"[OK] 代码已保存到: {output_path}")
    
    # 5. 验证文件存在
    if output_path.exists():
        print(f"[OK] 文件已创建，大小: {output_path.stat().st_size} 字节")
        
        # 6. 读取并验证
        with open(output_path, "r", encoding="utf-8") as f:
            saved_code = f.read()
        
        if saved_code == code:
            print("[OK] 保存的代码与生成的代码一致")
        else:
            print("[ERROR] 保存的代码与生成的代码不一致")
    
    # 7. 清理测试文件
    if output_path.exists():
        output_path.unlink()
        print("[OK] 测试文件已清理")


if __name__ == "__main__":
    print("开始测试 AI 策略生成器\n")
    
    test_prompt_generation()
    test_code_validation()
    test_code_cleaning()
    test_custom_llm()
    test_full_workflow()
    
    print("\n" + "=" * 80)
    print("所有测试完成！")
    print("=" * 80)

