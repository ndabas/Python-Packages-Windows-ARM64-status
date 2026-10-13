"""Combine per-matrix-job install results with the sheet-aligned blocker TSV.

Writes a TSV with one line per line of package-blockers.tsv, and so one line per
sheet row in sheet order. Package and Potential Blockers are always the first two
columns, followed by every install result column, so a quick (--no-results) and
a full TSV can be pasted into the same place in the sheet. Packages are matched
on their normalized names, so duplicate sheet rows all receive the results;
blank, comment and untested rows are left empty.
"""

import argparse
import csv
import glob
import os
import sys
from datetime import date

from common import DATA_DIR, normalize_name
from sheet import is_package_row

LEADING_COLUMNS = ["Package", "Potential Blockers"]


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
        help="Write only the blocker columns, e.g. when the install tests were skipped",
    )
    parser.add_argument(
        "--blockers",
        default=os.path.join(DATA_DIR, "package-blockers.tsv"),
        help="Sheet-aligned TSV written by report_package_blockers.py",
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

    output = args.output or os.path.join(DATA_DIR, f"win_arm64-status-{date.today():%Y%m%d}.tsv")
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    matched: set[str] = set()
    with open(output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(header + columns)
        for row in rows:
            name = row[0] if row else ""
            key = normalize_name(name) if is_package_row(name) else ""
            found = results.get(key, {})
            if found:
                matched.add(key)
            row += [""] * (len(header) - len(row))
            writer.writerow(row + [found.get(c, "") for c in columns])

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
