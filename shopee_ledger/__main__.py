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
    watch = sub.add_parser("watch", help="列表页监控：列出已记录的文档")
    watch.add_argument("--new", action="store_true", help="只看还没看过的新文档")
    watch.add_argument("--seen", type=int, metavar="ENTRY_ID", help="把某条标记为已读")
    watch.add_argument("--from-file", metavar="HTML",
                       help="导入一个 Ctrl+S 存下来的列表页（渲染后的 HTML）")
    watch.add_argument("--url", help="配合 --from-file：这页的地址（用于对上 watch 配置）")
    watch.add_argument("--fetch", action="store_true",
                       help="用本机无头浏览器渲染已登记的列表页并比对（我自己跑，不需要浏览器插件）")
    appr = sub.add_parser("approve", help="确认候选值 → 写进覆盖层")
    appr.add_argument("--id", type=int, required=True)
    appr.add_argument("--grade", required=True, choices=("A", "B", "C"))
    appr.add_argument("--note", help="备注；A 级必须能说明依据")
    appr.add_argument("--force", action="store_true",
                      help="同一参数已有更晚候选时仍确认这一条（默认拒绝）")
    rej = sub.add_parser("reject", help="驳回候选值（不改参数）")
    rej.add_argument("--id", type=int, required=True)
    rej.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    ledger = Ledger(args.db)
    try:
        return _run(ledger, args)
    finally:
        ledger.close()


def _watch_fetch(ledger, args) -> int:
    """用本机无头浏览器渲染已登记的列表页并比对。

    这是"我自己跑"的通路：不需要浏览器插件，不需要人点。
    只抓第 1 页——新通知总在最上面，监测够用了。
    """
    from shopee_ledger.watch import find_browser, load_watches, parse_listing_html, render_page

    watches = [item for item in load_watches() if item.access == "public" and item.url.startswith("http")]
    if not watches:
        print("spec/sources.json 里没有可抓的列表页（watch 段的 url 要是 http 开头）")
        return 1
    browser = find_browser()
    print("浏览器: %s" % (browser or "没找到 Chrome/Edge"))
    if not browser:
        return 1
    ledger.init()
    total_new = 0
    for item in watches:
        html, error = render_page(item.url)
        if not html:
            print("  %s 渲染失败：%s" % (item.id, error))
            continue
        entries, pager = parse_listing_html(html, base_url=item.url)
        if not entries:
            print("  %s 渲染成功但没解析出条目——页面结构可能变了" % item.id)
            continue
        result = ledger.record_listing(item.id, entries, page_url=item.url)
        print("  %s：本页 %d 篇，新出现 %d 篇%s"
              % (item.id, result["total"], len(result["new"]),
                 "（分页 当前 %s）" % pager["current"] if pager["current"] else ""))
        for fresh in result["new"]:
            print("     🆕 %s  %s" % (fresh["published_at"] or "日期未知", fresh["title"]))
        total_new += len(result["new"])
    print("\n合计新文档 %d 篇。看全部：shopee_ledger watch" % total_new)
    return 0


def _watch_from_file(ledger, args) -> int:
    """导入一个 Ctrl+S 存下来的列表页。

    这是不依赖油猴脚本的兜底通路：保存的页面是**渲染后的 DOM**，里面有真实链接。
    """
    from pathlib import Path as _Path

    from shopee_ledger.sources import page_key
    from shopee_ledger.watch import load_watches, parse_listing_html

    path = _Path(args.from_file)
    if not path.exists():
        print("找不到文件：%s" % path)
        return 1
    html = path.read_text(encoding="utf-8", errors="replace")
    url = args.url or ""
    known = next((item for item in load_watches() if url and page_key(item.url) == page_key(url)),
                 None)
    watch_id = known.id if known else "WATCH-MANUAL"
    entries, pager = parse_listing_html(html, base_url=url or "https://shopee.cn")
    if not entries:
        print("没解析出条目——这页可能不是列表页（或保存时没等渲染完）")
        return 1
    ledger.init()
    result = ledger.record_listing(watch_id, entries, page_url=url)
    print("导入 %s" % path.name)
    print("  条目 %d 条；分页 当前 %s / 共 %s 个页码 / 有下一页 %s"
          % (result["total"], pager["current"] or "?", pager["page_count"], pager["has_next"]))
    if result["new"]:
        print("  🆕 新出现 %d 篇：" % len(result["new"]))
        for item in result["new"]:
            print("     %s  %s" % (item["published_at"] or "日期未知", item["title"]))
    else:
        print("  （没有新文档）")
    if watch_id == "WATCH-MANUAL":
        print("  提示：加了 --url 才能对上 spec/sources.json 里的 watch 配置")
    return 0


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
    if args.cmd == "watch":
        if args.from_file:
            return _watch_from_file(ledger, args)
        if args.fetch:
            return _watch_fetch(ledger, args)
        if args.seen:
            ledger.mark_watch_seen(args.seen)
            print("已标记 #%s 为已读" % args.seen)
            return 0
        rows = ledger.watch_entries(only_new=args.new)
        if not rows:
            print("还没有列表页记录。用油猴面板的「这是列表页」按钮抓一次，"
                  "或在 spec/sources.json 的 watch 段里填好列表页地址。")
            return 0
        for row in rows:
            mark = "🆕" if row.get("status") == "new" else "  "
            print("%s #%-4s %-12s %-8s %s" % (mark, row["id"], row.get("published_at") or "日期未知",
                                              row["article_id"], row["title"][:60]))
        fresh = len([row for row in rows if row.get("status") == "new"])
        print("\n共 %d 篇；🆕 %d 篇未读（标记已读：watch --seen <#id>）" % (len(rows), fresh))
        print("新文档只是线索——正文要打开后点「抓这一页」，值仍要你确认。")
        apis = ledger.discovered_apis()
        if apis:
            print("\n浏览器报回来的数据接口（拿到它就能把翻页与定时搬到服务端）：")
            for item in apis:
                print("  %-3s 次  %s" % (item["hits"], item["url"][:110]))
        else:
            print("\n还没发现数据接口——打开列表页时会自动记录（脚本会上报 performance 里的真实请求）。")
        return 0
    if args.cmd == "review":
        rows = ledger.pending_candidates()
        if not rows:
            print("没有待确认的候选值")
            return 0
        conflicts = ledger.candidate_conflicts()
        if conflicts:
            print("⚠️ 有 %d 个参数存在多条候选（新旧不一）——系统只标哪条最新，不替选：\n"
                  % len(conflicts))
        for row in rows:
            marks = []
            if row["conflict_count"] > 1:
                marks.append("最新" if row["is_newest"]
                             else "旧，已被 #%s 覆盖" % row["superseded_by"])
            if row["changed"]:
                marks.append("与当前值不同")
            print("#%-4s %-22s 当前 %-11s → 抓到 %-11s %s" % (
                row["id"], row["param_id"], row["current_value"], row["value"],
                " ".join("[%s]" % mark for mark in marks)))
            print("      抓于 %s  %s" % (row["captured_at"], row.get("source_url") or ""))
            if row.get("snapshot_ref"):
                print("      快照 %s" % row["snapshot_ref"])
        print("\n确认：approve --id N --grade A|B|C [--force]    驳回：reject --id N --reason ...")
        print("有更晚候选时，确认旧的会被拒——要确认旧的必须显式 --force。")
        return 0
    if args.cmd == "approve":
        try:
            ledger.approve_candidate(args.id, args.grade, note=args.note, force=args.force)
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
