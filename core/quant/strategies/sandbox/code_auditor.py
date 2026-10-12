"""
策略代码安全审计器

通过 AST 解析对用户提交的策略代码进行静态安全审计，
阻止危险模块导入和危险函数调用。

审计维度：
1. 危险 import（os、subprocess、socket、ctypes、shutil 等系统级模块）
2. 危险内置函数调用（eval、exec、compile、__import__、open 写模式）
3. 危险属性访问（__builtins__、__globals__、__subclasses__、__class__）
4. 双下划线变量读写（__import__、__loader__ 等）
5. 反射逃逸（getattr(__builtins__, ...)）

注意：AST 审计是第一道防线，无法覆盖所有攻击向量，
必须配合 multiprocessing 沙盒隔离执行。
"""

import ast
import re
from dataclasses import dataclass, field
from typing import List, Set, Tuple

from config.logger import get_logger

logger = get_logger(__name__)


# 危险模块黑名单（导入或使用即拒绝）
DANGEROUS_MODULES: Set[str] = {
    # 系统操作
    "os", "subprocess", "shutil", "sys",
    "ctypes", "cffi", "platform",
    # 网络
    "socket", "http", "urllib", "requests",
    "asyncio", "websocket", "flask", "django",
    # 进程/线程
    "multiprocessing", "threading", "concurrent",
    "signal", "resource",
    # 文件系统
    "pathlib", "glob", "tempfile", "io",
    "fileinput", "linecache",
    # 动态执行
    "importlib", "imp", "pkgutil", "runpy",
    # 序列化（可执行任意代码）
    "pickle", "cPickle", "marshal", "shelve",
    # 编译
    "compile", "code", "codeop",
    # 其他
    "builtins", "__builtin__",
    "pty", "ptyprocess", "pexpect",
    "webbrowser", "antigravity",
}

# 危险内置函数（调用即拒绝，除非是只读 open）
DANGEROUS_BUILTINS: Set[str] = {
    "eval", "exec", "compile",
    "__import__", "globals", "locals",
    "vars", "dir",
    "exit", "quit",
    "breakpoint", "help",
    "memoryview",
}

# 危险属性访问（访问即拒绝）
DANGEROUS_ATTRS: Set[str] = {
    "__subclasses__",
    "__bases__",
    "__mro__",
    "__class__",
    "__globals__",
    "__builtins__",
    "__code__",
    "__func__",
    "__self__",
    "__module__",
    "__dict__",
    "__loader__",
    "__spec__",
    "func_globals",
    "gi_frame",
    "gi_code",
    "cr_frame",
    "tb_frame",
    "f_locals",
    "f_globals",
    "f_builtins",
    "f_code",
}

# 双下划线变量名前缀（避免绕过）
DUNDER_PATTERN = re.compile(r"^__[a-z_]+__$")


@dataclass
class AuditResult:
    """审计结果"""

    passed: bool
    violations: List[str] = field(default_factory=list)
    code_lines: List[str] = field(default_factory=list)

    def add_violation(self, msg: str, lineno: int = 0) -> None:
        """添加违规项"""
        prefix = f"[行 {lineno}] " if lineno else ""
        self.violations.append(f"{prefix}{msg}")
        self.passed = False


