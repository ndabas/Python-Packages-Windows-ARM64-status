"""Download the consolidated TSV from the latest full workflow run into data.

Quick runs skip the install tests, so the latest full run is the newest one that
uploaded both the consolidated-results artifact and at least one results-*
artifact. Before writing, the TSV's rows are reordered, matched on package name,
to follow the current Google Sheet, so it can be pasted alongside the sheet even
after the sheet has been re-sorted. Sheet packages missing from the TSV are left
blank and TSV packages no longer in the sheet are dropped, with a warning for each.
"""

import argparse
import csv
import os
import re
import shutil
import sys
import tempfile

from common import DATA_DIR, ROOT, normalize_name
from github_cli import gh, require_gh, run
from sheet import is_package_row, load_sheet

ARTIFACT = "consolidated-results"
PACKAGE_COLUMN = "Package Name"
TSV_RE = re.compile(r"^win_arm64-status-(\d{8})\.tsv$")


def latest_full_run(artifacts: list[dict], branch: str | None = None) -> int | None:
    """Id of the newest run with both consolidated and per-job results artifacts."""
    names: dict[int, set[str]] = {}
    created: dict[int, str] = {}
    for artifact in artifacts:
        workflow_run = artifact.get("workflow_run") or {}
        if artifact.get("expired") or (branch and workflow_run.get("head_branch") != branch):
            continue
        run_id = workflow_run["id"]
        names.setdefault(run_id, set()).add(artifact["name"])
        if artifact["name"] == ARTIFACT:
            created[run_id] = artifact["created_at"]
    full = [
        (created[run_id], run_id)
        for run_id, found in names.items()
        if ARTIFACT in found and any(name.startswith("results-") for name in found)
    ]
    return max(full)[1] if full else None


def reorder(
    rows: list[list[str]], sheet_names: list[str]
) -> tuple[list[list[str]], list[str], list[str]]:
    """Reorder TSV rows (header first) to follow the sheet's package column.

    Returns the reordered rows, the sheet packages missing from the TSV (left
    blank) and the TSV packages no longer in the sheet (dropped). Rows are matched
    on the exact name, then the normalized name, and take the sheet's spelling.
    """
    header, *body = rows
    by_name: dict[str, list[str]] = {}
    by_key: dict[str, list[str]] = {}
    for row in body:
        name = row[0] if row else ""
        if is_package_row(name):
            by_name.setdefault(name, row)
            by_key.setdefault(normalize_name(name), row)

    output = [header]
    missing: list[str] = []
    for name in sheet_names:
        match = None
        if is_package_row(name):
            match = by_name.get(name) or by_key.get(normalize_name(name))
            if match is None and name not in missing:
                missing.append(name)
        values = match[1:] if match else []
        output.append([name, *values] + [""] * (len(header) - 1 - len(values)))

    sheet_keys = {normalize_name(name) for name in sheet_names if is_package_row(name)}
    dropped = list(
        dict.fromkeys(
            row[0]
            for row in body
            if row and is_package_row(row[0]) and normalize_name(row[0]) not in sheet_keys
        )
    )
    return output, missing, dropped


def name_list(names: list[str], shown: int = 20) -> str:
    text = ", ".join(names[:shown])
    return text + (f", ... and {len(names) - shown} more" if len(names) > shown else "")


def reorder_file(path: str, sheet_names: list[str]) -> None:
    """Rewrite a consolidated TSV in place to follow the sheet's package order."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))
    if rows and rows[0][:1] == ["Package"]:
        # Runs from before the column was renamed to avoid clashing with the sheet's own.
        rows[0][0] = PACKAGE_COLUMN
    if not rows or rows[0][:1] != [PACKAGE_COLUMN]:
        raise SystemExit(f"{path} does not start with a {PACKAGE_COLUMN!r} column")

    output, missing, dropped = reorder(rows, sheet_names)
    names = [row[0] if row else "" for row in rows[1:]]
    if names == sheet_names:
        print(f"Package order already matches the sheet ({len(sheet_names)} rows)")
    else:
        changed = sum(
            1 for i, name in enumerate(sheet_names) if i >= len(names) or names[i] != name
        )
        print(
            f"Reordered to match the sheet: {len(names)} TSV rows, "
            f"{len(sheet_names)} sheet rows, {changed} changed"
        )
    if missing:
        print(f"Warning: left blank, not in the TSV: {name_list(missing)}", file=sys.stderr)
    if dropped:
        print(f"Warning: dropped, no longer in the sheet: {name_list(dropped)}", file=sys.stderr)

    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh, delimiter="\t", lineterminator="\n").writerows(output)


def download(run_id: int, directory: str) -> list[str]:
    """Download the consolidated artifact of a run and return its file names."""
    result = run(
        ["gh", "run", "download", str(run_id), "--name", ARTIFACT, "--dir", directory],
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr.strip().splitlines() or ["gh failed"])[-1]
        raise SystemExit(f"gh run download {run_id}: {message}")
    files = sorted(os.listdir(directory))
    if not files:
        raise SystemExit(f"The {ARTIFACT} artifact of run {run_id} was empty")
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=os.path.join(ROOT, ".env"))
    parser.add_argument("--output-dir", default=DATA_DIR)
    parser.add_argument(
        "--branch", help="Only consider runs on this branch (default: any, including PRs)"
    )
    parser.add_argument(
        "--remove-older", action="store_true",
        help="Delete win_arm64-status-<date>.tsv files dated before the downloaded one",
    )
    parser.add_argument(
        "--skip-reorder", action="store_true",
        help="Keep the TSV's original row order instead of following the current sheet",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be written or removed without changing the output directory",
    )
    args = parser.parse_args()

    require_gh()
    sheet_names = None
    if not args.skip_reorder:
        print("Reading the sheet to reorder the TSV; pass --skip-reorder to skip this")
        sheet_rows, cols = load_sheet(args.env, keep_all=True)
        sheet_names = [row.get(cols["package"], "") for row in sheet_rows]

    artifacts = gh("api", "repos/{owner}/{repo}/actions/artifacts?per_page=100")
    assert isinstance(artifacts, dict)
    run_id = latest_full_run(artifacts["artifacts"], args.branch)
    if run_id is None:
        raise SystemExit(f"No full workflow run with a {ARTIFACT} artifact was found")
    print(f"Downloading {ARTIFACT} from run {run_id}")

    # gh run download refuses to overwrite existing files, so stage in a temp directory.
    with tempfile.TemporaryDirectory() as staging:
        files = download(run_id, staging)
        if sheet_names is not None:
            for name in files:
                if TSV_RE.match(name):
                    reorder_file(os.path.join(staging, name), sheet_names)
        if not args.dry_run:
            os.makedirs(args.output_dir, exist_ok=True)
        for name in files:
            target = os.path.join(args.output_dir, name)
            if args.dry_run:
                print(f"Would write {target}")
            else:
                shutil.copyfile(os.path.join(staging, name), target)
                print(f"Wrote {target}")

    dates = [match.group(1) for name in files if (match := TSV_RE.match(name))]
    if args.remove_older and dates and os.path.isdir(args.output_dir):
        for name in sorted(os.listdir(args.output_dir)):
            match = TSV_RE.match(name)
            if match and match.group(1) < max(dates):
                path = os.path.join(args.output_dir, name)
                if args.dry_run:
                    print(f"Would remove {path}")
                else:
                    os.remove(path)
                    print(f"Removed {path}")


if __name__ == "__main__":
    main()
