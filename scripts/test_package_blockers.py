"""Offline tests for dependency collection, ranking and the shared PyPI/sheet helpers."""

import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import common
import fetch_package_status
import pypi
import report_package_blockers as report
import sheet


def metadata(requirements: list[str] | None = None, wheels: tuple[str, ...] = ()) -> dict:
    return {
        "info": {"version": "1.2.3", "requires_dist": requirements},
        "urls": [{"filename": name, "packagetype": "bdist_wheel"} for name in wheels],
    }


class CollectionTests(unittest.TestCase):
    def test_all_requirements_are_preserved_and_names_normalized(self) -> None:
        requirements = [
            "Some_Package>=1",
            "some.package[feature]; extra == 'test'",
            "linux-only; sys_platform == 'linux'",
            "direct @ https://example.org/direct.whl",
        ]
        with patch.object(pypi, "fetch_metadata", return_value=metadata(requirements)):
            result = report.collect_package("parent")
        self.assertEqual(result.requirements, requirements)
        self.assertEqual(result.dependencies, ["direct", "linux-only", "some-package"])
        self.assertFalse(result.has_wheel)
        self.assertEqual(result.issues, [])

    def test_missing_and_empty_metadata_are_distinct(self) -> None:
        for requirements in (None, []):
            with self.subTest(requirements=requirements), patch.object(
                pypi, "fetch_metadata", return_value=metadata(requirements)
            ):
                result = report.collect_package("parent")
                self.assertEqual(result.requirements, requirements)
                self.assertEqual(result.dependencies, [])
                self.assertEqual(bool(result.issues), requirements is None)
                self.assertFalse(result.has_wheel)

    def test_invalid_requirements_keep_valid_edges(self) -> None:
        with patch.object(
            pypi, "fetch_metadata",
            return_value=metadata(["valid>=1", "not a requirement"]),
        ):
            result = report.collect_package("parent")
        self.assertEqual(result.dependencies, ["valid"])
        self.assertIn("Invalid requirement", result.issues[0])

    def test_malformed_requires_dist_is_reported(self) -> None:
        for requirements in ("dependency", [7]):
            data = metadata()
            data["info"]["requires_dist"] = requirements
            with self.subTest(requirements=requirements), patch.object(
                pypi, "fetch_metadata", return_value=data
            ):
                result = report.collect_package("parent")
                self.assertIsNone(result.requirements)
                self.assertIn("Invalid requires_dist", result.issues[0])

    def test_lookup_errors_leave_availability_unknown(self) -> None:
        errors = [
            urllib.error.HTTPError("https://pypi.org", 404, "Not found", {}, None),
            urllib.error.URLError("offline"),
            TimeoutError("timeout"),
            json.JSONDecodeError("invalid", "", 0),
        ]
        for error in errors:
            with self.subTest(error=error), patch.object(
                pypi, "fetch_metadata", side_effect=error
            ):
                result = report.collect_package("parent")
                self.assertIsNone(result.has_wheel)
                self.assertIsNone(result.requirements)
                self.assertTrue(result.issues)
                self.assertEqual(report.rank_blockers([result]), [])

    def test_invalid_sheet_name_is_not_queried(self) -> None:
        with patch.object(pypi, "fetch_metadata") as fetch:
            result = report.collect_package("Native library")
        fetch.assert_not_called()
        self.assertIsNone(result.has_wheel)
        self.assertEqual(result.issues, ["Invalid PyPI package name"])

    def test_wheel_availability(self) -> None:
        cases = [
            ((), False),
            (("pkg-1-py3-none-any.whl",), True),
            (("pkg-1-cp314-cp314-win_arm64.whl",), True),
            (("pkg-1-cp314-cp314-win_amd64.whl",), False),
            (("pkg-1-cp314-cp314-manylinux_aarch64.whl",), False),
        ]
        for wheels, available in cases:
            with self.subTest(wheels=wheels), patch.object(
                pypi, "fetch_metadata", return_value=metadata([], wheels)
            ):
                self.assertEqual(report.collect_package("pkg").has_wheel, available)


