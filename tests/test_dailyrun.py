"""每日任务"跑没跑"的自检。

背景（2026-10-08 实测）：任务计划显示当天 10:00:01 跑过、返回码 0，
但 `data/daily.log` 里一条记录都没有。任务计划自己的 LastRunTime /
LastTaskResult **不能作证**——它分不清"跑完没事"和"触发了但没启动"。

这组测试钉住：日志里没有记录要能被认出来；没日志（新克隆）不能误报；
正常跑过的一天不能瞎报警。
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopee_ledger.dailyrun import (  # noqa: E402
    DEFAULT_LOG,
    STALE_AFTER,
    describe,
    last_run,
)

LOG = """[2026-10-06 10:00:02] === begin ===
[2026-10-06 10:04:56]   告警：无
[2026-10-06 10:04:56] === end ===
[2026-10-07 10:00:02] === begin ===
[2026-10-07 10:04:56] === end ===
"""


class LastRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "daily.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_last_begin_wins_not_first(self):
        self.path.write_text(LOG, encoding="utf-8")
        self.assertEqual(last_run(self.path), datetime(2026, 10, 7, 10, 0, 2))

    def test_missing_file_is_none(self):
        self.assertIsNone(last_run(self.path))

    def test_log_without_begin_lines_is_none(self):
        """有日志文件但没有 begin（写了一半、或格式变了）→ 判不出来，不猜。"""
        self.path.write_text("[2026-10-07 10:00:02] --- alert ---\n", encoding="utf-8")
        self.assertIsNone(last_run(self.path))

    def test_malformed_timestamp_is_skipped(self):
        self.path.write_text("[2026-13-45 99:99:99] === begin ===\n" + LOG, encoding="utf-8")
        self.assertEqual(last_run(self.path), datetime(2026, 10, 7, 10, 0, 2))


class DescribeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "daily.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_log_at_all_is_not_an_alarm(self):
        """新克隆的仓库本来就没有日志（data/*.log 不进版本库）。"""
        note, alarm = describe(self.path)
        self.assertFalse(alarm, note)
        self.assertIn("还没有运行记录", note)

    def test_log_with_no_runs_alarms(self):
        """日志在、却一次 begin 都没有——这是"跑了但什么都没写"，必须报。"""
        self.path.write_text("随便什么\n", encoding="utf-8")
        _note, alarm = describe(self.path)
        self.assertTrue(alarm)

    def test_a_fresh_run_does_not_alarm(self):
        self.path.write_text(LOG, encoding="utf-8")
        note, alarm = describe(self.path, now=datetime(2026, 10, 7, 11, 0, 0))
        self.assertFalse(alarm, note)
        self.assertIn("2026-10-07 10:00", note)

    def test_one_missed_day_alarms(self):
        self.path.write_text(LOG, encoding="utf-8")
        # 上一次 10-07 10:00；到了 10-08 13:00 还没跑＝那天 10:00 的任务没起来
        # （26 小时闸门给 10:00-12:00 留了两个小时的抖动窗口）
        note, alarm = describe(self.path, now=datetime(2026, 10, 8, 13, 0, 0))
        self.assertTrue(alarm, note)
        self.assertIn("没起来", note)
        self.assertIn("不能当跑了没跑的证据", note)

    def test_just_under_the_threshold_does_not_alarm(self):
        """26 小时的闸门：23 小时前跑过是正常的（离下一次 10:00 还早）。"""
        self.path.write_text(LOG, encoding="utf-8")
        _note, alarm = describe(self.path, now=datetime(2026, 10, 7, 10, 0, 2) + STALE_AFTER
                                - timedelta(minutes=1))
        self.assertFalse(alarm)

    def test_clock_skew_into_the_future_is_not_an_alarm(self):
        """时间戳比"现在"还晚（时钟回拨）不该报成"负几天前跑过"。"""
        self.path.write_text(LOG, encoding="utf-8")
        note, alarm = describe(self.path, now=datetime(2026, 10, 7, 9, 0, 0))
        self.assertFalse(alarm, note)
        self.assertIn("0.0 小时前", note)


class TheRealLogTest(unittest.TestCase):
    def test_the_repo_log_parses(self):
        """真日志要能解析出来——否则这条自检在真机器上等于没装。"""
        self.assertTrue(DEFAULT_LOG.exists(), "本机应有每日任务日志：%s" % DEFAULT_LOG)
        moment = last_run(DEFAULT_LOG)
        self.assertIsNotNone(moment, "真日志里应能读到 === begin ===")
        self.assertGreater(moment.year, 2000)


if __name__ == "__main__":
    unittest.main()
