"""Consolidate per-matrix-job result CSVs into a single CSV keyed on package name."""

import argparse
import csv
import glob
import os


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", default="results", help="Directory containing results-*.csv files"
    )
    parser.add_argument(
        "--output", default="consolidated.csv", help="Path of the consolidated CSV"
    )
    args = parser.parse_args()

    paths = sorted(glob.glob(os.path.join(args.input, "*.csv")))
    if not paths:
        raise SystemExit(f"No CSV files found in {args.input}")

    rows: dict[str, dict[str, str]] = {}
    cols = ["package"]
    for path in paths:
        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            for col in reader.fieldnames:
                if col != "package" and col not in cols:
                    cols.append(col)
            for row in reader:
                rows.setdefault(row["package"], {}).update(row)

    with open(args.output, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, restval="")
        writer.writeheader()
        for pkg in sorted(rows):
            writer.writerow(rows[pkg])

    print(f"Wrote {args.output} ({len(rows)} packages, {len(cols)} columns)")


if __name__ == "__main__":
    main()
