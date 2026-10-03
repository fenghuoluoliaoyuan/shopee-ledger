"""受控表达式求值器：把 spec 里的 ``condition`` 字符串变成**三态布尔**。

为什么要自己写而不直接 ``eval``
--------------------------------
1. condition 来自配置文件，必须走 AST 白名单（与 simpleeval / asteval 同一思路，
   但这里不用 ``eval``，而是递归求值，彻底不给注入留口子）。
2. 上下文常常"只填了一半"：G1 阶段还没有订单数据，G5 阶段还没有回款数据。
   **未提供的字段必须求值为 UNKNOWN，而不是抛异常，也不能当成 False。**
3. 需要把 UNKNOWN 的字段名回报出去，供 GateService 生成 ``missing_fields``，
   对应四态里的 ``INCOMPLETE``（缺数据 ≠ 不可做）。

文法（Python 表达式子集，加载期不预编译、求值期不 eval）
------------------------------------------------------
* 字面量：``true`` / ``false`` / ``null`` 会被归一成 ``True`` / ``False`` / ``None``
* 参数引用：``P-TW-MARGIN-TH.value.warn`` → ``params["P-TW-MARGIN-TH"]["value"]["warn"]``
* 属性访问：``order.status``、``listing.ctr``（对象可为 dict、Bag 或普通对象）
* 逻辑：``&&`` / ``||`` / ``!``
* 比较：``== != < <= > >= in "not in"``
* 算术：``+ - * / %``
* 函数白名单：``match count suppliers max min abs len``
"""

from __future__ import annotations

import ast
import re
from typing import Any

__all__ = ["UNKNOWN", "EvalResult", "ExpressionError", "Bag", "evaluate", "prepare"]

PARAM_RE = re.compile(r"\bP-[A-Z0-9][A-Z0-9-]*\b")
CHAIN_RE = re.compile(r"\]\.([A-Za-z_][A-Za-z_0-9]*)")
LITERAL_RE = re.compile(r"\b(true|false|null)\b")
# && || ! → and or not（! 后面不能跟 =，否则会毁掉 !=）
LOGIC_RE = re.compile(r"&&|\|\||!(?!=)")
ALLOWED_FUNCS = ("match", "count", "suppliers", "max", "min", "abs", "len")

ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.List, ast.Tuple, ast.Set,
    ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Call, ast.Subscript, ast.Attribute,
)


class ExpressionError(ValueError):
    """condition 语法或用法超出白名单。属于配置错误，加载期就该暴露。"""


