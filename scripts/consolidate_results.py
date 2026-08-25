"""Merge per-matrix-job result CSVs into the package status CSV, keyed on package name."""

import argparse
import csv
import glob
import os
from datetime import date


def read_csv(path: str) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", default="results", help="Directory containing results-*.csv files"
    )
    parser.add_argument(
        "--base",
        default=os.path.join("data", "win_arm64-status.csv"),
        help="Existing status CSV the results are merged into",
    )
    parser.add_argument(
        "--output", help="Path of the merged CSV (default: <base>-<datestamp>.csv)"
    )
    args = parser.parse_args()

    paths = sorted(glob.glob(os.path.join(args.input, "*.csv")))
    if not paths:
        raise SystemExit(f"No CSV files found in {args.input}")

    cols, base_rows = read_csv(args.base)
    rows = {row["package"]: dict(row) for row in base_rows}

    for path in paths:
        fieldnames, result_rows = read_csv(path)
        for col in fieldnames:
            if col not in cols:
                cols.append(col)
        for row in result_rows:
            package = row["package"]
            rows.setdefault(package, {"package": package}).update(row)

    if args.output:
        output = args.output
    else:
        root, ext = os.path.splitext(args.base)
        output = f"{root}-{date.today():%Y%m%d}{ext}"

    with open(output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, restval="")
        writer.writeheader()
        writer.writerows(rows.values())

    print(f"Wrote {output} ({len(rows)} packages, {len(cols)} columns)")


if __name__ == "__main__":
    main()
