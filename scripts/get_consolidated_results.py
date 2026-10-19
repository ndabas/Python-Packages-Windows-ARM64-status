"""Download the consolidated TSV from the latest full workflow run into data.

Quick runs skip the install tests, so the latest full run is the newest one that
uploaded both the consolidated-results artifact and at least one results-*
artifact. Before writing, the TSV's rows are reordered, matched on package name,
to follow the current Google Sheet, so it can be pasted alongside the sheet even
after the sheet has been re-sorted. Sheet packages missing from the TSV are left
blank and TSV packages no longer in the sheet are dropped, with a warning for each.
The output of the run's consolidate step, including its warnings, is printed too.

consolidated-results.md is written alongside the TSV, with the reordered rows
that differ from the sheet's columns of the same name, and the consolidate
step's warning about packages with similarly named conda packages, which is
left out of the printed output.
"""

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import NamedTuple

from common import DATA_DIR, ROOT, normalize_name, write_text, write_tsv
from conda_repo import package_url
from github_cli import gh, require_gh, run
from sheet import is_package_row, load_sheet

ARTIFACT = "consolidated-results"
REPORT = "consolidated-results.md"
PACKAGE_COLUMN = "Package Name"
TSV_RE = re.compile(r"^win_arm64-status-(\d{8})\.tsv$")
HYPERLINK_RE = re.compile(r'^=HYPERLINK\("((?:[^"]|"")*)"\s*[,;]\s*"((?:[^"]|"")*)"\)$', re.I)
# The sheet shows dates as M/D/YYYY, while the TSV has YYYY-MM-DD.
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y")
CONDA_WARNING_RE = re.compile(r"^Warning: .*not on conda under their own name")
JOB = "consolidate-results"
STEP_START = "##[group]Run python scripts/consolidate_results.py"
# Lines that start the step after it in a job log.
STEP_ENDS = ("##[group]Run ", "Post job cleanup.", "Cleaning up orphan processes")
TIMESTAMP_RE = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
COMMAND_RE = re.compile(r"^##\[(\w+)\]")


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


def reorder_file(path: str, sheet_names: list[str]) -> list[list[str]]:
    """Rewrite a consolidated TSV in place to follow the sheet's package order.

    Returns the rewritten rows, header first.
    """
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

    write_tsv(path, output)
    return output


def comparable(value: str) -> str:
    """A cell value as the sheet shows it: HYPERLINK formulas as their label, dates as ISO."""
    value = value.strip()
    match = HYPERLINK_RE.match(value)
    if match:
        return match.group(2).replace('""', '"')
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            pass
    return value


def diff_rows(
    rows: list[list[str]], sheet_rows: list[dict[str, str]]
) -> list[tuple[int, list[str], set[int]]]:
    """Reordered TSV rows (header first) that differ from the sheet's columns of the same name.

    Returns each differing row's sheet row number, its values, and the indexes of
    its differing cells. Columns missing from the sheet are compared as blank.
    """
    header, *body = rows
    changed: list[tuple[int, list[str], set[int]]] = []
    for number, (row, sheet_row) in enumerate(zip(body, sheet_rows), start=2):
        cells = {
            i for i, column in enumerate(header)
            if comparable(row[i] if i < len(row) else "") != comparable(sheet_row.get(column, ""))
        }
        if cells:
            changed.append((number, row, cells))
    return changed


def markdown_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(
        "|", "&#124;"
    ).replace("*", "\\*").replace("_", "\\_").replace("\r", " ").replace("\n", " ")


def markdown_value(value: str) -> str:
    """A TSV cell for a Markdown table, with HYPERLINK formulas and URLs as links."""
    match = HYPERLINK_RE.match(value.strip())
    if match:
        url, label = (group.replace('""', '"') for group in match.groups())
        return f"[{markdown_text(label)}](<{url}>)"
    if re.match(r"^https?://\S+$", value):
        label = value.rstrip("/").rsplit("/", 1)[-1] or value
        return f"[{markdown_text(label)}](<{value}>)"
    return markdown_text(value)


