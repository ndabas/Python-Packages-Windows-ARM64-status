"""Offline tests for match_tracking_issue.py and the top PyPI packages parser."""

import unittest

import match_tracking_issue as mti
from pypi import TopPackage, parse_top_packages

COLS = {"package": "Package", "status": "Current Status", "pr": "GitHub PR Link"}
MINE = "https://github.com/o/a/pull/1"
OTHER = "https://github.com/o/b/pull/2"


class TopPackagesTests(unittest.TestCase):
    def test_parse_ranks_by_downloads_and_normalizes_names(self):
        top = parse_top_packages(
            'download_count,project\n10,"Ruamel.Yaml"\n30,"boto3"\n20,"typing_extensions"\n'
        )
        self.assertEqual(top["boto3"], TopPackage("boto3", 1, 30))
        self.assertEqual(top["typing-extensions"].rank, 2)
        self.assertEqual(top["ruamel-yaml"], TopPackage("Ruamel.Yaml", 3, 10))


class MatchTests(unittest.TestCase):
    def setUp(self):
        sheet_rows = [
            {"Package": "Alpha", "Current Status": "PR Merged", "GitHub PR Link": MINE},
            {"Package": "beta", "Current Status": "PR Under Review", "GitHub PR Link": OTHER},
            {"Package": "gamma_x", "Current Status": "", "GitHub PR Link": f"{OTHER} {MINE}"},
        ]
        self.rows = mti.authored_rows(sheet_rows, COLS, {MINE.lower()})

    def test_keeps_rows_of_any_status_that_link_authored_prs(self):
        self.assertEqual(
            self.rows,
            [
                {"package": "Alpha", "status": "PR Merged", "prs": [MINE]},
                {"package": "gamma_x", "status": "", "prs": [MINE]},
            ],
        )

    def test_report_lists_issue_and_top_matches_with_sheet_status(self):
        issue = mti.issue_packages(
            "## Section A\n"
            "| Done | Name | Notes | PRs |\n"
            "| --- | --- | --- | --- |\n"
            f"| ✅ | **alpha** | | {MINE} |\n"
            "| ❌ | **[Gamma-X](https://example.com)** | | |\n"
        )
        top = {"gamma-x": TopPackage("gamma-x", 7, 1234567)}
        issue_matches = mti.match_issue(self.rows, issue)
        top_matches = mti.match_top(self.rows, top)
        report = mti.render_report(
            issue_matches, top_matches, "o/r", "1", len(self.rows), "me", "url", 15000
        )

        self.assertIn(
            f"| Alpha | PR Merged | [o/a#1]({MINE}) | yes | ✅ | Section A |", report
        )
        self.assertIn(f"| gamma_x | — | [o/a#1]({MINE}) | no | ❌ | Section A |", report)
        self.assertIn("1 of 2 are among the [top 15,000 PyPI packages](url)", report)
        self.assertIn(f"| 7 | gamma_x | 1,234,567 | — | [o/a#1]({MINE}) |", report)


if __name__ == "__main__":
    unittest.main()
