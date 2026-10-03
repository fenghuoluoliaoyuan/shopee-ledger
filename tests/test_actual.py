"""估算 vs 实绩，以及 alert 命令行。

实绩这一块的纪律是：**账单实付直接覆盖费率估算**（CostEngine 的 actual_* 输入），
不另写一份公式；算不出来的部分宁可空着，也不编一个数字。
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.store import Ledger
from shopee_ledger.web import books_page

ROOT = Path(__file__).resolve().parents[1]

ESCROW = {"order_income": {
    "actual_shipping_fee": 60,
    "buyer_paid_shipping_fee": 0,
    "commission_fee": 40.0,
    "transaction_fee": 9.0,
}}


class ActualTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.init()
        self.ledger.set_param("TW", "local_per_cny", "4.5", "C")
        self.candidate = self.ledger.add_candidate("TW", "杯垫", 80, 20, 1.5, 350, 60, True)
        self.order = self.ledger.open_order(self.candidate)

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _row(self) -> dict:
        return [row for row in self.ledger.list_orders() if row["id"] == self.order][0]

    def test_open_order_freezes_the_estimate(self):
        row = self._row()
        self.assertIsNotNone(row["estimate_rate"], "下单时要把估算冻住，否则账本左列永远是空的")
        self.assertIsNotNone(row["estimate_net"])
        self.assertIsNone(row["actual_rate"])

    def test_estimate_does_not_move_when_params_change(self):
        frozen = self._row()["estimate_rate"]
        self.ledger.set_param("TW", "local_per_cny", "5.0", "C")  # 事后改汇率
        self.assertEqual(self._row()["estimate_rate"], frozen, "冻住的估算不该被后续改参数影响")

    def test_record_actual_uses_bill_amounts(self):
        view = self.ledger.record_actual(self.order, ESCROW)
        self.assertIsNotNone(view.rate, "账单佣金/手续费齐了就该能算出实绩")
        row = self._row()
        self.assertIsNotNone(row["actual_rate"])
        self.assertAlmostEqual(row["actual_rate"], view.rate, places=9)
        # 账单佣金 40 < 估算 350×14%=49，所以实绩利润率应当更高
        self.assertGreater(row["actual_rate"], row["estimate_rate"])

    def test_actual_does_not_overwrite_estimate(self):
        before = self._row()["estimate_rate"]
        self.ledger.record_actual(self.order, ESCROW)
        self.assertEqual(self._row()["estimate_rate"], before)

    def test_service_fee_without_owner_is_reported_not_guessed(self):
        payload = {"order_income": dict(ESCROW["order_income"], service_fee=5.0)}
        view = self.ledger.record_actual(self.order, payload)
        self.assertIn("service_fee_kind", view.missing)
        self.assertIn("归属待确认", view.explain())

    def test_books_page_shows_delta(self):
        self.ledger.record_actual(self.order, ESCROW)
        page = books_page(self.ledger)
        self.assertIn("估算（下单时）", page)
        self.assertIn("实绩（账单）", page)
        self.assertIn("个百分点", page, "要给出估算偏差，否则两列白放")

    def test_books_page_before_actual(self):
        page = books_page(self.ledger)
        self.assertIn("#%d" % self.order, page)
        self.assertIn("<td>—</td>", page, "还没填账单时实绩列应当是空的，不是 0")


class AlertCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "ledger.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "shopee_ledger", "--db", str(self.db), *args],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")

    def test_alert_lists_blocking_tasks(self):
        self._run("init")
        proc = self._run("alert")
        self.assertIn("阻塞第一单的核实", proc.stdout)
        self.assertIn("P-TW-DTS", proc.stdout)

    def test_alert_returns_nonzero_when_p1_present(self):
        """有计划任务依赖这个返回码：没有 P1 才算正常退出。"""
        self._run("init")
        self._run("param-set", "--site", "TW", "--key", "local_per_cny", "--value", "4.5", "--grade", "C")
        self._run("candidate-add", "--site", "TW", "--name", "杯垫", "--weight", "80",
                  "--purchase", "20", "--domestic", "1.5", "--price", "350", "--sls", "60",
                  "--free-shipping", "yes")
        self._run("supplier-add", "--name", "甲", "--url", "u", "--years", "3",
                  "--dropship", "yes", "--address-ok", "yes", "--pay", "10")
        self._run("order-open", "--candidate", "1")
        self._run("order-stock", "--order", "1", "--supplier", "1", "--in-stock", "yes")
        self._run("order-arrange", "--order", "1")

        clean = self._run("alert")
        self.assertEqual(clean.returncode, 0, "刚安排发货，没有 P1 告警")
        self.assertIn("告警：无", clean.stdout)

        # 把"进入 ship_arranged"的时间戳补录成 30 小时前
        from datetime import datetime, timedelta, timezone

        ledger = Ledger(self.db)
        ledger.init()
        stamp = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
        ledger.storage.record_audit("order.transition", "order", 1, at=stamp)
        ledger.close()

        alerted = self._run("alert")
        self.assertEqual(alerted.returncode, 1, "有 P1 告警时返回码应为 1")
        self.assertIn("超过 24 小时未拍单", alerted.stdout)


if __name__ == "__main__":
    unittest.main()
