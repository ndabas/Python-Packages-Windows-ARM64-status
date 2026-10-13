"""Rank sheet packages without wheels by their potential downstream impact.

Collect the latest PyPI release's runtime requirements for every sheet package.
Include all extras and environment markers without resolving versions. Count
unique direct and transitive dependents within the sheet, then rank only packages
without a pure-Python or win_arm64 wheel. Unknown availability is not a blocker.
Write a TSV of potential upstream blockers for each sheet row and a Markdown
report that omits blockers with fewer than --min-downstream downstream packages,
leaving the sheet untouched.
"""

import argparse
import csv
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from packaging.requirements import InvalidRequirement, Requirement

from common import DATA_DIR, ROOT, append_step_summary, normalize_name, write_text
from pypi import lookup, wheel_status
from sheet import is_package_row, load_sheet

BLOCKERS_TSV = "package-blockers.tsv"
REPORT_NAME = "package-blocker-report.md"

# Sheet columns copied into the Markdown report, as (load_sheet key, table header).
SHEET_COLUMNS = (
    ("priority", "Priority"),
    ("status", "Current status"),
    ("engagement", "Engagement"),
)


@dataclass
class PackageInfo:
    package: str
    has_wheel: bool | None = None
    requirements: list[str] | None = None
    dependencies: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass
class Blocker:
    package: str
    direct_dependents: list[str]
    downstream_packages: list[str]


def collect_package(package: str) -> PackageInfo:
    """Fetch wheel status and parse requirements in a single PyPI request."""
    result = PackageInfo(package)
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", package):
        result.issues.append("Invalid PyPI package name")
        return result
    data, error = lookup(package)
    if data is None:
        result.issues.append(f"PyPI lookup: {error}")
        return result

    result.has_wheel = wheel_status(data).has_wheel
    requirements = data.get("info", {}).get("requires_dist")
    if requirements is None:
        result.issues.append("requires_dist unavailable; dependency information unknown")
        return result
    if not isinstance(requirements, list) or not all(
        isinstance(item, str) for item in requirements
    ):
        result.issues.append("Invalid requires_dist metadata; dependency information unknown")
        return result
    result.requirements = requirements
    dependencies: set[str] = set()
    for requirement in requirements:
        try:
            dependencies.add(normalize_name(Requirement(requirement).name))
        except InvalidRequirement as exc:
            result.issues.append(f"Invalid requirement {requirement!r}: {exc}")
    result.dependencies = sorted(dependencies)
    return result


def rank_blockers(packages: list[PackageInfo]) -> list[Blocker]:
    """Reverse the sheet graph and count each downstream package at most once."""
    by_name = {normalize_name(p.package): p for p in packages}
    dependents: dict[str, set[str]] = {name: set() for name in by_name}
    for name, package in by_name.items():
        for dependency in package.dependencies:
            if dependency in dependents and dependency != name:
                dependents[dependency].add(name)

    def display_names(names: set[str]) -> list[str]:
        return sorted((by_name[name].package for name in names), key=str.casefold)

    blockers = []
    for name, package in by_name.items():
        if package.has_wheel is not False:
            continue
        visited = {name}
        pending = list(dependents[name])
        while pending:
            dependent = pending.pop()
            if dependent not in visited:
                visited.add(dependent)
                pending.extend(dependents[dependent] - visited)
        visited.remove(name)
        blockers.append(
            Blocker(package.package, display_names(dependents[name]), display_names(visited))
        )
    return sorted(
        blockers,
        key=lambda b: (
            -len(b.downstream_packages), -len(b.direct_dependents), b.package.casefold()
        ),
    )


def upstream_blockers(
    packages: list[PackageInfo], blockers: list[Blocker]
) -> dict[str, list[str]]:
    """Map normalized package names to the blockers they depend on, directly or not."""
    result: dict[str, list[str]] = {normalize_name(p.package): [] for p in packages}
    for blocker in blockers:
        for package in blocker.downstream_packages:
            result.setdefault(normalize_name(package), []).append(blocker.package)
    return {name: sorted(found, key=str.casefold) for name, found in result.items()}


