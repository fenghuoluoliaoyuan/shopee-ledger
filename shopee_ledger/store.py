"""台账门面：数据落在 ``spec/entities.json`` 声明的实体表上，参数是 spec 的**覆盖层**。

收敛前的两份真相
----------------
v1.4 的 store.py 手写 7 张表 DDL，其中：
  * ``parameters`` 存 10 个费率 key —— 与 spec 的 52 个参数**重复**，且 grade 不受约束；
  * ``sls_bands``   存运费档 —— 与 ``P-*-SLS-TIERS`` **重复**。
两者都是"同一概念两处声明"，正是本轮要消除的东西。

收敛后的分工（单一事实来源）
----------------------------
* **制度性参数**（平台规则 / 市场政策 / 经验阈值）→ ``spec/params/*.json``，只读。
* **本店实测覆盖**（抄到的真实费率、汇率、运费档）→ ``ParamOverride`` 表，只允许 A/B/C 级。
* **业务数据**（供应商 / 候选品 / 订单 / 核实任务）→ ``entities.json`` 声明的实体表。
本模块只做门面：把 spec + 覆盖层 + 实体表拼成 CLI 与网页要的形状。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from shopee_ledger.cost_engine import COMPUTED, CostEngine, CostInputs, CostResult
from shopee_ledger.fulfillment import (
    Fulfillment,
    advance,
    arrange_ship,
    confirm_address_format,
    copy_address,
    mark_inbound,
    mark_purchased,
    mark_stock,
    order_context,
)
from shopee_ledger.gates import REJECT, GateResult, GateService
from shopee_ledger.profit import Decision
from shopee_ledger.spec import Spec, default_spec
from shopee_ledger.statemachine import OrderMachine
from shopee_ledger.storage import DEFAULT_DB, Storage
from shopee_ledger.veto import evaluate_flags, veto_reasons

PARAM_HELP = {
    "local_per_cny": "1 人民币折合多少当地币",
    "commission_rate": "佣金率，按商品价",
    "transaction_rate": "交易手续费率，按商品价",
    "service_fee_kind": "service / shipping / none",
    "service_fee_rate": "仅 kind=service 时按商品价计提",
    "withdrawal_rate": "提现费率，按商品价",
    "fx_loss_rate": "汇损率，按商品价",
    "affiliate_rate": "联盟佣金率，起步可记 0",
    "go_rate": "可做阈值",
    "watch_rate": "观察阈值",
}

# 旧 key → (spec 参数 id 模板, 结构化字段名)。模板为 None 表示运行期输入（RUNTIME: 前缀）。
OVERRIDE_MAP: dict[str, tuple[str | None, str | None]] = {
    "local_per_cny": ("P-{site}-FX", None),
    "commission_rate": ("P-{site}-COMMISSION", None),
    "transaction_rate": ("P-{site}-TXN-FEE", None),
    "withdrawal_rate": ("P-{site}-WITHDRAW", "reserve_at"),
    "fx_loss_rate": ("P-FX-LOSS", "reserve_at"),
    "go_rate": ("P-{site}-MARGIN-TH", "pass"),
    "watch_rate": ("P-{site}-MARGIN-TH", "warn"),
    "service_fee_kind": (None, None),
    "service_fee_rate": (None, None),
    "affiliate_rate": (None, None),
}


@dataclass
class QuoteView:
    """CLI / 网页要的兼容视图：旧字段 + 新的完整核算与门禁结果。"""

    decision: Decision
    rate: float | None
    net: float | None
    missing: list[str]
    cost: CostResult
    gate: GateResult | None = None

    # 旧 CLI/网页读的是这几个字段，从新的成分表里取，保持兼容
    @property
    def net_shipping(self) -> float | None:
        return self.cost.components.get("net_freight")

    @property
    def platform_fee(self) -> float | None:
        return self.cost.components.get("platform_fee")

    @property
    def return_reserve(self) -> float | None:
        return self.cost.components.get("return_reserve")

    def explain(self) -> str:
        text = self.cost.explain()
        if self.gate is not None:
            text += "\n" + self.gate.explain()
        return text


@dataclass
class ActualView:
    """托管实绩的映射与重算结果。算不出来时 rate/net 保持 None，不编数字。"""

    mapped: dict[str, Any]
    missing: list[str]
    rate: float | None = None
    net: float | None = None
    cost: CostResult | None = None

    def explain(self) -> str:
        parts = ["%s=%s" % (key, value) for key, value in self.mapped.items() if value is not None]
        text = "账单字段：" + "、".join(parts) if parts else "账单字段：无"
        if self.net is not None:
            text += "；实绩净利 %.2f（%.2f%%）" % (self.net, (self.rate or 0) * 100)
        elif self.cost is not None and self.cost.missing:
            text += "；实绩还算不出，缺 " + "、".join(self.cost.missing)
        if self.missing:
            text += "；service_fee 归属待确认（" + "、".join(self.missing) + "）"
        return text


class Ledger:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.storage = Storage(self.path)
        self.conn = None  # 兼容旧调用方对属名的探测
        self._spec: Spec | None = None

    # ---- 生命周期与参数 -------------------------------------------------
    @property
    def spec(self) -> Spec:
        if self._spec is None:
            base = default_spec()
            rows = self._overrides() if self._table_exists() else []
            self._spec = base.with_overrides(rows)
        return self._spec

    def _table_exists(self) -> bool:
        row = self.storage.connect().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='param_override'").fetchone()
        return row is not None

    def _overrides(self) -> list[dict[str, Any]]:
        return self.storage.list("ParamOverride", limit=1000)

    def init(self) -> None:
        self.storage.init()
        self._seed_tasks()
        self.storage.record_config_version()
        self._spec = None

    def close(self) -> None:
        self.storage.close()

    def _seed_tasks(self) -> None:
        """把 spec 的核实任务灌进表里（首次）。任务的唯一来源仍是 spec。"""
        if self.storage.count("VerificationTask"):
            return
        for task_id, task in sorted(default_spec().tasks.items()):
            self.storage.insert("VerificationTask", {
                "spec_task_id": task_id,
                "module": task.get("module", ""),
                "item": task.get("item", ""),
                "channel": task.get("channel", ""),
                "blocks_first_order": task.get("blocks_first_order"),
                "target_param_id": task.get("target_param_id"),
                "status": task.get("status", "todo"),
            })

    # ---- 参数（覆盖层） -------------------------------------------------
    def _write_override(self, param_id: str, value: Any, grade: str, *, source: str | None = None,
                        source_url: str | None = None, snapshot_ref: str | None = None,
                        checked_at: str | None = None) -> None:
        """写一条覆盖值。证据出处必须一起落库——只写在审计里，配置页就说不清依据。"""
        if grade not in ("A", "B", "C"):
            raise ValueError("覆盖层只接受 A/B/C 级（实测或后台抄录）；%s 级请走核实任务升级路径" % grade)
        param = self.spec.params.get(param_id)
        spec_url = (param.source.get("url") if param else None) or ""
        if grade == "A" and not (source_url or "").strip() and not spec_url:
            raise ValueError("A 级覆盖必须有可打开的 URL——没有凭据的 A 级等于自述")
        self.storage.insert("ParamOverride", {
            "param_id": param_id, "value": value, "evidence_level": grade,
            "operator": "cli", "note": source or "本机录入",
            "checked_at": checked_at or date.today().isoformat(),
            "source_url": source_url, "snapshot_ref": snapshot_ref,
        })
        self._spec = None

    def set_param(self, site: str, key: str, value: Any, grade: str) -> None:
        if key not in OVERRIDE_MAP:
            raise ValueError("未知参数: %s" % key)
        template, inner = OVERRIDE_MAP[key]
        parsed = _parse_value(value)
        if template is None:
            self._write_override("RUNTIME:%s:%s" % (site, key), parsed, grade, source="运行期输入")
            return
        param_id = template.format(site=site)
        base = self.spec.params.get(param_id)
        if base is None:
            raise ValueError(
                "%s 站没有参数 %s：制度性费率由 spec 提供，本机只能覆盖实测值" % (site, param_id))
        if inner is not None:
            current = dict(base.value) if isinstance(base.value, dict) else {}
            current[inner] = parsed
            parsed = current
        self._write_override(param_id, parsed, grade)

    def params(self, site: str) -> dict[str, Any]:
        """按旧 key 返回**生效值**（spec 值，被覆盖则取覆盖值）。"""
        spec = self.spec
        overrides = self._overrides()
        effective: dict[str, Any] = {}
        for key, (template, inner) in OVERRIDE_MAP.items():
            if template is None:
                rows = [r for r in overrides if r["param_id"] == "RUNTIME:%s:%s" % (site, key)]
                effective[key] = rows[0]["value"] if rows else None
                continue
            param = spec.params.get(template.format(site=site))
            if param is None:
                effective[key] = None
                continue
            value = param.value
            if inner is not None and isinstance(value, dict):
                value = value.get(inner)
            effective[key] = value
        return effective

    # ---- 供应商 ---------------------------------------------------------
    def add_supplier(self, name: str, url: str, years: int, dropship: bool,
                     address_ok: bool, pay: float | None) -> int:
        return self.storage.insert("Supplier", {
            "name": name, "url": url, "years_in_business": years,
            "supports_dropship": dropship,
            "accepts_warehouse_address_format": address_ok,
            "pay_cny": pay,
        })

    def list_suppliers(self) -> list[dict[str, Any]]:
        rows = self.storage.list("Supplier", limit=500)
        return [{
            "id": r["id"], "name": r["name"], "url": r["url"],
            "years": r["years_in_business"], "dropship": r["supports_dropship"],
            "address_ok": r["accepts_warehouse_address_format"], "pay": r["pay_cny"],
        } for r in rows]

    def link_supplier(self, candidate_id: int, supplier_id: int, primary: bool = False) -> None:
        self.storage.insert("CandidateSupplier", {
            "candidate_id": str(candidate_id), "supplier_id": str(supplier_id), "is_primary": primary})

    def supplier_ids(self, candidate_id: int) -> list[int]:
        rows = self.storage.list("CandidateSupplier", limit=200,
                                 where="candidate_id = ?", args=(str(candidate_id),))
        return [int(r["supplier_id"]) for r in rows]

    def supplier_address_ok(self, supplier_id: int) -> bool:
        row = self.storage.get("Supplier", supplier_id)
        return bool(row and row.get("accepts_warehouse_address_format"))

    # ---- 候选品 ---------------------------------------------------------
    def add_candidate(self, site: str, name: str, weight_g: float, purchase_cny: float,
                      domestic_cny: float, price: float, sls_fee: float,
                      intends_free_shipping: bool, measured: bool = False) -> int:
        return self.storage.insert("ProductCandidate", {
            "platform": "shopee", "market": site, "source_url": "local:%s" % name, "title": name,
            "category": "", "veto_flags": {}, "competitor_notes": {}, "state": "candidate",
            "weight_g": weight_g, "purchase_cny": purchase_cny, "domestic_cny": domestic_cny,
            "price_local": price, "sls_fee": sls_fee,
            "intends_free_shipping": intends_free_shipping,
            "weight_is_measured": measured,
        })

    def list_candidates(self) -> list[dict[str, Any]]:
        out = []
        for row in self.storage.list("ProductCandidate", limit=500):
            item = dict(row)
            item["name"] = row.get("title")
            item["site"] = row.get("market")
            item["price"] = row.get("price_local")
            item["title_text"] = row.get("title_text") or ""
            item["flags"] = row.get("flags") or ""
            item["supplier_count"] = len(self.supplier_ids(row["id"]))
            out.append(item)
        return out

    def _candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return self.storage.get("ProductCandidate", candidate_id)

    def set_flags(self, candidate_id: int, flags: list[str]) -> list[str]:
        result = evaluate_flags(flags, self.spec)
        self.storage.update("ProductCandidate", candidate_id,
                            {"flags": ",".join(flags), "veto_flags": {"flags": flags}})
        self.storage.record_audit("candidate.flags", "candidate", candidate_id,
                                  result=result["result"],
                                  detail={"flags": flags, "advisories": result["advisories"]})
        return result["blocking"]

    def set_return_rate(self, candidate_id: int, rate: float) -> None:
        self.storage.update("ProductCandidate", candidate_id, {"return_rate": rate})

    def set_ads(self, candidate_id: int, ads: float) -> None:
        self.storage.update("ProductCandidate", candidate_id, {"ads": ads})

    def set_listing(self, candidate_id: int, sample_bought: bool, photo_ready: bool,
                    detail_ready: bool, title_text: str) -> None:
        self.storage.update("ProductCandidate", candidate_id, {
            "sample_bought": sample_bought, "photo_ready": photo_ready,
            "detail_ready": detail_ready, "title_text": title_text,
        })

    def set_measured(self, candidate_id: int, measured: bool,
                     weight_g: float | None = None) -> None:
        """记下"这个重量是称出来的"。上架前录的都是估算，属 D 级——不是实测。"""
        payload: dict[str, Any] = {"weight_is_measured": measured}
        if weight_g is not None:
            payload["weight_g"] = weight_g
        self.storage.update("ProductCandidate", candidate_id, payload)

    # ---- 核算 -----------------------------------------------------------
    def quote(self, candidate_id: int) -> QuoteView:
        row = self._candidate(candidate_id)
        if row is None:
            raise ValueError("找不到候选品 %s" % candidate_id)
        site = row.get("market") or "TW"
        spec = self.spec
        params = self.params(site)
        cost = CostEngine(spec).quote(CostInputs(
            market=site,
            price_local=row.get("price_local"),
            purchase_cny=row.get("purchase_cny"),
            domestic_cny=row.get("domestic_cny"),
            local_per_cny=_as_float(params.get("local_per_cny")),
            sls_freight=row.get("sls_fee"),
            buyer_paid_freight=row.get("buyer_paid_freight"),
            seller_pays_freight=bool(row.get("intends_free_shipping")),
            ad_spend=_as_float(row.get("ads")) or 0.0,
            return_rate=row.get("return_rate"),
        ))
        gate = None
        decision = Decision.INCOMPLETE
        if cost.computed:
            self.storage.save_cost_snapshot(cost, subject_type="candidate",
                                            subject_id=candidate_id, market=site)
            gate = GateService(spec).check("G3", cost.gate_context(), platform="shopee", market=site)
            decision = self._decision(cost, gate)
        return QuoteView(decision=decision, rate=cost.rate, net=cost.net,
                         missing=list(cost.missing), cost=cost, gate=gate)

    def _decision(self, cost: CostResult, gate: GateResult) -> Decision:
        threshold = self.spec.params.get("P-TW-MARGIN-TH")
        if threshold is None or not isinstance(threshold.value, dict):
            return Decision.THRESHOLD_UNSET
        if gate.result == REJECT:
            return Decision.CUT
        rate = cost.rate or 0.0
        if rate >= threshold.value.get("pass", 0.15):
            return Decision.GO
        if rate >= threshold.value.get("warn", 0.10):
            return Decision.WATCH
        return Decision.CUT

    def survival(self, site: str) -> tuple[int, int, float | None]:
        """存活率：录完数据且达到可做阈值的候选占比；否决品与未录完的不进分母。"""
        passed = complete = 0
        for row in self.list_candidates():
            if row.get("market") != site:
                continue
            flags = [f for f in (row.get("flags") or "").split(",") if f]
            if veto_reasons(flags, self.spec):
                continue
            view = self.quote(row["id"])
            if view.cost.status != COMPUTED:
                continue
            complete += 1
            if view.decision == Decision.GO:
                passed += 1
        rate = (passed / complete) if complete else None
        return passed, complete, rate

    # ---- 运费档（收敛到 P-*-SLS-TIERS） --------------------------------
    def add_band(self, site: str, max_g: float, fee: float) -> None:
        param_id = "P-%s-SLS-TIERS" % site
        bands = [b for b in self.bands(site) if b["max_g"] != max_g]
        bands.append({"max_g": max_g, "fee": fee})
        bands.sort(key=lambda b: b["max_g"])
        self._write_override(param_id, {"tiers": bands}, "C", source="卖家中心抄录")

    def bands(self, site: str) -> list[dict[str, float]]:
        param = self.spec.params.get("P-%s-SLS-TIERS" % site)
        value = param.value if param else None
        return list(value.get("tiers") or []) if isinstance(value, dict) else []

    def fee_for_weight(self, site: str, weight_g: float) -> float | None:
        for band in self.bands(site):
            if weight_g <= band["max_g"]:
                return band["fee"]
        return None

    # ---- 订单（状态机 + 守卫规则） --------------------------------------
    def _gates(self) -> GateService:
        return GateService(self.spec)

    def _order_context(self, state: Fulfillment) -> dict[str, Any]:
        """守卫规则的上下文由 fulfillment 统一提供，store 不再自己映射一遍。"""
        return order_context(state)

    def open_order(self, candidate_id: int) -> int:
        candidate = self._candidate(candidate_id) or {}
        view = self.quote(candidate_id)  # 把下单时的估算冻住，之后改参数不影响这一单
        frozen = view.cost.computed
        return self.storage.insert("Order", {
            "platform": "shopee", "market": candidate.get("market", "TW"),
            "mode": "dropship", "listing_id": str(candidate_id), "candidate_id": str(candidate_id),
            "state": "created", "steps": "", "block_reason": "", "created_at": _now(),
            "estimate_net": view.net if frozen else None,
            "estimate_rate": view.rate if frozen else None,
        })

    def load_order(self, order_id: int) -> Fulfillment:
        row = self.storage.get("Order", order_id)
        if row is None:
            raise ValueError("找不到订单 %s" % order_id)
        state = Fulfillment(machine=OrderMachine(self.spec, state=row.get("state") or None))
        state.supplier_id = int(row["supplier_id"]) if row.get("supplier_id") else None
        state.warehouse_address = row.get("warehouse_address") or ""
        state.address_source = row.get("address_source") or ""
        state.block_reason = row.get("block_reason") or ""
        return state

    def save_order(self, order_id: int, state: Fulfillment) -> None:
        """落盘同时把状态机**本次走过的每一步**记进审计。

        放在唯一出口，而不是每个调用点各记一遍。按 machine.history 逐条记（不是对比前后状态）：
        order-inbound 一次会走"供应商发货→到仓扫描"两步，只记净变化的话，
        审计看上去就像从 po_created 直接跳到了 warehouse_scanned——恰恰制造了"疑似跳步"的假象。
        """
        self.storage.update("Order", order_id, {
            "state": state.state,
            "steps": ",".join(name for name, done in state.steps.items() if done),
            "supplier_id": str(state.supplier_id) if state.supplier_id else None,
            "warehouse_address": state.warehouse_address,
            "address_source": state.address_source,
            "block_reason": state.block_reason,
        })
        for record in state.machine.history:
            self.storage.record_audit("order.transition", "order", order_id, result="PASS",
                                      detail=dict(record))

    def advance_order(self, order_id: int, to_state: str) -> Fulfillment:
        """通用推进：顺序不对报错，守卫规则不过也报错。留痕由 save_order 统一负责。"""
        state = self.load_order(order_id)
        record = advance(state, to_state, gates=self._gates(), context=self._order_context(state))
        self.save_order(order_id, state)
        self.storage.record_audit("order.guard", "order", order_id, result="PASS",
                                  detail=dict(record, rules=state.machine.transition_rules(to_state)))
        return state

    def order_next(self, order_id: int) -> list[str]:
        return self.load_order(order_id).next_states()

    def apply_stock(self, order_id: int, supplier_id: int, in_stock: bool, exhausted: bool) -> Fulfillment:
        state = self.load_order(order_id)
        mark_stock(state, supplier_id, in_stock, self.supplier_address_ok(supplier_id), exhausted)
        self.save_order(order_id, state)
        self.audit(order_id, "order.stock", result="WARN" if exhausted else "PASS",
                   detail={"in_stock": in_stock, "exhausted": exhausted, "state": state.state})
        return state

    def _step(self, order_id: int, action: str, func) -> Fulfillment:
        state = self.load_order(order_id)
        func(state, gates=self._gates(), context=self._order_context(state))
        self.save_order(order_id, state)
        self.audit(order_id, action, detail={"state": state.state})
        return state

    def apply_confirm_address(self, order_id: int) -> Fulfillment:
        state = self.load_order(order_id)
        confirm_address_format(state)
        self.save_order(order_id, state)
        self.audit(order_id, "order.confirm_address", detail={"state": state.state})
        return state

    def apply_arrange(self, order_id: int) -> Fulfillment:
        return self._step(order_id, "order.arrange_ship", arrange_ship)

    def apply_copy(self, order_id: int, address: str) -> Fulfillment:
        state = self.load_order(order_id)
        copy_address(state, address, "order_page",
                     gates=self._gates(), context=self._order_context(state))
        self.save_order(order_id, state)
        self.audit(order_id, "order.copy_address",
                   detail={"address_len": len(address), "state": state.state})
        return state

    def apply_purchase(self, order_id: int) -> Fulfillment:
        return self._step(order_id, "order.purchase", mark_purchased)

    def apply_inbound(self, order_id: int) -> Fulfillment:
        state = self.load_order(order_id)
        mark_inbound(state)
        self.save_order(order_id, state)
        self.audit(order_id, "order.inbound", detail={"state": state.state})
        return state

    def set_deadline(self, order_id: int, deadline: str) -> None:
        self.storage.update("Order", order_id, {"deadline": deadline})

    def record_actual(self, order_id: int, payload: dict[str, Any]) -> "ActualView":
        """用**账单实付**重算这一单，与下单时冻住的估算对照。

        账单里的佣金/手续费直接作为 CostInputs 的 actual_* 输入覆盖费率估算——
        所以不会出现第二份公式（这个模块刚消除过这类重复）。
        service_fee 归属未定时不给"实绩净利"，因为那会含一个未核实项。
        """
        from shopee_ledger.escrow import map_escrow

        row = self.storage.get("Order", order_id)
        if row is None:
            raise ValueError("找不到订单 %s" % order_id)
        mapped = map_escrow(payload)
        candidate = self._candidate(int(row["candidate_id"])) if row.get("candidate_id") else {}
        site = row.get("market") or "TW"
        params = self.params(site)
        cost = CostEngine(self.spec).quote(CostInputs(
            market=site,
            price_local=candidate.get("price_local"),
            purchase_cny=candidate.get("purchase_cny"),
            domestic_cny=candidate.get("domestic_cny"),
            local_per_cny=_as_float(params.get("local_per_cny")),
            sls_freight=mapped.get("sls_fee") or candidate.get("sls_fee"),
            buyer_paid_freight=mapped.get("buyer_shipping"),
            seller_pays_freight=bool(candidate.get("intends_free_shipping")),
            ad_spend=_as_float(candidate.get("ads")) or 0.0,
            return_rate=candidate.get("return_rate"),
            actual_commission=mapped.get("commission"),
            actual_txn_fee=mapped.get("transaction_fee"),
        ))
        missing = []
        if mapped.get("service_fee") is not None and not mapped.get("service_fee_kind"):
            missing.append("service_fee_kind")
        if cost.computed:
            self.storage.update("Order", order_id,
                                {"actual_net": cost.net, "actual_rate": cost.rate})
        self.storage.record_audit("order.actual", "order", order_id, result=cost.status,
                                  detail={k: v for k, v in mapped.items() if v is not None})
        return ActualView(mapped=mapped, missing=missing,
                          rate=cost.rate if cost.computed else None,
                          net=cost.net if cost.computed else None, cost=cost)

    def list_orders(self) -> list[dict[str, Any]]:
        out = []
        for row in self.storage.list("Order", limit=500):
            candidate = self._candidate(int(row["candidate_id"])) if row.get("candidate_id") else None
            item = dict(row)
            item["status"] = row.get("state")
            item["site"] = row.get("market")
            item["candidate_name"] = (candidate or {}).get("title", "—")
            machine = OrderMachine(self.spec, state=row.get("state") or None)
            item["next_states"] = machine.allowed()
            # 把"为什么这一步"一起带出来，前端不用自己查状态机
            item["next_guards"] = {
                state: (machine.transition_for(state).guard if machine.transition_for(state) else "")
                for state in item["next_states"]
            }
            out.append(item)
        return out

    def audit(self, order_id: int, action: str, *, result: str | None = None,
              detail: dict[str, Any] | None = None) -> None:
        self.storage.record_audit(action, "order", order_id, result=result, detail=detail)

    # ---- 告警：把异常分支接到真实时间戳 ---------------------------------
    def state_since(self, order_id: int) -> str | None:
        """当前状态的进入时间 = 该订单最后一条 order.transition 审计记录的 at。"""
        rows = [row for row in self.storage.list("AuditLog", limit=2000)
                if row["action"] == "order.transition" and str(row["object_id"]) == str(order_id)]
        return rows[0]["at"] if rows else None

    def hours_in_state(self, order_id: int, now: datetime | None = None) -> float | None:
        started = _parse_iso(self.state_since(order_id))
        if started is None:
            return None
        return ((now or datetime.now(timezone.utc)) - started).total_seconds() / 3600.0

    def dts_ready(self) -> bool:
        """发货时限参数是否已升到 A/B——未升级前不生成倒计时（P-TW-DTS 的 note 要求）。"""
        param = self.spec.params.get("P-TW-DTS")
        return bool(param and param.hard_eligible())

    def order_alerts(self) -> list[dict[str, Any]]:
        """EX-01/02/03 告警。EX-02 只在发货时限参数已核实时才启用。"""
        ready = self.dts_ready()
        alerts: list[dict[str, Any]] = []
        for row in self.list_orders():
            machine = OrderMachine(self.spec, state=row.get("status") or None)
            if row.get("status") == "paid":
                continue
            hours = self.hours_in_state(row["id"])
            for item in machine.exceptions(hours_in_state=hours, dts_ready=ready):
                alerts.append(dict(item, order_id=row["id"], state=machine.state,
                                   candidate=row.get("candidate_name"),
                                   hours_in_state=None if hours is None else round(hours, 1)))
        alerts.sort(key=lambda item: (item.get("priority") != "P1", item.get("order_id") or 0))
        return alerts

    def unverified_unlocks(self) -> list[dict[str, Any]]:
        """哪些功能正被未核实参数挡着——把"该核实什么"直接摆到台面上。"""
        unlocks = []
        for param_id, name, feature in (
            ("P-TW-DTS", "发货时限与迟发判定", "发货截止倒计时与 EX-02 到仓超期告警"),
            ("P-TW-SLS-TIERS", "SLS 运费档", "按重量自动取运费"),
            ("P-TW-FX", "汇率", "利润核算（缺它一律 INCOMPLETE）"),
        ):
            param = self.spec.params.get(param_id)
            if param is not None and not param.hard_eligible():
                unlocks.append({"param_id": param_id, "name": name, "feature": feature,
                                "level": param.evidence_level, "task_ref": param.task_ref})
        return unlocks

    # ---- 候选值：抓取/截图 → 人工确认 → 覆盖值 --------------------------
    def record_capture(self, capture: Any, *, channel: str | None = None) -> int | None:
        """记一次采集。

        **只有真的拿到了值才产生候选行**——抓失败不产出"待确认的值"，
        否则 review 列表会被失败项灌满，真正的变更反而看不见。
        失败仍然写审计，留痕不丢。
        """
        row = capture.as_row()
        if channel:
            row["channel"] = channel
        candidate_id = None
        if capture.value is not None:
            candidate_id = self.storage.insert("ParamCandidate", row)
        self.storage.record_audit(
            "capture." + capture.status, "Param", capture.param_id, result=capture.status,
            detail={"candidate_id": candidate_id, "url": capture.url,
                    "snapshot_ref": capture.snapshot_ref, "value": capture.value,
                    "message": capture.message})
        return candidate_id

    def pending_candidates(self) -> list[dict[str, Any]]:
        """待确认的候选值，带上当前生效值——变了没有要一眼看出来。"""
        out = []
        for row in self.storage.list("ParamCandidate", limit=500,
                                     where="status = ?", args=("pending",)):
            param = self.spec.params.get(row["param_id"])
            out.append(dict(
                row,
                param_name=(param.name if param else row["param_id"]),
                current_value=(param.value if param else None),
                current_level=(param.evidence_level if param else None),
                changed=bool(param is not None and param.value != row.get("value")),
            ))
        out.sort(key=lambda item: (not item["changed"], item["param_id"]))
        return out

    def capture_history(self, limit: int = 500) -> list[dict[str, Any]]:
        return self.storage.list("ParamCandidate", limit=limit)

    def approve_candidate(self, candidate_id: int, grade: str, *,
                          operator: str = "cli", note: str | None = None) -> None:
        """确认一条候选：写覆盖值 + 标记 approved。**等级由人给，不由抓取器给。**"""
        row = self.storage.get("ParamCandidate", candidate_id)
        if row is None:
            raise ValueError("找不到候选 #%s" % candidate_id)
        if row["status"] != "pending":
            raise ValueError("候选 #%s 已经是 %s，不能重复确认" % (candidate_id, row["status"]))
        if row.get("value") is None:
            raise ValueError("候选 #%s 没有值（%s），不能确认" % (candidate_id, row.get("message") or "抓取失败"))
        self._write_override(
            row["param_id"], row["value"], grade,
            source=note or ("抓取确认" if row.get("channel") == "fetch" else "截图确认"),
            source_url=row.get("source_url"), snapshot_ref=row.get("snapshot_ref"),
            checked_at=(row.get("captured_at") or "")[:10] or None)
        self.storage.update("ParamCandidate", candidate_id, {
            "status": "approved", "decided_at": _now(), "decided_by": operator})
        self.storage.record_audit("capture.approve", "Param", row["param_id"], result=grade,
                                  detail={"candidate_id": candidate_id, "value": row["value"],
                                          "source_url": row.get("source_url")})

    def reject_candidate(self, candidate_id: int, reason: str, *, operator: str = "cli") -> None:
        row = self.storage.get("ParamCandidate", candidate_id)
        if row is None:
            raise ValueError("找不到候选 #%s" % candidate_id)
        if row["status"] != "pending":
            raise ValueError("候选 #%s 已经是 %s" % (candidate_id, row["status"]))
        self.storage.update("ParamCandidate", candidate_id, {
            "status": "rejected", "decided_at": _now(), "decided_by": operator, "message": reason})
        self.storage.record_audit("capture.reject", "Param", row["param_id"], result="rejected",
                                  detail={"candidate_id": candidate_id, "reason": reason})

    # ---- 核实任务 -------------------------------------------------------
    def checklist_rows(self) -> list[dict[str, Any]]:
        rows = self.storage.list("VerificationTask", limit=500)
        rows.sort(key=lambda r: (not r.get("blocks_first_order"), r.get("spec_task_id") or ""))
        out = []
        for row in rows:
            target = row.get("target_param_id")
            param = self.spec.params.get(target) if target else None
            out.append({
                "id": row["id"], "module": row["module"], "item": row["item"],
                "channel": row["channel"], "grade": row.get("grade") or "",
                "checked_date": row.get("checked_date") or "",
                "conclusion": row.get("conclusion") or "",
                "spec_task_id": row.get("spec_task_id"),
                "blocks_first_order": row.get("blocks_first_order"), "status": row.get("status"),
                "source_url": row.get("source_url") or "",
                "snapshot_ref": row.get("snapshot_ref") or "",
                "target_param_id": target,
                "param_level": (param.evidence_level if param else None),
                "param_value": (param.value if param else None),
            })
        return out

    def set_checklist(self, item_id: int, checked_date: str, conclusion: str, grade: str,
                      *, source_url: str | None = None, snapshot_ref: str | None = None) -> None:
        if grade not in ("A", "B", "C", "D", "E"):
            raise ValueError("证据等级只能是 A-E")
        if grade == "A" and not (source_url or "").strip():
            raise ValueError("A 级必须有可打开的 URL——没有凭据的 A 级等于自述")
        self.storage.update("VerificationTask", item_id, {
            "checked_date": checked_date, "conclusion": conclusion, "grade": grade,
            "status": "done", "source_url": source_url, "snapshot_ref": snapshot_ref})
        row = self.storage.get("VerificationTask", item_id)
        if row and row.get("spec_task_id"):
            self.storage.record_audit("task.done", "VerificationTask", item_id, result=grade,
                                      detail={"spec_task_id": row["spec_task_id"],
                                              "source_url": source_url,
                                              "snapshot_ref": snapshot_ref})

    def set_param_value(self, param_id: str, value: Any, grade: str, *,
                        source_url: str | None = None, snapshot_ref: str | None = None,
                        note: str = "核实任务") -> None:
        """按 spec 参数 id 直接写覆盖值——核实任务的闭环出口。

        与 set_param 的区别：那个走旧的 10 个 key 映射，这个是新流程用的，
        参数 id 直接来自任务表，并带上证据出处。
        """
        if param_id not in self.spec.params:
            raise ValueError("spec 里没有参数 %s" % param_id)
        self._write_override(param_id, _parse_value(value), grade, source=note,
                             source_url=source_url, snapshot_ref=snapshot_ref)
        if source_url or snapshot_ref:
            self.storage.record_audit("param.override", "Param", param_id, result=grade,
                                      detail={"source_url": source_url,
                                              "snapshot_ref": snapshot_ref, "value": value})

    # ---- 兼容：托管明细导入 ---------------------------------------------
    def apply_escrow(self, candidate_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        from shopee_ledger.escrow import map_escrow

        mapped = map_escrow(payload)
        self.storage.record_audit("escrow.import", "candidate", candidate_id,
                                  detail={k: v for k, v in mapped.items() if v is not None})
        return mapped


def _parse_value(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if text[:1] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            return raw
    try:
        return float(text) if ("." in text or "e" in text.lower()) else int(text)
    except ValueError:
        return raw


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dump_steps(state: Any) -> str:
    return ",".join(name for name, done in state.steps.items() if done)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
