#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能卖家后台 · spec 分层校验器 (v4.0)
=====================================

校验 spec/ 下全部声明式配置：
  registry.json  entities.json  governance.json
  params/*.json  rules/*.json  rules/modes/*.json  modes/*.json  evidence/*.json

覆盖内容：
  1. 文件存在性与 JSON 可解析
  2. ID 唯一性与引用完整性（rule→param / task→param / module→entity / mode→rule）
  3. 验收不变量 INV-001 ~ INV-013 中可静态检查的部分
  4. 分层一致性（INV-012）：market.<code>.json 内的参数不得声明别的市场
  5. 模式边界（INV-013 / R-GOV-004）：模式文件不得自建注册表/成本模型/门禁
  6. 迁移路标检查：manual-core.json 不得再被引用

用法（PowerShell，注意带引号的 exe 路径必须以 & 开头）：
  & "<python.exe>" "<本文件路径>"

退出码：0 = 通过（可能有警告），1 = 存在错误。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

LEVELS = ("A", "B", "C", "D", "E")
HARD_OK = ("A", "B", "C")
RULE_LEVELS = ("hard", "soft", "advisory")
RULE_RESULTS = ("PASS", "INCOMPLETE", "WARN", "REJECT")
MATCH_KINDS = ("exact", "partial", "contextual")

errors = []
warnings = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# 核实覆盖层（spec/verified.json，随 git 走）。真值分两层：spec 文件是制度性的，
# 覆盖层是核实成果。查"有没有证据"时必须看两层，否则会误报——实测 11 条
# "A 级无快照"里大部分参数其实已有快照，只是记在覆盖层。
def load_verified():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verified.json")
    if not os.path.exists(path):
        return {}
    try:
        return load(path).get("params") or {}
    except (ValueError, OSError):
        return {}


verified = load_verified()


# ---------------------------------------------------------------- 收集

class Bag(object):
    def __init__(self):
        self.params = {}
        self.rules = {}
        self.tasks = {}
        self.entities = {}
        self.docs = {}


def add_param(bag, param, rel, file_scope):
    pid = param.get("id")
    if not pid:
        err("%s: 存在没有 id 的参数" % rel)
        return
    if pid in bag.params:
        err("参数 id 重复: %s (%s 与 %s)" % (pid, bag.params[pid][1], rel))
    bag.params[pid] = (param, rel)
    check_param(bag, param, rel, file_scope)


def add_rule(bag, rule, rel):
    rid = rule.get("id")
    if not rid:
        err("%s: 存在没有 id 的规则" % rel)
        return
    if rid in bag.rules:
        err("规则 id 重复: %s (%s 与 %s)" % (rid, bag.rules[rid][1], rel))
    bag.rules[rid] = (rule, rel)
    check_rule(bag, rule, rel)


def check_param(bag, param, rel, file_scope):
    pid = param.get("id")
    level = param.get("evidence_level")

    if level not in LEVELS:
        err("%s: evidence_level 非法 (%r)" % (pid, level))
        return

    # INV-002：E 级 value 必须为 null
    if level == "E" and param.get("value") is not None:
        err("INV-002 违反: %s 为 E 级但 value 不为 null" % pid)

    # A/B 级必须有来源与查询日期
    source = param.get("source") or {}
    if level in ("A", "B"):
        if not source.get("url"):
            err("%s: %s 级但缺少 source.url" % (pid, level))
        if not source.get("checked_at"):
            err("%s: %s 级但缺少 source.checked_at" % (pid, level))
        if not source.get("recheck_status"):
            err("%s: %s 级但缺少 source.recheck_status" % (pid, level))

    # INV-003：A 级必须有快照（corroboration 不能替代）
    #
    # 两层都查：spec 文件里的 source.snapshot_ref，或覆盖层里的 snapshot_ref。
    # 只看 spec 会误报——很多 A 级参数的快照是无头浏览器抓的，记在覆盖层。
    snapshot = source.get("snapshot_ref") or (verified.get(pid) or {}).get("snapshot_ref")
    if level == "A" and not snapshot:
        warn("INV-003 待补: %s 为 A 级但 snapshot_ref 为空（spec 与 verified.json 里都没有）" % pid)

    # 交叉验证条目格式
    for item in param.get("corroboration") or []:
        if not item.get("url"):
            err("%s: corroboration 缺少 url" % pid)
        if not item.get("checked_at"):
            err("%s: corroboration 缺少 checked_at" % pid)
        if item.get("match") not in MATCH_KINDS:
            err("%s: corroboration.match 非法 (%r)" % (pid, item.get("match")))

    if not param.get("state"):
        err("%s: 缺少 state" % pid)
    if not param.get("review_cycle"):
        err("%s: 缺少 review_cycle" % pid)

    # 门禁资格一致性
    if param.get("gate_eligible") is True and level not in HARD_OK:
        err("%s: %s 级却被标记 gate_eligible=true" % (pid, level))

    # INV-012：分层一致性
    scope = param.get("scope") or {}
    if not scope:
        err("%s: 缺少 scope" % pid)
    for dim, want in (file_scope or {}).items():
        if dim not in ("platform", "market", "mode"):
            continue
        if not want or want == "*":
            continue
        got = scope.get(dim)
        if got not in (None, "*", want):
            err("INV-012 违反: %s 在 %s（file_scope.%s=%s）中却声明 %s=%s"
                % (pid, rel, dim, want, dim, got))


def check_rule(bag, rule, rel):
    rid = rule.get("id")
    level = rule.get("level")

    if level not in RULE_LEVELS:
        err("%s: level 非法 (%r)" % (rid, level))
    if not rule.get("action"):
        err("%s: 缺少 action" % rid)
    if rule.get("result") and rule["result"] not in RULE_RESULTS:
        err("%s: result 非法 (%r)" % (rid, rule["result"]))

    deps = rule.get("depends_on")
    if deps is None:
        err("%s: 缺少 depends_on" % rid)
        deps = []

    # INV-001：硬门禁依赖必须是 A/B/C
    if level == "hard":
        for dep in deps:
            entry = bag.params.get(dep)
            if entry is None:
                continue  # 引用完整性在后面的 passes 里统一报
            # 等级要看**核实后的视图**：参数可能已在覆盖层里升到 A/B/C，
            # 只看 spec 会把"已经修好的降级"继续报出来（实测 4 条误报）。
            dep_level = ((verified.get(dep) or {}).get("evidence_level")
                         or entry[0].get("evidence_level"))
            if dep_level not in HARD_OK:
                if not rule.get("on_degrade"):
                    err("INV-001 违反: 硬规则 %s 依赖 %s 级参数 %s，且未声明 on_degrade"
                        % (rid, dep_level, dep))
                else:
                    warn("INV-001 降级声明: 硬规则 %s 依赖 %s 级参数 %s（已声明 on_degrade）"
                         % (rid, dep_level, dep))

    # INV-005：仅依赖 E 级参数的规则不得输出 REJECT
    if rule.get("result") == "REJECT":
        levels = [bag.params[d][0].get("evidence_level") for d in deps if d in bag.params]
        if levels and all(lv == "E" for lv in levels):
            err("INV-005 违反: %s 仅依赖 E 级参数却输出 REJECT" % rid)

    # INV-011：读取 unverified_claim 的规则必须 advisory 且非硬
    if rule.get("reads_unverified_claim"):
        if level == "hard":
            err("INV-011 违反: %s 读取 unverified_claim 但 level=hard" % rid)
        if rule.get("advisory_only") is not True:
            err("INV-011 违反: %s 读取 unverified_claim 但未标记 advisory_only" % rid)
        if rule.get("result") not in (None, "WARN", "INCOMPLETE"):
            err("INV-011 违反: %s 读取 unverified_claim 但 result=%r" % (rid, rule.get("result")))


# ---------------------------------------------------------------- 主流程

def main():
    registry_path = os.path.join(HERE, "registry.json")
    print("== 智能卖家后台 spec 校验 (v4.0) ==")
    print("root: %s" % HERE)

    if not os.path.exists(registry_path):
        print("FAIL: 找不到 registry.json")
        return 1
    try:
        registry = load(registry_path)
    except ValueError as exc:
        print("FAIL: registry.json 解析失败 -> %s" % exc)
        return 1

    order = (registry.get("project") or {}).get("load_order") or []
    if not order:
        err("registry.project.load_order 为空")

    bag = Bag()
    bag.docs["registry.json"] = registry

    for rel in order:
        if rel == "registry.json":
            continue
        path = os.path.join(HERE, rel)
        if not os.path.exists(path):
            err("load_order 中的文件不存在: %s" % rel)
            continue
        try:
            doc = load(path)
        except ValueError as exc:
            err("JSON 解析失败 %s -> %s" % (rel, exc))
            continue
        bag.docs[rel] = doc

        layer = doc.get("layer")
        fscope = doc.get("file_scope") or {}

        if layer == "param":
            for param in doc.get("params") or []:
                add_param(bag, param, rel, fscope)
        elif layer == "mode":
            for param in doc.get("mode_params") or []:
                add_param(bag, param, rel, fscope)
            # INV-013 / R-GOV-004：模式不得自建注册表、成本模型、门禁
            for forbidden in ("params_file", "cost_model", "gate_service", "evidence_engine"):
                if forbidden in doc:
                    err("INV-013 违反: 模式文件 %s 定义了 %s（模式只能有 mode_params 与 owns 能力）" % (rel, forbidden))
        elif layer == "rule":
            for rule in doc.get("rules") or []:
                add_rule(bag, rule, rel)
        elif layer == "entities":
            for entity in doc.get("entities") or []:
                eid = entity.get("id")
                if not eid:
                    err("%s: 存在没有 id 的实体" % rel)
                    continue
                if eid in bag.entities:
                    err("实体 id 重复: %s" % eid)
                bag.entities[eid] = entity
        elif layer == "governance":
            pass  # 治理文件在下方单独取用（invariants / 状态机 / 告警 / KPI）
        elif layer == "evidence":
            for task in doc.get("tasks") or []:
                tid = task.get("id")
                if not tid:
                    err("%s: 存在没有 id 的核实任务" % rel)
                    continue
                if tid in bag.tasks:
                    err("任务 id 重复: %s" % tid)
                bag.tasks[tid] = task
        else:
            warn("%s: 未知 layer (%r)" % (rel, layer))

    # ---- 引用完整性
    for rid, (rule, rel) in sorted(bag.rules.items()):
        for dep in rule.get("depends_on") or []:
            if dep not in bag.params:
                err("%s: 依赖的参数不存在: %s" % (rid, dep))
        if rule.get("layer") == "mode" and not rule.get("mode"):
            err("%s: layer=mode 但缺少 mode 字段" % rid)

    for tid, task in sorted(bag.tasks.items()):
        target = task.get("target_param_id")
        if target and target not in bag.params:
            err("%s: target_param_id 不存在: %s" % (tid, target))
        for extra in task.get("also_targets") or []:
            if extra not in bag.params:
                err("%s: also_targets 中的参数不存在: %s" % (tid, extra))
        if task.get("blocks_first_order") not in (True, False):
            err("%s: 缺少 blocks_first_order" % tid)
        if not task.get("status"):
            err("%s: 缺少 status" % tid)

    # INV-014：参数 task_ref 与任务 target_param_id / also_targets 必须双向一致
    task_targets = {}
    for tid, task in bag.tasks.items():
        targets = []
        if task.get("target_param_id"):
            targets.append(task.get("target_param_id"))
        targets.extend(task.get("also_targets") or [])
        task_targets[tid] = targets
    for pid, (param, rel) in sorted(bag.params.items()):
        ref = param.get("task_ref")
        if not ref:
            continue
        if ref not in bag.tasks:
            err("INV-014 违反: %s.task_ref 指向不存在的任务 %s" % (pid, ref))
        elif pid not in task_targets.get(ref, []):
            err("INV-014 违反: %s.task_ref=%s，但该任务的 target_param_id/also_targets 都未包含 %s"
                % (pid, ref, pid))

    for mid, module in sorted((m.get("id"), m) for m in (registry.get("modules") or [])):
        if not mid:
            err("modules 中存在没有 id 的条目")
            continue
        for eid in module.get("entities") or []:
            if eid not in bag.entities:
                err("模块 %s 引用了不存在的实体: %s" % (mid, eid))

    for mode in registry.get("fulfillment_modes") or []:
        mode_id = mode.get("id")
        spec_file = mode.get("spec_file")
        mode_doc = bag.docs.get(spec_file) if spec_file else None
        if spec_file and mode_doc is None:
            err("模式 %s 的 spec_file 未加载: %s" % (mode_id, spec_file))
            continue
        if mode_doc:
            if mode_doc.get("id") != mode_id:
                err("模式文件 %s 的 id (%s) 与 registry 不一致 (%s)" % (spec_file, mode_doc.get("id"), mode_id))
            for eid in mode_doc.get("entities") or []:
                if eid not in bag.entities:
                    err("模式 %s 引用了不存在的实体: %s" % (mode_id, eid))
            for rid in mode_doc.get("own_rules") or []:
                if rid not in bag.rules:
                    err("模式 %s 引用了不存在的规则: %s" % (mode_id, rid))
            for rid in mode_doc.get("own_rules") or []:
                entry = bag.rules.get(rid)
                if entry and entry[0].get("mode") not in (None, mode_id):
                    err("模式 %s 声明了属于 %s 的规则: %s" % (mode_id, entry[0].get("mode"), rid))

    for market in registry.get("markets") or []:
        code = market.get("code")
        pfile = market.get("params_file")
        if pfile and pfile not in bag.docs:
            err("市场 %s 的 params_file 未加载: %s" % (code, pfile))
        if market.get("dropship_viability") not in ("viable", "degraded", "not_viable", "unknown"):
            err("市场 %s: dropship_viability 非法 (%r)" % (code, market.get("dropship_viability")))

    # ---- 不变量与阻断清单
    gov = bag.docs.get("governance.json") or {}
    declared = [item.get("id") for item in gov.get("invariants") or []]
    for index in range(1, 15):
        want = "INV-%03d" % index
        if want not in declared:
            err("governance.invariants 缺少 %s" % want)

    ev = bag.docs.get("evidence/verification-tasks.json") or {}
    declared_blocking = set(ev.get("first_order_blocking_tasks") or [])
    actual_blocking = set(tid for tid, t in bag.tasks.items() if t.get("blocks_first_order") is True)
    for tid in sorted(actual_blocking - declared_blocking):
        err("first_order_blocking_tasks 漏列: %s" % tid)
    for tid in sorted(declared_blocking - actual_blocking):
        err("first_order_blocking_tasks 多列: %s" % tid)

    # ---- 迁移路标
    tombstone = os.path.join(HERE, "manual-core.json")
    if os.path.exists(tombstone):
        try:
            marker = load(tombstone)
            if not marker.get("deprecated"):
                err("manual-core.json 仍存在且未标记 deprecated")
            else:
                warn("manual-core.json 仍在（已标记 deprecated，建议删除）")
        except ValueError:
            err("manual-core.json 解析失败（应删除或保持为合法 JSON 路标）")

    # ---- 汇总
    cross = [pid for pid, (p, _) in bag.params.items() if p.get("verification_status") == "cross_verified"]
    modes_active = [m.get("id") for m in (registry.get("fulfillment_modes") or []) if m.get("status") == "active"]

    print("files loaded : %d" % len(bag.docs))
    print("counts       : params=%d rules=%d tasks=%d entities=%d invariants=%d modules=%d modes=%d markets=%d"
          % (len(bag.params), len(bag.rules), len(bag.tasks), len(bag.entities),
             len(declared), len(registry.get("modules") or []),
             len(registry.get("fulfillment_modes") or []), len(registry.get("markets") or [])))
    print("hard rules   : %d / soft %d / advisory %d"
          % (len([1 for r, _ in bag.rules.values() if r.get("level") == "hard"]),
             len([1 for r, _ in bag.rules.values() if r.get("level") == "soft"]),
             len([1 for r, _ in bag.rules.values() if r.get("level") == "advisory"])))
    print("active mode  : %s" % (", ".join(modes_active) or "-"))
    print("cross-verified (%d): %s" % (len(cross), ", ".join(sorted(cross)) or "-"))
    print("first-order blocking tasks (%d): %s"
          % (len(declared_blocking), ", ".join(sorted(declared_blocking)) or "-"))

    print("")
    if warnings:
        print("WARN (%d):" % len(warnings))
        for item in warnings:
            print("  - %s" % item)
        print("")
    if errors:
        print("FAIL (%d):" % len(errors))
        for item in errors:
            print("  - %s" % item)
        return 1

    print("PASS: 分层结构、引用完整性与不变量检查通过"
          + ("（含 %d 条警告）" % len(warnings) if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
