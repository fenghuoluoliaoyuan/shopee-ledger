"""命令行。用法：python -m shopee_ledger <命令>"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from shopee_ledger.profit import Decision
from shopee_ledger.store import DEFAULT_DB, PARAM_HELP, Ledger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shopee_ledger", description="Shopee 起步台账")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    param = sub.add_parser("param-set")
    param.add_argument("--site", required=True)
    param.add_argument("--key", required=True, choices=sorted(PARAM_HELP))
    param.add_argument("--value", required=True)
    param.add_argument("--grade", required=True, choices=("A", "B", "C", "D", "E"))
    show = sub.add_parser("param-list")
    show.add_argument("--site", required=True)

    supplier = sub.add_parser("supplier-add")
    supplier.add_argument("--name", required=True)
    supplier.add_argument("--url", default="")
    supplier.add_argument("--years", type=int, default=0)
    supplier.add_argument("--dropship", choices=("yes", "no"), required=True)
    supplier.add_argument("--address-ok", choices=("yes", "no"), required=True)
    supplier.add_argument("--pay", type=float)

    candidate = sub.add_parser("candidate-add")
    candidate.add_argument("--site", required=True)
    candidate.add_argument("--name", required=True)
    candidate.add_argument("--weight", type=float, required=True)
    candidate.add_argument("--purchase", type=float, required=True)
    candidate.add_argument("--domestic", type=float, required=True)
    candidate.add_argument("--price", type=float, required=True)
    candidate.add_argument("--sls", type=float, required=True)
    candidate.add_argument("--free-shipping", choices=("yes", "no"), default="yes")

    flags = sub.add_parser("candidate-flag")
    flags.add_argument("--id", type=int, required=True)
    flags.add_argument("--flags", default="", help="逗号分隔，如 apparel,fragile")

    ret = sub.add_parser("candidate-return-rate")
    ret.add_argument("--id", type=int, required=True)
    ret.add_argument("--rate", type=float, required=True)

    quote = sub.add_parser("quote")
    quote.add_argument("--id", type=int, required=True)
    survival = sub.add_parser("survival")
    survival.add_argument("--site", required=True)

    opened = sub.add_parser("order-open")
    opened.add_argument("--candidate", type=int, required=True)
    stock = sub.add_parser("order-stock")
    stock.add_argument("--order", type=int, required=True)
    stock.add_argument("--supplier", type=int, required=True)
    stock.add_argument("--in-stock", choices=("yes", "no"), required=True)
    stock.add_argument("--exhausted", choices=("yes", "no"), default="no")
    for name in ("order-confirm-address", "order-arrange", "order-purchase", "order-inbound"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--order", type=int, required=True)
    copied = sub.add_parser("order-copy-address")
    copied.add_argument("--order", type=int, required=True)
    copied.add_argument("--address", required=True)

    sub.add_parser("checklist")
    done = sub.add_parser("checklist-set")
    done.add_argument("--id", type=int, required=True)
    done.add_argument("--date", required=True)
    done.add_argument("--conclusion", required=True)
    done.add_argument("--grade", required=True)

    imported = sub.add_parser("import-escrow")
    imported.add_argument("--candidate", type=int, required=True)
    imported.add_argument("--file", required=True)

    fetched = sub.add_parser("fetch-escrow")
    fetched.add_argument("--candidate", type=int, required=True)
    fetched.add_argument("--order-sn", required=True)
    web = sub.add_parser("web")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)

    sub.add_parser("spec-info", help="打印 spec 概况、配置指纹与加载期问题")
    q2 = sub.add_parser("quote2", help="v4.0 核算引擎：参数来自 spec，只增不改地留档")
    q2.add_argument("--market", required=True, help="市场代码，如 TW / TH / MY")
    q2.add_argument("--price", type=float, required=True, help="商品价（不含买家运费）")
    q2.add_argument("--purchase", type=float, required=True, help="采购实付（人民币）")
    q2.add_argument("--domestic", type=float, required=True, help="国内段运费（人民币）")
    q2.add_argument("--fx", type=float, required=True, help="1 人民币折合多少当地币")
    q2.add_argument("--sls", type=float, help="SLS 运费（当地币）；缺省则报缺数据")
    q2.add_argument("--buyer-shipping", type=float, help="买家实付运费")
    q2.add_argument("--seller-pays-freight", action="store_true", help="卖家包邮")
    q2.add_argument("--return-rate", type=float, help="退货率预留（覆盖参数）")
    q2.add_argument("--withdraw-rate", type=float, help="提现费率（覆盖参数）")
    q2.add_argument("--fx-loss-rate", type=float, help="汇损率（覆盖参数）")
    q2.add_argument("--ads", type=float, default=0.0)
    q2.add_argument("--free-window", action="store_true", help="处于免佣窗口内")
    q2.add_argument("--grant", type=int, help="不填则只打印；填候选品 id 则留档成本快照")

    args = parser.parse_args(argv)
    ledger = Ledger(args.db)
    try:
        return _run(ledger, args)
    finally:
        ledger.close()


def _run(ledger: Ledger, args: argparse.Namespace) -> int:
    if args.cmd == "init":
        ledger.init()
        print(f"已建库 {args.db}。马来站阈值 15%/10% 标为 D，费率未填。")
        return 0
    if args.cmd == "param-set":
        ledger.set_param(args.site, args.key, args.value, args.grade)
        print(f"{args.site} {args.key} = {args.value} [{args.grade}]")
        return 0
    if args.cmd == "param-list":
        for key, value in ledger.params(args.site).items():
            print(f"{key}\t{value if value is not None else ''}\t{PARAM_HELP[key]}")
        return 0
    if args.cmd == "supplier-add":
        new_id = ledger.add_supplier(
            args.name, args.url, args.years, args.dropship == "yes", args.address_ok == "yes", args.pay
        )
        print(new_id)
        return 0
    if args.cmd == "candidate-add":
        new_id = ledger.add_candidate(
            args.site,
            args.name,
            args.weight,
            args.purchase,
            args.domestic,
            args.price,
            args.sls,
            args.free_shipping == "yes",
        )
        print(new_id)
        return 0
    if args.cmd == "candidate-flag":
        flags = [item for item in args.flags.split(",") if item]
        reasons = ledger.set_flags(args.id, flags)
        print("否决: " + ("；".join(reasons) if reasons else "无"))
        return 0
    if args.cmd == "candidate-return-rate":
        ledger.set_return_rate(args.id, args.rate)
        print(args.rate)
        return 0
    if args.cmd == "quote":
        result = ledger.quote(args.id)
        _print_quote(result)
        return 0 if result.decision != Decision.INCOMPLETE else 2
    if args.cmd == "survival":
        passed, complete, rate = ledger.survival(args.site)
        if rate is None:
            print("还没有录完数据的候选品")
            return 0
        print(f"{passed}/{complete} = {rate:.1%}")
        return 0
    if args.cmd == "order-open":
        print(ledger.open_order(args.candidate))
        return 0
    if args.cmd == "order-stock":
        state = ledger.apply_stock(args.order, args.supplier, args.in_stock == "yes", args.exhausted == "yes")
        print(state.status, state.block_reason)
        return 0
    if args.cmd == "order-confirm-address":
        ledger.apply_confirm_address(args.order)
        print("address_format_ok")
        return 0
    if args.cmd == "order-arrange":
        ledger.apply_arrange(args.order)
        print("ship_arranged")
        return 0
    if args.cmd == "order-copy-address":
        ledger.apply_copy(args.order, args.address)
        print("address_copied")
        return 0
    if args.cmd == "order-purchase":
        ledger.apply_purchase(args.order)
        print("purchased")
        return 0
    if args.cmd == "order-inbound":
        ledger.apply_inbound(args.order)
        print("handed_to_warehouse")
        return 0
    if args.cmd == "checklist":
        for row in ledger.checklist_rows():
            conclusion = row["conclusion"] or ""
            when = row["checked_date"] or ""
            print(f"{row['id']}\t{row['grade']}\t{row['module']}\t{row['item']}\t{when}\t{conclusion}")
        return 0
    if args.cmd == "checklist-set":
        ledger.set_checklist(args.id, args.date, args.conclusion, args.grade)
        print(args.id)
        return 0
    if args.cmd == "import-escrow":
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        mapped = ledger.apply_escrow(args.candidate, payload)
        print(json.dumps(mapped, ensure_ascii=False))
        print("服务费未归类。确认是服务费还是运费后，再 param-set service_fee_kind。")
        return 0
    if args.cmd == "fetch-escrow":
        from shopee_ledger.api import ReadClient, ReadConfig

        payload = ReadClient(ReadConfig.from_env()).get_escrow_detail(args.order_sn)
        mapped = ledger.apply_escrow(args.candidate, payload)
        print(json.dumps(mapped, ensure_ascii=False))
        return 0
    if args.cmd == "spec-info":
        from shopee_ledger.spec import default_spec

        spec = default_spec()
        print(spec.summary())
        print("指纹:", __import__("shopee_ledger.storage", fromlist=["Storage"]).Storage(args.db, spec).fingerprint())
        if spec.problems:
            print("加载期问题:")
            for item in spec.problems:
                print("  -", item)
            return 1
        print("加载期问题: 无")
        return 0
    if args.cmd == "quote2":
        from shopee_ledger.cost_engine import CostEngine, CostInputs
        from shopee_ledger.gates import GateService
        from shopee_ledger.spec import default_spec

        spec = default_spec()
        inputs = CostInputs(
            market=args.market,
            price_local=args.price,
            purchase_cny=args.purchase,
            domestic_cny=args.domestic,
            local_per_cny=args.fx,
            sls_freight=args.sls,
            buyer_paid_freight=args.buyer_shipping,
            seller_pays_freight=args.seller_pays_freight,
            in_free_window=args.free_window,
            ad_spend=args.ads,
            withdraw_rate=args.withdraw_rate,
            fx_loss_rate=args.fx_loss_rate,
            return_rate=args.return_rate,
        )
        result = CostEngine(spec).quote(inputs)
        print(result.explain())
        gate = GateService(spec).check("G3", result.gate_context(), platform="shopee", market=args.market)
        print()
        print(gate.explain())
        if args.grant:
            from shopee_ledger.storage import Storage

            with Storage(args.db, spec) as storage:
                storage.init()
                row_id = storage.save_cost_snapshot(
                    result, subject_type="candidate", subject_id=args.grant, market=args.market)
                storage.record_audit("quote", "candidate", args.grant, result=gate.result,
                                     detail={"snapshot_id": row_id})
            print("已留档 CostSnapshot #%s（只增不改）" % row_id)
        return 0
    if args.cmd == "web":
        from shopee_ledger.web import serve

        print(f"http://{args.host}:{args.port}")
        serve(args.db, args.host, args.port)
        return 0
    raise ValueError(args.cmd)


def _print_quote(result) -> None:
    print(result.decision.value)
    if result.missing:
        print("缺少: " + ", ".join(result.missing))
    if result.rate is not None and result.net is not None:
        print(f"净利润 {result.net:.4f}  净利润率 {result.rate:.2%}")
        print(f"净运费 {result.net_shipping:.4f}  平台费 {result.platform_fee:.4f}  退货预留 {result.return_reserve:.4f}")


if __name__ == "__main__":
    raise SystemExit(main())
