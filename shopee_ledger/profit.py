"""决策枚举：候选品的最终结论。

历史
----
这里原本还有一整套 v1.4 的利润引擎（``ProfitInput`` / ``ProfitResult`` / ``evaluate``）。
v4.0 的 ``CostEngine`` 取代它之后，**生产路径已无任何调用**（只有 ``Decision`` 枚举还在用），
于是删掉。

为什么必须删而不是留着：**两套并行的利润公式是隐患**——改一套忘另一套，两边会悄悄
算出不同的数；而"两处各有一套阈值逻辑"正是 cost_engine 开头明确要避免的事。
现在利润算式只有一处：``shopee_ledger/cost_engine.py``。

``Decision`` 留在这个模块只是为了不动既有 import；它不依赖任何引擎。
有一条测试守着"利润实现只有一处"，防止有人再写第二套。
"""

from __future__ import annotations

from enum import Enum


class Decision(str, Enum):
    GO = "go"
    WATCH = "watch"
    CUT = "cut"
    INCOMPLETE = "incomplete"
    THRESHOLD_UNSET = "threshold_unset"
