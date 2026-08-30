import os
import unittest
from datetime import datetime
from pathlib import Path

from ai_status import (
    StatusError,
    format_number,
    parse_codex_usage,
    parse_copilot_usage,
    progress_bar,
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
        self.assertEqual([window.label for window in windows], ["5-hour limit", "Weekly limit"])
        self.assertEqual(windows[0].used, 63.5)

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
