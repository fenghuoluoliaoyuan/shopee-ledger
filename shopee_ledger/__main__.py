"""命令行。用法：python -m shopee_ledger <命令>"""

from __future__ import annotations

import argparse
import datetime as _date
import json
from pathlib import Path

from shopee_ledger.profit import Decision
from shopee_ledger.store import DEFAULT_DB, PARAM_HELP, Ledger



def root_reference_dir():
    """spec/reference 目录——delivery 命令刷新数据时用。"""
    return Path(__file__).resolve().parent.parent / "spec" / "reference"


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
    q2.add_argument("--sls", type=float, help="SLS 运费（当地币）；缺省且给了 --weight-g 与 --channel 时按官方运费表算")
    q2.add_argument("--weight-g", type=float, help="包裹重量（克）：配合 --channel 从官方运费表算运费")
    q2.add_argument("--channel", help="物流渠道名（支持部分匹配），如 蝦皮店到店")
    q2.add_argument("--cargo", default="Normal", choices=("Normal", "Special"), help="货类：普货/特货")
    q2.add_argument("--coupon", type=float, default=0.0, help="优惠券与回扣（官方结算口径里从订单收入减掉）")
    q2.add_argument("--order-adjustment", type=float, default=0.0,
                    help="订单调整，如马来西亚高价值商品税（官方结算口径里的单独一项）")
    q2.add_argument("--buyer-shipping", type=float, help="买家实付运费")
    q2.add_argument("--seller-pays-freight", action="store_true", help="卖家包邮")
    q2.add_argument("--return-rate", type=float, help="退货率预留（覆盖参数）")
    q2.add_argument("--withdraw-rate", type=float, help="提现费率（覆盖参数）")
    q2.add_argument("--fx-loss-rate", type=float, help="汇损率（覆盖参数）")
    q2.add_argument("--ads", type=float, default=0.0)
    q2.add_argument("--free-window", action="store_true", help="处于免佣窗口内")
    q2.add_argument("--grant", type=int, help="不填则只打印；填候选品 id 则留档成本快照")

    assoc = sub.add_parser("associate",
                           help="把抓到的文档关联到它可能回答的参数（只定位，不取值）")
    assoc.add_argument("--param", help="只看某个参数")
    assoc.add_argument("--context", action="store_true", help="连命中处的上下文一起打印")

    fr = sub.add_parser("freight", help="拉取 SLS 运费费率表并与仓库里最新快照比对变化")
    fr.add_argument("--date", help="生效日期 YYYY-MM-DD（默认今天）")
    fr.add_argument("--check", action="store_true", help="只比对不保存；有变化时返回码 1")
    fr.add_argument("--list", metavar="CHANNEL", help="列出名字含该关键词的渠道费率档位")

    sub.add_parser("export-verified",
                   help="把核实成果导出到 spec/verified.json（真值进 git，数据库只是派生物）")
    imp = sub.add_parser("import-verified",
                         help="把 spec/verified.json 回灌进库（换机器/重建库后恢复核实成果）")
    imp.add_argument("--dry-run", action="store_true", help="只报将要写入什么")

    land = sub.add_parser("landed",
                          help="多站点落地成本对比：同一个品在各站点/渠道需要卖多少钱才达标")
    land.add_argument("--purchase", type=float, required=True, help="采购实付（人民币）")
    land.add_argument("--domestic", type=float, default=0.0, help="国内段运费（人民币）")
    land.add_argument("--weight-g", type=float, required=True, help="包裹重量（克）")
    land.add_argument("--target-margin", type=float, default=0.15, help="目标净利率，如 0.15")
    land.add_argument("--fx", action="append", default=[],
                      metavar="MY=0.65", help="各市场汇率（可重复），缺的市场不参与对比")
    land.add_argument("--market", action="append", default=[], help="只比这几个市场")
    land.add_argument("--cargo", default="Normal", choices=("Normal", "Special"))
    land.add_argument("--per-market", type=int, default=4, help="每个市场列几个渠道（按运费从低到高）")
    land.add_argument("--channel", help="只看名字含该关键词的渠道")
    land.add_argument("--seller-pays-freight", action="store_true")
    land.add_argument("--withdraw-rate", type=float, default=0.0)
    land.add_argument("--fx-loss-rate", type=float, default=0.0)
    land.add_argument("--return-rate", type=float, default=0.0)

    sub.add_parser("manual",
                   help="待人工读数的留档：浏览器抓到了正文但没配方，值要人来读")

    delivery = sub.add_parser("delivery",
                              help="算一个订单的最迟发货时间与最迟到仓扫描时间")
    delivery.add_argument("--market", default="TW", help="站点代码，如 TW/TH/BR")
    delivery.add_argument("--order-date", help="下单日 YYYY-MM-DD")
    delivery.add_argument("--dts", type=int, default=1, help="备货时长（发货日），默认 1")
    delivery.add_argument("--verify", action="store_true",
                          help="拿存下来的口径样本回验规则，不传 --order-date")
    delivery.add_argument("--refresh", action="store_true",
                          help="重新拉豁免日期表与口径样本（节假日每年会变）")

    sub.add_parser("calibrate",
                   help="KPI 校准：拿实测值对照经验值（只报告，不自动改阈值）")

    sub.add_parser("check-lint",
                   help="静态检查：函数里有没有「赋值前读取」的局部名")
    sub.add_parser("check-snapshots",
                   help="体检 A 级参数的快照：文件在不在、进没进版本库")
    sub.add_parser("check-sources", help="检查各参数来源 URL 是否真的打得开（A 级的定义就是可打开）")
    harvest = sub.add_parser("harvest", help="用无头浏览器抓已监测文档的正文，留档待读（自己找参数值用）")
    harvest.add_argument("--limit", type=int, default=40, help="本次最多抓几篇")
    harvest.add_argument("--redo", action="store_true", help="已有的正文也重抓一遍")
    harvest.add_argument("--only", action="append", default=[],
                         help="只抓指定文章号（可重复）——定向抓，不必把整批都拉下来")
    fetch = sub.add_parser("fetch", help="抓公开来源，产出候选值（不直接改参数）")
    fetch.add_argument("--param", help="只抓某个参数")
    fetch.add_argument("--include-blocked", action="store_true",
                        help="连已知打不开的来源也重试一遍（默认跳过）")
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
    if args.cmd == "harvest":
        from shopee_ledger.browser import BrowserError, Chrome
        from shopee_ledger.sources import save_snapshot
        from shopee_ledger.watch import (STATUS_PAGE_GATED, article_text, find_browser,
                                         page_status)

        if not find_browser():
            print("找不到 Chrome/Edge，无法渲染正文")
            return 1
        ledger.init()
        if args.only:
            wanted = set(args.only)
            entries = [row for row in ledger.all_watch_entries()
                       if row["article_id"] in wanted]
            missed = wanted - {row["article_id"] for row in entries}
            if missed:
                print("列表里没有这些文章号：%s" % "、".join(sorted(missed)))
        else:
            entries = (ledger.all_watch_entries() if args.redo
                       else ledger.entries_without_content())[: args.limit]
        if not entries:
            print("没有需要抓正文的文档（都已留档；要重抓加 --redo）")
            return 0
        print("要抓 %d 篇（共用一条浏览器会话）…" % len(entries))
        ok = failed = gated = 0
        try:
            with Chrome() as chrome:
                for index, entry in enumerate(entries, 1):
                    try:
                        chrome.open(entry["url"], wait_seconds=5.0)
                        html = chrome.html()
                    except BrowserError as exc:
                        print("  [%d/%d] %s 失败：%s" % (index, len(entries),
                                                        entry["article_id"], exc))
                        failed += 1
                        continue
                    # 浏览器连不上时会把错误页也渲染出来——别把它当正文存下来
                    from shopee_ledger.browser import render_failed

                    failure = render_failed(html)
                    if failure:
                        print("  [%d/%d] %s 浏览器打不开这页：%s"
                              % (index, len(entries), entry["article_id"], failure))
                        failed += 1
                        continue
                    text = article_text(html)
                    if len(text) < 200:
                        print("  [%d/%d] %s 正文太短（%d 字）"
                              % (index, len(entries), entry["article_id"], len(text)))
                        failed += 1
                        continue
                    # 登录门禁与抓取失败要分开：前者只能等人登录，后者要修抓取
                    state = page_status(text)
                    if state == STATUS_PAGE_GATED:
                        print("  [%d/%d] %s 登录门禁（%d 字，正文区只剩登录提示）"
                              % (index, len(entries), entry["article_id"], len(text)))
                        gated += 1
                        continue
                    ref, _ = save_snapshot(text, "ARTICLE-" + entry["article_id"])
                    ledger.save_article_text(entry["id"], ref, length=len(text))
                    print("  [%d/%d] %s %4d 字  %s"
                          % (index, len(entries), entry["article_id"], len(text),
                             entry["title"][:38]))
                    ok += 1
        except BrowserError as exc:
            print("浏览器起不来：%s" % exc)
            return 1
        print("\n成功 %d 篇，失败 %d 篇，登录门禁 %d 篇" % (ok, failed, gated))
        return 0
    if args.cmd == "associate":
        from pathlib import Path as _Path

        from shopee_ledger.associate import associate, coverage_report, load_keywords

        keywords = load_keywords()
        if not keywords:
            print("spec/keywords.json 是空的——先给参数配关键词")
            return 1
        ledger.init()
        documents = []
        for row in ledger.all_watch_entries():
            if not row.get("content_ref"):
                continue
            path = _Path(row["content_ref"])
            if not path.is_absolute():
                path = _Path(__file__).resolve().parent.parent / row["content_ref"]
            if not path.exists():
                continue
            documents.append({"article_id": row["article_id"], "url": row["url"],
                              "title": row["title"], "published_at": row["published_at"],
                              "text": path.read_text(encoding="utf-8", errors="replace")})
        if not documents:
            print("还没有文档正文——先跑 harvest")
            return 1
        findings = associate(documents, keywords=keywords, only=args.param)
        # 传 spec 参数（不是 keywords）：coverage_report 要按「还需要取证吗」分类，
        # 只给关键词表的话每个参数都会被判成「还需要取证」——实测 6 个 A 级参数
        # 被报成「一条线索都没找到」，看起来像缺口，其实早就核实了。
        report = coverage_report(findings, ledger.spec.params)
        print("已读 %d 篇正文；%d 个参数里有 %d 个找到线索\n"
              % (len(documents), len(keywords), report["with_hits"]))
        for finding in findings:
            level = ledger.spec.params[finding.param_id].evidence_level \
                if finding.param_id in ledger.spec.params else "?"
            print("=== %s（当前 %s 级，%d 篇候选）" % (finding.param_id, level,
                                                     finding.document_count))
            for article_id, hits in finding.by_document().items():
                hit = hits[0]
                print("   %s  %-7s %s" % (hit.published_at or "?", article_id, hit.title[:40]))
                if args.context:
                    print("       [%s] %s" % (hit.keyword, hit.context[:150]))
            print()
        if report["without_hits"]:
            print("**还需要取证**但一条线索都没找到的参数（%d 个）：" % len(report["without_hits"]))
            print("  " + "、".join(report["without_hits"]))
            print("  提示：可能是关键词没配，或这类信息只在需登录的页面（卖家中心/帮助中心）")
        verified = report.get("verified_without_hits") or []
        if verified:
            print("\n另有 %d 个**已核实**的参数没有新线索——不算缺口，不逐个列了：%s%s"
                  % (len(verified), "、".join(verified[:5]), "…" if len(verified) > 5 else ""))
        print("\n只定位不取值——取值要读原文判断适用范围与生效日期。")
        return 0
    if args.cmd == "freight":
        import urllib.request

        from shopee_ledger import freight as fr

        date = args.date or _date.date.today().isoformat()
        try:
            config = fr.fetch_site_config(date)
        except Exception as exc:
            print("拉取失败（%s）：%s" % (date, exc))
            return 1
        cards = fr.rate_cards(config)
        sites = sorted({key.split("/")[0] for key in cards})
        print("运费费率表 生效日期 %s ｜ %d 个站点 ｜ %d 条档位" % (config["date"], len(sites), len(cards)))

        if args.list:
            channel = args.list.lower()
            for key in sorted(cards):
                if channel in key.lower():
                    card = cards[key]
                    print("  %-58s 首重 %s 终重 %s 费用 %s 增量 %s/%s 生效 %s"
                          % (key[:58], card["start_weight_g"], card["end_weight_g"],
                             card["fee"], card["increment_amount"],
                             card["increment_unit_g"], card["effective_date"]))
            return 0

        previous_path = fr.latest_reference()
        if not previous_path:
            print("仓库里还没有快照——本次将保存为基准")
            previous = None
        else:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))["data"]
            print("与仓库快照比对：%s" % previous_path.name)

        changes = fr.diff_config(previous, config) if previous else []
        if previous and not changes:
            print("没有变化。")
            if args.check:
                return 0
        else:
            print("发现 %d 处变化：" % len(changes))
            for item in changes[:25]:
                print("  " + item)
            if len(changes) > 25:
                print("  …共 %d 处" % len(changes))

        if args.check:
            return 1 if changes else 0

        target = fr.REFERENCE_DIR / ("sls-site-config-%s.json" % config["date"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"code": 0, "data": config}, ensure_ascii=False) + "\n",
                          encoding="utf-8")
        print("已保存 %s" % target.name)
        ledger.init()
        ledger.storage.record_audit("freight.refresh", "Param", "P-TW-SLS-TIERS",
                                    result="PASS" if not changes else "WARN",
                                    detail={"date": config["date"], "cards": len(cards),
                                            "changes": changes[:20]})
        return 0
    if args.cmd == "export-verified":
        from shopee_ledger.verified import build_verified, write_verified

        ledger.init()
        payload = build_verified(ledger.storage.list("ParamOverride", limit=2000),
                                 ledger.storage.list("VerificationTask", limit=2000))
        path = write_verified(payload)
        print("已导出 %d 个参数覆盖、%d 条核实结论 → %s"
              % (len(payload["params"]), len(payload["checklist_done"]), path))
        print("数据库是派生物；真值现在随 git 走了。")
        return 0
    if args.cmd == "import-verified":
        from shopee_ledger.verified import read_verified

        payload = read_verified()
        print("文件里有 %d 个参数覆盖、%d 条核实结论"
              % (len(payload["params"]), len(payload["checklist_done"])))
        if args.dry_run:
            for param_id, item in sorted(payload["params"].items()):
                print("   将写入 %-26s %s 级" % (param_id, item.get("evidence_level")))
            return 0
        ledger.init()
        restored_params = restored_tasks = 0
        for param_id, item in sorted(payload["params"].items()):
            existing = [row for row in ledger.storage.list("ParamOverride", limit=2000)
                        if row["param_id"] == param_id
                        and row.get("checked_at") == item.get("checked_at")]
            if existing:
                continue
            ledger.storage.insert("ParamOverride", {
                "param_id": param_id, "value": item.get("value"),
                "evidence_level": item.get("evidence_level"),
                "source_url": item.get("source_url"), "snapshot_ref": item.get("snapshot_ref"),
                "checked_at": item.get("checked_at"),
                "note": "从 spec/verified.json 回灌",
                "operator": item.get("operator") or "import",
            })
            restored_params += 1
        tasks = {row["spec_task_id"]: row for row in ledger.checklist_rows()}
        for item in payload["checklist_done"]:
            row = tasks.get(item.get("spec_task_id"))
            if not row or row["conclusion"]:
                continue
            ledger.set_checklist(row["id"], item.get("checked_date") or "",
                                 item.get("conclusion") or "", item.get("grade"),
                                 source_url=item.get("source_url"),
                                 snapshot_ref=item.get("snapshot_ref"))
            restored_tasks += 1
        print("回灌完成：参数覆盖 %d 条、核实结论 %d 条" % (restored_params, restored_tasks))
        return 0
    if args.cmd == "landed":
        from shopee_ledger.landed import compare, reference_fx_by_market, render

        fx_by_market: dict[str, float] = {}
        for item in args.fx:
            if "=" not in item:
                print("--fx 格式应为 市场=汇率，例如 MY=0.65；收到 %r" % item)
                return 1
            code, _, value = item.partition("=")
            fx_by_market[code.strip().upper()] = float(value)
        if not fx_by_market:
            fx_by_market = reference_fx_by_market()
            print("未指定 --fx，使用官方成本计算器的参考汇率（B 级，非结算汇率）\n")

        rows = compare(ledger.spec, purchase_cny=args.purchase, domestic_cny=args.domestic,
                       weight_g=args.weight_g, fx_by_market=fx_by_market,
                       target_margin=args.target_margin,
                       withdraw_rate=args.withdraw_rate, fx_loss_rate=args.fx_loss_rate,
                       return_rate=args.return_rate,
                       seller_pays_freight=args.seller_pays_freight,
                       markets=[m.upper() for m in args.market] or None,
                       cargo=args.cargo, channels_per_market=args.per_market,
                       channel_filter=args.channel)
        print(render(rows, target_margin=args.target_margin, purchase_cny=args.purchase,
                     domestic_cny=args.domestic, weight_g=args.weight_g))
        return 0
    if args.cmd == "delivery":
        # **不要在这里 `from datetime import date as _date`。**
        # 模块顶层已经有 `import datetime as _date`，函数内的同名 import 会把
        # 整个 _run 里的 _date 变成局部变量——于是同一函数里靠后的 freight 分支
        # 一访问就 UnboundLocalError（实测：freight --check 直接崩）。
        # Python 的规则是"函数里只要有一次对某名字的赋值，它就是全程局部"。
        from shopee_ledger.delivery import (BUSINESS_DAY_MARKETS, SOURCE_URL, deadlines,
                                            verify_samples)

        if args.refresh:
            import json as _json
            import urllib.request as _urlopen

            from shopee_ledger.delivery import EXEMPT_API, RESULT_API

            folder = root_reference_dir()
            request = _urlopen.Request(EXEMPT_API, headers={"User-Agent": "Mozilla/5.0"})
            with _urlopen.urlopen(request, timeout=25) as response:
                doc = _json.loads(response.read().decode("utf-8"))
            if doc.get("code") != 200000:
                print("豁免日期表接口返回异常：code=%s" % doc.get("code"))
                return 1
            (folder / "delivery-exempt-days.json").write_text(
                _json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("豁免日期表已更新：%d 条" % len(doc.get("data") or []))
            print("  （口径样本不在这里重抓——它是对规则的凭据，要显式重取）")
            print("  API：%s" % EXEMPT_API)
            return 0
        if args.verify:
            report = verify_samples()
            if not report["available"]:
                print("没有口径样本文件：spec/reference/delivery-deadline-samples.json")
                return 1
            print("规则回验：%d 例，不一致 %d 例" % (report["checked"],
                                                    len(report["mismatched"])))
            for item in report["mismatched"][:10]:
                print("  x %s 下单%s dts=%s  应为 %s，规则算出 %s"
                      % (item["site"], item["order_date"], item["dts"],
                         item["expected"], item["predicted"]))
            if report["mismatched"]:
                print("\n规则与样本不符——要么规则被改错了，要么样本过期了。别急着改样本。")
                return 1
            print("规则与官方接口口径一致。")
            print("来源：%s" % (report.get("source") or SOURCE_URL))
            return 0

        if not args.order_date:
            print("要给 --order-date YYYY-MM-DD，或者用 --verify 只回验规则")
            return 1
        try:
            order_day = _date.date.fromisoformat(args.order_date)
        except ValueError:
            print("--order-date 应当是 YYYY-MM-DD，收到 %r" % args.order_date)
            return 1
        # 校验站点：只有 TH/BR/AR 走"发货日"口径，拼错会静默套用另一种规则。
        # 界限用计算器覆盖的 9 站，而不是项目当前的 5 个市场维度——
        # 数据是官方给全的，没必要因为维度还没扩就不让算。
        from shopee_ledger.delivery import CALCULATOR_MARKETS

        try:
            result = deadlines(order_day, market=args.market, dts_days=args.dts,
                               known_markets=CALCULATOR_MARKETS)
        except ValueError as exc:
            print(exc)
            return 1
        print(result.render())
        active = {m.get("code") for m in (ledger.spec.registry.get("markets") or [])}
        if result.market not in active:
            print("\n注：%s 不在项目当前的市场维度（%s）里，所以订单与账目还用不上它；"
                  "这里只是能算。" % (result.market, "、".join(sorted(c for c in active if c))))
        print("\n发货日 = 非周日、非节假日（**周六算工作日**，实测）；"
              "自然日站点的扫描截止只看日历，不顺延。")
        print("按发货日推扫描截止的站点：%s" % "、".join(sorted(BUSINESS_DAY_MARKETS)))
        print("来源：%s（公开接口，无需登录）" % SOURCE_URL)
        return 0
    if args.cmd == "manual":
        ledger.init()
        queue = ledger.manual_queue()
        if not queue:
            print("没有待读数的留档。")
            print("登录 Shopee 后打开目标页面 → 面板里选好参数 → 点「抓这一页」，")
            print("正文会留档进这里；读数由我（或你）来做，不需要你抄字。")
            return 0
        print("待读数的留档 %d 条：\n" % len(queue))
        for item in queue:
            param = ledger.spec.params.get(item["param_id"])
            print("  %s" % item["param_id"])
            if param is not None:
                print("     参数：%s（当前 %s 级）"
                      % (getattr(param, "name", ""), param.evidence_level))
            print("     页面：%s" % (item["url"] or "（没记 URL）"))
            path = Path(item["snapshot_ref"])
            if not path.is_absolute():
                path = Path(__file__).resolve().parent.parent / item["snapshot_ref"]
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                print("     正文：%s（%d 字）" % (item["snapshot_ref"], len(text)))
            else:
                print("     正文：%s（**文件不在了**）" % item["snapshot_ref"])
            print("     抓于：%s" % item["at"])
            print()
        print("下一步：读这些正文得出值，再 approve 到对应参数。")
        print("读数不需要原页面——正文已经在这里了。")
        return 0
    if args.cmd == "calibrate":
        from shopee_ledger.calibrate import render

        ledger.init()
        items = ledger.calibration_items()
        if not items:
            print("没有可校准的参数")
            return 0
        print(render(items))
        return 0
    if args.cmd == "check-lint":
        from pathlib import Path as _Path

        from shopee_ledger.unbound import check_package

        found = check_package(_Path(__file__).resolve().parent)
        print("静态检查「赋值前读取」：%d 处" % len(found))
        for item in found:
            print("  " + item.render())
        if found:
            print()
            print("这些分支一旦被走到就会 UnboundLocalError——而且正因测试没走到才活下来。")
            return 1
        print("干净。这一条守的是两个真实 bug（第 11 / 12 轮各一个），")
        print("它们共同的特征是：赋值藏在条件分支里、读取在分支之外且在赋值之前。")
        return 0
    if args.cmd == "check-snapshots":
        from shopee_ledger.snapshots import audit

        ledger.init()
        items = audit(ledger.spec.params)
        usable = [item for item in items if item.usable]
        missing = [item for item in items if not item.ref]
        broken = [item for item in items if item.ref and not item.usable]
        print("A 级参数的快照体检：%d 个" % len(items))
        print("  可用            %d" % len(usable))
        print("  没写 snapshot_ref %d" % len(missing))
        print("  写了但不可用     %d" % len(broken))
        for item in missing:
            print("     %-26s 需要补 snapshot_ref（来源本身打不开的话，是已知缺口）"
                  % (item.param_id or "?"))
        for item in broken:
            print("     %-26s %s" % (item.param_id or "?", item.ref))
            print("        %s" % item.detail)
        if broken:
            print("\n**引用字符串在 ≠ 证据在**：文件没进版本库的话，clone 出来证据链就是断的。")
            return 1
        if missing:
            print("\n没写引用的这些是已知缺口（来源本身打不开）。")
        else:
            print("\n全部可用。")
        return 0
    if args.cmd == "check-sources":
        import urllib.error
        import urllib.request

        # 每条来源 URL → 哪些参数在用它
        usage: dict[str, list[str]] = {}
        for pid, param in sorted(ledger.spec.params.items()):
            url = param.source.get("url")
            if url:
                usage.setdefault(url, []).append("%s(%s)" % (pid, param.evidence_level))
        for row in ledger.storage.list("ParamOverride", limit=500):
            url = row.get("source_url")
            if url:
                usage.setdefault(url, []).append("覆盖:%s(%s)" % (row["param_id"],
                                                                row.get("evidence_level")))
        if not usage:
            print("没有任何来源 URL")
            return 0
        print("检查 %d 个来源 URL…\n" % len(usage))
        broken = []
        statuses: list[tuple[str, str]] = []
        kinds: dict[str, str] = {}
        from shopee_ledger.reachability import CERT_ISSUE, FAILURE_LABEL, classify_failure

        for url, users in sorted(usage.items()):
            status = ""
            kind = "ok"
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
                with urllib.request.urlopen(request, timeout=8) as response:
                    status = "HTTP %s" % response.status
            except urllib.error.HTTPError as exc:
                kind = classify_failure(exc)
                status = "HTTP %s" % exc.code
                # 4xx/5xx 的来源 URL 不能当证据用——它现在打不开了
                if kind != "ok":
                    broken.append((url, users))
            except Exception as exc:
                # 证书问题与网络不通要分开：前者站点是通的，只是本机信任库不认，
                # 浏览器往往读得到（实测踩过：泰国税务那篇因此被搁置好几轮）。
                kind = classify_failure(exc)
                status = FAILURE_LABEL.get(kind, "打不开")
                if kind != CERT_ISSUE:
                    broken.append((url, users))
                else:
                    broken.append((url, users))
            kinds[url] = kind
            statuses.append((url, status))
            mark = "OK  " if kind == "ok" else ("⚠  " if kind == CERT_ISSUE else "❌  ")
            print("%s%-58s %-34s %s" % (mark, url[:58], status, ", ".join(users)[:50]))
        print("\n打不开的来源 %d 个" % len(broken))
        # 把可达性记成证据：这是「关于证据的证据」，A 级是否成立要看它
        from shopee_ledger.reachability import record_reachability

        record = record_reachability(ledger.path.parent.parent,
                                     [(url, status) for url, status in statuses],
                                     kind_by_url=kinds)
        print("已记录到 %s（%s 条）" % (record, len(statuses)))
        cert = [url for url, kind in kinds.items() if kind == CERT_ISSUE]
        if cert:
            print("\n⚠ %d 个来源是**证书验证失败**（不是网络不通）：" % len(cert))
            for url in cert:
                print("   %s" % url[:90])
            print("  站点可能读得到——用无头浏览器再试（Chrome 有自己的信任库），")
            print("  或直接 python -m shopee_ledger harvest 抓它。别急着判定「不可达」。")
        if broken:
            print("\n打不开的来源 %d 个" % len(broken))
            print("提示：A 级的定义是「有可打开的 URL」。这些 URL 打不开，")
            print("      对应的 A 级证据就只是记录，不构成可复核的证据。")
            print("      若你用代理/VPN 能打开，重跑本命令即可更新这份记录。")
            return 1
        return 0
    if args.cmd == "sources":
        from shopee_ledger.sources import load_sources

        for source in load_sources():
            access = "公开" if source.access == "public" else "需登录"
            print("%-22s %-26s %-6s %-11s %s" % (
                source.id, source.param_id, access, source.review_cycle, source.url))
        return 0
    if args.cmd == "fetch":
        from shopee_ledger.browser import BrowserError, Chrome
        from shopee_ledger.sources import fetch_source, load_sources

        sources = load_sources()
        if args.param:
            sources = [item for item in sources if item.param_id == args.param]
        # 已知打不开的来源默认跳过：每天重试只会让每日任务恒返回失败码，
        # 而"每天都失败"会训练人忽略失败。缺口本身记在 sources.json 的 blocked_reason
        # 与 spec/evidence/reachability.json 里，不会因为"跳过"而消失。
        # 注意要从 sources 里挑 blocked，不能从 todo 里挑——todo 的 public 过滤已经把它们排除了。
        blocked = [item for item in sources if item.access == "blocked"]
        if blocked and not args.include_blocked:
            print("跳过 %d 个已知打不开的来源（--include-blocked 可强制重试）：" % len(blocked))
            for item in blocked:
                print("  %s ← %s" % (item.param_id, item.url))
                if item.blocked_reason:
                    print("     %s" % item.blocked_reason[:200])
            print()
        todo = [item for item in sources
                if args.include_login or item.access == "public"
                or (args.include_blocked and item.access == "blocked")]
        if not todo:
            print("没有可抓的配方（需登录的来源请用油猴脚本，或加 --include-login 看状态）")
            return 0
        ledger.init()
        failed = 0
        # 直取失败的来源退回浏览器渲染——shopee.cn/edu 是 JS 空壳，直取永远拿不到正文
        with Chrome() as chrome:
            def renderer(url: str) -> str:
                chrome.open(url, wait_seconds=5.0)
                return chrome.html()

            for source in todo:
                capture = fetch_source(source, renderer=renderer)
                ledger.record_capture(capture)
                print("  " + capture.describe())
                if not capture.ok:
                    failed += 1
        pending = ledger.pending_candidates()
        print("\n成功 %d 条，失败 %d 条；待确认共 %d 条（用 review / approve）"
              % (len(todo) - failed, failed, len(pending)))
        print("注：抓到与当前值相同的不会进待确认——不然每日任务会按天把队列灌满。")
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

        # 摘要用**核实后的视图**。指纹仍用基础 spec——它记录的是"制度性配置"的版本，
        # 与运行期的核实覆盖无关（覆盖是数据，不是配置）。
        print(ledger.spec.summary())
        print("指纹:", __import__("shopee_ledger.storage", fromlist=["Storage"]).Storage(args.db, default_spec()).fingerprint())
        if ledger.spec.problems:
            print("加载期问题:")
            for item in ledger.spec.problems:
                print("  -", item)
            return 1
        print("加载期问题: 无")
        return 0
    if args.cmd == "quote2":
        from shopee_ledger.cost_engine import CostEngine, CostInputs
        from shopee_ledger.gates import GateService

        spec = ledger.spec
        inputs = CostInputs(
            market=args.market,
            price_local=args.price,
            purchase_cny=args.purchase,
            domestic_cny=args.domestic,
            local_per_cny=args.fx,
            sls_freight=args.sls,
            weight_g=args.weight_g,
            channel=args.channel,
            cargo=args.cargo,
            coupon_discount=args.coupon,
            order_adjustment=args.order_adjustment,
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
        if result.status != "COMPUTED":
            # 同一款在 quote2 与 quote（候选品流程）下常得到不同结论，根因是
            # **字段来源不同**，不是哪一边算错：
            #   quote --id N 会从候选品里读买家运费、退货率等字段；
            #   quote2 是纯 what-if，字段要你给全（如 --buyer-shipping）。
            # 不说清这一点，用户会以为其中一个坏了。
            print()
            print("提示：这是纯 what-if 计算，字段要你给全（如 --buyer-shipping）。")
            print("      候选品流程（quote --id N）会自己从候选品读这些字段——")
            print("      同一款两边结论不同，通常就是这个原因，不是哪一边算错了。")
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
