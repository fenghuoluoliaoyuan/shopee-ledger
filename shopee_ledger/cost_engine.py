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
    # 不填 sls_freight 时，用它从官方运费表按重量算（缺表就算不出，不会瞎猜）
    weight_g: float | None = None
    channel: str | None = None
    cargo: str = "Normal"
    buyer_paid_freight: float | None = None
    seller_pays_freight: bool = False
    # 订单属性
    in_free_window: bool = False
    is_presale: bool = False
    ad_spend: float = 0.0
    affiliate_rate: float = 0.0
    # 官方结算口径里的「优惠券与回扣」：结算前从订单收入里减掉
    coupon_discount: float = 0.0
    # 官方结算口径里的「订单调整」：如马来西亚站点高价值商品税
    order_adjustment: float = 0.0
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

    def __post_init__(self) -> None:
        """INV-004：INCOMPLETE 必须说清缺什么。

        这条以前只在 governance.json 里是一句话，**源码与测试里都没人提**——
        典型的"纸面不变量"。而它恰恰守着一个很容易犯的错：拿"算不出来"当结论
        却不说明为什么，调用方只能猜。说不出来就是引擎 bug，不是数据不全，
        所以在构造时就拒绝，而不是等人在报表里发现。
        """
        if self.status == INCOMPLETE and not self.missing:
            raise ValueError(
                "INV-004 违反：status=INCOMPLETE 但 missing 为空——这是引擎 bug，不是数据不全")

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
    def __init__(self, spec: Spec, today: str | None = None,
                 freight_config: dict | None = None):
        self.spec = spec
        self.today = today
        # 官方运费表（定价模拟器接口）。不给就按调用方传入的 sls_freight 算，
        # 或者在有重量与渠道时直接说"算不出"——不猜。
        if freight_config is None:
            try:
                from shopee_ledger.freight import load_latest_config

                freight_config = load_latest_config()
            except Exception:
                freight_config = None
        self.freight_config = freight_config

    def freight_from_weight(self, market: str, channel: str, weight_g: float,
                            cargo: str = "Normal") -> tuple[float | None, str]:
        """按重量从官方运费表算卖家承担的跨境物流成本。返回 (金额, 来源说明)。"""
        if not self.freight_config:
            return None, "没有运费表快照（跑 freight 拉取）"
        from shopee_ledger.freight import FREIGHT_ALGORITHM_SOURCE, seller_fee

        fee = seller_fee(self.freight_config, market, channel, weight_g, cargo=cargo)
        if fee is None:
            return None, "运费表里没有 %s/%s/%s 这个渠道" % (market, cargo, channel)
        return fee, "官方运费表 %s" % self.freight_config.get("date", "?")

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

        # 0) 运费：没直接给就用官方运费表按重量算（表里有就自己算，没有就照实缺）
        freight_value = data.sls_freight
        freight_source = "输入"
        if freight_value is None and data.weight_g and data.channel:
            computed, note = self.freight_from_weight(market, data.channel, data.weight_g,
                                                      data.cargo)
            if computed is not None:
                freight_value, freight_source = computed, note

        # 1) 必填输入
        for name in ("price_local", "purchase_cny", "domestic_cny", "local_per_cny"):
            if getattr(data, name) is None:
                missing.append(name)
        if freight_value is None:
            missing.append("sls_freight")
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

        # 两项容易被漏掉的平台费用。放在"缺字段提前返回"**之前**算：
        # 缺别的字段时更该看得见费用构成，而不是整块被吞掉（实测泰国站就看不到）。
        price_now = float(data.price_local) if data.price_local else 0.0
        infra = self.per_order_fee(market)
        tech = self.tech_fee_rate(market)
        infra_amount = float(infra.value) if infra.ok else 0.0
        tech_amount = price_now * float(tech.value) if tech.ok else 0.0
        # 官方列表里列了这个站点却取不到值 → 真缺；没列 → 该站点不收取，记 0
        if not infra.ok and market in self.fee_markets("P-INFRA-FEE"):
            missing.append("infra_fee")
        if not tech.ok and market in self.fee_markets("P-TECH-FEE"):
            missing.append("tech_fee")
        fee_trace = [
            TraceEntry("平台基础设施费", infra_amount, infra.source, infra.level,
                       "每笔已完成订单固定额，含增值税；官方列表未列该站点时按不收取"),
            TraceEntry("技术支持费", tech_amount, tech.source, tech.level,
                       "按已完成订单商品总额比例，含税费"),
        ]

        if missing:
            context = {
                "price_local": data.price_local,
                "sls_freight": freight_value,
                "buyer_paid_freight": data.buyer_paid_freight,
                "in_free_window": data.in_free_window,
                "market": market_doc,
                "import_tax_treatment": treatment,
                "inputs": {"price_local_includes_shipping": False},
            }
            return CostResult(INCOMPLETE, missing=sorted(set(missing)), trace=trace + fee_trace,
                              buyer_price_uplift=buyer_uplift, notes=notes, context=context)

        # 4) 计算
        price = float(data.price_local)
        purchase_local = float(data.purchase_cny) * float(data.local_per_cny)
        domestic_local = float(data.domestic_cny) * float(data.local_per_cny)
        buyer = 0.0 if data.seller_pays_freight else float(data.buyer_paid_freight or 0.0)
        net_freight = max(float(freight_value) - buyer, 0.0)

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

        platform_fee = (commission_amount + txn_amount + presale_amount + service_amount
                        + infra_amount + tech_amount)
        affiliate = price * data.affiliate_rate
        withdraw_amount = price * float(withdraw.value)
        fx_amount = price * float(fx_loss.value)
        return_reserve = float(return_rate.value) * (purchase_local + domestic_local)

        # 官方结算口径（#25770 附录2）：
        #   订单收入 = 商品总额 - 运费总额 - 优惠券与回扣 - 各项费用
        #   最终金额 = 订单收入 - 订单调整
        # 各项费用 = 佣金 + 服务费 + 交易手续费；订单调整如马来西亚高价值商品税。
        order_income = (price - net_freight - platform_fee - affiliate
                        - float(data.coupon_discount))
        net = (order_income - purchase_local - domestic_local - float(data.ad_spend)
               - withdraw_amount - fx_amount - return_reserve - tax_as_cost
               - float(data.order_adjustment))
        rate_value = net / price if price else None

        trace.extend([
            TraceEntry("售价(商品价)", price, "输入", "-", "不含买家运费"),
            TraceEntry("采购成本(本币)", purchase_local, "输入 × 汇率", "C"),
            TraceEntry("国内段运费(本币)", domestic_local, "输入 × 汇率", "C"),
            TraceEntry("SLS 运费(卖家承担)", float(freight_value), freight_source, "-",
                       ("按 %s 与 %sg 从运费表算出" % (data.channel, data.weight_g))
                       if data.sls_freight is None and data.weight_g else ""),
            TraceEntry("净运费", net_freight, "max(SLS − 买家实付, 0)", "-", "买家多付不退，不计收入"),
            TraceEntry("佣金", commission_amount, commission_source, commission_level, commission_note),
            TraceEntry("交易手续费", txn_amount, txn_source, txn_level, "免佣窗口内照收"),
            TraceEntry("平台基础设施费", infra_amount, infra.source, infra.level,
                       "每笔已完成订单固定额，含增值税"),
            TraceEntry("技术支持费", tech_amount, tech.source, tech.level,
                       "按已完成订单商品总额比例，含税费"),
            TraceEntry("平台费合计", platform_fee, "佣金+手续费+预售+服务费+基础设施费+技术支持费"),
            TraceEntry("优惠券与回扣", float(data.coupon_discount), "输入", "-", "官方结算口径里从订单收入减掉"),
            TraceEntry("联盟佣金", affiliate, "输入"),
            TraceEntry("订单收入", order_income, "官方口径：商品总额-运费总额-优惠券与回扣-各项费用"),
            TraceEntry("广告费", float(data.ad_spend), "输入"),
            TraceEntry("提现费", withdraw_amount, withdraw.source, withdraw.level),
            TraceEntry("汇损", fx_amount, fx_loss.source, fx_loss.level),
            TraceEntry("退货预留", return_reserve, "%s × (采购+国内)" % return_rate.source, return_rate.level),
            TraceEntry("订单调整", float(data.order_adjustment), "输入", "-",
                       "官方口径里的订单调整，如马来高价值商品税"),
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
            "infra_fee": infra_amount,
            "tech_fee": tech_amount,
            # 官方结算口径里的两项，便于与平台账单逐项对齐
            "order_income": order_income,
            "order_adjustment": float(data.order_adjustment),
            "coupon_discount": float(data.coupon_discount),
        }
        context = {
            "price_local": price,
            "sls_freight": float(freight_value),
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
            "order_income": order_income,
            "order_adjustment": float(data.order_adjustment),
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

    # ---- 被漏掉的平台费用：基础设施费与技术支撑费 -------------------------
    def _table(self, param_id: str) -> tuple[dict[str, Any] | None, str]:
        """取按市场分列的费用表。返回 (by_market 字典, 参数等级)。"""
        param = self.spec.params.get(param_id)
        if param is None:
            return None, "-"
        raw = param.value if param.value is not None else param.unverified_claim
        if not isinstance(raw, dict):
            return None, param.evidence_level
        table = raw.get("by_market")
        return (table if isinstance(table, dict) else {}), param.evidence_level

    def per_order_fee(self, market: str, param_id: str = "P-INFRA-FEE") -> Rate:
        """每笔已完成订单的固定额（含税），如平台基础设施费。

        区分两件事，别混：
        * **官方列表里没有这个站点** → 该站点不收取，记 0（不是"未知"）；
        * **列表里有这个站点却取不到值** → 真缺，调用方应记 INCOMPLETE。
        """
        table, level = self._table(param_id)
        if table is None:
            return Rate(None, param_id, level)
        if market not in table:
            return Rate(0.0, "%s：官方列表未列该站点，按不收取" % param_id, level)
        value = table[market]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return Rate(float(value), "%s.by_market.%s" % (param_id, market), level)
        return Rate(None, "%s.by_market.%s" % (param_id, market), level)

    def tech_fee_rate(self, market: str, param_id: str = "P-TECH-FEE") -> Rate:
        """按已完成订单商品总额收取的技术支持费（含税费）比例。"""
        table, level = self._table(param_id)
        if table is None:
            return Rate(None, param_id, level)
        if market not in table:
            return Rate(0.0, "%s：官方列表未列该站点，按不收取" % param_id, level)
        entry = table[market]
        if not isinstance(entry, dict):
            return Rate(None, "%s.by_market.%s" % (param_id, market), level)
        rate = entry.get("rate_incl_tax")
        if rate is None:
            rate = entry.get("rate")
        if rate is None:
            return Rate(None, "%s.by_market.%s" % (param_id, market), level)
        return Rate(float(rate), "%s.by_market.%s.rate_incl_tax" % (param_id, market), level)

    def fee_markets(self, param_id: str) -> set[str]:
        table, _level = self._table(param_id)
        return set(table or {})
