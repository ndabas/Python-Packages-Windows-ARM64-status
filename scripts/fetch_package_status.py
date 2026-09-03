"""Report win_arm64 wheel availability for a package list held in a Google Sheet.

Reads GOOGLE_SHEET_URL / GOOGLE_SHEET_NAME / GOOGLE_SHEET_COL_PACKAGE_NAME (and the
optional GOOGLE_SHEET_GID) from a .env file, queries the PyPI JSON API for each
package, and writes a status CSV plus
the two package lists consumed by the test/build matrices. It then compares the
sheet against PyPI and the linked GitHub pull requests and writes a markdown report
of the rows that need updating (the sheet itself is never modified). The GitHub CLI
(`gh`) is required for the pull request lookups.
"""

import argparse
import csv
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
USER_AGENT = "Python-Packages-Windows-ARM64-status/1.0"
TARGET_PLATFORM = "win_arm64"
SHEET_EXPORT_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=csv&gid={gid}"
SHEET_GVIZ_URL = "https://docs.google.com/spreadsheets/d/{id}/gviz/tq?{query}"
SHEET_HTMLVIEW_URL = "https://docs.google.com/spreadsheets/d/{id}/htmlview"
SHEET_TAB_RE = re.compile(r'\{name:\s*"((?:[^"\\]|\\.)*)"[^{}]*?gid:\s*"(\d+)"')
PR_URL_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", re.I)

STATUS_BUILDS_AVAILABLE = "Builds Available"
STATUS_PR_UNDER_REVIEW = "PR Under Review"
STATUS_PR_MERGED = "PR Merged"
TIMELINE_DONE = "Done"


