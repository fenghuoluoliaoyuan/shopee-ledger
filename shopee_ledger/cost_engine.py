"""成本核算引擎：由 ParamRegistry 驱动的**纯函数**，产出经济结果与证据链。

与 ``profit.py`` 的分工
----------------------
``profit.py`` 是 v1.4 的旧内核（写死字段、按 site 取参），保留以兼容既有 CLI 与测试。
本模块是 v4.0 内核：**只算经济结果，不做门禁判定**——判定交给 ``GateService``。
"引擎算数、门禁决策"是分层的关键，避免两处各有一套阈值逻辑。

四条硬语义
----------
1. ``None``（未核实）绝不退化成 0；缺字段进 ``missing``，状态 ``INCOMPLETE``（INV-006）。
2. 费率一律从 ParamRegistry 按 role 取，**代码里不出现 0.14 / 0.025 这类字面量**。
3. 参数 scope 严格隔离：台湾的提现费率**不得**被套用到泰国（INV-012）；取不到就是缺失，
   允许调用方用显式覆盖（what-if / 实测）补齐，覆盖会标注来源为 ``输入覆盖``。
4. 进口税负先分清**谁承担**：
   * 低值免税仍成立 → 不计；
   * 已取消但**平台下单时代收**（如泰国五平台协议）→ 不计入卖家成本，只记买家端价格上移；
   * 已取消且平台不代收 → 必须计入成本；税率未知则 ``INCOMPLETE``，不得静默省略。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shopee_ledger.spec import Param, Spec

COMPUTED = "COMPUTED"
INCOMPLETE = "INCOMPLETE"

# role → 参数 id 后缀约定（P-<MARKET>-<SUFFIX>）
ROLE_SUFFIX = {
    "commission": "COMMISSION",
    "txn_fee": "TXN-FEE",
    "presale_fee": "PRESALE-FEE",
    "freeship_fee": "FREESHIP-FEE",
    "margin_th": "MARGIN-TH",
    "withdraw": "WITHDRAW",
    "return_rate": "RETURN-RATE",
    "fx_loss": "FX-LOSS",
    "import_duty": "IMPORT-DUTY",
    "vat": "VAT",
}

# 结构化参数内部字段的候选键（按顺序尝试，不猜语义）
ROLE_KEYS = {
    "margin_th": ("pass", "value"),
    "withdraw": ("reserve_at", "max", "rate"),
    "return_rate": ("reserve_at", "max", "rate"),
    "fx_loss": ("reserve_at", "max", "rate"),
    "import_duty": ("low_value_start_rate", "rate", "duty_rate"),
    "vat": ("rate", "vat_rate"),
    "commission": ("rate",),
    "txn_fee": ("rate",),
    "presale_fee": ("rate",),
    "freeship_fee": ("rate",),
}


@dataclass
class CostInputs:
    market: str
    platform: str = "shopee"
    mode: str = "dropship"
    # 商品与采购
    price_local: float | None = None          # 商品价，不含买家运费（R-COST-004）
    purchase_cny: float | None = None
    domestic_cny: float | None = None
    local_per_cny: float | None = None        # 汇率：P-*-FX 是 E 级值为 null，必须外部提供
    # 物流
    sls_freight: float | None = None
    buyer_paid_freight: float | None = None
    seller_pays_freight: bool = False
    # 订单属性
    in_free_window: bool = False
    is_presale: bool = False
    ad_spend: float = 0.0
    affiliate_rate: float = 0.0
    service_fee: float = 0.0
    service_fee_kind: str = "none"            # service | shipping | none
    # 实测优先（平台账单）
    actual_commission: float | None = None
    actual_txn_fee: float | None = None
    # 显式覆盖（what-if / 跨市场实测），留空则回落到参数
    withdraw_rate: float | None = None
    fx_loss_rate: float | None = None
    return_rate: float | None = None
    duty_rate: float | None = None
    vat_rate: float | None = None


@dataclass
class Rate:
    value: float | None
    source: str
    level: str = "-"

    @property
    def ok(self) -> bool:
        return self.value is not None


@dataclass
class TraceEntry:
    label: str
    value: float | None
    source: str
    level: str = "-"
    note: str = ""


@dataclass
class CostResult:
    status: str
    net: float | None = None
    rate: float | None = None
    missing: list[str] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)
    buyer_price_uplift: float | None = None
    notes: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def computed(self) -> bool:
        return self.status == COMPUTED

    def gate_context(self, **extra: Any) -> dict[str, Any]:
        """喂给 GateService.check 的上下文（G3 门禁用）。

        必须带齐 G3 规则引用到的字段，否则规则会落在 UNKNOWN 上、
        门禁永远返回 INCOMPLETE——"引擎算数、门禁决策"就断了。
        """
        context: dict[str, Any] = dict(self.context)
        context.setdefault("net_margin", self.rate)
        context.setdefault("net_profit", self.net)
        context.update(extra)
        return context

    def explain(self) -> str:
        lines = ["核算结果: %s" % self.status]
        if self.net is not None and self.rate is not None:
            lines.append("  净利润 %.2f  净利润率 %.2f%%" % (self.net, self.rate * 100))
        if self.buyer_price_uplift is not None:
            lines.append("  买家端价格上移 %.0f%%（平台代收税，不计入卖家成本）" % (self.buyer_price_uplift * 100))
        if self.missing:
            lines.append("  缺字段: %s" % ", ".join(self.missing))
        for entry in self.trace:
            amount = "" if entry.value is None else " = %.4f" % entry.value
            suffix = (" — " + entry.note) if entry.note else ""
            lines.append("  · %s%s  [%s | %s]%s" % (entry.label, amount, entry.source, entry.level, suffix))
        for note in self.notes:
            lines.append("  ! %s" % note)
        return "\n".join(lines)


class CostEngine:
    def __init__(self, spec: Spec, today: str | None = None):
        self.spec = spec
        self.today = today

    # ---- 参数取用 -------------------------------------------------------
    def param_for_role(self, role: str, market: str) -> Param | None:
        suffix = ROLE_SUFFIX[role]
        for pid in ("P-%s-%s" % (market, suffix), "P-%s" % suffix):
            param = self.spec.params.get(pid)
            if param is not None and param.scope.get("market") in (market, "*"):
                return param
        for param in self.spec.params.values():
            if param.id.endswith("-" + suffix) and param.scope.get("market") in (market, "*"):
                return param
        return None

    def rate(self, role: str, market: str, override: float | None = None) -> Rate:
        """取费率。E 级读 unverified_claim（按 ROLE_KEYS 候选键），绝不读 value 里的假值。"""
        if override is not None:
            return Rate(float(override), "输入覆盖", "C")
        param = self.param_for_role(role, market)
        if param is None:
            return Rate(None, "无对应参数", "-")

        raw = param.value
        source = param.id
        if raw is None:
            raw = param.unverified_claim
            if raw is None:
                return Rate(None, param.id, param.evidence_level)
            source = "%s(unverified_claim)" % param.id

        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return Rate(float(raw), source, param.evidence_level)

        if isinstance(raw, dict):
            for key in ROLE_KEYS.get(role, ()):
                candidate = raw.get(key)
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                    return Rate(float(candidate), "%s.%s" % (source, key), param.evidence_level)
        return Rate(None, source, param.evidence_level)

    # ---- 主流程 ---------------------------------------------------------
    def quote(self, data: CostInputs) -> CostResult:
        market = data.market
        missing: list[str] = []
        trace: list[TraceEntry] = []
        notes: list[str] = []

        # 1) 必填输入
        for name in ("price_local", "purchase_cny", "domestic_cny", "local_per_cny", "sls_freight"):
            if getattr(data, name) is None:
                missing.append(name)
        if data.price_local is not None and data.price_local <= 0:
            missing.append("price_local")
        if data.buyer_paid_freight is None and not data.seller_pays_freight:
            missing.append("buyer_paid_freight")

        # 2) 费率
        commission_rate = self.rate("commission", market)
        txn_rate = self.rate("txn_fee", market)
        presale_rate = self.rate("presale_fee", market)
        withdraw = self.rate("withdraw", market, data.withdraw_rate)
        fx_loss = self.rate("fx_loss", market, data.fx_loss_rate)
        return_rate = self.rate("return_rate", market, data.return_rate)
        margin_rate = self.rate("margin_th", market)

        uses_actual_commission = data.actual_commission is not None
        uses_actual_txn = data.actual_txn_fee is not None

        if not data.in_free_window and not uses_actual_commission and not commission_rate.ok:
            missing.append("commission_rate")
        if not uses_actual_txn and not txn_rate.ok:
            missing.append("txn_fee_rate")
        if data.is_presale and not presale_rate.ok:
            missing.append("presale_fee_rate")
        if not withdraw.ok:
            missing.append("withdraw_rate")
        if not fx_loss.ok:
            missing.append("fx_loss_rate")
        if not return_rate.ok:
            missing.append("return_rate")

        # 3) 进口税负：先分清谁承担
        policy = self.spec.market(market).get("low_value_tax_policy", "unknown")
        duty = self.rate("import_duty", market, data.duty_rate)
        vat = self.rate("vat", market, data.vat_rate)
        withhold = self._platform_withholds(market)

        tax_as_cost = 0.0
        buyer_uplift: float | None = None
        if policy == "intact":
            treatment = "exempt"
            trace.append(TraceEntry("进口税负", 0.0, "市场政策:intact", "A", "低值免税仍成立"))
        elif withhold:
            treatment = "buyer_uplift"
            if duty.ok and vat.ok:
                buyer_uplift = duty.value + vat.value
                trace.append(TraceEntry(
                    "买家端价格上移", buyer_uplift, "%s / %s" % (duty.source, vat.source),
                    duty.level, "平台下单时代收，不计入卖家成本"))
            else:
                buyer_uplift = None
                notes.append("平台代收税，但税率未能解析（%s / %s）：买家端上移幅度未知" % (duty.source, vat.source))
        elif not (duty.ok and vat.ok):
            treatment = "seller_cost"
            missing.extend([name for name, rate in (("import_duty_rate", duty), ("vat_rate", vat)) if not rate.ok])
        else:
            treatment = "seller_cost"
            tax_as_cost = float(data.price_local or 0.0) * (duty.value + vat.value)
            trace.append(TraceEntry("进口税负(成本)", tax_as_cost, "%s / %s" % (duty.source, vat.source),
                                    duty.level, "平台不代收，须由卖家承担"))

        market_doc = self.spec.market(market)
        if missing:
            context = {
                "price_local": data.price_local,
                "sls_freight": data.sls_freight,
                "buyer_paid_freight": data.buyer_paid_freight,
                "in_free_window": data.in_free_window,
                "market": market_doc,
                "import_tax_treatment": treatment,
                "inputs": {"price_local_includes_shipping": False},
            }
            return CostResult(INCOMPLETE, missing=sorted(set(missing)), trace=trace,
                              buyer_price_uplift=buyer_uplift, notes=notes, context=context)

        # 4) 计算
        price = float(data.price_local)
        purchase_local = float(data.purchase_cny) * float(data.local_per_cny)
        domestic_local = float(data.domestic_cny) * float(data.local_per_cny)
        buyer = 0.0 if data.seller_pays_freight else float(data.buyer_paid_freight or 0.0)
        net_freight = max(float(data.sls_freight) - buyer, 0.0)

        if data.in_free_window:
            commission_amount, commission_note = 0.0, "免佣窗口内记 0"
            commission_source, commission_level = "免佣窗口规则", "A"
        elif uses_actual_commission:
            commission_amount, commission_note = float(data.actual_commission), "平台账单实测"
            commission_source, commission_level = "平台账单", "B"
        else:
            commission_amount = price * float(commission_rate.value)
            commission_note = "%.2f%% × 商品价" % (commission_rate.value * 100)
            commission_source, commission_level = commission_rate.source, commission_rate.level

        if uses_actual_txn:
            txn_amount, txn_source, txn_level = float(data.actual_txn_fee), "平台账单", "B"
        else:
            txn_amount = price * float(txn_rate.value)
            txn_source, txn_level = txn_rate.source, txn_rate.level

        presale_amount = price * float(presale_rate.value) if (data.is_presale and presale_rate.ok) else 0.0
        service_amount = data.service_fee if data.service_fee_kind == "service" else 0.0
        platform_fee = commission_amount + txn_amount + presale_amount + service_amount
        affiliate = price * data.affiliate_rate
        withdraw_amount = price * float(withdraw.value)
        fx_amount = price * float(fx_loss.value)
        return_reserve = float(return_rate.value) * (purchase_local + domestic_local)

        net = (price - purchase_local - domestic_local - net_freight - platform_fee
               - affiliate - float(data.ad_spend) - withdraw_amount - fx_amount
               - return_reserve - tax_as_cost)
        rate_value = net / price if price else None

        trace.extend([
            TraceEntry("售价(商品价)", price, "输入", "-", "不含买家运费"),
            TraceEntry("采购成本(本币)", purchase_local, "输入 × 汇率", "C"),
            TraceEntry("国内段运费(本币)", domestic_local, "输入 × 汇率", "C"),
            TraceEntry("净运费", net_freight, "max(SLS − 买家实付, 0)", "-", "买家多付不退，不计收入"),
            TraceEntry("佣金", commission_amount, commission_source, commission_level, commission_note),
            TraceEntry("交易手续费", txn_amount, txn_source, txn_level, "免佣窗口内照收"),
            TraceEntry("平台费合计", platform_fee, "佣金+手续费+预售+服务费"),
            TraceEntry("联盟佣金", affiliate, "输入"),
            TraceEntry("广告费", float(data.ad_spend), "输入"),
            TraceEntry("提现费", withdraw_amount, withdraw.source, withdraw.level),
            TraceEntry("汇损", fx_amount, fx_loss.source, fx_loss.level),
            TraceEntry("退货预留", return_reserve, "%s × (采购+国内)" % return_rate.source, return_rate.level),
        ])
        if margin_rate.ok:
            notes.append("阈值 %s：可做 ≥%.0f%%（D 级经验值，对无货源可能过严，第一轮建议先按较低阈值找品）"
                         % (margin_rate.source, margin_rate.value * 100))

        components = {
            "purchase_local": purchase_local, "domestic_local": domestic_local,
            "net_freight": net_freight, "commission": commission_amount, "txn_fee": txn_amount,
            "platform_fee": platform_fee, "affiliate": affiliate, "ad_spend": float(data.ad_spend),
            "withdraw": withdraw_amount, "fx_loss": fx_amount, "return_reserve": return_reserve,
            "import_tax": tax_as_cost,
        }
        context = {
            "price_local": price,
            "sls_freight": float(data.sls_freight),
            "buyer_paid_freight": buyer,
            "net_freight": net_freight,
            "in_free_window": data.in_free_window,
            "commission": commission_amount,
            "txn_fee": txn_amount,
            "withdraw_rate": data.withdraw_rate if data.withdraw_rate is not None else withdraw.value,
            "net_margin": rate_value,
            "net_profit": net,
            "market": market_doc,
            "import_tax_treatment": treatment,
            "inputs": {"price_local_includes_shipping": False},
        }
        return CostResult(COMPUTED, net=net, rate=rate_value, trace=trace, components=components,
                          buyer_price_uplift=buyer_uplift, notes=notes, context=context)

    def _platform_withholds(self, market: str) -> bool:
        for param in self.spec.params.values():
            if not param.id.endswith("PLATFORM-WITHHOLD"):
                continue
            if param.scope.get("market") not in (market, "*"):
                continue
            raw = param.unverified_claim if param.value is None else param.value
            if isinstance(raw, dict) and raw.get("platform_collects_at_checkout"):
                return True
        return False
