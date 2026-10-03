"""发货时效规则的测试。

这个文件的重点是**把"从哪里归纳出来的"钉住**：
规则不是读官方措辞读出来的，而是从接口样本归纳、再样本外验证 630/630 得到的。
照着措辞写会错两处（我实际错了），所以下面每条都对应一个真实踩过的坑。
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.delivery import (  # noqa: E402
    BUSINESS_DAY_MARKETS,
    add_scan_days,
    add_shipping_days,
    deadlines,
    holidays_between,
    is_shipping_day,
    load_holidays,
    verify_samples,
)

MID_AUTUMN = date(2026, 9, 25)       # 中秋
NATIONAL_DAY = date(2026, 10, 1)     # 国庆
SATURDAY = date(2026, 10, 17)
SUNDAY = date(2026, 10, 18)


class ShippingDayTest(unittest.TestCase):
    def setUp(self):
        self.holidays = load_holidays()
        self.assertTrue(self.holidays, "豁免日期表应当在 spec/reference 里")

    def test_saturday_is_a_shipping_day(self):
        """**周六算工作日**——这条是从样本反推出来的，与直觉相反。

        TW 10-16（周五）下单备货 1 天，DTS 落在 10-17 周六而不是顺延到周一。
        照着「工作日 = 周一至周五」写会错。
        """
        self.assertTrue(is_shipping_day(SATURDAY, self.holidays))

    def test_sunday_is_not(self):
        self.assertFalse(is_shipping_day(SUNDAY, self.holidays))

    def test_holidays_are_not(self):
        self.assertFalse(is_shipping_day(NATIONAL_DAY, self.holidays))
        self.assertFalse(is_shipping_day(MID_AUTUMN, self.holidays))

    def test_ordinary_weekday_is(self):
        self.assertTrue(is_shipping_day(date(2026, 10, 14), self.holidays))

    def test_without_a_holiday_table_only_sunday_is_excluded(self):
        """没有豁免表时不假装知道节假日，只排除周日。"""
        self.assertTrue(is_shipping_day(NATIONAL_DAY, {}))
        self.assertFalse(is_shipping_day(SUNDAY, {}))


class AddShippingDaysTest(unittest.TestCase):
    def setUp(self):
        self.holidays = load_holidays()

    def test_saturday_order_rolls_over_sunday(self):
        result = add_shipping_days(SATURDAY, 1, self.holidays)
        self.assertEqual(result, date(2026, 10, 19), "周六下单次日是周日 → 顺延到周一")

    def test_friday_order_lands_on_saturday(self):
        result = add_shipping_days(date(2026, 10, 16), 1, self.holidays)
        self.assertEqual(result, date(2026, 10, 17), "周五下单次日是周六，算发货日，不顺延")

    def test_holiday_is_skipped(self):
        # 09-24 周四 + 1：09-25 中秋 → 顺延到 09-26 周六（周六算工作日）
        result = add_shipping_days(date(2026, 9, 24), 1, self.holidays)
        self.assertEqual(result, date(2026, 9, 26))


class ScanDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.holidays = load_holidays()

    def test_natural_day_markets_add_three_calendar_days(self):
        """自然日站点：纯加 3 天，**不因节假日顺延**。

        实测 TW 09-29 下单 → 扫描截止 10-02，中间夹着国庆 10-01 也照样是 10-02。
        我第一版给这里加了节假日顺延，样本立刻报错。
        """
        self.assertEqual(add_scan_days(date(2026, 9, 29), "TW", self.holidays),
                         date(2026, 10, 2))
        self.assertEqual(add_scan_days(date(2026, 9, 29), "MX", self.holidays),
                         date(2026, 10, 2))

    def test_business_day_markets_skip_sunday_and_holidays(self):
        # TH 09-23 周三 + 3 发货日：09-24、09-25 中秋顺延、09-26 周六（算）→ 间隔不足，继续
        result = add_scan_days(date(2026, 9, 23), "TH", self.holidays)
        self.assertNotEqual(result, date(2026, 9, 26))
        self.assertEqual(result, date(2026, 9, 28))

    def test_business_day_markets_are_exactly_three(self):
        self.assertEqual(BUSINESS_DAY_MARKETS, frozenset({"TH", "BR", "AR"}))

    def test_the_two_rules_diverge_across_a_weekend(self):
        """两类规则只在窗口跨周末时才分开——这就是为什么只看措辞验不出来。"""
        dts_day = date(2026, 10, 16)   # 周五
        self.assertEqual(add_scan_days(dts_day, "TW", self.holidays),
                         date(2026, 10, 19))   # 自然日：+3 → 周一
        self.assertEqual(add_scan_days(dts_day, "TH", self.holidays),
                         date(2026, 10, 20))   # 发货日：周五+3 → 周二


class OfficialExampleTest(unittest.TestCase):
    """复现 spec 里原有的两个官方示例——它们来自**另一条独立来源**，所以是互证。"""

    def test_dts_one(self):
        result = deadlines(date(2026, 9, 23), market="TW", dts_days=1)
        self.assertEqual(result.dts_day, date(2026, 9, 24))
        self.assertEqual(result.scan_day, date(2026, 9, 27))

    def test_dts_two(self):
        result = deadlines(date(2026, 9, 23), market="TW", dts_days=2)
        self.assertEqual(result.dts_day, date(2026, 9, 26))
        self.assertEqual(result.scan_day, date(2026, 9, 29))

    def test_all_markets_agree_on_the_same_input(self):
        """官方页面写着「各站点发货时效一致」——实测 9 个站点的 DTS 完全相同。"""
        days = {deadlines(date(2026, 10, 15), market=m, dts_days=1).dts_day
                for m in ("TW", "MY", "PH", "SG", "TH", "VN", "BR", "MX", "AR")}
        self.assertEqual(len(days), 1)


class RenderTest(unittest.TestCase):
    def test_render_says_which_rule_applied(self):
        text = deadlines(date(2026, 10, 15), market="TH", dts_days=1).render()
        self.assertIn("DTS + 3 发货日", text)
        text = deadlines(date(2026, 10, 15), market="TW", dts_days=1).render()
        self.assertIn("DTS + 3 自然日", text)

    def test_render_names_the_holidays_it_shifted_over(self):
        text = deadlines(date(2026, 10, 3), market="TW", dts_days=1).render()
        self.assertIn("国庆", text)

    def test_missing_table_is_disclosed_not_hidden(self):
        result = deadlines(date(2026, 10, 3), market="TW", dts_days=1, holidays={})
        self.assertIn("节假日顺延可能不准", result.note)


class HolidaysBetweenTest(unittest.TestCase):
    def test_names_are_deduplicated(self):
        names = holidays_between(date(2026, 9, 20), date(2026, 10, 10), load_holidays())
        self.assertIn("2026 中秋节", names)
        self.assertIn("2026 国庆", names)
        self.assertEqual(len(names), len(set(names)))


class SampleVerificationTest(unittest.TestCase):
    def test_rule_matches_the_saved_official_samples(self):
        """规则被改错时这里会立刻报出来——这是把"归纳过程"固化下来的那一步。"""
        report = verify_samples()
        self.assertTrue(report["available"], "spec/reference 里应当有口径样本")
        self.assertGreater(report["checked"], 50)
        self.assertEqual(report["mismatched"], [],
                         "规则与官方样本不符：%s" % report["mismatched"][:3])


if __name__ == "__main__":
    unittest.main()
