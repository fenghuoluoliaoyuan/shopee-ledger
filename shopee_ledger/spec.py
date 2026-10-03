"""v4.0 分层声明式配置的加载与寻址。

设计要点（与 docs/CONFIG-README.md、spec/governance.json 对齐）：

- 加载顺序严格按 ``registry.project.load_order``，不猜文件名。
- 参数按 ``scope{platform, market, mode}`` 寻址，``"*"`` 为通配。
- ``value=None`` 表示**未核实**，绝不退化成 0（INV-002 / INV-006）。
- 证据等级决定门禁资格：A/B/C 可硬拦；D 只能软提示；E 只能生成任务（INV-001）。
- A/B 级参数超过 ``next_review_at`` 自动视为 ``stale``，退出硬门禁（INV-010）。
- 未核实的候选数值只放在 ``unverified_claim``，只有 advisory 规则能读（INV-011）。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Iterable

DEFAULT_SPEC_ROOT = Path(__file__).resolve().parent.parent / "spec"

HARD_OK_LEVELS = ("A", "B", "C")
ALL_LEVELS = ("A", "B", "C", "D", "E")


class SpecError(RuntimeError):
    """配置结构性错误。加载期就该炸，不要带病运行（R-GOV-001）。"""


def _as_scope(raw: dict | None) -> dict[str, str]:
    scope = {"platform": "*", "market": "*", "mode": "*"}
    for key, value in (raw or {}).items():
        if key in scope and value:
            scope[key] = str(value)
    return scope


def _applies(scope: dict[str, str], want: dict[str, str]) -> bool:
    for dim, asked in want.items():
        if asked in (None, "*"):
            continue
        have = scope.get(dim, "*")
        if have not in ("*", asked):
            return False
    return True


@dataclass
class Param:
    id: str
    name: str
    scope: dict[str, str]
    value: Any
    evidence_level: str
    state: str
    gate_eligible: bool
    unverified_claim: Any = None
    source: dict[str, Any] = field(default_factory=dict)
    review_cycle: str | None = None
    next_review_at: str | None = None
    task_ref: str | None = None
    domain: str | None = None
    intended_level: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    # ---- 证据状态 -------------------------------------------------------
    def is_stale(self, today: str | None = None) -> bool:
        """A/B 级超过复核日即过期（INV-010）。"""
        if self.evidence_level not in ("A", "B"):
            return False
        if not self.next_review_at:
            return True
        return (today or date.today().isoformat()) > self.next_review_at

    def effective_state(self, today: str | None = None) -> str:
        if self.state == "to_verify" or self.value is None:
            return "to_verify"
        if self.is_stale(today):
            return "stale"
        return self.state

    def hard_eligible(self, today: str | None = None) -> bool:
        """能否参与硬门禁。校验器 INV-001/010 的运行时对应物。"""
        return self.evidence_level in HARD_OK_LEVELS and self.effective_state(today) == "active"

    def soft_eligible(self, today: str | None = None) -> bool:
        """能否产生软提示（D 级可以，E 级不行）。"""
        return self.effective_state(today) != "to_verify" and self.evidence_level in ("A", "B", "C", "D")

    def public(self, today: str | None = None) -> dict[str, Any]:
        """给规则求值器的只读视图。"""
        return {
            "id": self.id,
            "value": self.value,
            "unverified_claim": self.unverified_claim,
            "evidence_level": self.evidence_level,
            "state": self.effective_state(today),
            "gate_eligible": self.gate_eligible,
            "hard_eligible": self.hard_eligible(today),
            "name": self.name,
        }

    def overridden(self, row: dict[str, Any]) -> "Param":
        """用实测/后台抄录的值覆盖 spec 值（覆盖层只允许 A/B/C，见 ParamOverride 实体）。"""
        level = row.get("evidence_level") or self.evidence_level
        if level not in HARD_OK_LEVELS:
            raise SpecError("参数覆盖只能用 A/B/C 级，收到 %r（%s）" % (level, self.id))
        source = dict(self.source)
        for key, src_key in (("url", "source_url"), ("checked_at", "checked_at"),
                             ("snapshot_ref", "snapshot_ref")):
            if row.get(src_key) is not None:
                source[key] = row[src_key]
        source["override"] = True
        source.setdefault("recheck_status", "machine_ok")
        return replace(
            self,
            value=row.get("value"),
            evidence_level=level,
            state="active",
            gate_eligible=True,
            source=source,
            raw=dict(self.raw, overridden=True),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Param":
        pid = raw.get("id")
        if not pid:
            raise SpecError("参数缺少 id")
        level = raw.get("evidence_level")
        if level not in ALL_LEVELS:
            raise SpecError("%s: evidence_level 非法 %r" % (pid, level))
        if level == "E" and raw.get("value") is not None:
            raise SpecError("INV-002 违反: %s 为 E 级但 value 非空" % pid)
        return cls(
            id=pid,
            name=raw.get("name", pid),
            scope=_as_scope(raw.get("scope")),
            value=raw.get("value"),
            evidence_level=level,
            state=raw.get("state", "to_verify"),
            gate_eligible=bool(raw.get("gate_eligible")),
            unverified_claim=raw.get("unverified_claim"),
            source=raw.get("source") or {},
            review_cycle=raw.get("review_cycle"),
            next_review_at=raw.get("next_review_at"),
            task_ref=raw.get("task_ref"),
            domain=raw.get("domain"),
            intended_level=raw.get("intended_level"),
            raw=raw,
        )


@dataclass
class Rule:
    id: str
    name: str
    layer: str
    gate: str
    level: str
    action: str
    condition: str
    depends_on: list[str]
    mode: str | None = None
    evidence_level: str | None = None
    on_degrade: str | None = None
    result: str | None = None
    advisory_only: bool = False
    reads_unverified_claim: bool = False
    priority: str | None = None
    message: str = ""
    target: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Rule":
        rid = raw.get("id")
        if not rid:
            raise SpecError("规则缺少 id")
        if raw.get("level") not in ("hard", "soft", "advisory"):
            raise SpecError("%s: level 非法 %r" % (rid, raw.get("level")))
        return cls(
            id=rid,
            name=raw.get("name", rid),
            layer=raw.get("layer", "platform"),
            gate=raw.get("gate", ""),
            level=raw["level"],
            action=raw.get("action", "WARN"),
            condition=raw.get("condition", "True"),
            depends_on=list(raw.get("depends_on") or []),
            mode=raw.get("mode"),
            evidence_level=raw.get("evidence_level"),
            on_degrade=raw.get("on_degrade"),
            result=raw.get("result"),
            advisory_only=bool(raw.get("advisory_only")),
            reads_unverified_claim=bool(raw.get("reads_unverified_claim")),
            priority=raw.get("priority"),
            message=raw.get("message", ""),
            target=raw.get("target"),
            raw=raw,
        )


class Spec:
    """一次性加载整份配置，之后只读。"""

    def __init__(self, root: Path | str = DEFAULT_SPEC_ROOT):
        self.root = Path(root)
        self.registry: dict[str, Any] = {}
        self.governance: dict[str, Any] = {}
        self.entities: dict[str, Any] = {}
        self.immutable_entities: set[str] = set()
        self.params: dict[str, Param] = {}
        self.rules: dict[str, Rule] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.modes: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.problems: list[str] = []

    # ---- 加载 -----------------------------------------------------------
    @classmethod
    def load(cls, root: Path | str = DEFAULT_SPEC_ROOT) -> "Spec":
        spec = cls(root)
        spec._load()
        return spec

    def _read(self, rel: str) -> dict[str, Any]:
        path = self.root / rel
        if not path.exists():
            raise SpecError("load_order 中的文件不存在: %s" % rel)
        with path.open(encoding="utf-8") as handle:
            try:
                return json.load(handle)
            except ValueError as exc:
                raise SpecError("JSON 解析失败 %s -> %s" % (rel, exc)) from exc

    def _load(self) -> None:
        if not (self.root / "registry.json").exists():
            raise SpecError("找不到 %s" % (self.root / "registry.json"))
        self.registry = self._read("registry.json")
        self.files["registry.json"] = self.registry

        order = (self.registry.get("project") or {}).get("load_order") or []
        if not order:
            raise SpecError("registry.project.load_order 为空")

        for rel in order:
            if rel == "registry.json":
                continue
            doc = self._read(rel)
            self.files[rel] = doc
            layer = doc.get("layer")

            if layer == "param":
                for raw in doc.get("params") or []:
                    self._add_param(raw, rel)
            elif layer == "mode":
                for raw in doc.get("mode_params") or []:
                    self._add_param(raw, rel)
                self.modes[doc.get("id")] = doc
            elif layer == "rule":
                for raw in doc.get("rules") or []:
                    self._add_rule(raw, rel)
            elif layer == "entities":
                for ent in doc.get("entities") or []:
                    self.entities[ent["id"]] = ent
                self.immutable_entities = set(doc.get("immutable_entities") or [])
            elif layer == "governance":
                self.governance = doc
            elif layer == "evidence":
                for task in doc.get("tasks") or []:
                    self.tasks[task["id"]] = task

        self._self_check()

    def _add_param(self, raw: dict[str, Any], rel: str) -> None:
        param = Param.from_dict(raw)
        if param.id in self.params:
            raise SpecError("参数 id 重复: %s (%s)" % (param.id, rel))
        self.params[param.id] = param

    def _add_rule(self, raw: dict[str, Any], rel: str) -> None:
        rule = Rule.from_dict(raw)
        if rule.id in self.rules:
            raise SpecError("规则 id 重复: %s (%s)" % (rule.id, rel))
        self.rules[rule.id] = rule

    def _self_check(self) -> None:
        """加载期自检：把 INV-001/002 提前到启动时炸出来。"""
        for rule in self.rules.values():
            for dep in rule.depends_on:
                param = self.params.get(dep)
                if param is None:
                    self.problems.append("%s: 依赖的参数不存在 %s" % (rule.id, dep))
                    continue
                if rule.level == "hard" and not param.hard_eligible() and not rule.on_degrade:
                    self.problems.append(
                        "INV-001: 硬规则 %s 依赖 %s 级参数 %s 且未声明 on_degrade"
                        % (rule.id, param.evidence_level, dep)
                    )
        for task in self.tasks.values():
            target = task.get("target_param_id")
            if target and target not in self.params:
                self.problems.append("%s: target_param_id 不存在 %s" % (task["id"], target))
            for extra in task.get("also_targets") or []:
                if extra not in self.params:
                    self.problems.append("%s: also_targets 不存在 %s" % (task["id"], extra))

    # ---- 查询 -----------------------------------------------------------
    def param(self, param_id: str) -> Param:
        try:
            return self.params[param_id]
        except KeyError as exc:
            raise SpecError("未知参数: %s" % param_id) from exc

    def resolve(self, param_id: str, *, platform="*", market="*", mode="*") -> Param:
        """按 scope 取参数，并校验它确实适用于该组合（INV-012 的运行时对应物）。"""
        param = self.param(param_id)
        want = {"platform": platform, "market": market, "mode": mode}
        if not _applies(param.scope, want):
            raise SpecError(
                "参数 %s 的 scope %s 不适用于 %s" % (param_id, param.scope, want)
            )
        return param

    def value_of(self, param_id: str, *, platform="*", market="*", mode="*") -> Any:
        return self.resolve(param_id, platform=platform, market=market, mode=mode).value

    def for_scope(self, *, platform="*", market="*", mode="*") -> dict[str, Param]:
        want = {"platform": platform, "market": market, "mode": mode}
        return {pid: p for pid, p in self.params.items() if _applies(p.scope, want)}

    def payload(self, *, platform="*", market="*", mode="*", today: str | None = None) -> dict[str, Any]:
        """给表达式求值器的参数命名空间。"""
        return {
            pid: param.public(today)
            for pid, param in self.for_scope(platform=platform, market=market, mode=mode).items()
        }

    def rules_for_gate(self, gate: str, mode: str | None = None) -> list[Rule]:
        picks = []
        for rule in self.rules.values():
            if rule.gate != gate:
                continue
            if rule.layer == "mode":
                if mode and rule.mode != mode:
                    continue
            picks.append(rule)
        return sorted(picks, key=lambda item: item.id)

    def mode(self, mode_id: str) -> dict[str, Any]:
        try:
            return self.modes[mode_id]
        except KeyError as exc:
            raise SpecError("未知履约模式: %s" % mode_id) from exc

    def market(self, code: str) -> dict[str, Any]:
        for item in self.registry.get("markets") or []:
            if item.get("code") == code:
                return item
        raise SpecError("未知市场: %s" % code)

    def summary(self) -> str:
        return "params=%d rules=%d tasks=%d modes=%d markets=%d entities=%d" % (
            len(self.params),
            len(self.rules),
            len(self.tasks),
            len(self.modes),
            len(self.registry.get("markets") or []),
            len(self.entities),
        )

    def with_overrides(self, overrides: Iterable[dict[str, Any]]) -> "Spec":
        """返回合并了覆盖层的新 Spec。

        覆盖层（ParamOverride 表）是「参数升级路径」的载体：
        用户在卖家中心抄到真实费率后写进覆盖层，而不是复制 spec 的制度性参数（INV-009）。
        """
        rows = [row for row in overrides if row.get("param_id")]
        if not rows:
            return self
        # 覆盖层只增不改：同一参数可能有多条历史记录，按 id 升序应用 → 最新的覆盖生效
        rows.sort(key=lambda row: row.get("id") or 0)
        clone = copy.copy(self)
        clone.params = dict(self.params)
        for row in rows:
            base = clone.params.get(row["param_id"])
            if base is None:
                continue  # 未知参数忽略，避免脏数据污染
            clone.params[row["param_id"]] = base.overridden(row)
        return clone


_CACHE: dict[str, Spec] = {}


def default_spec(root: Path | str | None = None) -> Spec:
    """进程内缓存的默认 Spec。适配器（veto/desk/web）用它，避免每次调用都读盘。"""
    key = str(Path(root) if root else DEFAULT_SPEC_ROOT)
    if key not in _CACHE:
        _CACHE[key] = Spec.load(root or DEFAULT_SPEC_ROOT)
    return _CACHE[key]
