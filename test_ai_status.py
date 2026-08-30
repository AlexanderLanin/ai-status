import unittest
from datetime import datetime

from ai_status import (
    UsageError,
    german_number,
    parse_codex_usage,
    parse_copilot_usage,
    progress_bar,
    reset_text,
)


class AiStatusTests(unittest.TestCase):
    def test_parses_codex_windows_and_reset(self):
        windows = parse_codex_usage(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 63.5,
                        "limit_window_seconds": 18_000,
                        "reset_at": 1_800_000_000,
                    },
                    "secondary_window": {
                        "used_percent": 25,
                        "limit_window_seconds": 604_800,
                        "reset_at": 1_800_100_000,
                    },
                }
            }
        )
        self.assertEqual([window.label for window in windows], ["5-Stunden-Limit", "Wochenlimit"])
        self.assertEqual(windows[0].used, 63.5)
        self.assertEqual(windows[0].used_percent, 63.5)
        self.assertEqual(windows[0].reset_at, "2027-01-15T08:00:00Z")

    def test_parses_copilot_credits_and_unlimited_quota(self):
        windows = parse_copilot_usage(
            {
                "quota_reset_date": "2026-09-01",
                "quota_snapshots": {
                    "chat": {
                        "unlimited": True,
                        "percent_remaining": 100,
                        "credits_used": 0,
                        "entitlement": 0,
                        "quota_reset_at": 0,
                    },
                    "premium_interactions": {
                        "unlimited": False,
                        "percent_remaining": 27.8,
                        "credits_used": 18046,
                        "entitlement": 25000,
                        "quota_reset_at": 0,
                    },
                },
            }
        )
        self.assertEqual(windows[0].id, "premium_interactions")
        self.assertEqual(windows[0].used, 18046)
        self.assertEqual(windows[0].limit, 25000)
        self.assertEqual(windows[0].used_percent, 72.2)
        self.assertTrue(windows[1].unlimited)

    def test_rejects_incomplete_responses(self):
        with self.assertRaisesRegex(UsageError, "keine Rate-Limits"):
            parse_codex_usage({})
        with self.assertRaisesRegex(UsageError, "keine Quoten"):
            parse_copilot_usage({})

    def test_formats_german_numbers_and_progress(self):
        self.assertEqual(german_number(18046), "18.046")
        self.assertEqual(german_number(63.5), "63,5")
        self.assertEqual(progress_bar(120), "[####################]")

    def test_formats_countdown(self):
        now = datetime.fromisoformat("2026-08-30T12:00:00+00:00")
        self.assertEqual(
            reset_text("2026-08-30T14:18:15Z", now),
            "Noch 2 Std. · 18 Min.",
        )


if __name__ == "__main__":
    unittest.main()
