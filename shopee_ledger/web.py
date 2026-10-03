"""本机网页。侧栏和表格按 shadcn 后台，指标卡按 Tremor。"""

from __future__ import annotations

import json
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from shopee_ledger.desk import listing_gate, survival_advice
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        notice = _first(query, "notice")
        error = _first(query, "error")
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
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
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
            return _save_product(ledger, form)
        if path == "/orders":
            return _order_post(ledger, form)
        if path == "/checklist":
            ledger.set_checklist(int(_need(form, "id")), _need(form, "date"), _need(form, "conclusion"), _need(form, "grade"))
            return "/checklist?" + urlencode({"notice": "核实结果已写入"})
        if path == "/escrow":
            view = ledger.record_actual(int(_need(form, "order")), json.loads(_need(form, "payload")))
            return "/books?" + urlencode({"notice": view.explain()})
        raise ValueError("未知页面")


def _order_post(ledger: Ledger, form: dict) -> str:
    action = _need(form, "action")
    if action == "open":
        ledger.open_order(int(_need(form, "candidate")))
        return "/orders?" + urlencode({"notice": "订单已建立，估算已冻住"})
    order_id = int(_need(form, "order"))
    if action == "stock":
        ledger.apply_stock(order_id, int(_need(form, "supplier")), _first(form, "in_stock") == "yes", _first(form, "exhausted") == "yes")
    elif action == "confirm":
        ledger.apply_confirm_address(order_id)
    elif action == "arrange":
        ledger.apply_arrange(order_id)
    elif action == "copy":
        ledger.apply_copy(order_id, _need(form, "address"))
    elif action == "purchase":
        ledger.apply_purchase(order_id)
    elif action == "inbound":
        ledger.apply_inbound(order_id)
    elif action == "deadline":
        ledger.set_deadline(order_id, _need(form, "deadline"))
    elif action == "actual":
        result = ledger.record_actual(order_id, json.loads(_need(form, "payload")))
        text = "实绩已分开记下" if result.rate is not None else "实绩还缺：" + "、".join(result.missing)
        return "/orders?" + urlencode({"notice": text})
    else:
        raise ValueError("未知履约动作")
    return "/orders?" + urlencode({"notice": "履约步骤已更新"})


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


