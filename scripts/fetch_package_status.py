"""Build the install-test package lists and a sheet update report from a Google Sheet.

Reads GOOGLE_SHEET_URL / GOOGLE_SHEET_NAME / GOOGLE_SHEET_COL_PACKAGE_NAME (and the
optional GOOGLE_SHEET_GID) from the environment or a .env file and queries the PyPI
JSON API for each package. It compares the sheet against PyPI and the linked
GitHub pull requests and writes a markdown report of the rows that need updating
(the sheet itself is never modified). It then writes the two package lists
consumed by the test/build matrices from each package's updated status (the
proposed status where the report has one, otherwise the sheet's):
test-win_arm64.txt holds the packages whose status is "Builds Available", and
test-win_arm64-buildtools.txt those with no status or "Needs PR". The GitHub CLI
(`gh`) is required for the pull request lookups.
"""

import argparse
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from common import DATA_DIR, ROOT, append_step_summary, write_text
from github_cli import pr_refs, pull_request, require_gh
from pypi import TARGET_PLATFORM, WheelStatus, lookup, wheel_status
from sheet import (
    STATUS_BUILDS_AVAILABLE,
    STATUS_NEEDS_PR,
    STATUS_PR_MERGED,
    STATUS_PR_UNDER_REVIEW,
    load_sheet,
)


@dataclass
class PackageStatus:
    package: str
    wheels: WheelStatus | None = None
    error: str = ""

    @property
    def has_target_wheel(self) -> bool:
        return bool(self.wheels and self.wheels.has_target_wheel)


def check_package(package: str) -> PackageStatus:
    data, error = lookup(package)
    return PackageStatus(package, wheel_status(data) if data is not None else None, error)


def check_pull_requests(sheet_rows: list[dict[str, str]], column: str) -> dict[str, dict]:
    """Look up every distinct PR linked from the sheet, reporting progress."""
    refs: dict[str, tuple[str, str, str]] = {}
    for row in sheet_rows:
        for url, owner, repo, number in pr_refs(row.get(column, "")):
            refs.setdefault(url, (owner, repo, number))

    cache: dict[str, dict] = {}
    for index, (url, (owner, repo, number)) in enumerate(refs.items(), start=1):
        data = pull_request(f"{owner}/{repo}", number)
        entry = {
            "url": url,
            "state": data.get("state", ""),
            "merged": bool(data.get("merged_at")),
            "author": (data.get("user") or {}).get("login", ""),
        }
        cache[url] = entry
        state = "merged" if entry["merged"] else entry["state"]
        print(f"[{index}/{len(refs)}] {url}: {state}", flush=True)
    return cache


def evaluate(
    sheet_row: dict[str, str],
    has_target_wheel: bool,
    cache: dict[str, dict],
    cols: dict[str, str],
) -> dict:
    """Work out the status the sheet should show for one package."""
    # Rows with wheels on PyPI are settled, so their PRs are never looked up.
    prs = [cache[url] for url, *_ in pr_refs(sheet_row.get(cols["pr"], "")) if url in cache]

    if has_target_wheel:
        proposed = STATUS_BUILDS_AVAILABLE
    elif any(pr["state"] == "open" for pr in prs):
        proposed = STATUS_PR_UNDER_REVIEW
    elif any(pr["merged"] for pr in prs):
        proposed = STATUS_PR_MERGED
    else:
        proposed = ""

    return {
        "package": sheet_row[cols["package"]],
        "current_status": sheet_row.get(cols["status"], ""),
        "proposed_status": proposed,
        "prs": prs,
    }


def render_report(
    results: list[dict],
    cols: dict[str, str],
    ignored: list[tuple[str, str]],
) -> str:
    """Render the rows whose status differs from the sheet."""
    changes = [
        r
        for r in results
        if r["proposed_status"] and r["proposed_status"] != r["current_status"]
    ]

    lines = [
        "# Google Sheet update report",
        "",
        f"{len(changes)} of {len(results)} rows need updating. "
        "The sheet is not modified by this report.",
        "",
    ]

    if changes:
        lines += [
            f"| Package | {cols['status']} (sheet) | {cols['status']} (proposed) | "
            "Pull requests |",
            "| --- | --- | --- | --- |",
        ]
        for row in changes:
            prs = "<br>".join(
                f"[#{pr['url'].rsplit('/', 1)[1]}]({pr['url']}) "
                f"({'merged' if pr['merged'] else pr['state']}"
                f"{', @' + pr['author'] if pr['author'] else ''})"
                for pr in row["prs"]
            )
            lines.append(
                f"| {row['package']} | {row['current_status'] or '—'} | "
                f"{row['proposed_status']} | {prs or '—'} |"
            )
        lines.append("")

    if ignored:
        lines += [
            "## Packages not looked up on PyPI",
            "",
            f"{len(ignored)} package names were skipped; their pull request links are "
            "still checked above.",
            "",
            "| Package | Reason |",
            "| --- | --- |",
        ]
        lines += [f"| {name} | {reason} |" for name, reason in ignored]
        lines.append("")

    return "\n".join(lines)


