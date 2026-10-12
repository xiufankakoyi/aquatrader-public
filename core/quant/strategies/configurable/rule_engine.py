"""
规则引擎 - 解析并执行配置化的交易规则

设计目标：
1. 表达式解析：支持复杂的条件表达式
2. 向量化执行：利用 Pandas 进行高效的条件判断
3. 可扩展性：易于添加新的操作符和函数
4. 安全性：避免 eval/exec，防止代码注入

支持的条件表达式：
- 比较操作: >, <, >=, <=, ==, !=
- 逻辑操作: and, or, not
- 函数调用: crossover(ma5, ma20), above(close, ma20), etc.

使用示例：
    engine = RuleEngine(data_with_indicators)
    
    # 简单条件
    buy_signals = engine.evaluate("close > ma20")
    
    # 复合条件
    signals = engine.evaluate("close > ma5 and ma5 > ma20")
    
    # 函数条件
    signals = engine.evaluate("crossover(ma5, ma20)")
"""

import pandas as pd
import numpy as np
import re
from typing import Dict, Any, List, Callable, Optional, Union
from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    """词法分析 Token 类型"""
    NUMBER = auto()
    STRING = auto()
    IDENTIFIER = auto()  # 列名或函数名
    OPERATOR = auto()    # > < >= <= == != and or not
    LPAREN = auto()      # (
    RPAREN = auto()      # )
    COMMA = auto()       # ,
    EOF = auto()


@dataclass
class Token:
    """词法单元"""
    type: TokenType
    value: Any
    pos: int


@dataclass
class ASTNode:
    """抽象语法树节点"""
    type: str
    value: Any = None
    children: List['ASTNode'] = None
    
    def __post_init__(self):
        if self.children is None:
            self.children = []


class Lexer:
    """
    词法分析器
    
    将表达式字符串转换为 Token 序列
    """
    
    OPERATORS = {'>', '<', '>=', '<=', '==', '!=', 'and', 'or', 'not'}
    
    def __init__(self, expression: str):
        self.expression = expression
        self.pos = 0
        self.length = len(expression)
    
    def tokenize(self) -> List[Token]:
        """将表达式转换为 Token 列表"""
        tokens = []
        
        while self.pos < self.length:
            char = self.expression[self.pos]
            
            # 跳过空白字符
            if char.isspace():
                self.pos += 1
                continue
            
            # 数字
            if char.isdigit() or (char == '.' and self.pos + 1 < self.length and self.expression[self.pos + 1].isdigit()):
                token = self._read_number()
                tokens.append(token)
                continue
            
            # 字符串（单引号或双引号）
            if char in '"\'':
                token = self._read_string()
                tokens.append(token)
                continue
            
            # 标识符或关键字
            if char.isalpha() or char == '_':
                token = self._read_identifier()
                tokens.append(token)
                continue
            
            # 操作符
            if char in '><=!':
                token = self._read_operator()
                tokens.append(token)
                continue
            
            # 括号
            if char == '(':
                tokens.append(Token(TokenType.LPAREN, '(', self.pos))
                self.pos += 1
                continue
            
            if char == ')':
                tokens.append(Token(TokenType.RPAREN, ')', self.pos))
                self.pos += 1
                continue
            
            # 逗号
            if char == ',':
                tokens.append(Token(TokenType.COMMA, ',', self.pos))
                self.pos += 1
                continue
            
            # 未知字符
            raise SyntaxError(f"Unexpected character '{char}' at position {self.pos}")
        
        tokens.append(Token(TokenType.EOF, None, self.pos))
        return tokens
    
    def _read_number(self) -> Token:
        """读取数字"""
        start = self.pos
        has_dot = False
        
        while self.pos < self.length:
            char = self.expression[self.pos]
            if char.isdigit():
                self.pos += 1
            elif char == '.' and not has_dot:
                has_dot = True
                self.pos += 1
            else:
                break
        
        value = float(self.expression[start:self.pos]) if has_dot else int(self.expression[start:self.pos])
        return Token(TokenType.NUMBER, value, start)
    
    def _read_string(self) -> Token:
        """读取字符串"""
        start = self.pos
        quote = self.expression[self.pos]
        self.pos += 1
        
        while self.pos < self.length and self.expression[self.pos] != quote:
            self.pos += 1
        
        if self.pos >= self.length:
            raise SyntaxError(f"Unterminated string at position {start}")
        
        value = self.expression[start + 1:self.pos]
        self.pos += 1  # 跳过结束引号
        return Token(TokenType.STRING, value, start)
    
    def _read_identifier(self) -> Token:
        """读取标识符"""
        start = self.pos
        
        while self.pos < self.length and (self.expression[self.pos].isalnum() or self.expression[self.pos] == '_'):
            self.pos += 1
        
        value = self.expression[start:self.pos]
        
        # 检查是否是操作符关键字
        if value in self.OPERATORS:
            return Token(TokenType.OPERATOR, value, start)
        
        return Token(TokenType.IDENTIFIER, value, start)
    
    def _read_operator(self) -> Token:
        """读取操作符"""
        start = self.pos
        
        # 检查双字符操作符
        if self.pos + 1 < self.length:
            two_char = self.expression[self.pos:self.pos + 2]
            if two_char in ('>=', '<=', '==', '!='):
                self.pos += 2
                return Token(TokenType.OPERATOR, two_char, start)
        
        # 单字符操作符
        char = self.expression[self.pos]
        if char in '><':
            self.pos += 1
            return Token(TokenType.OPERATOR, char, start)
        
        raise SyntaxError(f"Unexpected operator at position {start}")


