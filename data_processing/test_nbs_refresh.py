"""Published report periods and delayed within-month collection regressions."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import stats_gov_crawler as nbs
from update_sources import SourceTasks, due


class FrozenDateTime(datetime):
    at = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.at.astimezone(tz) if tz else cls.at.replace(tzinfo=None)


class DelayedReleaseTests(unittest.TestCase):
    def test_october_checks_again_and_collects_september_after_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            history = root / "output/stats_gov"
            history.mkdir(parents=True)
            old = history / "stats_gov_2026-08.json"
            old.write_text('[{"report_month":"2026-08"}]')
            earlier = "<a href='./202609/t20260915_1.html'>2026年8月份70个大中城市商品住宅销售价格变动情况</a>"
            released = "<a href='./202610/t20261019_2.html'>2026年9月份70个大中城市商品住宅销售价格变动情况</a>"

            def write_report(year, month, url, html, checked_at, overwrite=False):
                self.assertEqual((year, month), (2026, 9))
                self.assertIn("t20261019", url)
                (history / "stats_gov_2026-09.json").write_text(
                    '[{"report_month":"2026-09"}]'
                )
                return "written", 560

            with (
                patch.multiple(nbs, STATS_DIR=history, datetime=FrozenDateTime),
                patch.object(nbs.time, "sleep"),
            ):
                FrozenDateTime.at = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
                with (
                    patch.object(nbs, "http_get", return_value=earlier),
                    patch.object(nbs, "write_month") as writer,
                ):
                    first = SourceTasks(root, lambda *args: None).run("nbs", {})
                    writer.assert_not_called()
                self.assertEqual(first["data_period"], "2026-08")
                self.assertEqual(first["expected_period"], "2026-09")
                self.assertEqual(first["availability"], "awaiting_release")
                self.assertFalse(first["data_changed"])
                self.assertTrue(
                    due(
                        {"checked_at": FrozenDateTime.at.isoformat()},
                        12,
                        datetime(2026, 10, 2, 10, tzinfo=timezone.utc),
                    )
                )
                FrozenDateTime.at = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
                with (
                    patch.object(nbs, "http_get", return_value=released + earlier),
                    patch.object(
                        nbs, "write_month", side_effect=write_report
                    ) as writer,
                ):
                    later = SourceTasks(root, lambda *args: None).run("nbs", {})
                    writer.assert_called_once()
                self.assertEqual(later["data_period"], "2026-09")
                self.assertEqual(later["availability"], "available")
                self.assertEqual(later["added_periods"], ["2026-09"])
                self.assertTrue(later["data_changed"])
                self.assertEqual(
                    json.loads(old.read_text())[0]["report_month"], "2026-08"
                )

    def test_failed_new_report_is_retried_on_the_next_check(self):
        with tempfile.TemporaryDirectory() as folder:
            history = Path(folder)
            (history / "stats_gov_2026-08.json").write_text("[]")
            articles = {
                (2026, 9): "https://www.stats.gov.cn/sj/zxfb/202610/t20261019_2.html"
            }
            with (
                patch.object(nbs, "STATS_DIR", history),
                patch.object(nbs, "discover_articles", return_value=articles),
                patch.object(nbs, "http_get", return_value="incomplete"),
                patch.object(nbs, "write_month", return_value=("failed", 35)) as writer,
                patch.object(nbs.time, "sleep"),
            ):
                for _ in range(2):
                    report = nbs.quick_update(details=True)
                    self.assertEqual(report["result"], "partial")
                    self.assertEqual(report["data_period"], "2026-08")
                    self.assertEqual(report["published_period"], "2026-09")
                    self.assertEqual(report["availability"], "pending_collection")
                self.assertEqual(writer.call_count, 2)
                self.assertFalse((history / "stats_gov_2026-09.json").exists())

    def test_empty_directory_cannot_claim_waiting_or_current(self):
        with patch.object(nbs, "discover_articles", return_value={}):
            with self.assertRaises(RuntimeError):
                nbs.quick_update(details=True)

    def test_article_title_cannot_relabel_august_as_september(self):
        with self.assertRaisesRegex(ValueError, "report month differs"):
            nbs.build_month_records(
                2026,
                9,
                "https://www.stats.gov.cn/202610/t20261019_2.html",
                "<title>2026年8月份70个大中城市商品住宅销售价格变动情况</title>",
                "2026-10-19",
            )

    def test_expected_month_uses_beijing_calendar_and_year_rollover(self):
        rollover = nbs.publication_state(
            {(2026, 11): "url"},
            {(2026, 11)},
            at=datetime(2026, 12, 31, 17, tzinfo=timezone.utc),
        )
        self.assertEqual(rollover["expected_period"], "2026-12")
        self.assertEqual(rollover["data_period"], "2026-11")
        self.assertEqual(rollover["availability"], "awaiting_release")
        self.assertEqual(nbs.publication_state({}, set())["availability"], "unknown")


if __name__ == "__main__":
    unittest.main()
