"""Offline tests for combining install results with the sheet-aligned blocker TSV."""

import contextlib
import csv
import io
import os
import tempfile
import unittest
from unittest.mock import patch

import consolidate_results


class ConsolidateTests(unittest.TestCase):
    def test_results_are_aligned_with_every_sheet_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            results = os.path.join(directory, "results")
            os.makedirs(results)
            with open(os.path.join(results, "results-py3.14.csv"), "w", encoding="utf-8-sig") as fh:
                fh.write(
                    '"package","py3.14_install_status","py3.14_binary_url"\n'
                    '"Core_Pkg","success","https://example.org/core.whl"\n'
                    '"not-in-sheet","failure",""\n'
                )
            with open(
                os.path.join(results, "results-py3.14_buildtools.csv"), "w", encoding="utf-8"
            ) as fh:
                fh.write(
                    "package,py3.14_buildtools_install_status,py3.14_buildtools_binary_url\n"
                    "client,failure,\n"
                )
            blockers = os.path.join(directory, "package-blockers.tsv")
            with open(blockers, "w", encoding="utf-8", newline="") as fh:
                fh.write(
                    "Package\tPotential Blockers\n"
                    "Core_Pkg\t\n\t\ncore.pkg\t\n# Section\t\nclient\tCore_Pkg\nuntested\t\n"
                )
            output = os.path.join(directory, "combined.tsv")
            with patch(
                "sys.argv",
                ["consolidate", "--input", results, "--blockers", blockers, "--output", output],
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ) as warnings:
                consolidate_results.main()
            self.assertIn("not-in-sheet", warnings.getvalue())
            with open(output, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh, delimiter="\t"))

        url = "https://example.org/core.whl"
        self.assertEqual(
            rows,
            [
                [
                    "Package", "Potential Blockers",
                    "py3.14_install_status", "py3.14_binary_url",
                    "py3.14_buildtools_install_status", "py3.14_buildtools_binary_url",
                ],
                ["Core_Pkg", "", "success", url, "", ""],
                ["", "", "", "", "", ""],
                ["core.pkg", "", "success", url, "", ""],
                ["# Section", "", "", "", "", ""],
                ["client", "Core_Pkg", "", "", "failure", ""],
                ["untested", "", "", "", "", ""],
            ],
        )

    def test_missing_results_are_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "sys.argv", ["consolidate", "--input", directory]
        ):
            with self.assertRaises(SystemExit) as raised:
                consolidate_results.main()
        self.assertIn("No CSV files found", str(raised.exception.code))


if __name__ == "__main__":
    unittest.main()
