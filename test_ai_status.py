import unittest

from ai_status import CommandResult, decode_output, format_result, run_status


class AiStatusTests(unittest.TestCase):
    def test_runs_command_with_status(self):
        result = run_status("printf '%s'")

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "/status")

    def test_reports_failed_command(self):
        result = run_status("false")

        self.assertEqual(result.exit_code, 1)
        self.assertIsNone(result.error)
        self.assertIn("Exit code: 1", format_result(result))

    def test_formats_empty_output(self):
        result = CommandResult("example", exit_code=0)

        self.assertEqual(format_result(result), "[example]\n  No output.")

    def test_removes_terminal_control_codes(self):
        output = decode_output([b"\x1b[31mstatus\x1b[0m\r\n"])

        self.assertEqual(output, "status")


if __name__ == "__main__":
    unittest.main()
