"""Combine per-matrix-job install results with the sheet-aligned blocker TSV.

Writes a TSV with one line per line of package-blockers.tsv, and so one line per
sheet row in sheet order. Package Name, PR Opened, Potential Blockers and
Available on Conda are always the first four columns, followed by every install
result column, so a quick (--no-results) and a full TSV can be pasted into the
same place in the sheet. PR Opened is the date the newest open linked PR was
opened, from the open-pr-dates.tsv written by fetch_package_status.py, and is
blank when no linked PR is open. Available on Conda is matched against the
conda-packages.txt written by fetch_conda_packages.py: "Yes" is written as a
Google Sheets HYPERLINK formula to the package's anaconda.org page (plain "Yes"
with --plain-conda), "No" when it is absent. Packages are matched on their
normalized names, so duplicate sheet rows all receive the results; blank and
comment rows are left empty, as are the result columns of untested rows.
"""

import argparse
import csv
import glob
import os
import sys
from datetime import date

from common import DATA_DIR, OPEN_PR_DATES, PR_OPENED_COLUMN, normalize_name, write_tsv
from conda_repo import (
    CONDA_PACKAGES,
    conda_name,
    package_url,
    read_conda_packages,
    similar_names,
)
from sheet import is_package_row

LEADING_COLUMNS = ["Package Name", "Potential Blockers"]
CONDA_COLUMN = "Available on Conda"


def conda_cell(name: str, available: dict[str, str], link: bool = True) -> str:
    """Available on Conda value for a sheet package cell; blank for non-package rows."""
    if not is_package_row(name):
        return ""
    found = conda_name(name, available)
    if not found:
        return "No"
    return f'=HYPERLINK("{package_url(found)}","Yes")' if link else "Yes"


def read_pr_dates(path: str) -> dict[str, str]:
    """PR Opened dates keyed by normalized package name."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))[1:]
    return {normalize_name(row[0]): row[1] for row in rows if len(row) >= 2 and row[0]}


def read_results(paths: list[str]) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Result columns in file order, and each package's results keyed by normalized name."""
    columns: list[str] = []
    results: dict[str, dict[str, str]] = {}
    for path in paths:
        # Export-Csv in Windows PowerShell writes a BOM.
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            columns += [c for c in reader.fieldnames or [] if c != "package" and c not in columns]
            for row in reader:
                values = {k: v for k, v in row.items() if k != "package"}
                results.setdefault(normalize_name(row["package"]), {}).update(values)
    return columns, results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--input", default="results", help="Directory containing results-*.csv files"
    )
    source.add_argument(
        "--no-results",
        action="store_true",
        help="Write only the blocker and conda columns, e.g. when the install tests were skipped",
    )
    parser.add_argument(
        "--blockers",
        default=os.path.join(DATA_DIR, "package-blockers.tsv"),
        help="Sheet-aligned TSV written by report_package_blockers.py",
    )
    parser.add_argument(
        "--conda",
        default=os.path.join(DATA_DIR, CONDA_PACKAGES),
        help="Conda package list written by fetch_conda_packages.py",
    )
    parser.add_argument(
        "--pr-dates",
        default=os.path.join(DATA_DIR, OPEN_PR_DATES),
        help="Open pull request dates written by fetch_package_status.py",
    )
    parser.add_argument(
        "--plain-conda",
        action="store_true",
        help="Write Available on Conda as plain Yes/No instead of Yes hyperlink formulas",
    )
    parser.add_argument(
        "--output",
        help="Path of the combined TSV (default: data/win_arm64-status-<datestamp>.tsv)",
    )
    args = parser.parse_args()

    columns: list[str] = []
    results: dict[str, dict[str, str]] = {}
    if not args.no_results:
        paths = sorted(glob.glob(os.path.join(args.input, "*.csv")))
        if not paths:
            raise SystemExit(f"No CSV files found in {args.input}")
        columns, results = read_results(paths)

    with open(args.blockers, newline="", encoding="utf-8") as fh:
        header, *rows = csv.reader(fh, delimiter="\t")
    if header[: len(LEADING_COLUMNS)] != LEADING_COLUMNS:
        raise SystemExit(
            f"{args.blockers} must start with the columns {LEADING_COLUMNS}; found {header}"
        )
    if not os.path.isfile(args.conda):
        raise SystemExit(f"{args.conda} not found; run fetch_conda_packages.py first")
    conda = read_conda_packages(args.conda)
    if not os.path.isfile(args.pr_dates):
        raise SystemExit(f"{args.pr_dates} not found; run fetch_package_status.py first")
    pr_dates = read_pr_dates(args.pr_dates)
    width = len(LEADING_COLUMNS)

    output = args.output or os.path.join(DATA_DIR, f"win_arm64-status-{date.today():%Y%m%d}.tsv")
    matched: set[str] = set()
    # Packages missing from conda, by normalized name, with conda names that may be them.
    similar: dict[str, tuple[str, list[str]]] = {}
    lines = [
        header[:1] + [PR_OPENED_COLUMN] + header[1:width] + [CONDA_COLUMN]
        + header[width:] + columns
    ]
    for row in rows:
        name = row[0] if row else ""
        key = normalize_name(name) if is_package_row(name) else ""
        found = results.get(key, {})
        if found:
            matched.add(key)
        if key and key not in similar and not conda_name(name, conda):
            similar[key] = (name, similar_names(name, conda))
        row = row + [""] * (len(header) - len(row))
        cell = conda_cell(name, conda, link=not args.plain_conda)
        lines.append(
            row[:1] + [pr_dates.get(key, "") if key else ""] + row[1:width] + [cell]
            + row[width:] + [found.get(c, "") for c in columns]
        )
    write_tsv(output, lines)

    candidates = [(name, names) for name, names in similar.values() if names]
    if candidates:
        print(
            f"Warning: {len(candidates)} packages are not on conda under their own name, "
            "but conda packages with a similar name are; add any that are the same "
            "package to CONDA_ALIASES in conda_repo.py:\n"
            + "".join(f"  {name}: {', '.join(names)}\n" for name, names in candidates),
            end="",
            file=sys.stderr,
        )
    unmatched = sorted(set(results) - matched)
    if unmatched:
        print(
            f"Warning: {len(unmatched)} tested packages are not in {args.blockers}: "
            + ", ".join(unmatched),
            file=sys.stderr,
        )
    print(
        f"Wrote {output} ({len(rows)} sheet rows, {len(matched)} of {len(results)} "
        f"tested packages matched, {len(columns)} result columns)"
    )


if __name__ == "__main__":
    main()