class RankingTests(unittest.TestCase):
    def test_transitive_diamond_counts_unique_packages_through_available_nodes(self) -> None:
        packages = [
            report.PackageInfo("Core_Pkg", has_wheel=False),
            report.PackageInfo("left", has_wheel=True, dependencies=["core-pkg"]),
            report.PackageInfo("right", has_wheel=True, dependencies=["core-pkg"]),
            report.PackageInfo("top", has_wheel=True, dependencies=["left", "right", "core-pkg"]),
            report.PackageInfo("unused", has_wheel=False),
            report.PackageInfo("unknown", has_wheel=None),
        ]
        blockers = report.rank_blockers(packages)
        self.assertEqual([b.package for b in blockers], ["Core_Pkg", "unused"])
        self.assertEqual(blockers[0].direct_dependents, ["left", "right", "top"])
        self.assertEqual(blockers[0].downstream_packages, ["left", "right", "top"])
        self.assertEqual(blockers[1].downstream_packages, [])

    def test_cycles_and_self_dependencies_do_not_count_blocker_itself(self) -> None:
        packages = [
            report.PackageInfo("a", has_wheel=False, dependencies=["a", "b"]),
            report.PackageInfo("b", has_wheel=False, dependencies=["a"]),
            report.PackageInfo("c", has_wheel=True, dependencies=["b"]),
        ]
        blockers = report.rank_blockers(packages)
        self.assertEqual([b.package for b in blockers], ["b", "a"])
        self.assertEqual(blockers[0].direct_dependents, ["a", "c"])
        self.assertEqual(blockers[0].downstream_packages, ["a", "c"])
        self.assertEqual(blockers[1].direct_dependents, ["b"])
        self.assertEqual(blockers[1].downstream_packages, ["b", "c"])

    def test_sort_uses_total_then_direct_then_name(self) -> None:
        packages = [
            report.PackageInfo("z", has_wheel=False),
            report.PackageInfo("a", has_wheel=False),
            report.PackageInfo("b", has_wheel=False),
            report.PackageInfo("middle", has_wheel=True, dependencies=["a"]),
            report.PackageInfo("top", has_wheel=True, dependencies=["middle"]),
            report.PackageInfo("one", has_wheel=True, dependencies=["z"]),
            report.PackageInfo("two", has_wheel=True, dependencies=["z"]),
        ]
        self.assertEqual([b.package for b in report.rank_blockers(packages)], ["z", "a", "b"])

    def test_external_dependencies_are_not_blockers(self) -> None:
        packages = [report.PackageInfo("tracked", has_wheel=True, dependencies=["external"])]
        self.assertEqual(report.rank_blockers(packages), [])
        self.assertEqual(report.rank_blockers([]), [])

    def test_upstream_blockers_are_transitive_and_exclude_self(self) -> None:
        packages = [
            report.PackageInfo("top", has_wheel=True, dependencies=["mid", "zeta"]),
            report.PackageInfo("mid", has_wheel=True, dependencies=["alpha"]),
            report.PackageInfo("Alpha", has_wheel=False, dependencies=["zeta"]),
            report.PackageInfo("zeta", has_wheel=False, dependencies=["alpha"]),
            report.PackageInfo("free", has_wheel=True),
        ]
        self.assertEqual(
            report.upstream_blockers(packages, report.rank_blockers(packages)),
            {
                "top": ["Alpha", "zeta"], "mid": ["Alpha", "zeta"],
                "alpha": ["zeta"], "zeta": ["Alpha"], "free": [],
            },
        )


