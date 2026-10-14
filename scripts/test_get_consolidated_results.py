"""Offline tests for downloading and reordering the consolidated TSV."""

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import get_consolidated_results as gcr


def artifact(name: str, run_id: int, created: str, branch: str = "master", expired: bool = False) -> dict:
    return {
        "name": name,
        "created_at": created,
        "expired": expired,
        "workflow_run": {"id": run_id, "head_branch": branch},
    }


class LatestFullRunTests(unittest.TestCase):
    def test_quick_expired_and_other_branch_runs_are_skipped(self) -> None:
        artifacts = [
            # Quick run: no per-job results.
            artifact("consolidated-results", 5, "2026-10-14T05:00:00Z"),
            artifact("package-lists", 5, "2026-10-14T04:00:00Z"),
            artifact("consolidated-results", 4, "2026-10-14T03:00:00Z", expired=True),
            artifact("results-py3.14", 4, "2026-10-14T02:00:00Z"),
            artifact("consolidated-results", 3, "2026-10-13T03:00:00Z", branch="pr"),
            artifact("results-py3.14", 3, "2026-10-13T02:00:00Z", branch="pr"),
            artifact("consolidated-results", 2, "2026-10-12T03:00:00Z"),
            artifact("results-py3.13-buildtools", 2, "2026-10-12T02:00:00Z"),
            artifact("consolidated-results", 1, "2026-10-11T03:00:00Z"),
            artifact("results-py3.13", 1, "2026-10-11T02:00:00Z"),
        ]
        self.assertEqual(gcr.latest_full_run(artifacts), 3)
        self.assertEqual(gcr.latest_full_run(artifacts, "master"), 2)
        self.assertIsNone(gcr.latest_full_run(artifacts, "missing"))


class ReorderTests(unittest.TestCase):
    def test_rows_follow_the_sheet(self) -> None:
        rows = [
            ["Package Name", "Potential Blockers", "status"],
            ["Core_Pkg", "", "success"],
            ["", "", ""],
            ["# Section", "", ""],
            ["client", "Core_Pkg", "failure"],
            ["gone", "", "success"],
            ["short"],
        ]
        sheet = ["client", "# Section", "core.pkg", "", "Core_Pkg", "new-one", "short", "client"]
        output, missing, dropped = gcr.reorder(rows, sheet)
        self.assertEqual(
            output,
            [
                ["Package Name", "Potential Blockers", "status"],
                ["client", "Core_Pkg", "failure"],
                ["# Section", "", ""],
                ["core.pkg", "", "success"],
                ["", "", ""],
                ["Core_Pkg", "", "success"],
                ["new-one", "", ""],
                ["short", "", ""],
                ["client", "Core_Pkg", "failure"],
            ],
        )
        self.assertEqual(missing, ["new-one"])
        self.assertEqual(dropped, ["gone"])

    def test_file_is_renamed_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "win_arm64-status-20261014.tsv")
            with open(path, "w", newline="", encoding="utf-8") as fh:
                fh.write('Package\tPotential Blockers\nb\t"has\ttab"\na\t\n')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                gcr.reorder_file(path, ["b", "a"])
            self.assertIn("already matches", out.getvalue())
            with open(path, newline="", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), 'Package Name\tPotential Blockers\nb\t"has\ttab"\na\t\n')

    def test_unexpected_first_column_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "status.tsv")
            with open(path, "w", newline="", encoding="utf-8") as fh:
                fh.write("Potential Blockers\tPackage Name\n\tpkg\n")
            with self.assertRaises(SystemExit) as raised:
                gcr.reorder_file(path, ["pkg"])
        self.assertIn("does not start with", str(raised.exception.code))


class MainTests(unittest.TestCase):
    def run_main(self, directory: str, *args: str) -> str:
        artifacts = {
            "artifacts": [
                artifact("consolidated-results", 7, "2026-10-14T03:00:00Z"),
                artifact("results-py3.14", 7, "2026-10-14T02:00:00Z"),
            ]
        }

        def fake_run(command: list[str], check: bool = True):
            target = command[command.index("--dir") + 1]
            with open(os.path.join(target, "win_arm64-status-20261014.tsv"), "w", encoding="utf-8") as fh:
                fh.write("Package Name\tPotential Blockers\na\t\nb\ta\n")
            return subprocess.CompletedProcess(command, 0, "", "")

        sheet = ([{"Package": "b"}, {"Package": "a"}], {"package": "Package"})
        with patch("sys.argv", ["gcr", "--output-dir", directory, *args]), patch.object(
            gcr, "require_gh"
        ), patch.object(gcr, "gh", return_value=artifacts), patch.object(
            gcr, "run", side_effect=fake_run
        ), patch.object(gcr, "load_sheet", return_value=sheet), contextlib.redirect_stdout(
            io.StringIO()
        ) as out:
            gcr.main()
        return out.getvalue()

    def test_downloads_reorders_and_removes_older(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for name in ("win_arm64-status-20261001.tsv", "win_arm64-status-20261014.tsv",
                         "win_arm64-status-20261231.tsv", "other.tsv"):
                with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
                    fh.write("old")
            out = self.run_main(directory, "--remove-older")
            self.assertIn("run 7", out)
            self.assertEqual(
                sorted(os.listdir(directory)),
                ["other.tsv", "win_arm64-status-20261014.tsv", "win_arm64-status-20261231.tsv"],
            )
            with open(os.path.join(directory, "win_arm64-status-20261014.tsv"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "Package Name\tPotential Blockers\nb\ta\na\t\n")

    def test_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = os.path.join(directory, "win_arm64-status-20261001.tsv")
            with open(old, "w", encoding="utf-8") as fh:
                fh.write("old")
            out = self.run_main(directory, "--remove-older", "--dry-run")
            self.assertIn("Would write", out)
            self.assertIn("Would remove", out)
            self.assertEqual(os.listdir(directory), ["win_arm64-status-20261001.tsv"])


if __name__ == "__main__":
    unittest.main()
