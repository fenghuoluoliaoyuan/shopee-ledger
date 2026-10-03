"""本机网页。侧栏和表格按 shadcn 后台，指标卡按 Tremor。"""

from __future__ import annotations

import json
from datetime import date
from html import escape
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from shopee_ledger.desk import listing_gate, listing_gate_result, survival_advice
from shopee_ledger.fulfillment import STEP_LABEL, STEPS
from shopee_ledger.profit import Decision
from shopee_ledger.spec import default_spec
from shopee_ledger.store import PARAM_HELP, Ledger
from shopee_ledger.veto import VETO_FLAGS

DECISION_LABEL = {
    Decision.GO: "可做",
    Decision.WATCH: "观察",
    Decision.CUT: "砍掉",
    Decision.INCOMPLETE: "缺数据",
    Decision.THRESHOLD_UNSET: "阈值未设",
}
NAV = (
    ("/", "今日"),
    ("/products", "选品"),
    ("/orders", "订单"),
    ("/books", "账本"),
    ("/params", "参数"),
    ("/spec", "配置"),
    ("/suppliers", "供应商"),
    ("/checklist", "待核实"),
)


def serve(db: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    Ledger(db).init()
    Handler.db_path = db
    ThreadingHTTPServer((host, port), Handler).serve_forever()


class Handler(BaseHTTPRequestHandler):
    db_path = "data/ledger.sqlite"
    snapshot_dir: str | None = None   # 测试注入用；None 表示落默认目录

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        notice = _first(query, "notice")
        error = _first(query, "error")

        if parsed.path == "/sources.json":
            # 给油猴脚本的配方清单——下拉框据此生成，脚本里不重复写一份
            from shopee_ledger.sources import sources_payload

            self._send_json(sources_payload())
            return

        if parsed.path == "/watches.json":
            # 已登记的列表页。脚本用它判断"当前页是不是我要盯的那个"，
            # 是的话自动报送一次（每天一次），这样不需要任何定时器。
            from shopee_ledger.watch import load_watches

            self._send_json([{"id": item.id, "url": item.url, "note": item.note,
                              "access": item.access, "covers": item.covers}
                             for item in load_watches()])
            return

        ledger = Ledger(self.db_path)
        try:
            ledger.init()
            page = self._page(ledger, parsed.path, notice, error)
        finally:
            ledger.close()
        if page is None:
            self._send(404, b"not found")
            return
        self._send(200, page.encode("utf-8"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)

        if parsed.path == "/ingest":
            # 油猴脚本把浏览器渲染后的文本送到这里；本机用同一份 spec 配方提取
            self._send_json(_ingest_payload(raw_body, self.db_path,
                                            snapshot_dir=self.snapshot_dir))
            return

        form = parse_qs(raw_body.decode("utf-8"))
        ledger = Ledger(self.db_path)
        try:
            ledger.init()
            target = self._post(ledger, parsed.path, form)
        except (ValueError, json.JSONDecodeError) as exc:
            target = parsed.path + "?" + urlencode({"error": str(exc)})
        finally:
            ledger.close()
        self.send_response(303)
        self.send_header("Location", target)
        self.end_headers()

    def _send_json(self, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, ledger: Ledger, path: str, notice: str, error: str) -> str | None:
        pages = {
            "/": lambda: today(ledger),
            "/params": lambda: params_page(ledger),
            "/spec": lambda: spec_page(ledger),
            "/suppliers": lambda: suppliers_page(ledger),
            "/products": lambda: products_page(ledger),
            "/candidates": lambda: products_page(ledger),
            "/orders": lambda: orders_page(ledger),
            "/books": lambda: books_page(ledger),
            "/checklist": lambda: checklist_page(ledger),
            "/escrow": lambda: books_page(ledger),
        }
        builder = pages.get(path)
        if builder is None:
            return None
        return shell(path, builder(), notice, error)

    def _post(self, ledger: Ledger, path: str, form: dict) -> str:
        if path == "/params":
            ledger.set_param(_need(form, "site"), _need(form, "key"), _need(form, "value"), _need(form, "grade"))
            return "/params?" + urlencode({"notice": "参数已保存"})
        if path == "/suppliers":
            pay = _first(form, "pay")
            ledger.add_supplier(
                _need(form, "name"),
                _first(form, "url"),
                int(_first(form, "years") or "0"),
                _first(form, "dropship") == "yes",
                _first(form, "address_ok") == "yes",
                float(pay) if pay else None,
            )
            return "/suppliers?" + urlencode({"notice": "供应商已建档"})
        if path in ("/candidates", "/products"):
            if _first(form, "action") == "update":
                return _update_product(ledger, form)
            return _save_product(ledger, form)
        if path == "/orders":
            return _order_post(ledger, form)
        if path == "/checklist":
            return _checklist_post(ledger, form)
        if path == "/escrow":
            view = ledger.record_actual(int(_need(form, "order")), json.loads(_need(form, "payload")))
            return "/books?" + urlencode({"notice": view.explain()})
        raise ValueError("未知页面")


def _order_post(ledger: Ledger, form: dict) -> str:
    """订单动作。除"建单"外一律走状态机，顺序与守卫由 spec 把关，前端不再自己判断可行性。"""
    action = _need(form, "action")
    if action == "open":
        ledger.open_order(int(_need(form, "candidate")))
        return "/orders?" + urlencode({"notice": "订单已建立"})
    order_id = int(_need(form, "order"))
    try:
        if action == "stock":
            ledger.apply_stock(order_id, int(_need(form, "supplier")),
                               _first(form, "in_stock") == "yes",
                               _first(form, "exhausted") == "yes")
        elif action == "cancel":
            ledger.advance_order(order_id, "cancelled")
        elif action == "advance":
            to_state = _need(form, "to")
            if to_state == "stock_checked":
                ledger.apply_stock(order_id, int(_need(form, "supplier")),
                                   _first(form, "in_stock") == "yes",
                                   _first(form, "exhausted") == "yes")
            elif to_state == "address_captured":
                ledger.apply_copy(order_id, _need(form, "address"))
            elif to_state == "ship_arranged":
                ledger.apply_arrange(order_id)
            elif to_state == "po_created":
                ledger.apply_purchase(order_id)
            else:
                # warehouse_scanned 及之后都不需要额外簿记，直接推进
                ledger.advance_order(order_id, to_state)
        elif action == "deadline":
            ledger.set_deadline(order_id, _need(form, "deadline"))
        elif action == "actual":
            view = ledger.record_actual(order_id, json.loads(_need(form, "payload")))
            return "/orders?" + urlencode({"notice": view.explain()})
        else:
            raise ValueError("未知履约动作：%s" % action)
    except ValueError as exc:
        # 守卫拦下、跳步、参数没填——都当成可读提示回给页面，而不是 500
        return "/orders?" + urlencode({"error": str(exc)})
    return "/orders?" + urlencode({"notice": "已更新：" + ledger.load_order(order_id).state})


def shell(active: str, body: str, notice: str, error: str) -> str:
    current = "/products" if active in ("/products", "/candidates") else active
    links = "".join(
        f'<a class="{"on" if path == current else ""}" href="{path}">{escape(label)}</a>'
        for path, label in NAV
    )
    banner = ""
    if error:
        banner = f'<p class="banner bad">{escape(error)}</p>'
    elif notice:
        banner = f'<p class="banner ok">{escape(notice)}</p>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>起步台账</title>
<style>
:root {{
  --bg: #f6f7f9; --card: #fff; --ink: #18181b; --muted: #71717a;
  --line: #e4e4e7; --side: #18181b; --side-ink: #fafafa; --accent: #1d4ed8;
  --ok: #166534; --ok-bg: #f0fdf4; --watch: #92400e; --watch-bg: #fffbeb;
  --bad: #991b1b; --bad-bg: #fef2f2; --chip: #f4f4f5;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: var(--ink); background: var(--bg);
  font: 14px/1.5 "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }}
.layout {{ display: grid; grid-template-columns: 220px 1fr; min-height: 100vh; }}
aside {{ background: var(--side); color: var(--side-ink); padding: 22px 14px; }}
.brand {{ font-weight: 650; letter-spacing: -0.02em; margin: 0 8px 18px; }}
.brand small {{ display: block; color: #a1a1aa; font-weight: 400; margin-top: 4px; }}
aside a {{ display: block; color: #d4d4d8; text-decoration: none; border-radius: 8px; padding: 8px 10px; }}
aside a.on, aside a:hover {{ background: #27272a; color: white; }}
main {{ padding: 28px 32px 48px; max-width: 1180px; }}
h1 {{ font-size: 22px; letter-spacing: -0.03em; margin: 0 0 6px; }}
.lead {{ color: var(--muted); margin: 0 0 18px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }}
.card {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }}
.k {{ color: var(--muted); font-size: 12px; }}
.v {{ font-size: 28px; font-variant-numeric: tabular-nums; letter-spacing: -0.04em; margin-top: 6px; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }}
th {{ color: var(--muted); font-size: 12px; font-weight: 600; }}
form.stack {{ display: grid; gap: 8px; }}
label {{ display: grid; gap: 4px; font-size: 12px; color: var(--muted); }}
input, select, textarea {{ font: inherit; color: var(--ink); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; background: white; }}
textarea {{ min-height: 140px; }}
button {{ background: var(--accent); color: white; border: 0; border-radius: 8px; padding: 8px 12px; font: inherit; cursor: pointer; }}
button.ghost {{ background: white; color: var(--ink); border: 1px solid var(--line); }}
button.slim {{ padding: 4px 8px; font-size: 12px; }}
.banner {{ border-radius: 8px; padding: 10px 12px; }}
.banner.ok {{ background: var(--ok-bg); color: var(--ok); }}
.banner.bad {{ background: var(--bad-bg); color: var(--bad); }}
.pill {{ display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; background: var(--chip); }}
.pill.go {{ background: var(--ok-bg); color: var(--ok); }}
.pill.watch {{ background: var(--watch-bg); color: var(--watch); }}
.pill.cut, .pill.bad {{ background: var(--bad-bg); color: var(--bad); }}
.steps {{ display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }}
.step {{ border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; color: var(--muted); font-size: 12px; }}
.step.done {{ background: var(--ok-bg); color: var(--ok); border-color: transparent; }}
.row {{ display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 12px; align-items: start; }}
.checks {{ display: flex; gap: 10px; flex-wrap: wrap; color: var(--ink); }}
@media (max-width: 900px) {{
  .layout {{ grid-template-columns: 1fr; }}
  .grid, .row {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<div class="layout">
<aside>
  <p class="brand">起步台账<small>手册 v1.4 · 本机</small></p>
  {links}
</aside>
<main>
{banner}
{body}
</main>
</div>
</body>
</html>"""


def _card(title: str, body: str, hint: str = "") -> str:
    note = f'<p class="k">{escape(hint)}</p>' if hint else ""
    return f'<section class="card" style="margin-top:12px"><h2>{escape(title)}</h2>{note}{body}</section>'


def today(ledger: Ledger) -> str:
    """今日＝按优先级排的行动清单：告警 → 阻塞第一单的核实 → 选品卡点 → 订单下一步 → 解锁清单。

    这个页面存在的意义是回答"今天该做哪 3 件事"，所以**顺序本身就是结论**。
    """
    spec = default_spec()
    cards = []
    for market in spec.registry.get("markets") or []:
        code = market.get("code")
        viability = market.get("dropship_viability", "unknown")
        _passed, _complete, rate = ledger.survival(code)
        shown = "—" if rate is None else f"{rate:.0%}"
        title = f"{market.get('name', code)}站存活率"
        badge = "" if viability == "viable" else f'<div class="k">直发可行性：{escape(viability)}</div>'
        cards.append(
            f'<article class="card"><div class="k">{escape(title)}</div><div class="v">{shown}</div>'
            f'<div class="k">{escape(survival_advice(rate))}</div>{badge}</article>'
        )

    sections: list[str] = []

    alerts = ledger.order_alerts()
    if alerts:
        items = "".join(
            '<li><span class="pill bad">%s</span> 订单 #%s %s：%s%s</li>' % (
                escape(item["priority"]), item["order_id"], escape(item.get("candidate") or ""),
                escape(item["message"]),
                ("（已停留 %s 小时）" % item["hours_in_state"])
                if item.get("hours_in_state") is not None else "",
            )
            for item in alerts
        )
        sections.append(_card("要先处理", f"<ul>{items}</ul>",
                              "异常分支 EX-01/02/03，按订单在当前状态的停留时长算出来。"))

    blocking = [row for row in ledger.checklist_rows()
                if row.get("blocks_first_order") and not row["conclusion"]]
    if blocking:
        items = "".join(
            '<li><code>%s</code> %s <span class="k">%s</span></li>' % (
                escape(row.get("spec_task_id") or ""), escape((row.get("item") or "")[:70]),
                escape(row.get("channel") or ""))
            for row in blocking
        )
        sections.append(_card(f"阻塞第一单的核实（{len(blocking)} 件）", f"<ul>{items}</ul>",
                              "这些没核实，第一单不该下。逐条在「待核实」页填写结论与等级。"))

    stuck = []
    for row in ledger.list_candidates():
        verdict = _gate(ledger, row)
        if verdict != "可上架":
            stuck.append(f"<li>{escape(row['name'])}：{escape(verdict)}</li>")
    if stuck:
        sections.append(_card("选品卡在哪", f"<ul>{''.join(stuck)}</ul>"))
    elif not ledger.list_candidates():
        sections.append(_card("选品卡在哪", '<p class="k">还没录入候选品。先去「选品」录一款样品。</p>'))

    orders = []
    for row in ledger.list_orders():
        if row["status"] in ("paid", "cancelled"):
            continue
        orders.append(f'<li>订单 #{row["id"]} {escape(row["candidate_name"])}：{escape(_next_step(row))}</li>')
    if orders:
        sections.append(_card("订单下一步", f"<ul>{''.join(orders)}</ul>"))

    unlocks = ledger.unverified_unlocks()
    if unlocks:
        items = "".join(
            '<li><code>%s</code>（%s 级%s）挡着：%s</li>' % (
                escape(item["param_id"]), escape(item["level"]),
                ("，任务 " + escape(item["task_ref"])) if item.get("task_ref") else "",
                escape(item["feature"]))
            for item in unlocks
        )
        sections.append(_card("未核实参数挡着哪些功能", f"<ul>{items}</ul>",
                              "核实后这些功能自动启用，不需要改代码。"))

    if not sections:
        sections.append('<section class="card" style="margin-top:12px"><p>今天没有待办。</p></section>')

    return (f'<h1>今日</h1><p class="lead">按优先级从上往下做。'
            f'「待核实」不挡录入，但会挡住依赖它的功能——挡了哪些，页面底部会写。</p>'
            f'<section class="grid">{"".join(cards)}</section>{"".join(sections)}')


def _gate_kwargs(ledger: Ledger, row) -> dict:
    """候选品 → 门禁上下文的唯一映射点（网页各处不再各拼一份）。"""
    return dict(
        supplier_count=len(ledger.supplier_ids(row["id"])),
        sample_bought=bool(row["sample_bought"]),
        weighed=row["weight_g"] is not None,
        measured=bool(row.get("weight_is_measured")),
        purchase_price_cny=row["purchase_cny"],
        photo_ready=bool(row["photo_ready"]),
        title_ready=bool((row["title_text"] or "").strip()),
        detail_ready=bool(row["detail_ready"]),
    )


def _gate(ledger: Ledger, row) -> str:
    return listing_gate(ledger.quote(row["id"]), **_gate_kwargs(ledger, row))


def _gate_badges(ledger: Ledger, row) -> str:
    """逐门显示 G1–G4：卡在哪一门要一眼看到，而不是只给一句结论。"""
    results = listing_gate_result(ledger.quote(row["id"]), **_gate_kwargs(ledger, row))
    css = {"PASS": "go", "WARN": "watch", "INCOMPLETE": "", "REJECT": "cut"}
    badges = []
    for gate in results:
        reasons = "；".join(item.message for item in gate.outcomes if item.result != "PASS")
        badges.append('<span class="pill %s" title="%s">%s %s</span>' % (
            css.get(gate.result, ""), escape(reasons[:140]) or "通过", gate.gate_id, gate.result))
    return " ".join(badges)


def _next_step(row) -> str:
    """下一步提示改为按状态机的**允许转移**给出，不再靠"哪一步没打勾"。"""
    status = row.get("status")
    if status == "cancelled":
        return row.get("block_reason") or "三家都无货，先下架"
    next_states = row.get("next_states") or []
    if not next_states:
        return "已到终态（%s）" % STEP_LABEL.get(status, status)
    return "下一步：" + " 或 ".join(STEP_LABEL.get(state, state) for state in next_states)


def _save_product(ledger: Ledger, form: dict) -> str:
    new_id = ledger.add_candidate(
        _need(form, "site"),
        _need(form, "name"),
        float(_need(form, "weight")),
        float(_need(form, "purchase")),
        float(_need(form, "domestic")),
        float(_need(form, "price")),
        float(_need(form, "sls")),
        _first(form, "free_shipping") == "yes",
    )
    rate = _first(form, "return_rate")
    if rate:
        ledger.set_return_rate(new_id, float(rate))
    ads = _first(form, "ads")
    if ads:
        ledger.set_ads(new_id, float(ads))
    flags = form.get("flag", [])
    if flags:
        ledger.set_flags(new_id, flags)
    for supplier_id in form.get("supplier", []):
        ledger.link_supplier(new_id, int(supplier_id))
    ledger.set_listing(
        new_id,
        _first(form, "sample") == "yes",
        _first(form, "photo") == "yes",
        _first(form, "detail") == "yes",
        _first(form, "title"),
    )
    return "/products?" + urlencode({"notice": "已录入，今日页会告诉你卡在哪"})


# 曾经这里有一张 PUBLIC_NOTES 表，按旧的 1–22 编号往核实清单里预填"结论"。
# 两个问题：清单已换成 spec 的 48 条任务（按 VT-001…VT-048 排序），编号对不上，
# 结论会写到 VT-005/006/007/015 上；而且它是在**代替用户宣告已核实**（等级还写 E）。
# 现由 checklist 页逐条人工填写，未核实的参数挡住哪个功能由 Ledger.unverified_unlocks 说明。


def params_page(ledger: Ledger) -> str:
    """运行期费率（存 DB）。市场清单来自 registry，不再写死 MY/TW。"""
    spec = default_spec()
    codes = [m.get("code") for m in (spec.registry.get("markets") or []) if m.get("params_file")]
    blocks = []
    for site in codes:
        rows = []
        current = ledger.params(site)
        for key, help_text in PARAM_HELP.items():
            value = current.get(key)
            shown = "—" if value in (None, "") else str(value)
            rows.append(
                f"<tr><td><code>{escape(key)}</code><div class='k'>{escape(help_text)}</div></td><td>{escape(shown)}</td></tr>"
            )
        options = "".join(f'<option value="{escape(key)}">{escape(key)}</option>' for key in PARAM_HELP)
        grades = "".join(f'<option>{grade}</option>' for grade in "ABCDE")
        blocks.append(
            f"""<section class="card"><h2>{escape(site)}</h2><table>{''.join(rows)}</table>
<form class="stack" method="post" action="/params" style="margin-top:12px">
<input type="hidden" name="site" value="{escape(site)}">
<label>参数<select name="key">{options}</select></label>
<label>值<input name="value" required></label>
<label>等级<select name="grade">{grades}</select></label>
<button>保存</button></form></section>"""
        )
    return (f'<h1>参数</h1><p class="lead">各市场分开填，市场清单来自 registry；'
            f'空着的费率不会按 0 计算。完整的 52 个参数与证据等级见 <a href="/spec">配置</a>。</p>'
            f'<div class="row">{"".join(blocks)}</div>')


def spec_page(ledger: Ledger) -> str:
    """配置页：完全由 spec 驱动——市场、参数、证据等级、复核日、核实任务。"""
    from shopee_ledger.storage import Storage

    spec = default_spec()
    storage = Storage(ledger.path, spec)
    fingerprint = storage.fingerprint()

    market_rows = "".join(
        "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td class='k'>%s</td></tr>" % (
            escape(m.get("code", "")), escape(m.get("name", "")), escape(m.get("currency", "")),
            escape(m.get("dropship_viability", "")), escape((m.get("viability_basis") or "")[:70]),
        )
        for m in (spec.registry.get("markets") or [])
    )

    overrides = {row["param_id"]: row
                 for row in ledger.storage.list("ParamOverride", limit=500)}
    param_rows = []
    for pid, param in sorted(spec.params.items()):
        if param.value is None:
            shown = '<span class="k">未核实</span>'
        elif isinstance(param.value, (dict, list)):
            shown = escape(json.dumps(param.value, ensure_ascii=False)[:48])
        else:
            shown = escape(str(param.value))
        state = param.effective_state()
        flag = "" if param.hard_eligible() else ' <span class="k">（不可硬拦）</span>'
        override = overrides.get(pid)
        if override:
            link = ('<a href="%s">来源</a>' % escape(override["source_url"])
                    if override.get("source_url") else "无来源链接")
            origin = "已核实 %s 级 · %s · %s" % (
                escape(override.get("evidence_level") or ""),
                escape(override.get("checked_at") or "未记日期"), link)
            if override.get("snapshot_ref"):
                origin += " · 存档 %s" % escape(override["snapshot_ref"])
        elif param.source.get("url"):
            origin = '<a href="%s">来源</a>' % escape(param.source["url"])
        else:
            origin = '<span class="k">未记来源</span>'
        param_rows.append(
            "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s%s</td><td>%s</td><td>%s</td></tr>" % (
                escape(pid), escape(param.name[:22]), shown,
                escape(param.evidence_level), escape(state), flag,
                escape(param.next_review_at or "—"), origin,
            )
        )

    task_rows = []
    for tid, task in sorted(spec.tasks.items()):
        blocking = task.get("blocks_first_order")
        task_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                escape(tid), "**是**" if blocking else "否",
                escape(task.get("module", "")), escape(task.get("item", "")[:40]),
                escape(task.get("status", "")),
            )
        )

    modules = "".join(
        '<li><code>%s</code> %s <span class="k">[%s]</span></li>' % (
            escape(m.get("id", "")), escape(m.get("name", "")), escape(m.get("status", "")))
        for m in (spec.registry.get("modules") or [])
    )
    blocking_count = len([t for t in spec.tasks.values() if t.get("blocks_first_order")])

    return f"""<h1>配置</h1>
<p class="lead">本页全部读自 <code>spec/</code>，改 JSON 即改判定。配置指纹 <code>{escape(fingerprint)}</code>；
参数 {len(spec.params)} · 规则 {len(spec.rules)} · 任务 {len(spec.tasks)}（其中 {blocking_count} 条阻塞第一单）。</p>

<section class="card"><h2>市场</h2><table>
<tr><th>代码</th><th>名称</th><th>币种</th><th>直发可行性</th><th>依据</th></tr>{market_rows}</table></section>

<section class="card" style="margin-top:12px"><h2>模块</h2><ul>{modules}</ul></section>

<section class="card" style="margin-top:12px"><h2>参数与证据等级</h2>
<p class="k">「不可硬拦」= 该证据等级不能参与硬门禁（INV-001/010）。
「已经核实」的参数来自 ParamOverride 覆盖层，点来源可回到当时的依据。</p>
<table><tr><th>ID</th><th>名称</th><th>值</th><th>等级</th><th>状态</th><th>下次复核</th><th>出处</th></tr>{''.join(param_rows)}</table></section>

<section class="card" style="margin-top:12px"><h2>核实任务</h2>
<table><tr><th>ID</th><th>阻塞第一单</th><th>模块</th><th>事项</th><th>状态</th></tr>{''.join(task_rows)}</table></section>"""


def suppliers_page(ledger: Ledger) -> str:
    body = "".join(
        "<tr>"
        f"<td>{row['id']}</td><td>{escape(row['name'])}</td><td>{'是' if row['dropship'] else '否'}</td>"
        f"<td>{'已确认' if row['address_ok'] else '未确认'}</td><td>{row['years'] or ''}</td>"
        "</tr>"
        for row in ledger.list_suppliers()
    )
    return f"""<h1>供应商</h1><p class="lead">建档时确认能否把货发到中转仓地址。确认过的，出单时不用再问。</p>
<div class="row"><section class="card"><table><tr><th>ID</th><th>名称</th><th>代发</th><th>地址格式</th><th>年限</th></tr>{body}</table></section>
<form class="card stack" method="post" action="/suppliers">
<label>名称<input name="name" required></label>
<label>链接<input name="url"></label>
<label>开店年限<input name="years" type="number" value="0"></label>
<label>下单页实付<input name="pay"></label>
<label>一件代发<select name="dropship"><option value="yes">是</option><option value="no">否</option></select></label>
<label>接受中转仓地址<select name="address_ok"><option value="yes">已确认</option><option value="no">未确认</option></select></label>
<button>建档</button></form></div>"""


def _update_product(ledger: Ledger, form: dict) -> str:
    """候选品录入**之后**的更新入口：买样、称重、拍图、写标题都是后来的事。"""
    candidate_id = int(_need(form, "id"))
    ledger.set_listing(candidate_id, _first(form, "sample") == "yes",
                       _first(form, "photo") == "yes", _first(form, "detail") == "yes",
                       _first(form, "title"))
    weight = _first(form, "weight")
    ledger.set_measured(candidate_id, _first(form, "measured") == "yes",
                        float(weight) if weight else None)
    rate = _first(form, "return_rate")
    if rate:
        ledger.set_return_rate(candidate_id, float(rate))
    ads = _first(form, "ads")
    if ads:
        ledger.set_ads(candidate_id, float(ads))
    ledger.set_flags(candidate_id, form.get("flag", []))
    for supplier_id in form.get("supplier", []):
        if int(supplier_id) not in ledger.supplier_ids(candidate_id):
            ledger.link_supplier(candidate_id, int(supplier_id))
    return "/products?" + urlencode({"notice": "已更新这一款"})


def products_page(ledger: Ledger) -> str:
    suppliers = ledger.list_suppliers()
    rows = []
    for row in ledger.list_candidates():
        result = ledger.quote(row["id"])
        money = (f"{result.net:.2f} / {result.rate:.1%}"
                 if result.rate is not None and result.net is not None else "")
        linked = ledger.supplier_ids(row["id"])
        checked_flags = {f for f in (row.get("flags") or "").split(",") if f}
        row_suppliers = "".join(
            '<label class="checks"><input type="checkbox" name="supplier" value="%s"%s> %s</label>'
            % (item["id"], " checked" if item["id"] in linked else "", escape(item["name"]))
            for item in suppliers) or '<span class="k">还没有供应商</span>'
        row_flags = "".join(
            '<label class="checks"><input type="checkbox" name="flag" value="%s"%s> %s</label>'
            % (key, " checked" if key in checked_flags else "", escape(label))
            for key, label in VETO_FLAGS.items())
        rows.append(
            "<tr><td>%s</td><td>%s</td><td class='pill %s'>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%d 家</td></tr>"
            "<tr><td colspan='7'><details><summary class='k'>更新这一款</summary>"
            "<form method='post' action='/products' class='stack' style='margin-top:8px'>"
            "<input type='hidden' name='action' value='update'><input type='hidden' name='id' value='%d'>"
            "<label>标题<input name='title' value='%s' placeholder='核心词 + 属性 + 场景 + 地域'></label>"
            "<label>重量 g<input name='weight' value='%s'></label>"
            "<label class='checks'><input type='checkbox' name='sample' value='yes'%s> 样品已买</label>"
            "<label class='checks'><input type='checkbox' name='measured' value='yes'%s> 重量是称出来的（不是估的）</label>"
            "<label class='checks'><input type='checkbox' name='photo' value='yes'%s> 主图是自己拍的</label>"
            "<label class='checks'><input type='checkbox' name='detail' value='yes'%s> 详情前 3 屏已写</label>"
            "<label>退货率<input name='return_rate' value='%s'></label>"
            "<label>本单广告费<input name='ads' value='%s'></label>"
            "<div class='checks'>%s</div><div class='checks'>%s</div>"
            "<button class='slim'>更新这一款</button></form></details></td></tr>" % (
                escape(row["name"]), escape(row["site"]), result.decision.value,
                DECISION_LABEL[result.decision], money, _gate_badges(ledger, row),
                escape(_gate(ledger, row)), len(linked),
                row["id"], escape(row["title_text"] or ""),
                "" if row["weight_g"] is None else row["weight_g"],
                " checked" if row["sample_bought"] else "",
                " checked" if row.get("weight_is_measured") else "",
                " checked" if row["photo_ready"] else "",
                " checked" if row["detail_ready"] else "",
                "" if row["return_rate"] is None else row["return_rate"],
                "" if row["ads"] is None else row["ads"],
                row_suppliers, row_flags))

    market_options = "".join(
        '<label class="checks"><input type="radio" name="site" value="%s"%s> %s</label>' % (
            escape(market.get("code", "")), " checked" if market.get("code") == "TW" else "",
            escape(market.get("name", "")))
        for market in (default_spec().registry.get("markets") or []))
    supplier_checks = "".join(
        '<label class="checks"><input type="checkbox" name="supplier" value="%s"> %s</label>'
        % (item["id"], escape(item["name"])) for item in suppliers) or '<span class="k">还没有供应商</span>'
    flag_checks = "".join(
        '<label class="checks"><input type="checkbox" name="flag" value="%s"> %s</label>'
        % (key, escape(label)) for key, label in VETO_FLAGS.items())

    return f"""<h1>选品</h1><p class="lead">四道门禁逐门显示：G1 否决 → G2 实测取证 → G3 核算 → G4 上架质量。
卡在「先买样品再称重」时，样品到手要在下面「更新这一款」里打勾并填实测重量——估算重量不算实测。</p>
<div class="row"><section class="card"><table>
<tr><th>品名</th><th>站</th><th>利润</th><th>净利润</th><th>门禁</th><th>卡在哪</th><th>供应商</th></tr>
{''.join(rows) or '<tr><td colspan="7" class="k">还没有候选品</td></tr>'}</table></section>
<form class="card stack" method="post" action="/products">
<label>站点</label><div class="checks">{market_options}</div>
<label>品名<input name="name" required></label>
<label>标题<input name="title" placeholder="核心词 + 属性 + 场景 + 地域"></label>
<label>重量 g（先填估算，称过再更新）<input name="weight" required></label>
<label>采购价 ¥<input name="purchase" required></label>
<label>国内段运费 ¥<input name="domestic" required></label>
<label>商品价<input name="price" required></label>
<label>SLS 运费<input name="sls" required></label>
<label>退货率<input name="return_rate" placeholder="0.05"></label>
<label>本单广告费<input name="ads" value="0"></label>
<label>包邮<select name="free_shipping"><option value="yes">是，买家运费记 0</option><option value="no">否</option></select></label>
<div class="checks">{supplier_checks}</div>
<div class="checks">{flag_checks}</div>
<button>录入</button></form></div>"""


def books_page(ledger: Ledger) -> str:
    rows = []
    for row in ledger.list_orders():
        estimate_rate, estimate_net = row["estimate_rate"], row["estimate_net"]
        actual_rate, actual_net = row["actual_rate"], row["actual_net"]
        estimate = "—" if estimate_rate is None else f"{estimate_net:.2f} / {estimate_rate:.1%}"
        actual = "—" if actual_rate is None else f"{actual_net:.2f} / {actual_rate:.1%}"
        if estimate_rate is not None and actual_rate is not None:
            delta = actual_rate - estimate_rate
            gap = "%s %.1f 个百分点" % ("▲" if delta > 0 else ("▼" if delta < 0 else "="), abs(delta) * 100)
        else:
            gap = "—"
        rows.append(
            f"<tr><td>#{row['id']} {escape(row['candidate_name'])}</td>"
            f"<td>{estimate}</td><td>{actual}</td><td>{gap}</td></tr>"
        )
    return f"""<h1>账本</h1><p class="lead">左列是**下单时冻住**的估算，右列是用账单实付重算的实绩。
两列都留着，才看得出自己的估算偏了多少。实绩不覆盖估算。</p>
<section class="card"><table><tr><th>订单</th><th>估算（下单时）</th><th>实绩（账单）</th><th>差</th></tr>
{''.join(rows) or '<tr><td colspan="4" class="k">还没有订单</td></tr>'}</table></section>
<p class="k">实绩要填「这一单的托管 JSON」才会算；佣金和手续费直接取账单数字，不再用费率估。</p>"""


def _order_forms(row: dict, supplier_options: str) -> str:
    """按状态机 allowed() 渲染操作入口——spec 里加一个状态，这里自动多一个按钮。

    需要额外输入的转移（查库存要供应商、抄地址要文本）单独渲染，其余一律给一个按钮。
    """
    order_id = row["id"]
    forms: list[str] = []
    for state in row.get("next_states") or []:
        label = STEP_LABEL.get(state, state)
        guard = (row.get("next_guards") or {}).get(state) or ""
        hint = f'<span class="k">{escape(guard)}</span>' if guard else ""
        if state == "stock_checked":
            forms.append(f"""<form method="post" action="/orders" class="stack" style="margin-bottom:8px">
<input type="hidden" name="order" value="{order_id}"><input type="hidden" name="action" value="advance">
<input type="hidden" name="to" value="stock_checked">
<label>供应商<select name="supplier">{supplier_options}</select></label>
<label>有货<select name="in_stock"><option value="yes">有货</option><option value="no">无货</option></select></label>
<button>记录库存</button>{hint}</form>""")
        elif state == "address_captured":
            forms.append(f"""<form method="post" action="/orders" class="stack" style="margin-bottom:8px">
<input type="hidden" name="order" value="{order_id}"><input type="hidden" name="action" value="advance">
<input type="hidden" name="to" value="address_captured">
<label>当单中转仓地址（只能从订单页复制，禁止手工编）<input name="address" required></label>
<button>抄入地址</button>{hint}</form>""")
        elif state == "cancelled":
            forms.append(f"""<form method="post" action="/orders" style="display:inline-block;margin:0 6px 8px 0">
<input type="hidden" name="order" value="{order_id}"><input type="hidden" name="action" value="cancel">
<button class="ghost">全部无货：先下架并取消订单</button></form>""")
        else:
            forms.append(f"""<form method="post" action="/orders" style="display:inline-block;margin:0 6px 8px 0">
<input type="hidden" name="order" value="{order_id}"><input type="hidden" name="action" value="advance">
<input type="hidden" name="to" value="{state}"><button class="ghost">{escape(label)}</button></form>{hint}""")
    if not forms:
        forms.append('<p class="k">已到终态，没有可执行的操作。</p>')
    return "".join(forms)


def orders_page(ledger: Ledger) -> str:
    suppliers = ledger.list_suppliers()
    supplier_options = "".join(f'<option value="{row["id"]}">{row["id"]} {escape(row["name"])}</option>' for row in suppliers)
    candidate_options = "".join(
        f'<option value="{row["id"]}">{row["id"]} {escape(row["name"])}</option>' for row in ledger.list_candidates()
    )
    blocks = []
    for row in ledger.list_orders():
        state = row.get("status") or ""
        if state == "cancelled":
            bar = '<span class="step">已取消</span>'
        else:
            done = set(filter(None, (row["steps"] or "").split(",")))
            bar = "".join(
                f'<span class="step{" done" if step in done else ""}">{STEP_LABEL[step]}</span>' for step in STEPS
            )
        blocks.append(
            f"""<section class="card"><h2>#{row['id']} {escape(row['candidate_name'])} · {escape(row['site'])}</h2>
<p class="k">当前：<b>{escape(STEP_LABEL.get(state, state))}</b> <code>{escape(state)}</code> {escape(row['block_reason'] or '')}</p>
<div class="steps">{bar}</div>
<p class="k">当单地址：{escape(row['warehouse_address'] or '还没抄')}</p>
{_order_forms(row, supplier_options)}
<details><summary class="k">其它记录</summary>
<form method="post" action="/orders" class="stack" style="margin-top:8px">
<input type="hidden" name="order" value="{row['id']}">
<label>发货截止<input name="deadline" value="{escape(row['deadline'] or '')}" placeholder="2026-10-04 18:00"></label>
<button class="ghost" name="action" value="deadline">记下截止时间</button>
<label>这一单的托管 JSON<textarea name="payload"></textarea></label>
<button class="ghost" name="action" value="actual">记下实绩</button>
</form></details></section>"""
        )
    opener = f"""<form class="card stack" method="post" action="/orders">
<input type="hidden" name="action" value="open">
<label>候选品<select name="candidate">{candidate_options}</select></label>
<button>建立订单</button></form>"""
    return (f'<h1>订单</h1><p class="lead">下面每个按钮都是状态机当前允许走的那一步；'
            f'跳步或前置条件不满足会被拒，并告诉你为什么。地址只能抄当单页面。</p>'
            f'<div class="row">{opener}<div>{"".join(blocks) or "<p class=\'k\'>还没有订单</p>"}</div></div>')


def checklist_page(ledger: Ledger) -> str:
    today_text = date.today().isoformat()
    rows = []
    for row in ledger.checklist_rows():
        grades = "".join(
            "<option%s>%s</option>" % (" selected" if grade == row["grade"] else "", grade)
            for grade in "ABCDE")
        target = row.get("target_param_id")
        value_field = ""
        if target:
            current = row.get("param_value")
            shown = "" if current is None or isinstance(current, (dict, list)) else str(current)
            value_field = (
                "<label>抄到的值 <code>%s</code>（当前 %s 级）"
                "<input name='value' value='%s' placeholder='填了就顺便升级这个参数'></label>"
                % (escape(target), escape(row.get("param_level") or "—"), escape(shown)))
        badge = '<span class="pill cut">阻塞第一单</span> ' if row.get("blocks_first_order") else ""
        done = ('<span class="pill go">已核实 %s</span>' % escape(row["grade"] or "")
                if row["conclusion"] else "")
        rows.append(
            "<tr><td><code>%s</code> %s%s<div class='k'>%s</div><div class='k'>渠道：%s</div></td>"
            "<td><form method='post' action='/checklist' class='stack'>"
            "<input type='hidden' name='id' value='%s'>"
            "<label>日期<input name='date' value='%s'></label>"
            "<label>来源 URL<input name='url' value='%s' placeholder='https://…（A 级必填）'></label>"
            "<label>截图 / 存档引用<input name='snapshot' value='%s' placeholder='本地路径或可打开链接'></label>"
            "<label>结论<textarea name='conclusion' rows='2'>%s</textarea></label>"
            "%s"
            "<label>等级<select name='grade'>%s</select></label>"
            "<button class='slim'>保存%s</button></form></td></tr>" % (
                escape(row.get("spec_task_id") or ""), badge, done, escape(row["item"]),
                escape(row["channel"] or "—"), row["id"],
                escape(row["checked_date"] or today_text), escape(row["source_url"]),
                escape(row["snapshot_ref"]), escape(row["conclusion"]), value_field, grades,
                "并升级参数" if target else ""))
    remaining = len([item for item in ledger.checklist_rows()
                     if item.get("blocks_first_order") and not item["conclusion"]])
    return f"""<h1>待核实</h1>
<p class="lead">阻塞第一单的排在最前（还剩 {remaining} 条）。A 级必须有可打开的 URL——没有凭据的 A 级等于自述。
这条任务若绑定了解锁参数，填「抄到的值」会在保存的同时把它升到该等级，对应功能随即启用。</p>
<section class="card"><table>{''.join(rows)}</table></section>"""


def _ingest_payload(raw_body: bytes, db_path: str,
                    snapshot_dir: Path | str | None = None) -> dict:
    """处理油猴脚本送来的渲染后页面文本。

    流程与命令行 fetch 完全一致：解析配方 → 提取 → 存快照 → 只产出**候选值**。
    这里是本机端点，没有鉴权——但它只写候选（pending），不直接改参数，
    所以最坏情况也只是多一条待你确认的记录。

    snapshot_dir 只为测试注入：不传就落到 data/snapshots。测试必须能隔离，
    否则跑一次测试就往真实数据目录里塞一堆固件快照。
    """
    from shopee_ledger.sources import ingest_text

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": "请求体不是合法 JSON：%s" % exc}

    # 列表页：脚本送回的是结构化条目（链接 + 标题 + 日期），不是正文
    if payload.get("links"):
        return _ingest_listing(payload, db_path)

    text = payload.get("text") or ""
    if not text.strip():
        return {"ok": False, "error": "text 为空；请把页面正文一起发过来"}
    kwargs = {"snapshot_dir": snapshot_dir} if snapshot_dir else {}
    capture = ingest_text(
        text, param_id=payload.get("param_id"), url=payload.get("url"),
        captured_at=payload.get("captured_at"), channel="userscript", **kwargs)
    ledger = Ledger(db_path)
    try:
        ledger.init()
        candidate_id = ledger.record_capture(capture)
        if candidate_id is None:
            hints = {
                "url_mismatch": "把浏览器切到 %s 再点一次；或给当前页面单独加一条配方"
                                % (capture.expected_url or "配方登记的页面"),
                "extract_failed": "提取失败不等于没有收获：快照已存，去修 spec/sources.json 的规则",
            }
            return {"ok": False, "status": capture.status, "param_id": capture.param_id,
                    "message": capture.message, "snapshot_ref": capture.snapshot_ref,
                    "expected_url": capture.expected_url,
                    "hint": hints.get(capture.status, "未产出候选值")}
        current = ledger.spec.params.get(capture.param_id)
        return {"ok": True, "status": capture.status, "param_id": capture.param_id,
                "value": capture.value, "candidate_id": candidate_id,
                "changed": bool(current and current.value != capture.value),
                "current_value": (current.value if current else None),
                "snapshot_ref": capture.snapshot_ref,
                "hint": "已记为候选，未生效。回终端跑 review / approve"}
    finally:
        ledger.close()


def _ingest_listing(payload: dict, db_path: str) -> dict:
    """列表页只做**发现**：记下有哪些文档、哪个是新的。不从这里提取任何数值。

    理由：列表页没有正文。而"新文档出现了"本身是有用的信号——
    费率变更通常是**新发一篇通知**，不是改旧文章。
    """
    from shopee_ledger.sources import page_key
    from shopee_ledger.watch import entries_from_links, load_watches

    url = payload.get("url") or ""
    known = next((item for item in load_watches() if page_key(item.url) == page_key(url)), None)
    watch_id = payload.get("watch_id") or (known.id if known else "WATCH-MANUAL")
    entries = entries_from_links(payload.get("links") or [], base_url=url)
    if not entries:
        return {"ok": False, "error": "没解析出任何条目；请确认这是列表页（链接里要含 /article/ 编号）"}
    ledger = Ledger(db_path)
    try:
        ledger.init()
        result = ledger.record_listing(watch_id, entries, page_url=url,
                                       api_calls=payload.get("api_calls") or [])
        result.update({"ok": True, "watch_id": watch_id,
                       "known_watch": bool(known),
                       "hint": "列表页只做发现。正文要另点一次「抓这一页」，且仍需你确认"})
        return result
    finally:
        ledger.close()


def _checklist_post(ledger: Ledger, form: dict) -> str:
    """核实结果的闭环出口：记录结论与出处；若这条任务绑定了参数且填了值，顺手把参数升上去。

    等级校验交给 Ledger.set_checklist（A 级必须有 URL），异常由 do_POST 兜成页面提示。
    """
    item_id = int(_need(form, "id"))
    grade = _need(form, "grade")
    url = _first(form, "url")
    snapshot = _first(form, "snapshot")
    row = next((item for item in ledger.checklist_rows() if item["id"] == item_id), None)
    ledger.set_checklist(item_id, _need(form, "date"), _first(form, "conclusion"), grade,
                         source_url=url, snapshot_ref=snapshot)
    target = (row or {}).get("target_param_id")
    value = _first(form, "value")
    if target and value and grade in ("A", "B", "C"):
        ledger.set_param_value(target, value, grade, source_url=url, snapshot_ref=snapshot)
        return "/checklist?" + urlencode({
            "notice": "%s 已记录，%s 升到 %s 级" % (row.get("spec_task_id") or item_id, target, grade)})
    if target and value:
        return "/checklist?" + urlencode({
            "notice": "%s 级不能写入覆盖层，参数未升级；请先补到 A/B/C" % grade})
    return "/checklist?" + urlencode({"notice": "核实结果已写入"})


def _need(form: dict, key: str) -> str:
    value = _first(form, key).strip()
    if not value:
        raise ValueError("请填写 " + key)
    return value


def _first(form: dict, key: str) -> str:
    values = form.get(key) or [""]
    return values[0]


def _num(value) -> str:
    if value is None:
        return "—"
    return f"{value:g}"
