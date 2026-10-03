"""把核实成果导出成可进 git 的文件——数据库只当派生物。

为什么必须有这一层
------------------
``data/*.sqlite`` 是 gitignored 的，而核实出来的参数值（ParamOverride，每条带
来源 URL、检查日期、快照引用）与核实任务结论只存在库里。实测踩到：19 条
ParamOverride、13 条核实结论、110 篇文档全在库中，spec 文件里 ``P-TW-DTS``
还写着 ``value=None, level=E``。**换台机器 clone 下来，核实成果全部丢失。**

所以真值要进版本控制，数据库降级为运行时的缓存与索引。这也是配置即代码的
常规做法：**声明式真值随代码走，派生存储可以随时重建**。

导出的是「参数的最新覆盖」+「已完成的核实任务」，两条都带完整出处。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "spec" / "verified.json"

# 文件行的 id 基准。用负数是为了永远排在数据库行（autoincrement ≥ 1）之前，
# 因为 Spec.with_overrides 是"后应用者生效"。见 as_override_rows 的说明。
FILE_ID_BASE = 100000

# 导出时保留的字段。少了出处就等于没导出。
OVERRIDE_FIELDS = ("param_id", "value", "evidence_level", "source_url", "snapshot_ref",
                   "checked_at", "grade", "operator", "created_at")
CHECKLIST_FIELDS = ("spec_task_id", "conclusion", "grade", "checked_date", "source_url",
                    "snapshot_ref", "status")


def _pick(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: row.get(key) for key in fields if row.get(key) not in (None, "")}


def build_verified(overrides: list[dict[str, Any]],
                   checklist: list[dict[str, Any]]) -> dict[str, Any]:
    """把库里的覆盖与核实结论整理成一份稳定的 JSON。

    同一参数有多条历史覆盖时**只导出最新那条**（按 id 升序取最后一条）——
    导出的是当前生效状态，历史留在审计表里。
    """
    latest: dict[str, dict[str, Any]] = {}
    for row in sorted(overrides, key=lambda item: item.get("id") or 0):
        param_id = row.get("param_id")
        if param_id:
            latest[param_id] = _pick(row, OVERRIDE_FIELDS)

    done = []
    for row in sorted(checklist, key=lambda item: item.get("id") or 0):
        if not row.get("conclusion"):
            continue
        done.append(_pick(row, CHECKLIST_FIELDS))

    return {
        "note": "核实成果的可追溯快照。真值在这里（随 git 走），数据库是派生物。"
                "用 export-verified 生成，用 import-verified 回灌。",
        "params": latest,
        "checklist_done": done,
    }


def write_verified(payload: dict[str, Any], path: Path | str = DEFAULT_PATH) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return target


def read_verified(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    """读导出文件。不存在时返回空结构，不报错——没跑过导出也要能用。"""
    target = Path(path)
    if not target.exists():
        return {"params": {}, "checklist_done": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return {"params": {}, "checklist_done": []}
    return {"params": data.get("params") or {}, "checklist_done": data.get("checklist_done") or []}


def as_override_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """转成 Spec.with_overrides 认的行格式。

    ``Spec.with_overrides`` 按 ``id`` 升序应用，**后应用的生效**（历史覆盖只增不改）。
    文件行与库行的 id 出自两个不同的序列，直接混用会撞车——实测踩过：
    库里的新覆盖 id=1，文件里 P-TW-FX 的 id 排在后面，于是**文件反而赢了**，
    页面上看到的还是旧值。
    所以文件行一律用**负数 id**：既保证文件内部顺序不变，又保证永远排在库行之前。
    """
    rows = []
    for index, (param_id, item) in enumerate(sorted(payload.get("params", {}).items()), start=1):
        row = dict(item)
        row["param_id"] = param_id
        row["id"] = index - FILE_ID_BASE     # 负数，永远排在库行前面
        rows.append(row)
    return rows