class Parser:
    """
    语法分析器
    
    将 Token 序列解析为抽象语法树 (AST)
    
    语法规则：
        expression     : logical_or
        logical_or     : logical_and (('or') logical_and)*
        logical_and    : logical_not (('and') logical_not)*
        logical_not    : 'not' logical_not | comparison
        comparison     : function_call (('>' | '<' | '>=' | '<=' | '==' | '!=') function_call)?
        function_call  : IDENTIFIER '(' arguments ')' | primary
        arguments      : expression (',' expression)*
        primary        : NUMBER | STRING | IDENTIFIER | '(' expression ')'
    """
    
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0
    
    def parse(self) -> ASTNode:
        """解析表达式为 AST"""
        return self._parse_expression()
    
    def _current(self) -> Token:
        """获取当前 Token"""
        return self.tokens[self.pos]
    
    def _advance(self) -> Token:
        """前进到下一个 Token"""
        token = self._current()
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return token
    
    def _expect(self, token_type: TokenType, value: Any = None) -> Token:
        """期望特定类型的 Token"""
        token = self._current()
        if token.type != token_type:
            raise SyntaxError(f"Expected {token_type.name}, got {token.type.name} at position {token.pos}")
        if value is not None and token.value != value:
            raise SyntaxError(f"Expected '{value}', got '{token.value}' at position {token.pos}")
        return self._advance()
    
    def _parse_expression(self) -> ASTNode:
        """解析表达式"""
        return self._parse_logical_or()
    
    def _parse_logical_or(self) -> ASTNode:
        """解析逻辑或表达式"""
        left = self._parse_logical_and()
        
        while self._current().type == TokenType.OPERATOR and self._current().value == 'or':
            self._advance()
            right = self._parse_logical_and()
            left = ASTNode('or', 'or', [left, right])
        
        return left
    
    def _parse_logical_and(self) -> ASTNode:
        """解析逻辑与表达式"""
        left = self._parse_logical_not()
        
        while self._current().type == TokenType.OPERATOR and self._current().value == 'and':
            self._advance()
            right = self._parse_logical_not()
            left = ASTNode('and', 'and', [left, right])
        
        return left
    
    def _parse_logical_not(self) -> ASTNode:
        """解析逻辑非表达式"""
        if self._current().type == TokenType.OPERATOR and self._current().value == 'not':
            self._advance()
            operand = self._parse_logical_not()
            return ASTNode('not', 'not', [operand])
        
        return self._parse_comparison()
    
    def _parse_comparison(self) -> ASTNode:
        """解析比较表达式"""
        left = self._parse_function_call()
        
        if self._current().type == TokenType.OPERATOR and self._current().value in ('>', '<', '>=', '<=', '==', '!='):
            op = self._advance().value
            right = self._parse_function_call()
            return ASTNode('comparison', op, [left, right])
        
        return left
    
    def _parse_function_call(self) -> ASTNode:
        """解析函数调用"""
        if self._current().type == TokenType.IDENTIFIER:
            name = self._current().value
            
            # 检查是否是函数调用（后面跟着左括号）
            if self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].type == TokenType.LPAREN:
                self._advance()  # 消费函数名
                self._advance()  # 消费左括号
                
                # 解析参数
                args = []
                if self._current().type != TokenType.RPAREN:
                    args.append(self._parse_expression())
                    while self._current().type == TokenType.COMMA:
                        self._advance()
                        args.append(self._parse_expression())
                
                self._expect(TokenType.RPAREN)
                return ASTNode('function', name, args)
            
            # 不是函数调用，是普通标识符
            self._advance()
            return ASTNode('identifier', name)
        
        return self._parse_primary()
    
    def _parse_primary(self) -> ASTNode:
        """解析基本元素"""
        token = self._current()
        
        if token.type == TokenType.NUMBER:
            self._advance()
            return ASTNode('number', token.value)
        
        if token.type == TokenType.STRING:
            self._advance()
            return ASTNode('string', token.value)
        
        if token.type == TokenType.IDENTIFIER:
            self._advance()
            return ASTNode('identifier', token.value)
        
        if token.type == TokenType.LPAREN:
            self._advance()
            expr = self._parse_expression()
            self._expect(TokenType.RPAREN)
            return expr
        
        raise SyntaxError(f"Unexpected token {token.type.name} at position {token.pos}")


