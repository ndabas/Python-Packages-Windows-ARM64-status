"""Offline tests for combining install results with the sheet-aligned blocker TSV."""

import contextlib
import csv
import io
import os
import tempfile
import unittest
from unittest.mock import patch

import conda_repo
import consolidate_results
import fetch_conda_packages
from common import write_tsv

CORE_URL = "https://anaconda.org/anaconda/core-pkg"
CORE_LINK = f'=HYPERLINK("{CORE_URL}","Yes")'


def write_conda(directory: str) -> str:
    path = os.path.join(directory, "conda-packages.txt")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("client-python\ncore-pkg\nother\n")
    return path


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
                    "Package Name\tPotential Blockers\n"
                    "Core_Pkg\t\n\t\ncore.pkg\t\n# Section\t\nclient\tCore_Pkg\nuntested\t\n"
                )
            conda = write_conda(directory)
            output = os.path.join(directory, "combined.tsv")
            argv = [
                "consolidate", "--input", results, "--blockers", blockers,
                "--conda", conda, "--output", output,
            ]
            with patch("sys.argv", argv), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()) as warnings:
                consolidate_results.main()
            self.assertIn("not-in-sheet", warnings.getvalue())
            self.assertIn("1 packages are not on conda", warnings.getvalue())
            self.assertIn("  client: client-python\n", warnings.getvalue())
            with open(output, newline="", encoding="utf-8") as fh:
                text = fh.read()
            rows = list(csv.reader(io.StringIO(text), delimiter="\t"))

        # Formulas are left unquoted so Google Sheets evaluates them when pasted.
        self.assertIn(f"Core_Pkg\t\t{CORE_LINK}\t", text)
        url = "https://example.org/core.whl"
        self.assertEqual(
            rows,
            [
                [
                    "Package Name", "Potential Blockers", "Available on Conda",
                    "py3.14_install_status", "py3.14_binary_url",
                    "py3.14_buildtools_install_status", "py3.14_buildtools_binary_url",
                ],
                ["Core_Pkg", "", CORE_LINK, "success", url, "", ""],
                ["", "", "", "", "", "", ""],
                ["core.pkg", "", CORE_LINK, "success", url, "", ""],
                ["# Section", "", "", "", "", "", ""],
                ["client", "Core_Pkg", "No", "", "", "failure", ""],
                ["untested", "", "No", "", "", "", ""],
            ],
        )

    def test_no_results_keeps_only_the_blocker_and_conda_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blockers = os.path.join(directory, "package-blockers.tsv")
            with open(blockers, "w", encoding="utf-8", newline="") as fh:
                fh.write("Package Name\tPotential Blockers\nCore_Pkg\t\n\t\nclient\tCore_Pkg\n")
            conda = write_conda(directory)
            output = os.path.join(directory, "combined.tsv")
            argv = [
                "consolidate", "--no-results", "--plain-conda", "--blockers", blockers,
                "--conda", conda, "--output", output,
            ]
            with patch("sys.argv", argv), contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                consolidate_results.main()
            with open(output, newline="", encoding="utf-8") as fh:
                self.assertEqual(
                    fh.read(),
                    "Package Name\tPotential Blockers\tAvailable on Conda\n"
                    "Core_Pkg\t\tYes\n\t\t\nclient\tCore_Pkg\tNo\n",
                )

    def test_blocker_columns_must_come_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blockers = os.path.join(directory, "package-blockers.tsv")
            with open(blockers, "w", encoding="utf-8", newline="") as fh:
                fh.write("Potential Blockers\tPackage Name\n\tpkg\n")
            with patch(
                "sys.argv", ["consolidate", "--no-results", "--blockers", blockers]
            ), self.assertRaises(SystemExit) as raised:
                consolidate_results.main()
        self.assertIn("must start with the columns", str(raised.exception.code))

    def test_missing_conda_list_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blockers = os.path.join(directory, "package-blockers.tsv")
            with open(blockers, "w", encoding="utf-8", newline="") as fh:
                fh.write("Package Name\tPotential Blockers\npkg\t\n")
            argv = [
                "consolidate", "--no-results", "--blockers", blockers,
                "--conda", os.path.join(directory, "missing.txt"),
            ]
            with patch("sys.argv", argv), self.assertRaises(SystemExit) as raised:
                consolidate_results.main()
        self.assertIn("run fetch_conda_packages.py first", str(raised.exception.code))

    def test_fields_with_tabs_are_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(csv.Error):
                write_tsv(os.path.join(directory, "out.tsv"), [["a\tb"]])

    def test_missing_results_are_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "sys.argv", ["consolidate", "--input", directory]
        ):
            with self.assertRaises(SystemExit) as raised:
                consolidate_results.main()
        self.assertIn("No CSV files found", str(raised.exception.code))