class SharedClassificationTests(unittest.TestCase):
    def test_mixed_wheels(self) -> None:
        data = metadata([], ("pkg-1-py3-none-any.whl", "pkg-1-cp314-cp314-win_amd64.whl"))
        self.assertEqual(
            pypi.wheel_status(data),
            pypi.WheelStatus(platform_independent=True, has_target_wheel=False),
        )

    def test_non_cpython_pure_wheel_is_not_platform_independent(self) -> None:
        status = pypi.wheel_status(metadata([], ("pkg-1-pp3-none-any.whl",)))
        self.assertFalse(status.has_wheel)

    def test_lookup_errors(self) -> None:
        error = urllib.error.HTTPError("https://pypi.org", 404, "Not found", {}, None)
        with patch.object(pypi, "fetch_metadata", side_effect=error):
            self.assertEqual(pypi.lookup("pkg"), (None, "HTTP 404"))
        with patch.object(pypi, "fetch_metadata", side_effect=TimeoutError("slow")):
            self.assertEqual(pypi.lookup("pkg"), (None, "slow"))

    def test_status_script_check_package(self) -> None:
        data = metadata([], ("pkg-1-cp314-cp314-win_arm64.whl",))
        with patch.object(pypi, "fetch_metadata", return_value=data):
            status = fetch_package_status.check_package("pkg")
        self.assertTrue(status.has_target_wheel)
        self.assertEqual(status.error, "")
        with patch.object(pypi, "fetch_metadata", side_effect=TimeoutError("slow")):
            status = fetch_package_status.check_package("pkg")
        self.assertFalse(status.has_target_wheel)
        self.assertEqual(status.error, "slow")

    def test_status_script_package_lists_follow_updated_status(self) -> None:
        def result(package: str, current: str, proposed: str = "") -> dict:
            return {"package": package, "current_status": current, "proposed_status": proposed}

        results = [
            result("ready", "Builds Available"),
            result("todo", "needs pr"),
            result("blank", ""),
            result("review", "PR Under Review"),
            result("skipped", ""),
            result("new-wheel", "Needs PR", "Builds Available"),
            result("new-pr", "Needs PR", "PR Under Review"),
            result("merged", "", "PR Merged"),
        ]
        statuses = [
            fetch_package_status.PackageStatus(name)
            for name in ("Ready", "todo", "blank", "review", "new-wheel", "new-pr", "merged")
        ]
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(
            io.StringIO()
        ):
            fetch_package_status.write_package_lists(directory, statuses, results)
            with open(os.path.join(directory, "test-win_arm64.txt"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "Ready\nnew-wheel\n")
            path = os.path.join(directory, "test-win_arm64-buildtools.txt")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "todo\nblank\n")

    def test_status_script_open_pr_overrides_available_wheels(self) -> None:
        cols = {"package": "Package", "status": "Status", "pr": "PR"}
        url = "https://github.com/o/r/pull/1"
        row = {"Package": "xgboost", "Status": "Builds Available", "PR": url}

        def evaluate(state: str, merged: bool, has_wheel: bool) -> str:
            cache = {url: {"url": url, "state": state, "merged": merged, "author": ""}}
            return fetch_package_status.evaluate(row, has_wheel, cache, cols)[
                "proposed_status"
            ]

        self.assertEqual(evaluate("open", False, True), "PR Under Review")
        self.assertEqual(evaluate("closed", True, True), "Builds Available")
        self.assertEqual(evaluate("closed", False, True), "Builds Available")
        self.assertEqual(evaluate("closed", True, False), "PR Merged")
        self.assertEqual(evaluate("open", False, False), "PR Under Review")

    def test_status_script_tests_packages_with_wheels_under_review(self) -> None:
        wheels = pypi.wheel_status(metadata([], ("pkg-1-py3-none-win_arm64.whl",)))
        statuses = [fetch_package_status.PackageStatus("xgboost", wheels)]
        results = [
            {
                "package": "xgboost",
                "current_status": "Builds Available",
                "proposed_status": "PR Under Review",
            }
        ]
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(
            io.StringIO()
        ):
            fetch_package_status.write_package_lists(directory, statuses, results)
            with open(os.path.join(directory, "test-win_arm64.txt"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "xgboost\n")
            path = os.path.join(directory, "test-win_arm64-buildtools.txt")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "")

    def test_status_script_open_pr_dates_use_the_newest_open_pr(self) -> None:
        cols = {"package": "Package", "pr": "PR"}
        urls = [f"https://github.com/o/r/pull/{n}" for n in range(1, 5)]

        def pr(url: str, state: str, created_at: str) -> dict:
            return {"url": url, "state": state, "merged": False, "author": "",
                    "created_at": created_at}

        cache = {
            urls[0]: pr(urls[0], "open", "2026-08-01T10:00:00Z"),
            urls[1]: pr(urls[1], "open", "2026-09-18T23:30:00Z"),
            urls[2]: pr(urls[2], "closed", "2026-10-01T00:00:00Z"),
            urls[3]: pr(urls[3], "closed", "2026-07-01T00:00:00Z"),
        }
        rows = [
            {"Package": "multi", "PR": f"{urls[0]} {urls[2]}\n{urls[1]}"},
            {"Package": "closed-only", "PR": urls[3]},
            {"Package": "no-pr", "PR": ""},
            {"Package": "Dup_Name", "PR": urls[0]},
            {"Package": "dup-name", "PR": urls[1]},
        ]
        self.assertEqual(
            fetch_package_status.open_pr_dates(rows, cache, cols),
            [("multi", "2026-09-18"), ("Dup_Name", "2026-09-18")],
        )

    def test_read_sheet_filters_unless_keep_all(self) -> None:
        text = "Name,Other\nA,1\n,2\n# note,3\na,4\nB,5\n".encode()
        with patch.object(sheet, "fetch", return_value=text), patch.object(
            sheet, "sheet_csv_url", return_value="url"
        ):
            filtered = sheet.read_sheet("sheet", "tab", "Name")
            everything = sheet.read_sheet("sheet", "tab", "Name", keep_all=True)
        self.assertEqual([r["Name"] for r in filtered], ["A", "B"])
        self.assertEqual([r["Name"] for r in everything], ["A", "", "# note", "a", "B"])

    def test_compound_tags_and_sdist_are_handled(self) -> None:
        data = metadata([], ("pkg-1-cp313.cp314-cp314-win_amd64.win_arm64.whl",))
        data["urls"].append({"filename": "pkg-1.tar.gz", "packagetype": "sdist"})
        self.assertTrue(pypi.wheel_status(data).has_target_wheel)