class RuleEngine:
    """
    规则引擎
    
    解析条件表达式并在数据上执行
    
    使用示例：
        engine = RuleEngine(data_with_indicators)
        signals = engine.evaluate("close > ma20 and volume > volume_ma20")
    """
    
    # 比较操作符映射
    COMPARISON_OPS = {
        '>': lambda a, b: a > b,
        '<': lambda a, b: a < b,
        '>=': lambda a, b: a >= b,
        '<=': lambda a, b: a <= b,
        '==': lambda a, b: a == b,
        '!=': lambda a, b: a != b,
    }
    
    # 内置函数映射
    BUILTIN_FUNCTIONS = {
        'crossover': lambda data, fast, slow: _crossover(data, fast, slow),
        'crossunder': lambda data, fast, slow: _crossunder(data, fast, slow),
        'above': lambda data, col, ref: data[col] > data[ref],
        'below': lambda data, col, ref: data[col] < data[ref],
        'between': lambda data, col, low, high: (data[col] >= low) & (data[col] <= high),
        'max': lambda data, col, window: data.groupby('stock_code')[col].transform(lambda x: x.rolling(window).max()),
        'min': lambda data, col, window: data.groupby('stock_code')[col].transform(lambda x: x.rolling(window).min()),
    }
    
    def __init__(self, data: pd.DataFrame):
        """
        初始化规则引擎
        
        参数：
            data: 包含指标列的 DataFrame
        """
        self.data = data
        self._cache: Dict[str, pd.Series] = {}
    
    def evaluate(self, expression: str) -> pd.Series:
        """
        评估条件表达式
        
        参数：
            expression: 条件表达式字符串
        
        返回：
            pd.Series: 布尔序列，表示每行是否满足条件
        """
        # 检查缓存
        if expression in self._cache:
            return self._cache[expression]
        
        # 词法分析
        lexer = Lexer(expression)
        tokens = lexer.tokenize()
        
        # 语法分析
        parser = Parser(tokens)
        ast = parser.parse()
        
        # 执行 AST
        result = self._execute_ast(ast)
        
        # 缓存结果
        self._cache[expression] = result
        
        return result
    
    def _execute_ast(self, node: ASTNode) -> Union[pd.Series, Any]:
        """
        执行抽象语法树
        
        返回：
            pd.Series 或标量值
        """
        if node.type == 'number':
            return node.value
        
        if node.type == 'string':
            return node.value
        
        if node.type == 'identifier':
            # 检查是否是 DataFrame 的列
            if node.value in self.data.columns:
                return self.data[node.value]
            # 返回标识符名称（用于函数参数）
            return node.value
        
        if node.type == 'comparison':
            left = self._execute_ast(node.children[0])
            right = self._execute_ast(node.children[1])
            op = self.COMPARISON_OPS.get(node.value)
            if op is None:
                raise ValueError(f"Unknown comparison operator: {node.value}")
            return op(left, right)
        
        if node.type == 'and':
            left = self._execute_ast(node.children[0])
            right = self._execute_ast(node.children[1])
            return left & right
        
        if node.type == 'or':
            left = self._execute_ast(node.children[0])
            right = self._execute_ast(node.children[1])
            return left | right
        
        if node.type == 'not':
            operand = self._execute_ast(node.children[0])
            return ~operand
        
        if node.type == 'function':
            return self._execute_function(node)
        
        raise ValueError(f"Unknown AST node type: {node.type}")
    
    def _execute_function(self, node: ASTNode) -> pd.Series:
        """执行函数调用"""
        func_name = node.value
        args = [self._execute_ast(arg) for arg in node.children]
        
        # 检查是否是内置函数
        if func_name in self.BUILTIN_FUNCTIONS:
            func = self.BUILTIN_FUNCTIONS[func_name]
            # 第一个参数总是 data
            return func(self.data, *args)
        
        # 检查是否是 DataFrame 的方法
        if hasattr(self.data, func_name):
            method = getattr(self.data, func_name)
            return method(*args)
        
        raise ValueError(f"Unknown function: {func_name}")
    
    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()