def _gate(ledger: Ledger, row) -> str:
    result = ledger.quote(row["id"])
    return listing_gate(
        result,
        supplier_count=len(ledger.supplier_ids(row["id"])),
        sample_bought=bool(row["sample_bought"]),
        weighed=row["weight_g"] is not None,
        purchase_price_cny=row["purchase_cny"],
        photo_ready=bool(row["photo_ready"]),
        title_ready=bool((row["title_text"] or "").strip()),
        detail_ready=bool(row["detail_ready"]),
    )


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
        param_rows.append(
            "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s%s</td><td>%s</td></tr>" % (
                escape(pid), escape(param.name[:22]), shown,
                escape(param.evidence_level), escape(state), flag,
                escape(param.next_review_at or "—"),
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
<p class="k">「不可硬拦」= 该证据等级不能参与硬门禁（INV-001/010）。</p>
<table><tr><th>ID</th><th>名称</th><th>值</th><th>等级</th><th>状态</th><th>下次复核</th></tr>{''.join(param_rows)}</table></section>

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


def products_page(ledger: Ledger) -> str:
    rows = []
    for row in ledger.list_candidates():
        result = ledger.quote(row["id"])
        gate = _gate(ledger, row)
        money = ""
        if result.rate is not None and result.net is not None:
            money = f"{result.net:.2f} / {result.rate:.1%}"
        rows.append(
            "<tr>"
            f"<td>{escape(row['name'])}</td><td>{escape(row['site'])}</td>"
            f"<td class='pill {result.decision.value}'>{DECISION_LABEL[result.decision]}</td>"
            f"<td>{money}</td><td>{escape(gate)}</td><td>{len(ledger.supplier_ids(row['id']))} 家</td></tr>"
        )
    flags = "".join(
        f'<label class="checks"><input type="checkbox" name="flag" value="{key}"> {escape(label)}</label>'
        for key, label in VETO_FLAGS.items()
    )
    suppliers = "".join(
        f'<label class="checks"><input type="checkbox" name="supplier" value="{row["id"]}"> {escape(row["name"])}</label>'
        for row in ledger.list_suppliers()
    )
    return f"""<h1>选品</h1><p class="lead">可上架要同时满足：利润过门槛、样品已买已称、至少 3 家代发、主图标题详情齐。</p>
<div class="row"><section class="card"><table>
<tr><th>品名</th><th>站</th><th>利润</th><th>净利润</th><th>下一步</th><th>供应商</th></tr>
{''.join(rows)}</table></section>
<form class="card stack" method="post" action="/products">
<label>站点<select name="site"><option>MY</option><option>TW</option></select></label>
<label>品名<input name="name" required></label>
<label>标题<input name="title" placeholder="核心词 + 属性 + 场景 + 地域"></label>
<label>重量 g<input name="weight" required></label>
<label>采购价 ¥<input name="purchase" required></label>
<label>国内段运费 ¥<input name="domestic" required></label>
<label>商品价<input name="price" required></label>
<label>SLS 运费<input name="sls" required></label>
<label>退货率<input name="return_rate" placeholder="0.05"></label>
<label>本单广告费<input name="ads" value="0"></label>
<label>包邮<select name="free_shipping"><option value="yes">是，买家运费记 0</option><option value="no">否</option></select></label>
<label class="checks"><input type="checkbox" name="sample" value="yes"> 样品已买</label>
<label class="checks"><input type="checkbox" name="photo" value="yes"> 主图是自己拍的</label>
<label class="checks"><input type="checkbox" name="detail" value="yes"> 详情前 3 屏已写</label>
<div class="checks">{suppliers or '<span class="k">还没有供应商</span>'}</div>
<div class="checks">{flags}</div>
<button>录入</button></form></div>"""


def books_page(ledger: Ledger) -> str:
    rows = []
    for row in ledger.list_orders():
        estimate = "—" if row["estimate_rate"] is None else f"{row['estimate_net']:.2f} / {row['estimate_rate']:.1%}"
        actual = "—" if row["actual_rate"] is None else f"{row['actual_net']:.2f} / {row['actual_rate']:.1%}"
        rows.append(
            f"<tr><td>#{row['id']} {escape(row['candidate_name'])}</td><td>{estimate}</td><td>{actual}</td></tr>"
        )
    return f"""<h1>账本</h1><p class="lead">左列是下单时冻住的估算，右列是托管明细算出来的实绩。实绩不改选品上的估算。</p>
<section class="card"><table><tr><th>订单</th><th>估算</th><th>实绩</th></tr>{''.join(rows)}</table></section>"""


def orders_page(ledger: Ledger) -> str:
    suppliers = ledger.list_suppliers()
    supplier_options = "".join(f'<option value="{row["id"]}">{row["id"]} {escape(row["name"])}</option>' for row in suppliers)
    candidate_options = "".join(
        f'<option value="{row["id"]}">{row["id"]} {escape(row["name"])}</option>' for row in ledger.list_candidates()
    )
    blocks = []
    for row in ledger.list_orders():
        done = set(filter(None, (row["steps"] or "").split(",")))
        steps = "".join(
            f'<span class="step{" done" if step in done else ""}">{STEP_LABEL[step]}</span>' for step in STEPS
        )
        blocks.append(
            f"""<section class="card"><h2>#{row['id']} {escape(row['candidate_name'])} · {escape(row['site'])}</h2>
<p class="k">{escape(row['status'])} {escape(row['block_reason'] or '')}</p>
<div class="steps">{steps}</div>
<p class="k">当单地址：{escape(row['warehouse_address'] or '还没抄')}</p>
<form method="post" action="/orders" class="stack">
<input type="hidden" name="order" value="{row['id']}">
<label>供应商<select name="supplier">{supplier_options}</select></label>
<label>有货<select name="in_stock"><option value="yes">有货</option><option value="no">无货</option></select></label>
<label>三家都无货<select name="exhausted"><option value="no">否</option><option value="yes">是，先下架</option></select></label>
<button name="action" value="stock">记录库存</button>
<button class="ghost" name="action" value="confirm">确认地址格式</button>
<button class="ghost" name="action" value="arrange">安排发货</button>
<label>当单中转仓地址<input name="address"></label>
<button class="ghost" name="action" value="copy">抄入地址</button>
<button class="ghost" name="action" value="purchase">已拍单</button>
<label>发货截止<input name="deadline" value="{escape(row['deadline'] or '')}" placeholder="2026-10-04 18:00"></label>
<button class="ghost" name="action" value="deadline">记下截止时间</button>
<label>这一单的托管 JSON<textarea name="payload"></textarea></label>
<button class="ghost" name="action" value="actual">记下实绩</button>
</form></section>"""
        )
    opener = f"""<form class="card stack" method="post" action="/orders">
<input type="hidden" name="action" value="open">
<label>候选品<select name="candidate">{candidate_options}</select></label>
<button>建立订单</button></form>"""
    return f'<h1>订单</h1><p class="lead">先查库存，再建档过的供应商可跳过地址确认，然后才安排发货。地址只能抄当单页面。</p><div class="row">{opener}<div>{"".join(blocks) or '<p class="k">还没有订单</p>'}</div></div>'


def checklist_page(ledger: Ledger) -> str:
    rows = []
    for row in ledger.checklist_rows():
        grades = "".join(
            f'<option{" selected" if grade == row["grade"] else ""}>{grade}</option>' for grade in "ABCDE"
        )
        rows.append(
            f"""<tr><td>{row['id']}</td><td>{escape(row['module'])}</td><td>{escape(row['item'])}</td>
<td>{escape(row['conclusion'] or '')}</td>
<td><form method="post" action="/checklist">
<input type="hidden" name="id" value="{row['id']}">
<input name="date" placeholder="日期" value="{escape(row['checked_date'] or '')}">
<input name="conclusion" placeholder="结论" value="{escape(row['conclusion'] or '')}">
<select name="grade">{grades}</select>
<button class="slim">保存</button></form></td></tr>"""
        )
    return f'''<h1>待核实</h1><p class="lead">这是附属记录。公开网页对不上中国跨境店的费率，所以不会把网上的百分比写进参数。</p>
<form method="post" action="/checklist">
<section class="card"><table>{''.join(rows)}</table></section>'''


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