def markdown_cell(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace(
        "|", "&#124;"
    ).replace("\r", " ").replace("\n", " ")


def render_report(
    packages: list[PackageInfo],
    blockers: list[Blocker],
    generated_at: str,
    sheet_fields: dict[str, dict[str, str]] | None = None,
    min_downstream: int = 0,
) -> str:
    """Render the Markdown report; sheet_fields maps normalized names to sheet values."""
    sheet_fields = sheet_fields or {}
    shown = [b for b in blockers if len(b.downstream_packages) >= min_downstream]
    known = sum(p.requirements is not None for p in packages)
    unknown_wheels = sum(p.has_wheel is None for p in packages)
    lines = [
        "# Package blocker report",
        "",
        f"Generated: {generated_at}",
        "",
        f"{len(packages)} sheet packages queried; {known} have dependency metadata; "
        f"{len(blockers)} lack wheels; {unknown_wheels} have unknown wheel availability.",
        "",
        "## Scope",
        "",
        "- Blockers are sheet packages whose latest PyPI release has neither a "
        "pure-Python wheel nor a win_arm64 wheel. Wheel availability is not Python-version-specific.",
        "- All declared runtime dependencies are included, even optional extras and "
        "dependencies conditional on other platforms or Python versions. Version constraints "
        "are not resolved. These counts describe **potential impact**, "
        "not confirmed installation failures or guaranteed unlocks.",
        "- Direct dependents and downstream packages are counted only within the sheet. "
        "Transitive paths pass through sheet packages, including those that already have wheels. "
        "External dependencies are not queried.",
        "- Each downstream package is counted once per blocker; the blocker itself is "
        "excluded, including in cycles. Counts for different blockers can overlap.",
        "- Missing metadata and lookup failures leave gaps in the graph. Unknown wheel "
        "availability is excluded from the blocker list. Build dependencies and sheet "
        "comments are not inferred.",
        "",
        "## Ranked blockers",
        "",
    ]
    if min_downstream > 0:
        lines += [
            f"Showing {len(shown)} blockers with at least {min_downstream} downstream "
            f"packages; {len(blockers) - len(shown)} with fewer are omitted "
            "(lower --min-downstream to include them).",
            "",
        ]
    lines += [
        "| Rank | Blocker | "
        + "".join(f"{header} | " for _, header in SHEET_COLUMNS)
        + "Direct dependents | Downstream packages | Affected packages |",
        "| --- | --- | " + "--- | " * len(SHEET_COLUMNS) + "---: | ---: | --- |",
    ]
    for index, blocker in enumerate(shown, start=1):
        fields = sheet_fields.get(normalize_name(blocker.package), {})
        sheet_cells = "".join(
            f"{markdown_cell(fields.get(key, '')) or '-'} | " for key, _ in SHEET_COLUMNS
        )
        lines.append(
            f"| {index} | {markdown_cell(blocker.package)} | {sheet_cells}"
            f"{len(blocker.direct_dependents)} | {len(blocker.downstream_packages)} | "
            f"{markdown_cell(', '.join(blocker.downstream_packages)) or '-'} |"
        )
    if not blockers:
        lines += ["", "No blockers identified from known wheel availability."]
    elif not shown:
        lines += ["", f"No blockers have at least {min_downstream} downstream packages."]
    lines += ["", "## Metadata gaps and errors", ""]
    incomplete = [p for p in packages if p.issues]
    if incomplete:
        lines += ["| Package | Issue |", "| --- | --- |"]
        for package in incomplete:
            for issue in package.issues:
                lines.append(f"| {markdown_cell(package.package)} | {markdown_cell(issue)} |")
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def write_blockers_tsv(
    path: str,
    packages: list[PackageInfo],
    blockers: list[Blocker],
    sheet_names: list[str] | None = None,
) -> None:
    """Write one line per sheet row; sheet_names holds every row's package cell, in order."""
    upstream = upstream_blockers(packages, blockers)
    if sheet_names is None:
        sheet_names = [p.package for p in packages]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(["Package", "Potential Blockers"])
        for name in sheet_names:
            found = upstream.get(normalize_name(name), []) if is_package_row(name) else []
            writer.writerow([name, ", ".join(found)])


def write_outputs(
    output_dir: str,
    packages: list[PackageInfo],
    blockers: list[Blocker],
    sheet_fields: dict[str, dict[str, str]] | None = None,
    min_downstream: int = 0,
    sheet_names: list[str] | None = None,
) -> None:
    os.makedirs(output_dir, exist_ok=True)
    tsv_path = os.path.join(output_dir, BLOCKERS_TSV)
    write_blockers_tsv(tsv_path, packages, blockers, sheet_names)
    report_path = os.path.join(output_dir, REPORT_NAME)
    generated_at = datetime.now(timezone.utc).isoformat()
    report = render_report(packages, blockers, generated_at, sheet_fields, min_downstream)
    write_text(report_path, report)
    for path in (tsv_path, report_path):
        print(f"Wrote {path}")
    append_step_summary(report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=os.path.join(ROOT, ".env"))
    parser.add_argument("--output-dir", default=DATA_DIR)
    parser.add_argument("--jobs", type=int, default=3, help="Number of simultaneous PyPI requests")
    parser.add_argument(
        "--min-downstream", type=int, default=2,
        help="Omit blockers with fewer downstream packages from the Markdown report",
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if args.min_downstream < 0:
        parser.error("--min-downstream must not be negative")

    # Keep every row so package-blockers.tsv lines up with the sheet.
    sheet_rows, cols = load_sheet(args.env, keep_all=True)
    sheet_names = [row.get(cols["package"], "") for row in sheet_rows]
    names: dict[str, str] = {}
    sheet_fields: dict[str, dict[str, str]] = {}
    for row in sheet_rows:
        name = row.get(cols["package"], "")
        if not is_package_row(name):
            continue
        names.setdefault(normalize_name(name), name)
        sheet_fields.setdefault(
            normalize_name(name),
            {key: row.get(cols[key], "") for key, _ in SHEET_COLUMNS if key in cols},
        )
    packages = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for index, package in enumerate(pool.map(collect_package, names.values()), start=1):
            packages.append(package)
            state = (
                "wheel availability unknown" if package.has_wheel is None
                else "wheels available" if package.has_wheel else "blocker"
            )
            print(f"[{index}/{len(names)}] {package.package}: {state}", flush=True)
            for issue in package.issues:
                print(f"Warning: {package.package}: {issue}", file=sys.stderr, flush=True)
    blockers = rank_blockers(packages)
    write_outputs(
        args.output_dir, packages, blockers, sheet_fields, args.min_downstream, sheet_names
    )


if __name__ == "__main__":
    main()