class StrategyCodeAuditor(ast.NodeVisitor):
    """
    策略代码 AST 安全审计器

    遍历 AST 节点，检测危险导入、调用、属性访问。
    """

    def __init__(self) -> None:
        self.result = AuditResult(passed=True)

    def visit_Import(self, node: ast.Import) -> None:
        """检查 import 语句"""
        for alias in node.names:
            module_name = alias.name.split(".")[0]
            if module_name in DANGEROUS_MODULES:
                self.result.add_violation(
                    f"禁止导入危险模块: {alias.name}",
                    lineno=node.lineno,
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """检查 from ... import 语句"""
        if node.module:
            module_name = node.module.split(".")[0]
            if module_name in DANGEROUS_MODULES:
                self.result.add_violation(
                    f"禁止从危险模块导入: {node.module}",
                    lineno=node.lineno,
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """检查函数调用"""
        func = node.func

        # 直接调用 eval(...)、exec(...) 等
        if isinstance(func, ast.Name) and func.id in DANGEROUS_BUILTINS:
            self.result.add_violation(
                f"禁止调用危险内置函数: {func.id}()",
                lineno=node.lineno,
            )

        # 调用 __import__(...)
        if isinstance(func, ast.Name) and func.id == "__import__":
            self.result.add_violation(
                "禁止使用 __import__() 动态导入",
                lineno=node.lineno,
            )

        # 调用 open() 检查模式（允许只读 'r'，禁止 'w'/'a'/'x'）
        if isinstance(func, ast.Name) and func.id == "open":
            self._check_open_call(node)

        # 调用 getattr(obj, '__builtins__') 等反射逃逸
        if isinstance(func, ast.Name) and func.id == "getattr":
            self._check_getattr_call(node)

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """检查属性访问"""
        attr = node.attr

        # 检查危险属性
        if attr in DANGEROUS_ATTRS:
            self.result.add_violation(
                f"禁止访问危险属性: .{attr}",
                lineno=node.lineno,
            )

        # 检查双下划线属性（防止绕过）
        if DUNDER_PATTERN.match(attr) and attr not in {
            "__init__", "__init_subclass__", "__str__", "__repr__",
            "__len__", "__iter__", "__next__", "__getitem__",
            "__setitem__", "__delitem__", "__contains__",
            "__enter__", "__exit__", "__call__",
        }:
            self.result.add_violation(
                f"禁止访问双下划线属性: .{attr}",
                lineno=node.lineno,
            )

        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """检查变量名访问"""
        # 检查 __builtins__ 等危险变量
        if node.id == "__builtins__":
            self.result.add_violation(
                "禁止访问 __builtins__",
                lineno=node.lineno,
            )
        self.generic_visit(node)

    def _check_open_call(self, node: ast.Call) -> None:
        """检查 open() 调用的模式参数"""
        if len(node.args) < 2 and not node.keywords:
            # 只有文件名参数，默认 'r' 模式，允许
            return

        mode = "r"
        if len(node.args) >= 2:
            mode_arg = node.args[1]
            if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
                mode = mode_arg.value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value

        # 检查是否包含写入模式
        if any(c in mode for c in ("w", "a", "x", "+")):
            self.result.add_violation(
                f"禁止以写入模式 open(mode='{mode}')，仅允许只读 'r'",
                lineno=node.lineno,
            )

    def _check_getattr_call(self, node: ast.Call) -> None:
        """检查 getattr(obj, '__xxx__') 反射逃逸"""
        if len(node.args) >= 2:
            attr_arg = node.args[1]
            if isinstance(attr_arg, ast.Constant) and isinstance(attr_arg.value, str):
                attr_name = attr_arg.value
                if attr_name in DANGEROUS_ATTRS or DUNDER_PATTERN.match(attr_name):
                    self.result.add_violation(
                        f"禁止通过 getattr() 反射访问危险属性: {attr_name}",
                        lineno=node.lineno,
                    )


def audit_strategy_code(code: str) -> Tuple[bool, List[str]]:
    """
    审计策略代码安全性。

    Args:
        code: 待审计的 Python 源代码字符串

    Returns:
        (passed, violations)
        - passed: True 表示通过审计，False 表示有违规
        - violations: 违规描述列表（含行号）
    """
    if not code or not code.strip():
        return False, ["代码为空"]

    # 1. 语法检查
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"语法错误: {e.msg} (行 {e.lineno})"]

    # 2. AST 审计
    auditor = StrategyCodeAuditor()
    auditor.visit(tree)

    # 3. 关键字快速扫描（防止 AST 漏过的字符串拼接执行）
    quick_violations = _quick_keyword_scan(code)
    auditor.result.violations.extend(quick_violations)
    if quick_violations:
        auditor.result.passed = False

    if auditor.result.violations:
        logger.warning(
            "[CodeAuditor] 代码审计未通过，发现 %d 项违规: %s",
            len(auditor.result.violations),
            auditor.result.violations,
        )
    else:
        logger.info("[CodeAuditor] 代码审计通过")

    return auditor.result.passed, auditor.result.violations


def _quick_keyword_scan(code: str) -> List[str]:
    """关键字快速扫描（防止 AST 解析之外的逃逸）"""
    violations: List[str] = []

    # 检测 __import__ 字符串拼接
    suspicious_patterns = [
        (r"__import__\s*\(", "检测到 __import__() 调用"),
        (r"getattr\s*\([^,]+,\s*['\"]__", "检测到 getattr() 反射访问双下划线属性"),
        (r"\beval\s*\(", "检测到 eval() 调用"),
        (r"\bexec\s*\(", "检测到 exec() 调用"),
        (r"\bcompile\s*\(", "检测到 compile() 调用"),
    ]

    for pattern, msg in suspicious_patterns:
        if re.search(pattern, code):
            violations.append(msg)

    return violations
