import os
import unittest
from datetime import datetime
from pathlib import Path

from ai_status import (
    StatusError,
    Target,
    UsageResult,
    format_number,
    format_result,
    parse_codex_reset_expiry,
    parse_codex_usage,
    parse_codex_available_resets,
    parse_copilot_usage,
    progress_bar,
    reset_expiry_text,
    reset_text,
    target_from_probe,
)


class AiStatusTests(unittest.TestCase):
    def test_resolves_codex_alias_and_home(self):
        target = target_from_probe(
            "codex1",
            "kind=alias\nalias codex1='CODEX_HOME=\"$HOME/.codex-account1\" codex'\n",
        )

        self.assertEqual(target.provider, "codex")
        self.assertEqual(target.codex_home, Path(os.environ["HOME"]) / ".codex-account1")

    def test_resolves_copilot_executable(self):
        target = target_from_probe("copilot", "kind=file\n/home/alex/.local/bin/copilot\n")

        self.assertEqual(target.provider, "copilot")

    def test_rejects_unknown_command(self):
        with self.assertRaisesRegex(StatusError, "cannot identify"):
            target_from_probe("other", "kind=file\n/home/alex/.local/bin/other\n")

    def test_parses_codex_windows_and_reset(self):
        data = {
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
            },
            "rate_limit_reset_credits": {"available_count": 2},
        }
        windows = parse_codex_usage(data)
        self.assertEqual([window.label for window in windows], ["5-hour limit", "Weekly limit"])
        self.assertEqual(windows[0].used, 63.5)
        self.assertEqual(parse_codex_available_resets(data), 2)
        data["rate_limit_reset_credits"] = {"available_count": 0}
        self.assertEqual(parse_codex_available_resets(data), 0)

    def test_parses_earliest_available_codex_reset_expiry(self):
        expiry = parse_codex_reset_expiry(
            {
                "credits": [
                    {"status": "available", "expires_at": "2026-09-20T12:00:00Z"},
                    {"status": "available", "expires_at": "2026-09-10T12:00:00Z"},
                    {"status": "redeemed", "expires_at": "2026-09-06T12:00:00Z"},
                ]
            }
        )

        self.assertEqual(expiry, "2026-09-10T12:00:00Z")

    def test_formats_available_codex_quota_resets(self):
        result = UsageResult(
            Target("codex1", "codex"),
            available_resets=2,
            next_reset_expires_at="2026-09-10T12:00:00Z",
        )

        formatted = format_result(result, datetime.now().astimezone())
        self.assertIn("Quota resets available: 2", formatted)
        self.assertIn("Next expiry: In", formatted)
        self.assertNotIn("2026-09-10", formatted)

    def test_highlights_codex_reset_expiring_within_seven_days(self):
        now = datetime.fromisoformat("2026-09-05T12:00:00+00:00")

        warning = reset_expiry_text("2026-09-10T12:00:00Z", now)
        safe = reset_expiry_text("2026-09-12T12:00:00Z", now)

        self.assertIn("WARNING: expires in less than 7 days", warning)
        self.assertNotIn("WARNING", safe)

    def test_parses_copilot_credits_and_unlimited_quota(self):
        windows = parse_copilot_usage(
            {
                "quota_reset_date": "2026-09-01",
                "quota_snapshots": {
                    "chat": {"unlimited": True},
                    "premium_interactions": {
                        "unlimited": False,
                        "percent_remaining": 27.8,
                        "credits_used": 18046,
                        "entitlement": 25000,
                    },
                },
            }
        )
        self.assertEqual(windows[0].label, "Premium interactions")
        self.assertEqual(windows[0].used, 18046)
        self.assertEqual(windows[0].limit, 25000)
        self.assertTrue(windows[1].unlimited)

    def test_formats_numbers_progress_and_countdown(self):
        self.assertEqual(format_number(18046), "18,046")
        self.assertEqual(format_number(63.5), "63.5")
        self.assertEqual(progress_bar(120), "[####################]")
        now = datetime.fromisoformat("2026-08-30T12:00:00+00:00")
        self.assertEqual(reset_text("2026-08-30T14:18:15Z", now), "In 00d 02h 18m 15s")


if __name__ == "__main__":
    unittest.main()
