"""极简行覆盖率（只用标准库）。

为什么自己写
------------
项目坚持零新依赖，装不了 coverage.py；而"哪些代码从没被执行过"正是这几轮反复
出问题的地方：

  · 第 11 轮 `freight --check` 崩了——因为那条分支**从没被任何测试执行过**
  · 第 12 轮 `overlap_note` 的问题同理
  · 第 8 轮删掉的 profit.py 旧引擎，也是"没人执行却还在仓库里"

用 Python 3.12 的 ``sys.monitoring``（专为这类工具设计），比 ``sys.settrace`` 快得多：
实测紧凑循环里约 7 倍开销，settrace 通常是 50 倍以上。

怎么用
------
    from shopee_ledger.coverage import collect
    with collect() as seen:
        ...跑你的工作负载...
    report = seen.report(ROOT / "shopee_ledger")

只统计 ``shopee_ledger/`` 下的文件，标准库与第三方不记。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType

TOOL_ID = sys.monitoring.COVERAGE_ID


def executable_lines(path: Path) -> set[int]:
    """哪些行是"可执行的"。

    用字节码的 ``co_lines()`` 而不是自己猜 AST——它给的是解释器真正认的行号，
    空行、纯注释、多行字符串的后续行都不会出现。

    **注意 ``co_lines()`` 返回的是 ``(起始字节, 结束字节, 行号)``。**
    第一版把起始/结束当成行号用了（于是出现"第 0 行"），整个统计都是错的。
    行号在第三位；解释器还会为 RESUME 之类产出 lineno=0，要排除。
    """
    source = path.read_text(encoding="utf-8")
    total = len(source.splitlines())
    lines: set[int] = set()

    def walk(code: CodeType) -> None:
        for _, _, lineno in code.co_lines():
            if lineno is None or lineno <= 0:
                continue
            if lineno <= total:
                lines.add(lineno)
        for const in code.co_consts:
            if isinstance(const, CodeType):
                walk(const)

    try:
        walk(compile(source, str(path), "exec"))
    except SyntaxError:
        return set()
    return lines


@dataclass
class Seen:
    """记录到执行过的 (文件, 行)。"""

    hits: set[tuple[str, int]] = field(default_factory=set)
    prefix: str = ""

    def add(self, filename: str, line: int) -> None:
        if self.prefix and self.prefix not in filename:
            return
        self.hits.add((filename, line))

    def lines_of(self, path: Path) -> set[int]:
        """某个文件被执行到的行。

        文件名比较要归一化：``co_filename`` 可能是相对或绝对、反斜杠或斜杠，
        直接比字符串会静默地一条都命中不了（第一版就把 profit.py 报成 0%）。
        """
        target = str(Path(path).resolve()).lower()
        return {line for name, line in self.hits
                if str(Path(name).resolve()).lower() == target}

    def report(self, package: Path) -> list[dict]:
        out = []
        for path in sorted(Path(package).glob("*.py")):
            all_lines = executable_lines(path)
            if not all_lines:
                continue
            hit = self.lines_of(path)
            missed = sorted(all_lines - hit)
            out.append({
                "module": path.name,
                "total": len(all_lines),
                "hit": len(all_lines & hit),
                "missed": missed,
                "ratio": len(all_lines & hit) / len(all_lines),
            })
        out.sort(key=lambda item: item["ratio"])
        return out

    def source_of_interest(self, package: Path, limit: int = 40) -> list[str]:
        """把没执行到的行连同源码摘出来——光看行号没用，要看是什么代码。"""
        lines_out: list[str] = []
        for item in self.report(package):
            if not item["missed"]:
                continue
            path = Path(package) / item["module"]
            source = path.read_text(encoding="utf-8").splitlines()
            lines_out.append("=== %s  命中 %d/%d（%.0f%%），未执行 %d 行"
                             % (item["module"], item["hit"], item["total"],
                                item["ratio"] * 100, len(item["missed"])))
            for number in item["missed"][:limit]:
                text = source[number - 1].strip() if number <= len(source) else ""
                lines_out.append("  %5d  %s" % (number, text[:100]))
            if len(item["missed"]) > limit:
                lines_out.append("  …还有 %d 行" % (len(item["missed"]) - limit))
        return lines_out


@contextmanager
def collect(prefix: str = "shopee_ledger"):
    """在 with 块内收集行命中。"""
    seen = Seen(prefix=prefix)
    monitoring = sys.monitoring

    def on_line(code: CodeType, line: int) -> None:
        seen.add(code.co_filename, line)

    monitoring.use_tool_id(TOOL_ID, "shopee-ledger-coverage")
    monitoring.register_callback(TOOL_ID, monitoring.events.LINE, on_line)
    monitoring.set_events(TOOL_ID, monitoring.events.LINE)
    try:
        yield seen
    finally:
        monitoring.set_events(TOOL_ID, 0)
        monitoring.register_callback(TOOL_ID, monitoring.events.LINE, None)
        monitoring.free_tool_id(TOOL_ID)


def summarize(report: list[dict]) -> str:
    total = sum(item["total"] for item in report)
    hit = sum(item["hit"] for item in report)
    lines = ["行覆盖率：%d/%d（%.1f%%），模块 %d 个"
             % (hit, total, (hit / total * 100) if total else 0.0, len(report))]
    worst = [item for item in report if item["ratio"] < 0.5]
    if worst:
        lines.append("命中不足一半的模块：")
        for item in worst:
            lines.append("  %-22s %5.1f%%  未执行 %d 行"
                         % (item["module"], item["ratio"] * 100, len(item["missed"])))
    return "\n".join(lines)