def updated_status(result: dict) -> str:
    """The status the sheet will show once the report's proposed changes are applied."""
    return result["proposed_status"] or result["current_status"]


def write_package_lists(
    output_dir: str, statuses: list[PackageStatus], results: list[dict]
) -> None:
    """Write the test lists from each package's updated sheet status."""
    sheet_status = {
        r["package"].lower(): updated_status(r).strip().lower() for r in results
    }
    testable = [
        s.package
        for s in statuses
        if sheet_status.get(s.package.lower()) == STATUS_BUILDS_AVAILABLE.lower()
    ]
    buildable = [
        s.package
        for s in statuses
        if sheet_status.get(s.package.lower()) in ("", STATUS_NEEDS_PR.lower())
    ]
    test_path = os.path.join(output_dir, f"test-{TARGET_PLATFORM}.txt")
    build_path = os.path.join(output_dir, f"test-{TARGET_PLATFORM}-buildtools.txt")
    for path, names in ((test_path, testable), (build_path, buildable)):
        write_text(path, "".join(f"{name}\n" for name in names))
        print(f"Wrote {path} ({len(names)} packages)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=os.path.join(ROOT, ".env"))
    parser.add_argument(
        "--output-dir",
        default=DATA_DIR,
        help="Directory for the package lists and the report",
    )
    parser.add_argument("--report-name", default="sheet-update-report.md")
    parser.add_argument(
        "--jobs", type=int, default=3, help="Number of simultaneous PyPI requests"
    )
    args = parser.parse_args()

    require_gh()
    sheet_rows, cols = load_sheet(args.env)

    # Only the PyPI lookup is skipped for these; their PR links are still checked below.
    ignored: list[tuple[str, str]] = []
    packages = []
    for row in sheet_rows:
        name = row[cols["package"]]
        if re.search(r"\s", name):
            ignored.append((name, "name contains whitespace"))
        else:
            packages.append(name)

    statuses: list[PackageStatus] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for index, status in enumerate(pool.map(check_package, packages), start=1):
            progress = f"[{index}/{len(packages)}] {status.package}"
            if status.error == "HTTP 404":
                ignored.append((status.package, "not found on PyPI"))
                print(f"{progress}: not on PyPI, ignored", flush=True)
                continue
            statuses.append(status)
            wheels = status.wheels
            state = status.error or (
                TARGET_PLATFORM if wheels and wheels.has_target_wheel
                else "pure-python" if wheels and wheels.platform_independent
                else "needs-wheel"
            )
            print(f"{progress}: {state}", flush=True)

    if ignored:
        print(f"Skipped the PyPI lookup for {len(ignored)} packages; see the report")

    with_wheels = {s.package.lower() for s in statuses if s.has_target_wheel}

    def has_target_wheel(row: dict[str, str]) -> bool:
        return row[cols["package"]].lower() in with_wheels

    unresolved = [row for row in sheet_rows if not has_target_wheel(row)]
    print(
        f"Checking pull requests for {len(unresolved)} of {len(sheet_rows)} packages "
        f"({len(sheet_rows) - len(unresolved)} already have {TARGET_PLATFORM} wheels)"
    )
    cache = check_pull_requests(unresolved, cols["pr"])
    results = [
        evaluate(row, has_target_wheel(row), cache, cols) for row in sheet_rows
    ]
    write_package_lists(args.output_dir, statuses, results)

    report = render_report(results, cols, ignored)
    report_path = os.path.join(args.output_dir, args.report_name)
    write_text(report_path, report)
    print(f"Wrote {report_path} ({len(cache)} pull requests checked)")
    append_step_summary(report)

    if any(s.error for s in statuses):
        raise SystemExit("Some packages could not be queried; see the errors above.")


if __name__ == "__main__":
    main()
