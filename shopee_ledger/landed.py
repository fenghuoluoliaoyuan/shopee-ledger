"""多站点落地成本对比：同一个品，在哪个站点/渠道最好做。

为什么需要
----------
前面几轮把三样东西补齐了：官方运费表（按重量算）、各站点佣金与手续费、
两项容易被漏掉的平台费用。这个模块把它们合起来回答选品时唯一重要的问题：
**同一个采购价，卖到哪个站点、走哪个渠道，需要卖多少钱才达标。**

算法（线性，可解析求解，不用迭代）
--------------------------------
::

    净利 = 售价 - 采购本币 - 国内本币 - 净运费 - 平台费(售价) - 提现 - 汇损 - 退货预留
    平台费(售价) = 售价 × (佣金 + 手续费 + 技术支持费) + 基础设施费
    ⇒ 净利 = 售价 × (1 - k) - F
    给定目标净利率 R：售价 = F / (1 - k - R)

其中 k 是与售价成比例的部分，F 是与售价无关的固定支出。
**k + R ≥ 1 时无解**——说明这个站点/渠道在该目标下根本做不了，必须如实报出来，
不能返回一个负数或无穷大糊弄过去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shopee_ledger.cost_engine import CostEngine
from shopee_ledger.spec import Spec


@dataclass
class LandedRow:
    market: str
    cargo: str
    channel: str
    currency: str
    seller_freight: float
    buyer_pays_freight: float
    commission: float
    txn_fee: float
    tech_fee: float
    infra_fee: float
    fixed_cost: float | None = None       # F：与售价无关的支出（本币）
    min_price: float | None = None        # 达到目标净利率所需售价（本币）
    landed_cost: float | None = None      # 在 min_price 下的全部成本（本币）
    local_per_cny: float | None = None    # 汇率，用于折算成人民币好横向比
    feasible: bool = True
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def proportional(self) -> float:
        """与售价成比例的费用合计（k）。"""
        return self.commission + self.txn_fee + self.tech_fee

    @property
    def min_price_cny(self) -> float | None:
        """最低售价折成人民币。

        跨站点对比**必须换成同一币种**，否则 TWD 144 和 THB 304 放在一起看不出谁高谁低。
        """
        if self.min_price is None or not self.local_per_cny:
            return None
        return self.min_price / self.local_per_cny


def _channel_names(config: dict, market: str, cargo: str, weight_g: float,
                   limit: int, only: str | None = None) -> list[str]:
    """挑出该市场值得看的渠道。

    排序按**卖家运费从低到高**，不是按名字长短——名字短的会挑出
    「Doorstep Delivery (Brunei)」这种跟跨境直邮无关的渠道。运费便宜才是真的划算。
    """
    from shopee_ledger.freight import channels_of, seller_fee

    grouped = channels_of(config, market)
    names = sorted({name.split("/", 1)[1] for name in grouped
                    if name.startswith(cargo + "/")})
    if only:
        names = [name for name in names if only in name]
    priced = [(seller_fee(config, market, name, weight_g, cargo=cargo) or 1e18, name)
              for name in names]
    priced.sort()
    return [name for _fee, name in priced[:limit]]


def compare(
    spec: Spec,
    *,
    purchase_cny: float,
    domestic_cny: float,
    weight_g: float,
    fx_by_market: dict[str, float],
    target_margin: float = 0.15,
    withdraw_rate: float = 0.0,
    fx_loss_rate: float = 0.0,
    return_rate: float = 0.0,
    seller_pays_freight: bool = False,
    markets: list[str] | None = None,
    cargo: str = "Normal",
    channels_per_market: int = 4,
    channel_filter: str | None = None,
) -> list[LandedRow]:
    """逐（市场 × 渠道）算达到目标净利率所需的最低售价。结果按最低售价升序。"""
    from shopee_ledger.freight import buyer_fee, load_latest_config, seller_fee

    config = load_latest_config()
    engine = CostEngine(spec, freight_config=config)
    markets = markets or [m.get("code") for m in (spec.registry.get("markets") or [])
                          if m.get("code") in fx_by_market]
    rows: list[LandedRow] = []

    for market in markets:
        currency = next((m.get("currency", "") for m in (spec.registry.get("markets") or [])
                         if m.get("code") == market), "")
        fx = fx_by_market.get(market)
        missing_market = [name for name, present in (
            ("local_per_cny", fx is not None),
            ("commission", engine.rate("commission", market).ok),
            ("txn_fee", engine.rate("txn_fee", market).ok),
        ) if not present]

        names = (_channel_names(config, market, cargo, weight_g, channels_per_market,
                                only=channel_filter)
                 if config else ["(无运费表)"])
        for channel in names:
            freight = seller_fee(config, market, channel, weight_g, cargo=cargo) if config else None
            buyer = (buyer_fee(config, market, channel, cargo=cargo) if config else None) or 0.0
            row = LandedRow(market=market, cargo=cargo, channel=channel, currency=currency,
                            seller_freight=freight or 0.0, buyer_pays_freight=buyer,
                            local_per_cny=fx,
                            commission=engine.rate("commission", market).value or 0.0,
                            txn_fee=engine.rate("txn_fee", market).value or 0.0,
                            tech_fee=engine.tech_fee_rate(market).value or 0.0,
                            infra_fee=engine.per_order_fee(market).value or 0.0)
            row.missing = list(missing_market)
            if freight is None:
                row.missing.append("sls_freight")
            if fx is None:
                row.missing.append("local_per_cny")

            if row.missing:
                row.feasible = False
                rows.append(row)
                continue

            purchase_local = purchase_cny * fx
            domestic_local = domestic_cny * fx
            net_freight = 0.0 if seller_pays_freight else max(freight - buyer, 0.0)
            return_reserve = return_rate * (purchase_local + domestic_local)
            fixed = purchase_local + domestic_local + net_freight + row.infra_fee + return_reserve

            k = row.proportional + withdraw_rate + fx_loss_rate
            denominator = 1.0 - k - target_margin
            row.fixed_cost = fixed
            if denominator <= 0:
                row.feasible = False
                row.notes.append("该站点费率合计 %.1f%% 已吃掉目标净利率 %.1f%%，无解"
                                 % (k * 100, target_margin * 100))
                rows.append(row)
                continue

            row.min_price = fixed / denominator
            row.landed_cost = row.min_price * k + fixed
            rows.append(row)

    # 排序必须用**折人民币**的价格：本币数字跨币种不可比
    # （越南盾动辄几十万，泰铢几百，放一起排出来是错的）。
    rows.sort(key=lambda item: (not item.feasible,
                                item.min_price_cny if item.min_price_cny else 1e18))
    return rows


def render(rows: list[LandedRow], *, target_margin: float, purchase_cny: float,
           domestic_cny: float, weight_g: float) -> str:
    lines = ["落地成本对比（采购 ¥%.2f + 国内 ¥%.2f，重量 %.0fg，目标净利率 %.0f%%）"
             % (purchase_cny, domestic_cny, weight_g, target_margin * 100), ""]
    lines.append("%-4s %-26s %-4s %11s %9s %13s %11s %10s" % (
        "市场", "渠道", "币种", "卖家运费", "费率合计", "最低售价", "折人民币", "落地成本"))
    for row in rows:
        if not row.feasible:
            reason = "；".join(row.notes) or ("缺数据: " + "、".join(row.missing))
            lines.append("%-4s %-26s %-4s %11.2f %9s %13s %11s %10s" % (
                row.market, row.channel[:26], row.currency, row.seller_freight,
                "%.2f%%" % (row.proportional * 100), "做不了", "—", "—"))
            lines.append("%s    ↳ %s" % (" " * 4, reason))
            continue
        cny = row.min_price_cny
        lines.append("%-4s %-26s %-4s %11.2f %9s %13.2f %11s %10.2f" % (
            row.market, row.channel[:26], row.currency, row.seller_freight,
            "%.2f%%" % (row.proportional * 100), row.min_price,
            ("¥%.2f" % cny) if cny else "—", row.landed_cost))
    lines.append("")
    lines.append("费率合计 = 佣金 + 交易手续费 + 技术支持费（提现与汇损按输入计入求解）")
    lines.append("最低售价 = 固定支出 / (1 − 费率合计 − 目标净利率)；分母 ≤0 表示该渠道做不了")
    lines.append("**跨站点比要看「折人民币」那一列**——币种不同，本币数字之间没有可比性")
    return "\n".join(lines)
