import unittest

from airpods.doctor import (
    Check,
    DoctorReport,
    _check_paired,
    format_report,
    parse_controller_mode,
    parse_rfkill,
)
from airpods.mock import MockBackend


class DoctorParsingTests(unittest.TestCase):
    def test_controller_mode(self):
        text = """
# comment
[General]
# ControllerMode = le
ControllerMode = bredr
[Policy]
ControllerMode = le
"""
        self.assertEqual(parse_controller_mode(text), "bredr")

    def test_controller_mode_default(self):
        self.assertIsNone(parse_controller_mode("[General]\nFastConnectable = true\n"))

    def test_controller_mode_ignores_comments(self):
        self.assertIsNone(parse_controller_mode("[General]\n# ControllerMode = le\n"))

    def test_rfkill(self):
        self.assertEqual(parse_rfkill("0: hci0: Bluetooth\n\tSoft blocked: yes\n"), (True, False))
        self.assertEqual(parse_rfkill("0: hci0: Bluetooth\n\tHard blocked: yes\n"), (False, True))
        self.assertEqual(parse_rfkill("0: hci0: Bluetooth\n\tSoft blocked: no\n"), (False, False))

    def test_paired_check_ignores_unpaired_devices(self):
        backend = MockBackend()
        backend.paired = False
        check = _check_paired(backend)
        self.assertEqual(check.status, "warn")
        self.assertIn("none are paired", check.detail)

    def test_paired_check_counts_paired(self):
        check = _check_paired(MockBackend())
        self.assertEqual(check.status, "ok")
        self.assertIn("1 paired", check.detail)

    def test_report_summary(self):
        report = DoctorReport(
            [
                Check("a", "ok", "fine"),
                Check("b", "warn", "meh", "hint"),
                Check("c", "fail", "broken", "fix it"),
            ]
        )
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(len(report.failures), 1)
        self.assertEqual(len(report.warnings), 1)
        text = format_report(report)
        self.assertIn("[FAIL] c — broken", text)
        self.assertIn("→ fix it", text)
        self.assertIn("1 failure(s), 1 warning(s)", text)

    def test_clean_report(self):
        report = DoctorReport([Check("a", "ok", "fine")])
        self.assertEqual(report.exit_code, 0)
        self.assertIn("Everything looks good", format_report(report))


if __name__ == "__main__":
    unittest.main()