# ==================== 辅助函数 ====================

def _crossover(data: pd.DataFrame, fast: str, slow: str) -> pd.Series:
    """检测金叉信号"""
    def _cross(group: pd.DataFrame) -> pd.Series:
        fast_line = group[fast]
        slow_line = group[slow]
        above_today = fast_line > slow_line
        above_yesterday = fast_line.shift(1) <= slow_line.shift(1)
        return above_today & above_yesterday
    
    return data.groupby('stock_code').apply(_cross).reset_index(level=0, drop=True)


def _crossunder(data: pd.DataFrame, fast: str, slow: str) -> pd.Series:
    """检测死叉信号"""
    def _cross(group: pd.DataFrame) -> pd.Series:
        fast_line = group[fast]
        slow_line = group[slow]
        below_today = fast_line < slow_line
        below_yesterday = fast_line.shift(1) >= slow_line.shift(1)
        return below_today & below_yesterday
    
    return data.groupby('stock_code').apply(_cross).reset_index(level=0, drop=True)


# ==================== 便捷函数 ====================

def evaluate_rule(data: pd.DataFrame, expression: str) -> pd.Series:
    """
    便捷函数：评估规则表达式
    
    参数：
        data: 包含指标列的 DataFrame
        expression: 条件表达式
    
    返回：
        pd.Series: 布尔序列
    
    示例：
        signals = evaluate_rule(data, "close > ma20 and volume > volume_ma20")
    """
    engine = RuleEngine(data)
    return engine.evaluate(expression)


def validate_expression(expression: str) -> bool:
    """
    验证表达式语法是否正确
    
    返回：
        bool: 表达式是否有效
    """
    try:
        lexer = Lexer(expression)
        tokens = lexer.tokenize()
        parser = Parser(tokens)
        parser.parse()
        return True
    except Exception:
        return False


def extract_identifiers(expression: str) -> List[str]:
    """
    从表达式中提取所有标识符（列名）
    
    返回：
        List[str]: 标识符列表
    """
    lexer = Lexer(expression)
    tokens = lexer.tokenize()
    
    identifiers = []
    for token in tokens:
        if token.type == TokenType.IDENTIFIER:
            identifiers.append(token.value)
    
    return list(set(identifiers))  # 去重
