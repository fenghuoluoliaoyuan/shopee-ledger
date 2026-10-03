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
from pathlib import Path
from typing import Any

from shopee_ledger.cost_engine import COMPUTED, CostEngine, CostInputs, CostResult
from shopee_ledger.gates import REJECT, GateResult, GateService
from shopee_ledger.profit import Decision
from shopee_ledger.spec import Spec, default_spec
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
    def _write_override(self, param_id: str, value: Any, grade: str, *, source: str = "本机录入") -> None:
        if grade not in ("A", "B", "C"):
            raise ValueError("覆盖层只接受 A/B/C 级（实测或后台抄录）；%s 级请走核实任务升级路径" % grade)
        self.storage.insert("ParamOverride", {
            "param_id": param_id, "value": value, "evidence_level": grade,
            "operator": "cli", "note": source, "checked_at": "2026-10-03",
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
                      intends_free_shipping: bool) -> int:
        return self.storage.insert("ProductCandidate", {
            "platform": "shopee", "market": site, "source_url": "local:%s" % name, "title": name,
            "category": "", "veto_flags": {}, "competitor_notes": {}, "state": "candidate",
            "weight_g": weight_g, "purchase_cny": purchase_cny, "domestic_cny": domestic_cny,
            "price_local": price, "sls_fee": sls_fee,
            "intends_free_shipping": intends_free_shipping,
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

    # ---- 订单 -----------------------------------------------------------
    def open_order(self, candidate_id: int) -> int:
        from shopee_ledger.fulfillment import Fulfillment

        state = Fulfillment()
        candidate = self._candidate(candidate_id) or {}
        return self.storage.insert("Order", {
            "platform": "shopee", "market": candidate.get("market", "TW"),
            "mode": "dropship", "listing_id": str(candidate_id), "candidate_id": str(candidate_id),
            "state": state.status, "steps": _dump_steps(state), "block_reason": "",
            "created_at": "2026-10-03T00:00:00+00:00",
        })

    def load_order(self, order_id: int) -> Any:
        from shopee_ledger.fulfillment import Fulfillment

        row = self.storage.get("Order", order_id)
        if row is None:
            raise ValueError("找不到订单 %s" % order_id)
        state = Fulfillment(status=row.get("state") or "open")
        state.supplier_id = int(row["supplier_id"]) if row.get("supplier_id") else None
        done = set(filter(None, (row.get("steps") or "").split(",")))
        for step in state.steps:
            state.steps[step] = step in done
        state.warehouse_address = row.get("warehouse_address") or ""
        state.address_source = row.get("address_source") or ""
        state.block_reason = row.get("block_reason") or ""
        return state

    def save_order(self, order_id: int, state: Any) -> None:
        self.storage.update("Order", order_id, {
            "state": state.status,
            "steps": _dump_steps(state),
            "supplier_id": str(state.supplier_id) if state.supplier_id else None,
            "warehouse_address": state.warehouse_address,
            "address_source": state.address_source,
            "block_reason": state.block_reason,
        })

    def apply_stock(self, order_id: int, supplier_id: int, in_stock: bool, exhausted: bool) -> Any:
        from shopee_ledger.fulfillment import mark_stock

        state = self.load_order(order_id)
        mark_stock(state, supplier_id, in_stock, self.supplier_address_ok(supplier_id), exhausted)
        self.save_order(order_id, state)
        self.audit(order_id, "order.stock", result="WARN" if exhausted else "PASS",
                   detail={"in_stock": in_stock, "exhausted": exhausted})
        return state

    def _step(self, order_id: int, action: str, func) -> None:
        state = self.load_order(order_id)
        func(state)
        self.save_order(order_id, state)
        self.audit(order_id, action)

    def apply_confirm_address(self, order_id: int) -> None:
        from shopee_ledger.fulfillment import confirm_address_format

        self._step(order_id, "order.confirm_address", confirm_address_format)

    def apply_arrange(self, order_id: int) -> None:
        from shopee_ledger.fulfillment import arrange_ship

        self._step(order_id, "order.arrange_ship", arrange_ship)

    def apply_copy(self, order_id: int, address: str) -> None:
        from shopee_ledger.fulfillment import copy_address

        state = self.load_order(order_id)
        copy_address(state, address, "order_page")
        self.save_order(order_id, state)
        self.audit(order_id, "order.copy_address", detail={"address_len": len(address)})

    def apply_purchase(self, order_id: int) -> None:
        from shopee_ledger.fulfillment import mark_purchased

        self._step(order_id, "order.purchase", mark_purchased)

    def apply_inbound(self, order_id: int) -> None:
        from shopee_ledger.fulfillment import mark_inbound

        self._step(order_id, "order.inbound", mark_inbound)

    def set_deadline(self, order_id: int, deadline: str) -> None:
        self.storage.update("Order", order_id, {"deadline": deadline})

    def record_actual(self, order_id: int, payload: dict[str, Any]) -> None:
        self.storage.update("Order", order_id, payload)

    def list_orders(self) -> list[dict[str, Any]]:
        out = []
        for row in self.storage.list("Order", limit=500):
            candidate = self._candidate(int(row["candidate_id"])) if row.get("candidate_id") else None
            item = dict(row)
            item["status"] = row.get("state")
            item["candidate_name"] = (candidate or {}).get("title", "—")
            out.append(item)
        return out

    def audit(self, order_id: int, action: str, *, result: str | None = None,
              detail: dict[str, Any] | None = None) -> None:
        self.storage.record_audit(action, "order", order_id, result=result, detail=detail)

    # ---- 核实任务 -------------------------------------------------------
    def checklist_rows(self) -> list[dict[str, Any]]:
        rows = self.storage.list("VerificationTask", limit=500)
        rows.sort(key=lambda r: r.get("spec_task_id") or "")
        return [{
            "id": r["id"], "module": r["module"], "item": r["item"], "channel": r["channel"],
            "grade": r.get("grade") or "", "checked_date": r.get("checked_date") or "",
            "conclusion": r.get("conclusion") or "", "spec_task_id": r.get("spec_task_id"),
            "blocks_first_order": r.get("blocks_first_order"), "status": r.get("status"),
        } for r in rows]

    def set_checklist(self, item_id: int, checked_date: str, conclusion: str, grade: str) -> None:
        if grade not in ("A", "B", "C", "D", "E"):
            raise ValueError("证据等级只能是 A-E")
        self.storage.update("VerificationTask", item_id, {
            "checked_date": checked_date, "conclusion": conclusion, "grade": grade, "status": "done"})
        row = self.storage.get("VerificationTask", item_id)
        if row and row.get("spec_task_id"):
            self.storage.record_audit("task.done", "VerificationTask", item_id, result=grade,
                                      detail={"spec_task_id": row["spec_task_id"]})

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