class OutputTests(unittest.TestCase):
    def test_main_writes_partial_report_and_normalizes_duplicate_sheet_names(self) -> None:
        rows = [
            {"name": name, "prio": priority, "state": "In progress", "eng": status}
            for name, priority, status in (
                ("Core_Pkg", "P0", "MSFT Engaged"), ("", "", ""),
                ("core.pkg", "P2", "Ignored duplicate"), ("# Section", "", ""),
                ("client", "", ""), ("missing", "", ""),
            )
        ]
        responses = {
            "Core_Pkg": metadata([]),
            "client": metadata(
                ["core-pkg>=1", "external; extra == 'test'"],
                ("client-1-py3-none-any.whl",),
            ),
        }

        def fetch(name: str) -> dict:
            if name not in responses:
                raise urllib.error.HTTPError("https://pypi.org", 404, "Not found", {}, None)
            return responses[name]

        cols = {"package": "name", "priority": "prio", "status": "state", "engagement": "eng"}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            report, "load_sheet", return_value=(rows, cols)
        ) as load, patch.object(pypi, "fetch_metadata", side_effect=fetch) as lookup, patch(
            "sys.argv",
            ["report", "--output-dir", directory, "--jobs", "1", "--min-downstream", "1"],
        ), patch.dict(
            os.environ, {"GITHUB_STEP_SUMMARY": os.path.join(directory, "summary.md")}
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as warnings:
            report.main()
            self.assertEqual(lookup.call_count, 3)
            self.assertIn("missing: PyPI lookup: HTTP 404", warnings.getvalue())
            # The step summary is reserved for the sheet update report.
            self.assertEqual(
                sorted(os.listdir(directory)),
                ["package-blocker-report.md", "package-blockers.tsv"],
            )
            with open(os.path.join(directory, "package-blockers.tsv"), newline="", encoding="utf-8") as fh:
                self.assertEqual(
                    list(csv.reader(fh, delimiter="\t")),
                    [
                        ["Package Name", "Potential Blockers"],
                        ["Core_Pkg", ""], ["", ""], ["core.pkg", ""], ["# Section", ""],
                        ["client", "Core_Pkg"], ["missing", ""],
                    ],
                )
            load.assert_called_once_with(os.path.join(common.ROOT, ".env"), keep_all=True)
            with open(os.path.join(directory, "package-blocker-report.md"), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("**potential impact**", text)
            self.assertIn(
                "3 sheet packages queried; 2 have dependency metadata; 1 lack wheels; "
                "1 have unknown wheel availability.",
                text,
            )
            self.assertIn("| Rank | Blocker | Priority | Current status | Engagement |", text)
            self.assertIn(
                "| 1 | Core_Pkg | P0 | In progress | MSFT Engaged | 1 | 1 | client |", text
            )
            self.assertIn("HTTP 404", text)

    def test_empty_report_and_tsv_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(
            io.StringIO()
        ):
            report.write_outputs(directory, [], [])
            with open(os.path.join(directory, "package-blockers.tsv"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "Package Name\tPotential Blockers\n")
            self.assertIn("No blockers identified", report.render_report([], [], "now"))

    def test_invalid_jobs_rejected_before_loading_sheet(self) -> None:
        with patch("sys.argv", ["report", "--jobs", "0"]), patch.object(
            report, "load_sheet"
        ) as load, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                report.main()
        self.assertEqual(raised.exception.code, 2)
        load.assert_not_called()

    def test_markdown_omits_blockers_below_min_downstream(self) -> None:
        blockers = [
            report.Blocker("big", ["a"], ["a", "b"]),
            report.Blocker("small", ["c"], ["c"]),
            report.Blocker("none", [], []),
        ]
        text = report.render_report([], blockers, "now", min_downstream=2)
        self.assertIn("| 1 | big |", text)
        self.assertNotIn("| small |", text)
        self.assertNotIn("| none |", text)
        self.assertIn("Showing 1 blockers with at least 2 downstream packages; 2 with fewer", text)
        text = report.render_report([], blockers[1:], "now", min_downstream=2)
        self.assertIn("No blockers have at least 2 downstream packages.", text)
        self.assertIn("| none |", report.render_report([], blockers, "now"))

    def test_markdown_escapes_metadata(self) -> None:
        package = report.PackageInfo("pkg", issues=["bad | <value>\nnext"])
        text = report.render_report([package], [], "now")
        self.assertIn("bad &#124; &lt;value&gt; next", text)


if __name__ == "__main__":
    unittest.main()