def load_env(path: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file."""
    values: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    return values


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, decoding its output as UTF-8 rather than the console codepage."""
    return subprocess.run(
        command,
        capture_output=True,
        check=check,
        encoding="utf-8",
        errors="replace",
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )


def spreadsheet_id(sheet_url: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
    if not match:
        raise SystemExit(f"Could not find a spreadsheet id in {sheet_url!r}")
    return match.group(1)


def find_gid(sheet_id: str, sheet_name: str) -> str:
    """Map a tab name to its gid using the public htmlview page; "" when unknown."""
    try:
        page = fetch(SHEET_HTMLVIEW_URL.format(id=sheet_id)).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError):
        return ""
    for name, gid in SHEET_TAB_RE.findall(page):
        try:
            name = json.loads(f'"{name}"')
        except json.JSONDecodeError:
            pass
        if html.unescape(name).strip().lower() == sheet_name.strip().lower():
            return gid
    return ""


def sheet_csv_url(sheet_url: str, sheet_name: str, gid: str = "") -> str:
    """CSV endpoint for the sheet.

    The export endpoint returns every row, while the gviz endpoint used as a fallback
    honours any filter applied to the tab and so can hide rows.
    """
    sheet_id = spreadsheet_id(sheet_url)
    gid = gid or find_gid(sheet_id, sheet_name)
    if gid:
        return SHEET_EXPORT_URL.format(id=sheet_id, gid=gid)
    print(
        f"Could not resolve a gid for sheet {sheet_name!r}; falling back to the gviz "
        "endpoint, which omits rows hidden by a filter. Set GOOGLE_SHEET_GID to avoid "
        "this.",
        file=sys.stderr,
    )
    query = urllib.parse.urlencode({"tqx": "out:csv", "sheet": sheet_name})
    return SHEET_GVIZ_URL.format(id=sheet_id, query=query)


def read_sheet(
    sheet_url: str, sheet_name: str, column: str, gid: str = ""
) -> list[dict[str, str]]:
    """Return the sheet rows that carry a package name, de-duplicated on that name."""
    text = fetch(sheet_csv_url(sheet_url, sheet_name, gid)).decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames or column not in reader.fieldnames:
        raise SystemExit(
            f"Column {column!r} not in sheet {sheet_name!r}; found {reader.fieldnames}"
        )

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in reader:
        name = (row.get(column) or "").strip()
        key = name.lower()
        if name and not name.startswith("#") and key not in seen:
            seen.add(key)
            rows.append({k: (v or "").strip() for k, v in row.items() if k})
    return rows


def classify(package: str) -> dict[str, str]:
    """Inspect the latest release's wheels for platform independence / win_arm64."""
    row = {
        "package": package,
        "version": "",
        "platform_independent": "",
        f"has_{TARGET_PLATFORM}": "",
        "needs_platform_specific": "",
        f"{TARGET_PLATFORM}_wheels": "",
        "error": "",
    }

    url = PYPI_JSON_URL.format(package=urllib.parse.quote(package, safe=""))
    try:
        data = json.loads(fetch(url))
    except urllib.error.HTTPError as exc:
        row["error"] = f"HTTP {exc.code}"
        return row
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        row["error"] = str(exc)
        return row

    row["version"] = data.get("info", {}).get("version", "")

    platform_independent = False
    platform_specific = False
    arm64_wheels: list[str] = []

    for item in data.get("urls", []):
        if item.get("packagetype") != "bdist_wheel":
            continue
        parts = item.get("filename", "").removesuffix(".whl").split("-")
        if len(parts) < 3:
            continue
        python_tag, abi_tag, platform_tag = parts[-3], parts[-2], parts[-1]
        # Each tag may carry several dot-separated values.
        python_tags = python_tag.split(".")
        platform_tags = platform_tag.split(".")
        runs_on_cpython = any(
            tag.startswith(("py", "cp")) for tag in python_tags
        )

        if runs_on_cpython and abi_tag == "none" and "any" in platform_tags:
            platform_independent = True
        else:
            platform_specific = True
        if TARGET_PLATFORM in platform_tags:
            arm64_wheels.append(item["filename"])

    has_arm64 = bool(arm64_wheels)
    row["platform_independent"] = str(platform_independent).lower()
    row[f"has_{TARGET_PLATFORM}"] = str(has_arm64).lower()
    row[f"{TARGET_PLATFORM}_wheels"] = " ".join(arm64_wheels)
    if platform_independent and platform_specific and not has_arm64:
        row["needs_platform_specific"] = "true"
    return row


def repository_owner() -> str:
    """Owner of the repository this script belongs to."""
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in slug:
        return slug.split("/", 1)[0]
    try:
        url = run(["git", "remote", "get-url", "origin"]).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    match = re.search(r"github\.com[:/]([^/\s]+)/", url)
    return match.group(1) if match else ""


def fetch_pull_request(owner: str, repo: str, number: str) -> dict:
    """Fetch a pull request through the gh CLI."""
    endpoint = f"repos/{owner}/{repo}/pulls/{number}"
    result = run(["gh", "api", endpoint], check=False)
    if result.returncode != 0:
        message = (result.stderr.strip().splitlines() or ["gh api failed"])[-1]
        raise SystemExit(f"gh api {endpoint}: {message}")
    return json.loads(result.stdout)


def pr_refs(links: str) -> list[tuple[str, str, str, str]]:
    """Every GitHub PR URL found in a whitespace separated cell, split into parts."""
    return [
        (f"https://github.com/{owner}/{repo}/pull/{number}", owner, repo, number)
        for owner, repo, number in PR_URL_RE.findall(links)
    ]


def check_pull_requests(sheet_rows: list[dict[str, str]], column: str) -> dict[str, dict]:
    """Look up every distinct PR linked from the sheet, reporting progress."""
    refs: dict[str, tuple[str, str, str]] = {}
    for row in sheet_rows:
        for url, owner, repo, number in pr_refs(row.get(column, "")):
            refs.setdefault(url, (owner, repo, number))

    cache: dict[str, dict] = {}
    for index, (url, (owner, repo, number)) in enumerate(refs.items(), start=1):
        data = fetch_pull_request(owner, repo, number)
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
    status_row: dict[str, str] | None,
    owner: str,
    cache: dict[str, dict],
    cols: dict[str, str],
) -> dict:
    """Work out the status/timeline the sheet should show for one package."""
    # Rows with wheels on PyPI are settled, so their PRs are never looked up.
    prs = [cache[url] for url, *_ in pr_refs(sheet_row.get(cols["pr"], "")) if url in cache]

    if status_row and status_row[f"has_{TARGET_PLATFORM}"] == "true":
        status = STATUS_BUILDS_AVAILABLE
    elif any(pr["state"] == "open" for pr in prs):
        status = STATUS_PR_UNDER_REVIEW
    elif any(pr["merged"] for pr in prs):
        status = STATUS_PR_MERGED
    else:
        status = ""

    timeline = TIMELINE_DONE if owner and any(
        pr["author"].lower() == owner.lower() for pr in prs
    ) else ""

    return {
        "package": sheet_row[cols["package"]],
        "current_status": sheet_row.get(cols["status"], ""),
        "proposed_status": status,
        "current_timeline": sheet_row.get(cols["timeline"], ""),
        "proposed_timeline": timeline,
        "prs": prs,
    }


