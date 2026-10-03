"""SQLite 台账。费率不预填网上数字。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from shopee_ledger.checklist import CHECKLIST
from shopee_ledger.escrow import map_escrow
from shopee_ledger.fulfillment import (
    Fulfillment,
    arrange_ship,
    confirm_address_format,
    copy_address,
    mark_inbound,
    mark_purchased,
    mark_stock,
)
from shopee_ledger.profit import Decision, ProfitInput, ProfitResult, evaluate
from shopee_ledger.veto import veto_reasons

DEFAULT_DB = Path("data") / "ledger.sqlite"

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


class Ledger:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS parameters (
              site TEXT NOT NULL,
              key TEXT NOT NULL,
              value TEXT,
              grade TEXT NOT NULL,
              PRIMARY KEY (site, key)
            );
            CREATE TABLE IF NOT EXISTS suppliers (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              url TEXT,
              years INTEGER,
              dropship INTEGER NOT NULL,
              address_ok INTEGER NOT NULL,
              pay_cny REAL
            );
            CREATE TABLE IF NOT EXISTS candidates (
              id INTEGER PRIMARY KEY,
              site TEXT NOT NULL,
              name TEXT NOT NULL,
              weight_g REAL,
              purchase_cny REAL,
              domestic_cny REAL,
              price REAL,
              intends_free_shipping INTEGER NOT NULL DEFAULT 1,
              sls_fee REAL,
              buyer_shipping REAL,
              ads REAL NOT NULL DEFAULT 0,
              commission_amount REAL,
              transaction_amount REAL,
              service_amount REAL,
              flags TEXT NOT NULL DEFAULT '',
              prelist INTEGER NOT NULL DEFAULT 1,
              return_rate REAL
            );
            CREATE TABLE IF NOT EXISTS orders (
              id INTEGER PRIMARY KEY,
              candidate_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              supplier_id INTEGER,
              warehouse_address TEXT NOT NULL DEFAULT '',
              address_source TEXT NOT NULL DEFAULT '',
              block_reason TEXT NOT NULL DEFAULT '',
              steps TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checklist (
              id INTEGER PRIMARY KEY,
              module TEXT NOT NULL,
              item TEXT NOT NULL,
              channel TEXT NOT NULL,
              grade TEXT NOT NULL,
              checked_date TEXT,
              conclusion TEXT
            );
            """
        )
        existing = self.conn.execute("SELECT COUNT(*) AS n FROM checklist").fetchone()["n"]
        if existing == 0:
            self.conn.executemany(
                "INSERT INTO checklist (id, module, item, channel, grade) VALUES (?, ?, ?, ?, ?)",
                CHECKLIST,
            )
        self._seed_param("MY", "go_rate", "0.15", "D")
        self._seed_param("MY", "watch_rate", "0.10", "D")
        self._seed_param("MY", "affiliate_rate", "0", "D")
        self._seed_param("TW", "affiliate_rate", "0", "D")
        self.migrate()
        self.conn.commit()

    def migrate(self) -> None:
        self._ensure_return_column()
        for table, column, ddl in (
            ("candidates", "sample_bought", "INTEGER NOT NULL DEFAULT 0"),
            ("candidates", "photo_ready", "INTEGER NOT NULL DEFAULT 0"),
            ("candidates", "title_text", "TEXT NOT NULL DEFAULT ''"),
            ("candidates", "detail_ready", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "deadline", "TEXT NOT NULL DEFAULT ''"),
            ("orders", "estimate_net", "REAL"),
            ("orders", "estimate_rate", "REAL"),
            ("orders", "actual_net", "REAL"),
            ("orders", "actual_rate", "REAL"),
        ):
            if column not in self._columns(table):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidate_suppliers (
              candidate_id INTEGER NOT NULL,
              supplier_id INTEGER NOT NULL,
              PRIMARY KEY (candidate_id, supplier_id)
            );
            CREATE TABLE IF NOT EXISTS sls_bands (
              id INTEGER PRIMARY KEY,
              site TEXT NOT NULL,
              max_g REAL NOT NULL,
              fee REAL NOT NULL
            );
            """
        )

    def _seed_param(self, site: str, key: str, value: str, grade: str) -> None:
        self.conn.execute(
            """
            INSERT INTO parameters (site, key, value, grade) VALUES (?, ?, ?, ?)
            ON CONFLICT(site, key) DO NOTHING
            """,
            (site, key, value, grade),
        )

    def set_param(self, site: str, key: str, value: str, grade: str) -> None:
        if key not in PARAM_HELP:
            raise ValueError("未知参数: " + key)
        if site not in ("MY", "TW"):
            raise ValueError("站点只能是 MY 或 TW")
        if key == "service_fee_kind" and value not in ("service", "shipping", "none"):
            raise ValueError("service_fee_kind 只能是 service、shipping、none")
        self.conn.execute(
            """
            INSERT INTO parameters (site, key, value, grade) VALUES (?, ?, ?, ?)
            ON CONFLICT(site, key) DO UPDATE SET value = excluded.value, grade = excluded.grade
            """,
            (site, key, value, grade),
        )
        self.conn.commit()

    def params(self, site: str) -> dict[str, str | None]:
        rows = self.conn.execute("SELECT key, value FROM parameters WHERE site = ?", (site,)).fetchall()
        found = {row["key"]: row["value"] for row in rows}
        return {key: found.get(key) for key in PARAM_HELP}

    def add_supplier(self, name: str, url: str, years: int, dropship: bool, address_ok: bool, pay_cny: float | None) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO suppliers (name, url, years, dropship, address_ok, pay_cny)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, url, years, int(dropship), int(address_ok), pay_cny),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_candidate(self, site: str, name: str, weight_g: float, purchase_cny: float, domestic_cny: float, price: float, sls_fee: float, intends_free_shipping: bool) -> int:
        if site not in ("MY", "TW"):
            raise ValueError("站点只能是 MY 或 TW")
        cur = self.conn.execute(
            """
            INSERT INTO candidates (
              site, name, weight_g, purchase_cny, domestic_cny, price, sls_fee, intends_free_shipping
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (site, name, weight_g, purchase_cny, domestic_cny, price, sls_fee, int(intends_free_shipping)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_flags(self, candidate_id: int, flags: list[str]) -> list[str]:
        reasons = veto_reasons(flags)
        self.conn.execute(
            "UPDATE candidates SET flags = ? WHERE id = ?",
            (",".join(flags), candidate_id),
        )
        self.conn.commit()
        return reasons

    def set_return_rate(self, candidate_id: int, rate: float) -> None:
        if not 0 <= rate <= 1:
            raise ValueError("退货率要在 0 到 1 之间")
        self._ensure_return_column()
        self.conn.execute("UPDATE candidates SET return_rate = ? WHERE id = ?", (rate, candidate_id))
        self.conn.commit()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def quote(self, candidate_id: int) -> ProfitResult:
        return self._quote_row(candidate_id, None, prelist=None)

    def _quote_row(self, candidate_id: int, escrow: dict | None, prelist: bool | None) -> ProfitResult:
        self.migrate()
        row = self.conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise ValueError("没有这个候选品")
        flags = [flag for flag in (row["flags"] or "").split(",") if flag]
        if veto_reasons(flags):
            return ProfitResult(Decision.CUT, missing=["veto:" + ",".join(flags)])
        site_params = self.params(row["site"])
        price = row["price"]
        commission = None if escrow is None else escrow.get("commission")
        transaction = None if escrow is None else escrow.get("transaction_fee")
        service = None if escrow is None else escrow.get("service_fee")
        if commission is None:
            commission = row["commission_amount"]
        if transaction is None:
            transaction = row["transaction_amount"]
        if service is None:
            service = row["service_amount"]
        if commission is None:
            commission = _amount(price, site_params.get("commission_rate"))
        if transaction is None:
            transaction = _amount(price, site_params.get("transaction_rate"))
        kind = site_params.get("service_fee_kind")
        if service is None and kind == "service":
            service = _amount(price, site_params.get("service_fee_rate"))
        sls = row["sls_fee"]
        buyer = row["buyer_shipping"]
        if escrow is not None:
            sls = escrow.get("sls_fee")
            buyer = escrow.get("buyer_shipping")
        elif sls is None and row["weight_g"] is not None:
            sls = self.fee_for_weight(row["site"], row["weight_g"])
        use_prelist = bool(row["prelist"]) if prelist is None else prelist
        data = ProfitInput(
            site=row["site"],
            price=price,
            purchase_cny=row["purchase_cny"],
            domestic_cny=row["domestic_cny"],
            local_per_cny=_float(site_params.get("local_per_cny")),
            sls_fee=sls,
            buyer_shipping=buyer,
            intends_free_shipping=bool(row["intends_free_shipping"]),
            prelist=use_prelist,
            commission=commission,
            transaction_fee=transaction,
            service_fee=service,
            service_fee_kind=kind,
            affiliate=_amount(price, site_params.get("affiliate_rate")) or 0.0,
            ads=row["ads"] or 0.0,
            withdrawal=_amount(price, site_params.get("withdrawal_rate")),
            fx_loss=_amount(price, site_params.get("fx_loss_rate")),
            return_rate=row["return_rate"],
            go_rate=_float(site_params.get("go_rate")),
            watch_rate=_float(site_params.get("watch_rate")),
        )
        return evaluate(data)

    def survival(self, site: str) -> tuple[int, int, float | None]:
        rows = self.conn.execute("SELECT id, flags FROM candidates WHERE site = ?", (site,)).fetchall()
        complete = 0
        passed = 0
        for row in rows:
            flags = [flag for flag in (row["flags"] or "").split(",") if flag]
            if veto_reasons(flags):
                continue
            result = self.quote(row["id"])
            if result.decision in (Decision.INCOMPLETE, Decision.THRESHOLD_UNSET):
                continue
            complete += 1
            if result.decision == Decision.GO:
                passed += 1
        if complete == 0:
            return 0, 0, None
        return passed, complete, passed / complete

    def open_order(self, candidate_id: int) -> int:
        state = Fulfillment()
        result = self.quote(candidate_id)
        cur = self.conn.execute(
            """
            INSERT INTO orders (
              candidate_id, status, supplier_id, warehouse_address, address_source, block_reason, steps,
              estimate_net, estimate_rate
            ) VALUES (?, ?, NULL, '', '', '', ?, ?, ?)
            """,
            (candidate_id, state.status, _dump_steps(state), result.net, result.rate),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def link_supplier(self, candidate_id: int, supplier_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO candidate_suppliers (candidate_id, supplier_id) VALUES (?, ?)",
            (candidate_id, supplier_id),
        )
        self.conn.commit()

    def supplier_ids(self, candidate_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT supplier_id FROM candidate_suppliers WHERE candidate_id = ? ORDER BY supplier_id",
            (candidate_id,),
        ).fetchall()
        return [row["supplier_id"] for row in rows]

    def set_listing(self, candidate_id: int, sample_bought: bool, photo_ready: bool, detail_ready: bool, title_text: str) -> None:
        self.conn.execute(
            """
            UPDATE candidates
            SET sample_bought = ?, photo_ready = ?, detail_ready = ?, title_text = ?
            WHERE id = ?
            """,
            (int(sample_bought), int(photo_ready), int(detail_ready), title_text.strip(), candidate_id),
        )
        self.conn.commit()

    def add_band(self, site: str, max_g: float, fee: float) -> None:
        if site not in ("MY", "TW"):
            raise ValueError("站点只能是 MY 或 TW")
        self.conn.execute("INSERT INTO sls_bands (site, max_g, fee) VALUES (?, ?, ?)", (site, max_g, fee))
        self.conn.commit()

    def bands(self, site: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM sls_bands WHERE site = ? ORDER BY max_g", (site,)))

    def fee_for_weight(self, site: str, weight_g: float) -> float | None:
        row = self.conn.execute(
            "SELECT fee FROM sls_bands WHERE site = ? AND max_g >= ? ORDER BY max_g LIMIT 1",
            (site, weight_g),
        ).fetchone()
        return None if row is None else float(row["fee"])

    def set_deadline(self, order_id: int, deadline: str) -> None:
        self.conn.execute("UPDATE orders SET deadline = ? WHERE id = ?", (deadline, order_id))
        self.conn.commit()

    def record_actual(self, order_id: int, payload: dict) -> ProfitResult:
        row = self.conn.execute("SELECT candidate_id FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise ValueError("没有这个订单")
        mapped = map_escrow(payload)
        result = self._quote_row(row["candidate_id"], mapped, prelist=False)
        self.conn.execute(
            "UPDATE orders SET actual_net = ?, actual_rate = ? WHERE id = ?",
            (result.net, result.rate, order_id),
        )
        self.conn.commit()
        return result

    def load_order(self, order_id: int) -> Fulfillment:
        row = self.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise ValueError("没有这个订单")
        state = Fulfillment(status=row["status"], supplier_id=row["supplier_id"])
        state.warehouse_address = row["warehouse_address"]
        state.address_source = row["address_source"]
        state.block_reason = row["block_reason"]
        saved = row["steps"].split(",") if row["steps"] else []
        for step in state.steps:
            state.steps[step] = step in saved
        return state

    def save_order(self, order_id: int, state: Fulfillment) -> None:
        self.conn.execute(
            """
            UPDATE orders
            SET status = ?, supplier_id = ?, warehouse_address = ?, address_source = ?, block_reason = ?, steps = ?
            WHERE id = ?
            """,
            (
                state.status,
                state.supplier_id,
                state.warehouse_address,
                state.address_source,
                state.block_reason,
                _dump_steps(state),
                order_id,
            ),
        )
        self.conn.commit()

    def supplier_address_ok(self, supplier_id: int) -> bool:
        row = self.conn.execute("SELECT address_ok FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
        if row is None:
            raise ValueError("没有这个供应商")
        return bool(row["address_ok"])

    def apply_stock(self, order_id: int, supplier_id: int, in_stock: bool, exhausted: bool) -> Fulfillment:
        state = self.load_order(order_id)
        mark_stock(state, supplier_id, in_stock, self.supplier_address_ok(supplier_id), exhausted)
        self.save_order(order_id, state)
        return state

    def apply_confirm_address(self, order_id: int) -> Fulfillment:
        state = self.load_order(order_id)
        confirm_address_format(state)
        self.save_order(order_id, state)
        return state

    def apply_arrange(self, order_id: int) -> Fulfillment:
        state = self.load_order(order_id)
        arrange_ship(state)
        self.save_order(order_id, state)
        return state

    def apply_copy(self, order_id: int, address: str) -> Fulfillment:
        state = self.load_order(order_id)
        copy_address(state, address, "order_page")
        self.save_order(order_id, state)
        return state

    def apply_purchase(self, order_id: int) -> Fulfillment:
        state = self.load_order(order_id)
        mark_purchased(state)
        self.save_order(order_id, state)
        return state

    def apply_inbound(self, order_id: int) -> Fulfillment:
        state = self.load_order(order_id)
        mark_inbound(state)
        self.save_order(order_id, state)
        return state

    def apply_escrow(self, candidate_id: int, payload: dict) -> dict:
        mapped = map_escrow(payload)
        self.conn.execute(
            """
            UPDATE candidates
            SET sls_fee = ?, buyer_shipping = ?, commission_amount = ?, transaction_amount = ?,
                service_amount = ?, prelist = 0
            WHERE id = ?
            """,
            (
                mapped["sls_fee"],
                mapped["buyer_shipping"],
                mapped["commission"],
                mapped["transaction_fee"],
                mapped["service_fee"],
                candidate_id,
            ),
        )
        self.conn.commit()
        return mapped

    def list_suppliers(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM suppliers ORDER BY id"))

    def list_candidates(self) -> list[sqlite3.Row]:
        self._ensure_return_column()
        return list(self.conn.execute("SELECT * FROM candidates ORDER BY id"))

    def list_orders(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT o.*, c.name AS candidate_name, c.site AS site
                FROM orders o
                JOIN candidates c ON c.id = o.candidate_id
                ORDER BY o.id
                """
            )
        )

    def set_ads(self, candidate_id: int, ads: float) -> None:
        if ads < 0:
            raise ValueError("广告费不能为负")
        self.conn.execute("UPDATE candidates SET ads = ? WHERE id = ?", (ads, candidate_id))
        self.conn.commit()

    def checklist_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM checklist ORDER BY id"))

    def set_checklist(self, item_id: int, checked_date: str, conclusion: str, grade: str) -> None:
        if grade not in ("A", "B", "C", "D", "E"):
            raise ValueError("等级只能是 A 到 E")
        found = self.conn.execute("SELECT id FROM checklist WHERE id = ?", (item_id,)).fetchone()
        if found is None:
            raise ValueError("没有这条待核实项")
        self.conn.execute(
            "UPDATE checklist SET checked_date = ?, conclusion = ?, grade = ? WHERE id = ?",
            (checked_date, conclusion, grade, item_id),
        )
        self.conn.commit()

    def _ensure_return_column(self) -> None:
        if "return_rate" not in self._columns("candidates"):
            self.conn.execute("ALTER TABLE candidates ADD COLUMN return_rate REAL")
            self.conn.commit()


def _float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _amount(price: float | None, rate: str | None) -> float | None:
    if price is None or rate is None or rate == "":
        return None
    return price * float(rate)


def _dump_steps(state: Fulfillment) -> str:
    return ",".join(step for step, done in state.steps.items() if done)
