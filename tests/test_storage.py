"""存储层契约测试。重点验证「只增不改」是在**数据库层**兜住的。"""

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.cost_engine import CostEngine, CostInputs
from shopee_ledger.spec import Spec
from shopee_ledger.storage import Storage, table_for

ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "spec"


class StorageTest(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors：Windows 上 sqlite 文件句柄回收有延迟，不该让清理失败淹没测试结果
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.spec = Spec.load(SPEC_ROOT)
        self.storage = Storage(Path(self.tmp.name) / "ledger.sqlite", self.spec)
        self.created = self.storage.init()

    def tearDown(self):
        try:
            self.storage.close()
        except sqlite3.Error:
            pass
        self.tmp.cleanup()

    # ---- 元数据驱动建表 -------------------------------------------------
    def test_creates_one_table_per_entity(self):
        self.assertEqual(len(self.created), len(self.spec.entities))
        rows = self.storage.connect().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {row["name"] for row in rows}
        for entity_id in self.spec.entities:
            self.assertIn(table_for(entity_id), names, entity_id)

    def test_columns_follow_entities_json(self):
        entity = self.spec.entities["Supplier"]
        cols = {row["name"] for row in self.storage.connect().execute(
            "PRAGMA table_info(supplier)").fetchall()}
        for field in entity["fields"]:
            self.assertIn(field["name"], cols)
        self.assertIn("created_at", cols)

    # ---- 通用读写 -------------------------------------------------------
    def test_insert_get_update_list_count(self):
        row_id = self.storage.insert("Supplier", {
            "name": "甲供应商", "url": "https://example.test", "years_in_business": 3,
            "supports_dropship": True, "accepts_warehouse_address_format": True,
        })
        row = self.storage.get("Supplier", row_id)
        self.assertEqual(row["name"], "甲供应商")
        self.assertEqual(row["supports_dropship"], 1)
        self.storage.update("Supplier", row_id, {"years_in_business": 5})
        self.assertEqual(self.storage.get("Supplier", row_id)["years_in_business"], 5)
        self.assertEqual(self.storage.count("Supplier"), 1)
        self.assertEqual(len(self.storage.list("Supplier")), 1)

    def test_json_fields_roundtrip(self):
        row_id = self.storage.insert("ProductCandidate", {
            "platform": "shopee", "market": "TW", "source_url": "u1", "title": "杯垫",
            "category": "home", "veto_flags": {"apparel": False}, "state": "candidate",
            "competitor_notes": {"discounted_same_items": 1, "conclusion": "ok"},
        })
        row = self.storage.get("ProductCandidate", row_id)
        self.assertEqual(row["veto_flags"], {"apparel": False})
        self.assertEqual(row["competitor_notes"]["conclusion"], "ok")

    # ---- 只增不改（本轮的核心） -----------------------------------------
    def test_immutable_entity_rejects_update_in_app_layer(self):
        row_id = self.storage.insert("SupplierQuote", {
            "supplier_id": "1", "price_cny": 10.0, "quoted_at": "2026-10-03",
        })
        with self.assertRaises(ValueError):
            self.storage.update("SupplierQuote", row_id, {"price_cny": 9.0})

    def test_immutable_entity_rejects_raw_sql_update_at_db_layer(self):
        """关键：绕过应用层的裸 SQL 也必须被触发器拦住（INV-007）。"""
        row_id = self.storage.insert("SupplierQuote", {
            "supplier_id": "1", "price_cny": 10.0, "quoted_at": "2026-10-03",
        })
        conn = self.storage.connect()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            conn.execute("UPDATE supplier_quote SET price_cny = 1 WHERE id = ?", (row_id,))
        self.assertIn("append-only", str(ctx.exception))

    def test_immutable_entity_rejects_delete(self):
        row_id = self.storage.insert("AuditLog", {
            "action": "test", "object_type": "x", "object_id": "1", "operator": "tester",
            "at": "2026-10-03T00:00:00+00:00",
        })
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.connect().execute("DELETE FROM audit_log WHERE id = ?", (row_id,))

    # ---- 审计与快照 -----------------------------------------------------
    def test_audit_log_records_and_is_readable(self):
        self.storage.record_audit("gate.check", "candidate", 7, rule_id="R-DATA-001",
                                  result="INCOMPLETE", detail={"missing": ["weight_g"]})
        rows = self.storage.list("AuditLog")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rule_id"], "R-DATA-001")
        self.assertEqual(rows[0]["detail"]["missing"], ["weight_g"])
        self.assertTrue(rows[0]["params_version"])

    def test_cost_snapshot_persists_trace_and_version(self):
        engine = CostEngine(self.spec, today="2026-10-03")
        result = engine.quote(CostInputs(
            market="TW", price_local=350.0, purchase_cny=20.0, domestic_cny=1.5,
            local_per_cny=4.5, sls_freight=60.0, seller_pays_freight=True,
            withdraw_rate=0.012, fx_loss_rate=0.005, return_rate=0.05,
        ))
        row_id = self.storage.save_cost_snapshot(result, subject_type="candidate", subject_id=1)
        row = self.storage.get("CostSnapshot", row_id)
        self.assertEqual(row["type"], "estimate")
        self.assertAlmostEqual(row["outputs"]["net_margin"], result.rate, places=9)
        self.assertTrue(len(row["trace"]) > 5)
        labels = [item["label"] for item in row["trace"]]
        self.assertIn("佣金", labels)
        self.assertEqual(row["params_version"], self.storage.fingerprint())
        self.assertEqual(self.storage.count("config_version"), 1)

    def test_fingerprint_changes_when_param_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            temp_spec = Path(folder) / "spec"
            shutil.copytree(SPEC_ROOT, temp_spec)
            before = Storage(Path(folder) / "a.sqlite", Spec.load(temp_spec)).fingerprint()

            path = temp_spec / "params" / "platform.shopee.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            for param in doc["params"]:
                if param["id"] == "P-TW-COMMISSION":
                    param["value"] = 0.30
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            after = Storage(Path(folder) / "b.sqlite", Spec.load(temp_spec)).fingerprint()
            self.assertNotEqual(before, after, "参数变更必须产生新的配置指纹（INV-009）")

    def test_repeated_init_is_idempotent(self):
        self.storage.init()
        self.storage.init()
        self.assertEqual(self.storage.count("Supplier"), 0)


if __name__ == "__main__":
    unittest.main()
