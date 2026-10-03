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
    adv = sub.add_parser("order-advance", help="推进到指定状态；顺序与守卫由 spec 状态机把关")
    adv.add_argument("--order", type=int, required=True)
    adv.add_argument("--to", required=True, help="如 supplier_shipped / warehouse_scanned / in_transit")
    nxt = sub.add_parser("order-next", help="当前状态允许去哪些状态")
    nxt.add_argument("--order", type=int, required=True)

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
    sub.add_parser("alert", help="打印告警、阻塞项与被挡功能；有 P1 告警时返回码 1")
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

    sub.add_parser("sources", help="列出抓取配方与访问方式")
    fetch = sub.add_parser("fetch", help="抓公开来源，产出候选值（不直接改参数）")
    fetch.add_argument("--param", help="只抓某个参数")
    fetch.add_argument("--include-login", action="store_true", help="连需登录的来源也走一遍（只会报状态）")
    sub.add_parser("review", help="列出待确认的候选值（改了没有一眼看出）")
    appr = sub.add_parser("approve", help="确认候选值 → 写进覆盖层")
    appr.add_argument("--id", type=int, required=True)
    appr.add_argument("--grade", required=True, choices=("A", "B", "C"))
    appr.add_argument("--note", help="备注；A 级必须能说明依据")
    rej = sub.add_parser("reject", help="驳回候选值（不改参数）")
    rej.add_argument("--id", type=int, required=True)
    rej.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    ledger = Ledger(args.db)
    try:
        return _run(ledger, args)
    finally:
        ledger.close()


def _run(ledger: Ledger, args: argparse.Namespace) -> int:
    if args.cmd == "init":
        ledger.init()
        print(f"已建库 {args.db}。参数来自 spec/，本机只存实测覆盖；费率未填时选品停在「缺数据」。")
        return 0
    if args.cmd == "param-set":
        try:
            ledger.set_param(args.site, args.key, args.value, args.grade)
        except ValueError as exc:
            print("参数未保存：%s" % exc)
            print("提示：制度性费率由 spec/params 提供；本机覆盖层只接受 A/B/C 级。")
            return 1
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
        print(state.state, state.block_reason)
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
        print("address_captured")
        return 0
    if args.cmd == "order-purchase":
        ledger.apply_purchase(args.order)
        print("po_created")
        return 0
    if args.cmd == "order-inbound":
        ledger.apply_inbound(args.order)
        print("warehouse_scanned")
        return 0
    if args.cmd == "order-advance":
        try:
            state = ledger.advance_order(args.order, args.to)
        except ValueError as exc:
            print("未推进：%s" % exc)
            print("当前允许：%s" % "、".join(ledger.order_next(args.order)))
            return 1
        print("已推进到 %s" % state.state)
        return 0
    if args.cmd == "order-next":
        state = ledger.load_order(args.order)
        print("当前 %s；允许：%s" % (state.state, "、".join(state.next_states()) or "（终态）"))
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
    if args.cmd == "sources":
        from shopee_ledger.sources import load_sources

        for source in load_sources():
            access = "公开" if source.access == "public" else "需登录"
            print("%-22s %-26s %-6s %-11s %s" % (
                source.id, source.param_id, access, source.review_cycle, source.url))
        return 0
    if args.cmd == "fetch":
        from shopee_ledger.sources import fetch_source, load_sources

        sources = load_sources()
        if args.param:
            sources = [item for item in sources if item.param_id == args.param]
        todo = [item for item in sources if args.include_login or item.access == "public"]
        if not todo:
            print("没有可抓的配方（需登录的来源请用油猴脚本，或加 --include-login 看状态）")
            return 0
        ledger.init()
        failed = 0
        for source in todo:
            capture = fetch_source(source)
            ledger.record_capture(capture)
            print("  " + capture.describe())
            if not capture.ok:
                failed += 1
        pending = ledger.pending_candidates()
        print("\n抓到 %d 条候选，失败 %d 条；待确认共 %d 条（用 review / approve）"
              % (len(todo) - failed, failed, len(pending)))
        return 1 if failed else 0
    if args.cmd == "review":
        rows = ledger.pending_candidates()
        if not rows:
            print("没有待确认的候选值")
            return 0
        for row in rows:
            print("#%s %s  %s  当前 %s → 抓到 %s   [%s %s]" % (
                row["id"], row["param_id"], "**变了**" if row["changed"] else "未变",
                row["current_value"], row["value"], row["captured_at"], row.get("channel") or ""))
            if row.get("source_url"):
                print("     来源 %s" % row["source_url"])
            if row.get("snapshot_ref"):
                print("     快照 %s" % row["snapshot_ref"])
            if row.get("message"):
                print("     备注 %s" % row["message"])
        print("\n确认：approve --id N --grade A|B|C    驳回：reject --id N --reason ...")
        return 0
    if args.cmd == "approve":
        try:
            ledger.approve_candidate(args.id, args.grade, note=args.note)
        except ValueError as exc:
            print("未确认：%s" % exc)
            return 1
        print("候选 #%s 已确认，参数升级到 %s 级（功能随之启用）" % (args.id, args.grade))
        return 0
    if args.cmd == "reject":
        try:
            ledger.reject_candidate(args.id, args.reason)
        except ValueError as exc:
            print("未驳回：%s" % exc)
            return 1
        print("候选 #%s 已驳回，参数未变" % args.id)
        return 0
    if args.cmd == "alert":
        alerts = ledger.order_alerts()
        blocking = [row for row in ledger.checklist_rows()
                    if row.get("blocks_first_order") and not row["conclusion"]]
        unlocks = ledger.unverified_unlocks()
        if alerts:
            print("告警（%d）:" % len(alerts))
            for item in alerts:
                stay = ("（已停留 %s 小时）" % item["hours_in_state"]
                        if item.get("hours_in_state") is not None else "")
                print("  [%s] 订单 #%s %s：%s%s" % (
                    item["priority"], item["order_id"], item.get("candidate") or "",
                    item["message"], stay))
        else:
            print("告警：无")
        if blocking:
            print("阻塞第一单的核实（%d）:" % len(blocking))
            for row in blocking:
                print("  [%s] %s（%s）" % (row.get("spec_task_id") or "", row["item"], row["channel"] or ""))
        if unlocks:
            print("未核实参数挡着：")
            for item in unlocks:
                print("  %s（%s 级%s）→ %s" % (
                    item["param_id"], item["level"],
                    "，任务 " + item["task_ref"] if item.get("task_ref") else "", item["feature"]))
        return 1 if [item for item in alerts if item.get("priority") == "P1"] else 0
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
