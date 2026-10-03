"""按重量算运费的契约测试。

算法是从 Shopee 定价模拟器自己的前端包里读出来的（不是猜的），所以这些用例
是**照着原码语义写的**：严格大于 start_weight、Flat 优先、Increment 向上取整。

档位与真实台湾店到店一致：
  Seller  WeightRange 0–500g   25
  Seller  Increment  500.01–1000   +30 / 500g
  Seller  Increment  1000.01–2000  +40 / 500g
  Seller  Increment  2000.01–2499.99 +50 / 500g
  Seller  Increment  2500+        +60 / 500g
  Buyer   Flat                   45
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.freight import (  # noqa: E402
    buyer_fee,
    channels_of,
    load_latest_config,
    rate_cards,
    seller_fee,
    tier_fee,
)


def tiers(*rows):
    out = []
    for payer, mode, start, end, fee, unit, amount in rows:
        out.append({"payer": payer, "mode": mode, "start_weight_g": start,
                    "end_weight_g": end, "fee": fee,
                    "increment_unit_g": unit, "increment_amount": amount})
    return out


REAL = tiers(
    ("Seller", "WeightRange", 0.0, 500.0, 25.0, None, None),
    ("Seller", "Increment", 500.01, 1000.0, None, 500.0, 30.0),
    ("Seller", "Increment", 1000.01, 2000.0, None, 500.0, 40.0),
    ("Seller", "Increment", 2000.01, 2499.99, None, 500.0, 50.0),
    ("Seller", "Increment", 2500.0, None, None, 500.0, 60.0),
    ("Buyer", "Flat", None, None, 45.0, None, None),
)


class TierFeeTest(unittest.TestCase):
    def test_first_tier_only(self):
        """400g：只命中首档 25。"""
        self.assertEqual(tier_fee(REAL, 400.0, "Seller"), 25.0)

    def test_exactly_500g_still_only_first_tier(self):
        """500g：500 > 0 命中首档；500 > 500.01 不成立。与模拟器原码一致。"""
        self.assertEqual(tier_fee(REAL, 500.0, "Seller"), 25.0)

    def test_501g_pays_one_increment(self):
        self.assertEqual(tier_fee(REAL, 501.0, "Seller"), 55.0)

    def test_1200g_hand_checked(self):
        """1200g = 25 + 30 + 40（第三档按已开始的 500g 收满）。"""
        self.assertEqual(tier_fee(REAL, 1200.0, "Seller"), 95.0)

    def test_2000g_charges_two_units_in_the_middle_band(self):
        """2000g = 25 + 30 + ceil(999.99/500)=2 × 40 = 135。"""
        self.assertEqual(tier_fee(REAL, 2000.0, "Seller"), 135.0)

    def test_2500g_does_not_hit_the_last_tier(self):
        """2500g：2500 > 2500 不成立，所以末档不计（原码是严格大于）。"""
        self.assertEqual(tier_fee(REAL, 2500.0, "Seller"), 185.0)

    def test_2501g_hits_the_last_tier(self):
        self.assertEqual(tier_fee(REAL, 2501.0, "Seller"), 245.0)

    def test_buyer_flat_fee_ignores_weight(self):
        for weight in (1.0, 400.0, 5000.0):
            self.assertEqual(tier_fee(REAL, weight, "Buyer"), 45.0)

    def test_flat_wins_over_accumulation(self):
        """同一付款方存在 Flat 档时直接取它，不做累加。"""
        mixed = tiers(("Seller", "Flat", None, None, 60.0, None, None),
                      ("Seller", "WeightRange", 0.0, 500.0, 25.0, None, None),
                      ("Seller", "Increment", 500.01, 1000.0, None, 500.0, 30.0))
        self.assertEqual(tier_fee(mixed, 1200.0, "Seller"), 60.0)

    def test_unknown_payer_is_none_not_zero(self):
        """查不到必须是 None——把"查不到"当 0 是这个项目最忌的错误。"""
        self.assertIsNone(tier_fee(REAL, 500.0, "Nobody"))

    def test_non_positive_weight_is_none(self):
        for weight in (0.0, -1.0, None):
            self.assertIsNone(tier_fee(REAL, weight, "Seller"))

    def test_increment_rounds_up(self):
        """Increment 用向上取整：1g 也算一个单位。"""
        one = tiers(("Seller", "WeightRange", 0.0, 500.0, 25.0, None, None),
                    ("Seller", "Increment", 500.01, 1000.0, None, 500.0, 30.0))
        self.assertEqual(tier_fee(one, 501.0, "Seller"), 55.0)
        self.assertEqual(tier_fee(one, 1000.0, "Seller"), 55.0)


class RealConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_latest_config()
        if not cls.config:
            raise unittest.SkipTest("仓库里还没有运费表快照")

    def test_exposes_tw_channels_with_payer_and_mode(self):
        cards = rate_cards(self.config)
        hits = [key for key in cards if key.startswith(
            "TW/Normal/蝦皮海外 - 蝦皮店到店/所有地区/Seller/WeightRange")]
        self.assertTrue(hits, "找不到店到店的卖家首档")
        card = cards[hits[0]]
        self.assertEqual(card["payer"], "Seller")
        self.assertEqual(card["mode"], "WeightRange")

    def test_real_store_to_store_has_all_six_tiers(self):
        """回归：真实数据里店到店普货有 6 档，曾被拉平压成 3 档。"""
        cards = rate_cards(self.config)
        keys = [key for key in cards
                if key.startswith("TW/Normal/蝦皮海外 - 蝦皮店到店/所有地区/")]
        self.assertEqual(len(keys), 6, "1 个卖家首档 + 4 个卖家增量档 + 1 个买家固定 = 6")

    def test_tw_store_to_store_seller_fee_by_weight(self):
        """真实数据上的手算值：与上面同构。"""
        self.assertEqual(seller_fee(self.config, "TW", "蝦皮店到店", 400.0), 25.0)
        self.assertEqual(seller_fee(self.config, "TW", "蝦皮店到店", 1200.0), 95.0)
        self.assertEqual(seller_fee(self.config, "TW", "蝦皮店到店", 2000.0), 135.0)

    def test_tw_store_to_store_buyer_fee(self):
        self.assertEqual(buyer_fee(self.config, "TW", "蝦皮店到店"), 45.0)

    def test_all_nine_sites_have_freight_data(self):
        cards = rate_cards(self.config)
        sites = {key.split("/")[0] for key in cards}
        self.assertEqual(len(sites), 9)
        for site in ("TW", "MY", "TH", "SG", "PH", "VN", "BR", "MX", "AR"):
            self.assertIn(site, sites)

    def test_unknown_channel_is_none(self):
        self.assertIsNone(seller_fee(self.config, "TW", "不存在的渠道", 500.0))

    def test_channels_of_filters_by_site(self):
        grouped = channels_of(self.config, "TW")
        self.assertTrue(grouped)
        self.assertTrue(all(name for name in grouped))

    def test_special_cargo_costs_more_than_normal(self):
        """特货应贵于普货——这是业务常识，若数据反了说明取错了档。"""
        normal = seller_fee(self.config, "TW", "蝦皮店到店", 1200.0, cargo="Normal")
        special = seller_fee(self.config, "TW", "蝦皮店到店", 1200.0, cargo="Special")
        self.assertIsNotNone(normal)
        self.assertIsNotNone(special)
        self.assertGreater(special, normal)


if __name__ == "__main__":
    unittest.main()