class _UnknownType:
    """未知值的单例。绝不等于任何东西，包括它自己。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNKNOWN"

    def __eq__(self, other: Any) -> bool:  # noqa: D105
        return self is other

    def __hash__(self) -> int:
        return hash("__unknown__")


UNKNOWN = _UnknownType()


class Bag:
    """把 dict 包成可属性访问的对象；缺失的键返回 UNKNOWN（由求值器记录）。"""

    __slots__ = ("_data",)

    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            return _wrap(self._data[name])
        return UNKNOWN

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str) and key in self._data:
            return _wrap(self._data[key])
        return UNKNOWN

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return "Bag(%s)" % ", ".join(sorted(self._data))


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return Bag(value)
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value


def _get_attr(obj: Any, name: str) -> Any:
    if isinstance(obj, Bag):
        return obj.__getattr__(name)
    if isinstance(obj, dict):
        return _wrap(obj[name]) if name in obj else UNKNOWN
    return _wrap(getattr(obj, name)) if hasattr(obj, name) else UNKNOWN


def prepare(condition: str) -> str:
    """把 spec 的伪语法归一成合法 Python 表达式文本（不执行）。"""
    text = condition.strip()
    text = LITERAL_RE.sub(lambda m: {"true": "True", "false": "False", "null": "None"}[m.group(1)], text)
    text = PARAM_RE.sub(lambda m: 'params["%s"]' % m.group(0), text)
    # params["X"].value.warn → params["X"]["value"]["warn"]
    for _ in range(6):
        new = CHAIN_RE.sub(lambda m: ']["%s"]' % m.group(1), text)
        if new == text:
            break
        text = new
    text = LOGIC_RE.sub(lambda m: {"&&": " and ", "||": " or "}.get(m.group(0), " not "), text)
    return text


class EvalResult:
    __slots__ = ("value", "unknowns")

    def __init__(self, value: Any, unknowns: list[str]):
        self.value = value
        self.unknowns = unknowns

    @property
    def is_unknown(self) -> bool:
        return self.value is UNKNOWN

    def __repr__(self) -> str:
        return "EvalResult(%r, unknowns=%r)" % (self.value, self.unknowns)


class _Evaluator:
    def __init__(self, context: dict[str, Any]):
        self.context = {key: _wrap(val) for key, val in context.items()}
        self.unknowns: list[str] = []

    # ---- 入口 -----------------------------------------------------------
    def run(self, text: str) -> Any:
        prepared = prepare(text)
        try:
            tree = ast.parse(prepared, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError("condition 语法错误: %s -> %s" % (text, exc)) from exc
        for node in ast.walk(tree):
            if not isinstance(node, ALLOWED_NODES):
                raise ExpressionError("condition 含不允许的语法: %s (%s)" % (type(node).__name__, text))
            if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
                raise ExpressionError("condition 只允许调用白名单函数: %s" % text)
        return self.eval(tree.body)

    # ---- 递归求值 -------------------------------------------------------
    def eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.context:
                return self.context[node.id]
            if node.id in ALLOWED_FUNCS:
                return node.id
            self._note(node.id)
            return UNKNOWN
        if isinstance(node, ast.List):
            return [self.eval(item) for item in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self.eval(item) for item in node.elts)
        if isinstance(node, ast.Set):
            return {self.eval(item) for item in node.elts}
        if isinstance(node, ast.UnaryOp):
            operand = self.eval(node.operand)
            if operand is UNKNOWN:
                return UNKNOWN
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            return +operand
        if isinstance(node, ast.BoolOp):
            return self._bool_op(node)
        if isinstance(node, ast.BinOp):
            left, right = self.eval(node.left), self.eval(node.right)
            if left is UNKNOWN or right is UNKNOWN:
                return UNKNOWN
            return self._bin_op(node.op, left, right)
        if isinstance(node, ast.Compare):
            return self._compare(node)
        if isinstance(node, ast.Subscript):
            base = self.eval(node.value)
            if base is UNKNOWN:
                return UNKNOWN
            key = self.eval(node.slice)
            if key is UNKNOWN:
                return UNKNOWN
            if isinstance(key, str) and key.startswith("__"):
                raise ExpressionError("禁止访问双下划线键: %s" % ast.unparse(node))
            try:
                return _wrap(base[key])
            except (KeyError, IndexError, TypeError):
                self._note(ast.unparse(node))
                return UNKNOWN
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ExpressionError("禁止访问下划线属性: %s" % ast.unparse(node))
            base = self.eval(node.value)
            if base is UNKNOWN:
                return UNKNOWN
            value = _get_attr(base, node.attr)
            if value is UNKNOWN:
                self._note(ast.unparse(node))
            return value
        if isinstance(node, ast.Call):
            return self._call(node)
        raise ExpressionError("不支持的节点: %s" % type(node).__name__)

    def _bool_op(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            saw_unknown = False
            for item in node.values:
                value = self.eval(item)
                if value is UNKNOWN:
                    saw_unknown = True
                elif not value:
                    return False
            return UNKNOWN if saw_unknown else True
        saw_unknown = False
        for item in node.values:
            value = self.eval(item)
            if value is UNKNOWN:
                saw_unknown = True
            elif value:
                return True
        return UNKNOWN if saw_unknown else False

    @staticmethod
    def _bin_op(op: ast.AST, left: Any, right: Any) -> Any:
        try:
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, ast.Div):
                return left / right
            if isinstance(op, ast.FloorDiv):
                return left // right
            if isinstance(op, ast.Mod):
                return left % right
        except TypeError:
            return UNKNOWN
        raise ExpressionError("不支持的运算符: %s" % type(op).__name__)

    def _compare(self, node: ast.Compare) -> Any:
        left = self.eval(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.eval(comparator)
            if left is UNKNOWN or right is UNKNOWN:
                return UNKNOWN
            try:
                if isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                elif isinstance(op, ast.Lt):
                    ok = left < right
                elif isinstance(op, ast.LtE):
                    ok = left <= right
                elif isinstance(op, ast.Gt):
                    ok = left > right
                elif isinstance(op, ast.GtE):
                    ok = left >= right
                elif isinstance(op, ast.In):
                    ok = left in right
                elif isinstance(op, ast.NotIn):
                    ok = left not in right
                else:
                    raise ExpressionError("不支持的比较符: %s" % type(op).__name__)
            except TypeError:
                # None 参与比较会抛 TypeError。返回 UNKNOWN 时**必须记下是哪些字段**，
                # 否则调用方只知道"算不出来"，不知道缺什么——实测 R-COST-002 因此
                # 报出「净利润率低于下限，砍掉」这种把未知当既成事实的话。
                self._note_none_names(node.left)
                self._note_none_names(comparator)
                return UNKNOWN
            if not ok:
                return False
            left = right
        return True

    def _note_none_names(self, node: ast.AST) -> None:
        """把子表达式里**取值为 None** 的字段名记进 unknowns。

        与 ``eval`` 里"名字不在 context 就记名"是两件事：这里的名字**在** context 里，
        只是值是 None（字段存在但还没算出来）。两种都该让调用方知道缺什么。
        """
        for child in ast.walk(node):
            if not isinstance(child, ast.Name):
                continue
            if child.id in self.context:
                if self.context[child.id] is None:
                    self._note(child.id)
            elif child.id not in ALLOWED_FUNCS:
                self._note(child.id)

    def _call(self, node: ast.Call) -> Any:
        name = node.func.id
        if name not in ALLOWED_FUNCS:
            raise ExpressionError("不在白名单的函数: %s" % name)
        args = [self.eval(arg) for arg in node.args]
        if any(arg is UNKNOWN for arg in args):
            return UNKNOWN
        if name == "match":
            haystack, needles = args[0], args[1]
            if needles is None:
                return UNKNOWN
            text = "" if haystack is None else str(haystack)
            terms = needles if isinstance(needles, (list, tuple, set)) else [needles]
            for term in terms:
                if term is None or term is UNKNOWN:
                    return UNKNOWN
                if str(term) and str(term) in text:
                    return True
            return False
        if name == "count":
            value = args[0]
            if value is None:
                return 0
            return len(value) if isinstance(value, (list, tuple, set, dict, str)) else 1
        if name == "suppliers":
            return self.context.get("suppliers", [])
        if name in ("max", "min", "abs", "len"):
            return {"max": max, "min": min, "abs": abs, "len": len}[name](*args)
        raise ExpressionError("未实现的函数: %s" % name)

    def _note(self, name: str) -> None:
        if name not in self.unknowns:
            self.unknowns.append(name)


def evaluate(condition: str, context: dict[str, Any]) -> EvalResult:
    """求值一个 condition。缺字段不抛异常，返回 UNKNOWN。"""
    evaluator = _Evaluator(context)
    value = evaluator.run(condition)
    return EvalResult(value, evaluator.unknowns)


def referenced(condition: str) -> dict[str, list[str]]:
    """静态提取 condition 引用的参数 id / 上下文名 / 函数名。

    用途：把「配置文法」变成可测试契约——
    * ``params`` 里只能是真实存在的参数 id（否则拼错了不会报错，只会永远 UNKNOWN）
    * 裸名字必须在声明的上下文词表内（见 gates.CONTEXT_KEYS）
    """
    tree = ast.parse(prepare(condition), mode="eval")
    param_ids: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "params":
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                param_ids.append(key.value)

    funcs = sorted({
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    })
    all_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names = sorted(all_names - set(funcs) - {"params"})
    return {"params": param_ids, "names": names, "funcs": funcs}