class CondaRepoTests(unittest.TestCase):
    def test_names_are_read_from_both_package_formats_and_normalized(self) -> None:
        repodata = {
            "packages": {
                "a.tar.bz2": {"name": "typing_extensions"},
                "b.tar.bz2": {"name": "NumPy"},
            },
            "packages.conda": {
                "c.conda": {"name": "typing-extensions"},
                "d.conda": {"name": "ruamel.yaml"},
                "e.conda": {"name": "NumPy"},
            },
        }
        names = conda_repo.repodata_names(repodata)
        self.assertEqual(names, ["NumPy", "ruamel.yaml", "typing-extensions", "typing_extensions"])
        self.assertEqual(
            conda_repo.index_names(names),
            {
                "numpy": "NumPy",
                "ruamel-yaml": "ruamel.yaml",
                "typing-extensions": "typing-extensions",
            },
        )
        self.assertEqual(conda_repo.repodata_names({}), [])

    def test_aliases_are_preferred_when_in_the_channel(self) -> None:
        available = {
            "pytorch": "pytorch",
            "graphviz": "graphviz",
            "python-graphviz": "python-graphviz",
            "tables": "tables",
        }
        self.assertEqual(conda_repo.conda_name("torch", available), "pytorch")
        self.assertEqual(conda_repo.conda_name("graphviz", available), "python-graphviz")
        self.assertEqual(conda_repo.conda_name("tables", available), "tables")
        self.assertEqual(conda_repo.conda_name("xgboost", available), "")

    def test_similar_names_add_a_separated_or_py_prefix_or_suffix(self) -> None:
        available = conda_repo.index_names(
            ["bitarray", "cython-blis", "lazrs-python", "pyray", "pythonray", "ray-core",
             "rayon", "x-ray", "xray"]
        )
        self.assertEqual(
            conda_repo.similar_names("Ray", available),
            ["pyray", "pythonray", "ray-core", "x-ray"],
        )
        self.assertEqual(conda_repo.similar_names("blis", available), ["cython-blis"])
        self.assertEqual(conda_repo.similar_names("lazrs", available), ["lazrs-python"])
        self.assertEqual(conda_repo.similar_names("missing", available), [])

    def test_fetch_writes_the_package_list(self) -> None:
        repodata = (
            b'{"packages": {"a": {"name": "numpy"}}, "packages.conda": {"b": {"name": "cffi"}}}'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "conda-packages.txt")
            with patch("sys.argv", ["fetch", "--output", path]), patch.object(
                fetch_conda_packages, "fetch", return_value=repodata
            ), contextlib.redirect_stdout(io.StringIO()):
                fetch_conda_packages.main()
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "cffi\nnumpy\n")
            self.assertEqual(
                conda_repo.read_conda_packages(path), {"cffi": "cffi", "numpy": "numpy"}
            )

    def test_empty_repodata_is_an_error(self) -> None:
        with patch("sys.argv", ["fetch"]), patch.object(
            fetch_conda_packages, "fetch", return_value=b"{}"
        ), self.assertRaises(SystemExit) as raised:
            fetch_conda_packages.main()
        self.assertIn("No packages found", str(raised.exception.code))

    def test_conda_cell(self) -> None:
        available = {"core-pkg": "core-pkg"}
        self.assertEqual(consolidate_results.conda_cell("", available), "")
        self.assertEqual(consolidate_results.conda_cell("# Section", available), "")
        self.assertEqual(consolidate_results.conda_cell("client", available), "No")
        self.assertEqual(consolidate_results.conda_cell("Core_Pkg", available), CORE_LINK)
        self.assertEqual(consolidate_results.conda_cell("core.pkg", available, link=False), "Yes")


if __name__ == "__main__":
    unittest.main()
