"""运费表与站点费率的抓取、比对测试。

重点在 diff：**"变了"必须具体到哪个渠道哪一档从 X 到 Y**，
否则等于没说。这里用合成数据钉住这个契约。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.freight import (  # noqa: E402
    diff_config,
    diff_site_fees,
    fetch_site_config,
    latest_reference,
    rate_cards,
)


def config(date="2026-10-03", fee=25.0, extra_channel=False):
    channels = [{
        "name": "StoreToStore", "cn_name": "蝦皮店到店",
        "zones": [{"name": "ALL", "cn_name": "所有地区", "fee_modes": [
            {"name": "WeightRange", "type": "Seller", "effective_date": "2024-05-01",
             "start_weight": 0.0, "end_weight": 500.0, "original_fee": fee,
             "increment_unit": None, "increment_amount": None},
            {"name": "Flat", "type": "Buyer", "effective_date": "2024-05-01",
             "start_weight": None, "end_weight": None, "original_fee": 45.0,
             "increment_unit": None, "increment_amount": None},
        ]}],
    }]
    if extra_channel:
        channels.append({"name": "New", "cn_name": "新渠道", "zones": [
            {"name": "ALL", "cn_name": "所有地区", "fee_modes": [
                {"name": "Flat", "type": "Buyer", "effective_date": "2026-01-01",
                 "start_weight": None, "end_weight": None, "original_fee": 99.0,
                 "increment_unit": None, "increment_amount": None}]}]})
    return {"date": date, "site_info": [{
        "name": "TW", "cn_name": "台湾站点", "currency_unit": "TWD",
        "cargo_types": [{"name": "Normal", "cn_name": "普货", "channels": channels}]}]}


class RateCardTest(unittest.TestCase):
    def find(self, cards, prefix):
        """按前缀找卡。key 里含区间，所以不用全等匹配。"""
        hits = [key for key in cards if key.startswith(prefix)]
        self.assertTrue(hits, "找不到 %s" % prefix)
        return cards[min(hits, key=len)]

    def test_flattens_nested_structure_into_addressable_keys(self):
        cards = rate_cards(config())
        seller = self.find(cards, "TW/Normal/蝦皮店到店/所有地区/Seller/WeightRange")
        self.assertEqual(seller["fee"], 25.0)
        self.assertEqual(seller["payer"], "Seller")

    def test_key_includes_the_weight_range(self):
        """区间必须进 key——否则同渠道的多个 Increment 档会互相覆盖。"""
        cards = rate_cards(config())
        seller_keys = [key for key in cards if "/Seller/" in key]
        self.assertEqual(len(seller_keys), 1, "合成数据里只有一个 Seller 档")
        self.assertTrue(seller_keys[0].endswith("/0.0/500.0"), seller_keys[0])

    def test_multiple_increment_tiers_survive_flattening(self):
        """回归：实测真实配置里店到店有 6 档，曾被压成 3 档，导致变化检测漏报。"""
        many = config()
        fees = many["site_info"][0]["cargo_types"][0]["channels"][0]["zones"][0]["fee_modes"]
        fees.extend([
            {"name": "Increment", "type": "Seller", "effective_date": "2024-05-01",
             "start_weight": 500.01, "end_weight": 1000.0, "original_fee": None,
             "increment_unit": 500.0, "increment_amount": 30.0},
            {"name": "Increment", "type": "Seller", "effective_date": "2024-05-01",
             "start_weight": 1000.01, "end_weight": 2000.0, "original_fee": None,
             "increment_unit": 500.0, "increment_amount": 40.0},
        ])
        cards = rate_cards(many)
        seller_keys = [key for key in cards if "/Seller/" in key]
        self.assertEqual(len(seller_keys), 3, "三档必须是三张卡")

    def test_buyer_and_seller_are_kept_apart(self):
        """买家付的运费和卖家承担的藏价是两回事，混在一起会算错利润。"""
        cards = rate_cards(config())
        self.assertEqual(self.find(cards, "TW/Normal/蝦皮店到店/所有地区/Buyer/Flat")["fee"], 45.0)
        self.assertEqual(
            self.find(cards, "TW/Normal/蝦皮店到店/所有地区/Seller/WeightRange")["fee"], 25.0)


class DiffTest(unittest.TestCase):
    def test_identical_config_reports_nothing(self):
        self.assertEqual(diff_config(config(), config()), [])

    def test_fee_change_names_the_channel_and_both_values(self):
        changes = diff_config(config(fee=25.0), config(fee=30.0))
        self.assertEqual(len(changes), 1)
        self.assertIn("蝦皮店到店", changes[0])
        self.assertIn("fee 25.0→30.0", changes[0])

    def test_new_channel_is_reported_as_added(self):
        changes = diff_config(config(), config(extra_channel=True))
        joined = " ".join(changes)
        self.assertIn("新增", joined)
        self.assertIn("新渠道", joined)
        self.assertNotIn("修改", joined, "只新增不该报修改")

    def test_removed_channel_is_reported(self):
        changes = diff_config(config(extra_channel=True), config())
        self.assertTrue(any("删除" in item and "新渠道" in item for item in changes))

    def test_date_change_is_reported_first(self):
        changes = diff_config(config(date="2026-10-03"), config(date="2026-11-01"))
        self.assertTrue(changes[0].startswith("生效日期：2026-10-03 → 2026-11-01"))

    def test_limit_is_respected(self):
        changes = diff_config(config(), config(extra_channel=True), limit=1)
        self.assertLessEqual(len(changes), 2)


class SiteFeeDiffTest(unittest.TestCase):
    def test_commission_change_is_reported(self):
        changes = diff_site_fees([{"name": "TW", "platform_commission": "14.00",
                                   "handling_fee": "2.50"}],
                                 [{"name": "TW", "platform_commission": "15.00",
                                   "handling_fee": "2.50"}])
        self.assertEqual(len(changes), 1)
        self.assertIn("TW platform_commission：14.00 → 15.00", changes[0])

    def test_identical_fees_report_nothing(self):
        fees = [{"name": "TW", "platform_commission": "14.00", "handling_fee": "2.50"}]
        self.assertEqual(diff_site_fees(fees, list(fees)), [])

    def test_new_site_is_reported(self):
        changes = diff_site_fees([], [{"name": "BR", "platform_commission": "1",
                                       "handling_fee": "2"}])
        self.assertEqual(changes, ["新增站点：BR"])


class FetchTest(unittest.TestCase):
    def test_fetch_uses_injected_opener_and_parses_data(self):
        payload = json.dumps({"code": 200000, "data": config()}).encode("utf-8")
        result = fetch_site_config("2026-10-03", opener=lambda url: payload)
        self.assertEqual(result["date"], "2026-10-03")
        cards = rate_cards(result)
        hits = [key for key in cards if key.startswith(
            "TW/Normal/蝦皮店到店/所有地区/Seller/WeightRange")]
        self.assertTrue(hits)
        self.assertEqual(cards[hits[0]]["fee"], 25.0)

    def test_api_success_code_is_200000(self):
        """实测：成功时 code=200000，msg="ok"。按 0 判成功会一直误报失败。"""
        payload = json.dumps({"code": 200000, "msg": "ok", "data": config()}).encode("utf-8")
        self.assertEqual(fetch_site_config("2026-10-03", opener=lambda url: payload)["date"],
                         "2026-10-03")

    def test_lenient_about_zero_code(self):
        payload = json.dumps({"code": 0, "data": config()}).encode("utf-8")
        self.assertTrue(fetch_site_config("2026-10-03", opener=lambda url: payload))

    def test_fetch_raises_on_api_error_code(self):
        payload = json.dumps({"code": 500, "msg": "boom"}).encode("utf-8")
        with self.assertRaises(ValueError):
            fetch_site_config("2026-10-03", opener=lambda url: payload)

    def test_error_message_includes_code_for_diagnosis(self):
        payload = json.dumps({"code": 500, "msg": "boom"}).encode("utf-8")
        with self.assertRaises(ValueError) as caught:
            fetch_site_config("2026-10-03", opener=lambda url: payload)
        self.assertIn("500", str(caught.exception))

    def test_latest_reference_picks_the_newest_snapshot(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            self.assertIsNone(latest_reference(folder))
            for name in ("sls-site-config-2026-10-03.json", "sls-site-config-2026-11-01.json"):
                (Path(folder) / name).write_text("{}", encoding="utf-8")
            self.assertEqual(latest_reference(folder).name, "sls-site-config-2026-11-01.json")


if __name__ == "__main__":
    unittest.main()
