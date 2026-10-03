"""异常分支接真实时间戳的契约测试。

背景：EX-01/02/03 早就定义在 spec/modes/dropship.json 里，但一直没接线
（`exceptions(hours_in_state=None)` 永远不触发）。这里锁住"接线"这件事，
以及一条更重要的纪律：**未核实的参数不得生成告警**。
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.spec import default_spec
from shopee_ledger.store import Ledger
from shopee_ledger.web import today as today_page


class AlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite", verified_path=None)
        self.ledger.init()
        self.ledger.set_param("TW", "local_per_cny", "4.5", "C")
        self.candidate = self.ledger.add_candidate("TW", "杯垫", 80, 20, 1.5, 350, 60, True)
        self.supplier = self.ledger.add_supplier("甲", "https://example.test", 3, True, True, 10)
        self.order = self.ledger.open_order(self.candidate)
        self.ledger.apply_stock(self.order, self.supplier, True, False)

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _backdate(self, hours: float, offset: str = "+00:00") -> None:
        """审计表只增不改，所以用"补录一条带旧时间戳的记录"来模拟停留时长。"""
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours))
        text = stamp.astimezone(timezone(timedelta(0))).isoformat(timespec="seconds")
        if offset == "+08:00":
            text = stamp.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        self.ledger.storage.record_audit("order.transition", "order", self.order,
                                         at=text, detail={"from": "stock_checked", "to": "ship_arranged"})

    def _enable_dts(self) -> None:
        """把 P-TW-DTS 升到 A 级——EX-02 只在参数已核实时才启用。"""
        self.ledger._spec = default_spec().with_overrides([{
            "param_id": "P-TW-DTS", "value": {"days": 5}, "evidence_level": "A",
            "checked_at": "2026-10-03", "source_url": "https://seller.test/dts",
        }])
        self.assertTrue(self.ledger.dts_ready())

    def _near_deadline(self, hours: float) -> None:
        """把到仓扫描截止挪到距现在 hours 小时处。"""
        stamp = (datetime.now(timezone.utc) + timedelta(hours=hours))
        self.ledger.storage.update("Order", self.order,
                                   {"scan_deadline": stamp.isoformat(timespec="seconds")})

    # ---- 时间戳接线 -----------------------------------------------------
    def test_state_since_reads_latest_transition(self):
        self.ledger.apply_arrange(self.order)
        self.assertIsNotNone(self.ledger.state_since(self.order))
        self.assertLess(self.ledger.hours_in_state(self.order), 1.0)

    def test_hours_in_state_handles_offset(self):
        self._backdate(30, offset="+08:00")
        hours = self.ledger.hours_in_state(self.order)
        self.assertIsNotNone(hours)
        self.assertAlmostEqual(hours, 30, delta=0.1, msg="带 +08:00 偏移的时间戳要能正确解析")

    def test_missing_timestamp_returns_none(self):
        self.assertIsNone(self.ledger.hours_in_state(999))

    # ---- EX-01 ----------------------------------------------------------
    def test_no_alert_when_just_arranged(self):
        self.ledger.apply_arrange(self.order)
        self.assertEqual(self.ledger.order_alerts(), [])

    def test_ex01_fires_after_24h_in_ship_arranged(self):
        self.ledger.apply_arrange(self.order)
        self._backdate(30)
        alerts = self.ledger.order_alerts()
        target = [item for item in alerts if item["id"] == "EX-01"]
        self.assertEqual(len(target), 1)
        self.assertEqual(target[0]["order_id"], self.order)
        self.assertEqual(target[0]["priority"], "P1")
        self.assertAlmostEqual(target[0]["hours_in_state"], 30, delta=0.2)

    def test_ex01_not_fired_at_23h(self):
        self.ledger.apply_arrange(self.order)
        self._backdate(23)
        self.assertEqual([a for a in self.ledger.order_alerts() if a["id"] == "EX-01"], [])

    def _to_supplier_shipped(self) -> None:
        """走合法路径到 supplier_shipped。地址那一步必须走 apply_copy，
        否则 R-ORDER-002 会（正确地）拦下 po_created。"""
        self.ledger.apply_arrange(self.order)
        self.ledger.apply_copy(self.order, "当单中转仓 订单号SN1")
        self.ledger.advance_order(self.order, "po_created")
        self.ledger.advance_order(self.order, "supplier_shipped")

    # ---- EX-02 与"未核实不告警" -----------------------------------------
    def test_dts_is_not_ready_by_default(self):
        self.assertFalse(self.ledger.dts_ready(), "P-TW-DTS 仍是 E 级，不该启用倒计时")

    def test_ex02_suppressed_while_dts_unverified(self):
        self._to_supplier_shipped()
        self._backdate(200)
        self.assertEqual([a for a in self.ledger.order_alerts() if a["id"] == "EX-02"], [],
                         "参数未核实就发告警＝拿未核实数据做决策")

    def test_ex02_fires_once_dts_verified(self):
        # 用 A 级覆盖把 P-TW-DTS 升级，模拟"用户在卖家中心抄到了"
        self.ledger._spec = default_spec().with_overrides([{
            "param_id": "P-TW-DTS", "value": {"days": 5}, "evidence_level": "A",
            "checked_at": "2026-10-03", "source_url": "https://seller.test/dts",
        }])
        self.assertTrue(self.ledger.dts_ready())
        self._to_supplier_shipped()
        self._near_deadline(hours=6)
        self.assertEqual(len([a for a in self.ledger.order_alerts() if a["id"] == "EX-02"]), 1)

    def test_ex02_does_not_fire_while_the_deadline_is_far(self):
        """这是改动的要点：**时间在过去不等于有风险**。

        旧口径是"进入 supplier_shipped 满 72 小时"，既不看站点也不看下单时刻——
        一单要是备货 7 天，它在第 3 天就会被误报。
        """
        self._enable_dts()
        self._to_supplier_shipped()
        self._backdate(200)          # 状态里待了很久
        self.assertEqual([a for a in self.ledger.order_alerts() if a["id"] == "EX-02"], [],
                         "到仓截止还早，不该因为「待得久」就报警")

    def test_ex02_says_how_long_is_left(self):
        """告警要给数字，不能只说"有风险"——靠它决定现在去催还是明天再说。"""
        self._enable_dts()
        self._to_supplier_shipped()
        self._near_deadline(hours=6)
        alert = [a for a in self.ledger.order_alerts() if a["id"] == "EX-02"][0]
        self.assertIn("到仓扫描截止", alert["message"])
        self.assertLessEqual(alert["hours_to_scan_deadline"], 24)
        self.assertTrue(alert["scan_deadline"])

    def test_ex02_distinguishes_already_overdue(self):
        self._enable_dts()
        self._to_supplier_shipped()
        self._near_deadline(hours=-3)      # 已过期
        alert = [a for a in self.ledger.order_alerts() if a["id"] == "EX-02"][0]
        self.assertIn("已过", alert["message"])

    def test_deadlines_are_frozen_at_order_time(self):
        """开单时算好并冻住——以后改商品的备货时长，不该改写已经开出去的单。"""
        info = self.ledger.order_deadlines(self.order)
        self.assertTrue(info["frozen"])
        self.assertTrue(info["dts"] and info["scan"])
        self.ledger.set_dts_days(self.candidate, 7)
        again = self.ledger.order_deadlines(self.order)
        self.assertEqual(again["scan"], info["scan"], "已开的单不该被改")
        # 但新开的单要用新值
        fresh = self.ledger.open_order(self.candidate)
        self.assertEqual(self.ledger.order_deadlines(fresh)["dts_days"], 7)

    def test_scan_deadline_is_after_the_dts_deadline(self):
        info = self.ledger.order_deadlines(self.order)
        self.assertLess(info["dts"], info["scan"])

    # ---- EX-03 与终态 ---------------------------------------------------
    def test_ex03_on_cancelled_order(self):
        fresh = self.ledger.open_order(self.candidate)
        self.ledger.apply_stock(fresh, self.supplier, False, True)
        alerts = [item for item in self.ledger.order_alerts() if item["id"] == "EX-03"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("下架", alerts[0]["message"])

    def test_paid_order_produces_no_alerts(self):
        self._to_supplier_shipped()
        for state in ("warehouse_scanned", "in_transit", "delivered",
                      "completed", "payable", "paid"):
            self.ledger.advance_order(self.order, state)
        self._backdate(500)
        self.assertEqual(self.ledger.order_alerts(), [])

    # ---- 解锁清单 -------------------------------------------------------
    def test_unverified_unlocks_names_the_blocked_feature(self):
        unlocks = {item["param_id"]: item for item in self.ledger.unverified_unlocks()}
        self.assertIn("P-TW-DTS", unlocks)
        self.assertIn("倒计时", unlocks["P-TW-DTS"]["feature"])
        self.assertEqual(unlocks["P-TW-DTS"]["task_ref"], "VT-015")

    def test_unlocks_shrinks_after_verification(self):
        self.ledger._spec = default_spec().with_overrides([{
            "param_id": "P-TW-DTS", "value": {"days": 5}, "evidence_level": "A"}])
        ids = [item["param_id"] for item in self.ledger.unverified_unlocks()]
        self.assertNotIn("P-TW-DTS", ids, "核实后该功能应自动从「被挡」清单里消失")


class TodayPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite", verified_path=None)
        self.ledger.init()

    def tearDown(self):
        try:
            self.ledger.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_today_shows_blocking_tasks_and_unlocks(self):
        page = today_page(self.ledger)
        self.assertIn("阻塞第一单的核实", page)
        self.assertIn("未核实参数挡着哪些功能", page)
        self.assertIn("P-TW-DTS", page)
        self.assertIn("选品卡在哪", page)

    def test_today_has_no_leftover_auto_conclusions(self):
        """旧的 PUBLIC_NOTES 会按错位编号预填"已核实"，已删除。"""
        page = today_page(self.ledger)
        self.assertNotIn("写入公开页已能确定的说明", page)
        conclusions = [row["conclusion"] for row in self.ledger.checklist_rows()]
        self.assertEqual([c for c in conclusions if c], [], "不得代替用户宣告已核实")

    def test_today_surfaces_ex01_alert(self):
        self.ledger.set_param("TW", "local_per_cny", "4.5", "C")
        candidate = self.ledger.add_candidate("TW", "杯垫", 80, 20, 1.5, 350, 60, True)
        supplier = self.ledger.add_supplier("甲", "u", 3, True, True, 10)
        order = self.ledger.open_order(candidate)
        self.ledger.apply_stock(order, supplier, True, False)
        self.ledger.apply_arrange(order)
        stamp = datetime.now(timezone.utc) - timedelta(hours=30)
        self.ledger.storage.record_audit("order.transition", "order", order,
                                         at=stamp.isoformat(timespec="seconds"))
        page = today_page(self.ledger)
        self.assertIn("要先处理", page)
        self.assertIn("超过 24 小时未拍单", page)
        self.assertIn("订单 #%d" % order, page)


if __name__ == "__main__":
    unittest.main()