def diff_table(header: list[str], changed: list[tuple[int, list[str], set[int]]]) -> list[str]:
    """Markdown table of the differing rows, with the package and every column that differs."""
    differing = set().union(*(cells for _, _, cells in changed))
    columns = [i for i in range(len(header)) if i == 0 or i in differing]
    lines = [
        "| Row | " + " | ".join(markdown_text(header[i]) for i in columns) + " |",
        "| ---: |" + " --- |" * len(columns),
    ]
    for number, row, cells in changed:
        values = []
        for i in columns:
            text = markdown_value(row[i] if i < len(row) else "")
            values.append(f"**{text or '(blank)'}**" if i in cells else text)
        lines.append(f"| {number} | " + " | ".join(values) + " |")
    return lines


def split_conda_warning(output: list[str]) -> tuple[list[str], list[str]]:
    """Split the warning about similarly named conda packages, with its indented
    entries, from the rest of the consolidate step's output."""
    warning: list[str] = []
    rest: list[str] = []
    in_warning = False
    for line in output:
        if CONDA_WARNING_RE.match(line):
            in_warning = True
            warning.append(line)
        elif in_warning and line.startswith("  "):
            warning.append(line)
        else:
            in_warning = False
            rest.append(line)
    return warning, rest


def conda_section(warning: list[str] | None, error: str = "") -> list[str]:
    lines = ["## Similarly named conda packages", ""]
    if warning is None:
        return lines + [f"The consolidate step's output could not be fetched: {error}", ""]
    if not warning:
        return lines + ["The consolidate step reported none.", ""]
    intro, *entries = warning
    lines += [markdown_text(intro.removeprefix("Warning: ")), ""]
    for entry in entries:
        name, _, names = entry.strip().partition(":")
        links = [
            f"[{markdown_text(found)}](<{package_url(found)}>)"
            for found in (n.strip() for n in names.split(","))
            if found
        ]
        lines.append(f"- {markdown_text(name)}: {', '.join(links)}")
    return lines + [""]


class TsvDiff(NamedTuple):
    name: str
    header: list[str]
    count: int
    changed: list[tuple[int, list[str], set[int]]]
    missing_columns: list[str]


def compare_with_sheet(
    name: str, rows: list[list[str]], sheet_rows: list[dict[str, str]]
) -> TsvDiff:
    columns = set().union(*(row.keys() for row in sheet_rows)) if sheet_rows else set()
    return TsvDiff(
        name,
        rows[0],
        len(rows) - 1,
        diff_rows(rows, sheet_rows),
        [column for column in rows[0] if column not in columns],
    )


def render_report(
    run_id: int,
    diffs: list[TsvDiff] | None,
    conda_warning: list[str] | None,
    conda_error: str = "",
    generated_at: str = "",
) -> str:
    """Markdown report; diffs is None when the sheet was not read."""
    generated_at = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Consolidated results",
        "",
        f"Generated: {generated_at} from workflow run {run_id}",
        "",
        "## Rows that differ from the sheet",
        "",
    ]
    if diffs is None:
        lines += ["Not compared: the sheet was not read because of --skip-reorder.", ""]
    for diff in diffs or []:
        if len(diffs or []) > 1:
            lines += [f"### {markdown_text(diff.name)}", ""]
        lines += [
            f"{len(diff.changed)} of {diff.count} rows in `{diff.name}` differ from the "
            "sheet's columns of the same name; differing cells are in bold. Dates are "
            "compared by value and HYPERLINK formulas by their label.",
            "",
        ]
        if diff.missing_columns:
            lines += [
                "Columns not in the sheet, compared as blank: "
                + ", ".join(markdown_text(c) for c in diff.missing_columns),
                "",
            ]
        if diff.changed:
            lines += diff_table(diff.header, diff.changed) + [""]
    lines += conda_section(conda_warning, conda_error)
    return "\n".join(lines)

