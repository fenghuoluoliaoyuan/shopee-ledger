"""静态检查：函数里"在赋值之前读取"的局部名。

为什么需要
----------
连续两轮踩同一个坑：

  第 11 轮  __main__._run 的 delivery 分支里 `from datetime import date as _date`
            撞上模块级 `import datetime as _date` → 同函数里靠后的 freight 分支
            UnboundLocalError，`freight --check` 直接崩。
  第 12 轮  cost_engine.quote 里 `overlap_note` 在 fee_trace 之后才赋值，
            而 fee_trace 更早就读它 → 同样 UnboundLocalError。

两次都因为"那条分支从没被执行过"而活了下来。这类错的共同形状是：
**赋值藏在条件分支里，读取在分支之外、且在赋值之前。**

Python 只在真走到那条路径时才报 UnboundLocalError，测试很难覆盖到；静态上它可判定。

保守原则
--------
只报**确定**有问题的：某局部名在某处被读，而它的**所有**赋值都在更靠后的行。
读在循环体内、赋值也在同一循环体内的情况不报（第二次迭代就有值了）。
宁可漏报，不要误报——误报会让人把检查关掉。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass
class Finding:
    path: str
    function: str
    name: str
    read_line: int
    first_assign_line: int

    def render(self) -> str:
        return ("%s:%d %s() 里读了 %r，但它的赋值在第 %d 行——"
                "赋值藏在分支里时这里会 UnboundLocalError"
                % (self.path, self.read_line, self.function, self.name, self.first_assign_line))


def _walk_own_body(statement: ast.stmt):
    """遍历某条语句，但不钻进嵌套的函数/类（那是另一个作用域）。

    **注意：作用域节点本身也不能被遍历进去。** 第一版漏了这点，
    于是 `outer` 里读 `shell` 的内层函数被算成外层的读取，报了误报。
    """
    if isinstance(statement, SCOPES):
        return
    for child in ast.iter_child_nodes(statement):
        if isinstance(child, SCOPES):
            continue
        yield child
        yield from _walk_own_body(child)


def _local_assignments(func: ast.AST) -> dict[str, list[int]]:
    """函数自身语句里各局部名的赋值行号（含 import）。"""
    result: dict[str, list[int]] = {}
    for statement in func.body:
        for node in _walk_own_body(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                result.setdefault(node.id, []).append(getattr(node, "lineno", 0))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                line = getattr(node, "lineno", 0)
                for alias in node.names:
                    result.setdefault((alias.asname or alias.name).split(".")[0], []).append(line)
    return result


def _local_reads(func: ast.AST) -> list[tuple[str, int]]:
    reads: list[tuple[str, int]] = []
    for statement in func.body:
        for node in _walk_own_body(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                reads.append((node.id, getattr(node, "lineno", 0)))
    return reads


def _loop_ranges(func: ast.AST) -> list[tuple[int, int]]:
    """函数体内每个循环覆盖的行区间。

    循环**节点自身**也要算——第一版只用 _walk_own_body 看子节点，
    于是 `for` 自己从不匹配，循环内的读/赋值被判成"读在赋值前"，报了误报。
    """
    ranges: list[tuple[int, int]] = []

    def scan(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, SCOPES):
                continue
            if isinstance(child, (ast.For, ast.While)):
                start = getattr(child, "lineno", 0)
                end = max((getattr(item, "lineno", start)
                           for item in ast.walk(child) if hasattr(item, "lineno")),
                          default=start)
                ranges.append((start, end))
            scan(child)

    for statement in func.body:
        if isinstance(statement, (ast.For, ast.While)):
            start = getattr(statement, "lineno", 0)
            end = max((getattr(item, "lineno", start)
                       for item in ast.walk(statement) if hasattr(item, "lineno")),
                      default=start)
            ranges.append((start, end))
        scan(statement)
    return ranges


def _comprehension_names(func: ast.AST) -> set[str]:
    """推导式里绑定的名字。

    **必须排除**：`[f(x) for x in items]` 里元素的文本位置在 `for` 之前，
    但求值顺序是「先 iterable、再绑定 x、再算元素」。按源码行号比就会误判成
    "读 x 在赋值 x 之前"——实测全代码库 50 条报告**全是这一类**。
    """
    names: set[str] = set()
    for statement in func.body:
        for node in _walk_all(statement):
            if isinstance(node, ast.comprehension):
                for target in ast.walk(node.target):
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _walk_all(node: ast.AST):
    """遍历全部子节点（**不跳过**嵌套作用域）——只为收集名字，不判作用域。"""
    for child in ast.iter_child_nodes(node):
        yield child
        yield from _walk_all(child)


def check_source(source: str, path: str = "<src>") -> list[Finding]:
    tree = ast.parse(source)
    findings: list[Finding] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameters = {arg.arg for arg in (list(func.args.posonlyargs) + list(func.args.args)
                                          + list(func.args.kwonlyargs))}
        if func.args.vararg:
            parameters.add(func.args.vararg.arg)
        if func.args.kwarg:
            parameters.add(func.args.kwarg.arg)
        nested = {node.name for node in ast.walk(func)
                  if isinstance(node, SCOPES) and node is not func}
        comp_names = _comprehension_names(func)

        assigns = _local_assignments(func)
        loops = _loop_ranges(func)
        for name, read_line in _local_reads(func):
            if name in parameters or name in nested or name in comp_names:
                continue
            lines = assigns.get(name)
            if not lines:
                continue                       # 不是局部变量
            first = min(lines)
            if first <= read_line:
                continue                       # 先赋值后读取，正常
            # 读在循环里、赋值也在同一个循环里 → 第二次迭代就有值，不报
            if any(start <= read_line <= end and start <= first <= end for start, end in loops):
                continue
            findings.append(Finding(path=path, function=func.name, name=name,
                                    read_line=read_line, first_assign_line=first))
    unique: dict[tuple[str, int, str], Finding] = {}
    for item in findings:
        unique[(item.function, item.read_line, item.name)] = item
    return sorted(unique.values(), key=lambda item: (item.read_line, item.name))


def check_file(path: Path) -> list[Finding]:
    return check_source(path.read_text(encoding="utf-8"), str(path))


def check_package(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(Path(root).glob("*.py")):
        findings.extend(check_file(path))
    return findings