def render_report(results: list[dict], owner: str, cols: dict[str, str]) -> str:
    """Render the rows whose status or timeline differs from the sheet."""
    changes = [
        r
        for r in results
        if (r["proposed_status"] and r["proposed_status"] != r["current_status"])
        or (r["proposed_timeline"] and r["proposed_timeline"] != r["current_timeline"])
    ]

    lines = [
        "# Google Sheet update report",
        "",
        f"Repository owner used for the {cols['timeline']!r} check: "
        f"{owner or '(unknown)'}",
        "",
        f"{len(changes)} of {len(results)} rows need updating. "
        "The sheet is not modified by this report.",
        "",
    ]

    if changes:
        lines += [
            f"| Package | {cols['status']} (sheet) | {cols['status']} (proposed) | "
            f"{cols['timeline']} (sheet) | {cols['timeline']} (proposed) | Pull requests |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in changes:
            status = (
                row["proposed_status"]
                if row["proposed_status"] != row["current_status"]
                else "—"
            )
            timeline = (
                row["proposed_timeline"]
                if row["proposed_timeline"] != row["current_timeline"]
                else "—"
            )
            prs = "<br>".join(
                f"[#{pr['url'].rsplit('/', 1)[1]}]({pr['url']}) "
                f"({'merged' if pr['merged'] else pr['state']}"
                f"{', @' + pr['author'] if pr['author'] else ''})"
                for pr in row["prs"]
            )
            lines.append(
                f"| {row['package']} | {row['current_status'] or '—'} | {status} | "
                f"{row['current_timeline'] or '—'} | {timeline} | {prs or '—'} |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=os.path.join(root, ".env"))
    parser.add_argument(
        "--output-dir",
        default=os.path.join(root, "data"),
        help="Directory for the CSV and package list files",
    )
    parser.add_argument("--csv-name", default=f"{TARGET_PLATFORM}-status.csv")
    parser.add_argument("--report-name", default="sheet-update-report.md")
    args = parser.parse_args()

    if not shutil.which("gh"):
        raise SystemExit("The GitHub CLI (gh) is required; see https://cli.github.com")

    env = load_env(args.env)

    def setting(name: str, default: str = "", required: bool = True) -> str:
        value = os.environ.get(name) or env.get(name) or default
        if not value and required:
            raise SystemExit(f"Set {name} in the environment or in {args.env}")
        return value

    sheet_url = setting("GOOGLE_SHEET_URL")
    sheet_name = setting("GOOGLE_SHEET_NAME")
    gid = setting("GOOGLE_SHEET_GID", required=False)
    cols = {
        "package": setting("GOOGLE_SHEET_COL_PACKAGE_NAME"),
        "status": setting("GOOGLE_SHEET_COL_CURRENT_STATUS", "Current Status"),
        "pr": setting("GOOGLE_SHEET_COL_PR_LINK", "GitHub PR Link"),
        "timeline": setting("GOOGLE_SHEET_COL_TIMELINES", "Timelines"),
    }

    sheet_rows = read_sheet(sheet_url, sheet_name, cols["package"], gid)
    packages = [row[cols["package"]] for row in sheet_rows]
    print(f"Read {len(packages)} packages from sheet {sheet_name!r}")

    rows = []
    for index, package in enumerate(packages, start=1):
        row = classify(package)
        rows.append(row)
        status = row["error"] or (
            f"{TARGET_PLATFORM}" if row[f"has_{TARGET_PLATFORM}"] == "true"
            else "pure-python" if row["platform_independent"] == "true"
            else "needs-wheel"
        )
        print(f"[{index}/{len(packages)}] {package}: {status}", flush=True)

    os.makedirs(args.output_dir, exist_ok=True)
    csv_path = os.path.join(args.output_dir, args.csv_name)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["package"])
        writer.writeheader()
        writer.writerows(rows)

    testable = [
        r["package"]
        for r in rows
        if r["platform_independent"] == "true" or r[f"has_{TARGET_PLATFORM}"] == "true"
    ]
    buildable = [r["package"] for r in rows if r["package"] not in set(testable)]

    test_path = os.path.join(args.output_dir, f"test-{TARGET_PLATFORM}.txt")
    build_path = os.path.join(args.output_dir, f"test-{TARGET_PLATFORM}-buildtools.txt")
    for path, names in ((test_path, testable), (build_path, buildable)):
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(f"{name}\n" for name in names))

    print(f"Wrote {csv_path} ({len(rows)} packages)")
    print(f"Wrote {test_path} ({len(testable)} packages)")
    print(f"Wrote {build_path} ({len(buildable)} packages)")

    owner = repository_owner()
    by_package = {r["package"].lower(): r for r in rows}
    unresolved = [
        row
        for row in sheet_rows
        if (by_package.get(row[cols["package"]].lower()) or {}).get(
            f"has_{TARGET_PLATFORM}"
        )
        != "true"
    ]
    print(
        f"Checking pull requests for {len(unresolved)} of {len(sheet_rows)} packages "
        f"({len(sheet_rows) - len(unresolved)} already have {TARGET_PLATFORM} wheels)"
    )
    cache = check_pull_requests(unresolved, cols["pr"])
    results = [
        evaluate(row, by_package.get(row[cols["package"]].lower()), owner, cache, cols)
        for row in sheet_rows
    ]

    report = render_report(results, owner, cols)
    report_path = os.path.join(args.output_dir, args.report_name)
    with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report)
    print(f"Wrote {report_path} ({len(cache)} pull requests checked)")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report + "\n")

    if any(r["error"] for r in rows):
        raise SystemExit("Some packages could not be queried; see the error column.")


if __name__ == "__main__":
    main()