def step_output(log: str) -> list[str] | None:
    """Lines printed by the consolidate step in a job log, or None if it isn't there.

    Timestamps and colours are removed, and workflow commands such as ##[error]
    are shown as "Error: ".
    """
    lines = [ANSI_RE.sub("", TIMESTAMP_RE.sub("", line)) for line in log.splitlines()]
    start = next((i for i, line in enumerate(lines) if line.startswith(STEP_START)), None)
    if start is None:
        return None
    # The step starts with a group echoing its script and environment.
    end_header = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("##[endgroup]")), start
    )
    output: list[str] = []
    for line in lines[end_header + 1:]:
        if line.startswith(STEP_ENDS):
            break
        output.append(COMMAND_RE.sub(lambda m: f"{m.group(1).capitalize()}: ", line))
    return output


def fetch_step_output(run_id: int) -> list[str]:
    """Fetch the consolidate step's output from a run; raise RuntimeError if unavailable."""
    jobs_path = f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100"
    result = run(["gh", "api", jobs_path], check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr.strip().splitlines() or ["gh failed"])[-1])
    job = next((job for job in json.loads(result.stdout)["jobs"] if job["name"] == JOB), None)
    if job is None:
        raise RuntimeError(f"run {run_id} has no {JOB} job")

    path = f"repos/{{owner}}/{{repo}}/actions/jobs/{job['id']}/logs"
    # Logs contain colour codes, which newer versions of gh refuse to print by default.
    result = run(["gh", "api", "--allow-escape-sequences", path], check=False)
    if result.returncode != 0 and "unknown flag" in result.stderr:
        result = run(["gh", "api", path], check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr.strip().splitlines() or ["gh failed"])[-1])
    output = step_output(result.stdout)
    if output is None:
        raise RuntimeError(f"the {JOB} job log has no consolidate_results.py step")
    return output


def print_step_output(run_id: int, output: list[str]) -> None:
    print(f"Output of consolidate_results.py in run {run_id}:")
    for line in output:
        print(f"  {line}" if line else "")


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
    sheet_rows = None
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
    conda_warning: list[str] | None = None
    conda_error = ""
    try:
        conda_warning, output = split_conda_warning(fetch_step_output(run_id))
        print_step_output(run_id, output)
    except RuntimeError as error:
        conda_error = str(error)
        print(f"Warning: could not fetch the consolidate step's output: {error}", file=sys.stderr)
    print(f"Downloading {ARTIFACT} from run {run_id}")

    diffs: list[TsvDiff] | None = None
    # gh run download refuses to overwrite existing files, so stage in a temp directory.
    with tempfile.TemporaryDirectory() as staging:
        files = download(run_id, staging)
        if sheet_rows is not None and sheet_names is not None:
            diffs = []
            for name in files:
                if TSV_RE.match(name):
                    rows = reorder_file(os.path.join(staging, name), sheet_names)
                    diffs.append(compare_with_sheet(name, rows, sheet_rows))
        if not args.dry_run:
            os.makedirs(args.output_dir, exist_ok=True)
        for name in files:
            target = os.path.join(args.output_dir, name)
            if args.dry_run:
                print(f"Would write {target}")
            else:
                shutil.copyfile(os.path.join(staging, name), target)
                print(f"Wrote {target}")

    report = os.path.join(args.output_dir, REPORT)
    changed = (
        "not compared with the sheet" if diffs is None
        else f"{sum(len(diff.changed) for diff in diffs)} rows differ from the sheet"
    )
    candidates = (
        "conda warning unavailable" if conda_warning is None
        else f"{max(len(conda_warning) - 1, 0)} packages with similarly named conda packages"
    )
    if args.dry_run:
        print(f"Would write {report} ({changed}, {candidates})")
    else:
        write_text(report, render_report(run_id, diffs, conda_warning, conda_error))
        print(f"Wrote {report} ({changed}, {candidates})")

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
    # Keep printed lines in order with the warnings on stderr when stdout is not a console.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    main()
