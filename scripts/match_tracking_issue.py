"""Match the sheet's rows that link your pull requests against a GitHub tracking issue
and the top PyPI packages.

Reads the same Google Sheet settings as fetch_package_status.py and keeps the rows,
whatever their status, that link a pull request opened by the author (the
authenticated GitHub CLI user unless --author is given). Writes a markdown report of
those packages that are also listed in the tracking issue's tables, and of those
that are among the top PyPI packages by downloads. The GitHub CLI (`gh`) is required
to read the issue and the pull requests.
"""

import argparse
import os
import re

from common import DATA_DIR, ROOT, normalize_name, write_text
from github_cli import gh, pr_refs, require_gh, search_issues
from pypi import TOP_PYPI_URL, TopPackage, fetch_top_packages
from sheet import load_sheet

DEFAULT_REPO = "khmyznikov/PyEnv-WoA-State"
DEFAULT_ISSUE = "1"
ISSUE_NAME_RE = re.compile(r"^\*\*\[?([^\]*]+?)\]?(?:\([^)]*\))?\*\*$")


def fetch_issue_body(repo: str, number: str) -> str:
    data = gh("issue", "view", number, "-R", repo, "--json", "body")
    assert isinstance(data, dict)
    return data["body"]


def authenticated_user() -> str:
    data = gh("api", "user")
    assert isinstance(data, dict)
    return data["login"]


def authored_pr_urls(author: str) -> set[str]:
    """Lower-cased URLs of every pull request opened by author."""
    return {item["html_url"].lower() for item in search_issues(f"is:pr author:{author}")}


def issue_packages(body: str) -> dict[str, dict]:
    """Packages from the issue's tables, keyed by normalized name."""
    packages: dict[str, dict] = {}
    section = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip().strip("*").strip()
            continue
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        match = ISSUE_NAME_RE.match(cells[1])
        if not match or match.group(1).strip().upper() == "TEMPLATE":
            continue
        name = match.group(1).strip()
        packages.setdefault(
            normalize_name(name),
            {
                "name": name,
                "section": section,
                "done": cells[0],
                "prs": {url for url, *_ in pr_refs(cells[3])},
            },
        )
    return packages


def authored_rows(
    sheet_rows: list[dict[str, str]], cols: dict[str, str], authored: set[str]
) -> list[dict]:
    """Sheet rows, of any status, that link at least one pull request in authored."""
    rows = []
    for row in sheet_rows:
        prs = [
            url for url, *_ in pr_refs(row.get(cols["pr"], "")) if url.lower() in authored
        ]
        if prs:
            rows.append(
                {
                    "package": row[cols["package"]],
                    "status": row.get(cols["status"], ""),
                    "prs": prs,
                }
            )
    return rows


def match_issue(rows: list[dict], issue: dict[str, dict]) -> list[dict]:
    matches = [
        {**row, "issue": issue[normalize_name(row["package"])]}
        for row in rows
        if normalize_name(row["package"]) in issue
    ]
    return sorted(matches, key=lambda match: match["package"].lower())


def match_top(rows: list[dict], top: dict[str, TopPackage]) -> list[dict]:
    matches = [
        {**row, "top": top[normalize_name(row["package"])]}
        for row in rows
        if normalize_name(row["package"]) in top
    ]
    return sorted(matches, key=lambda match: match["top"].rank)


def format_prs(urls: list[str]) -> str:
    return "<br>".join(
        f"[{url.split('/')[3]}/{url.split('/')[4]}#{url.rsplit('/', 1)[1]}]({url})"
        for url in urls
    ) or "—"


def render_report(
    issue_matches: list[dict],
    top_matches: list[dict],
    repo: str,
    number: str,
    total: int,
    author: str,
    top_url: str,
    top_count: int,
) -> str:
    issue_url = f"https://github.com/{repo}/issues/{number}"
    lines = [
        f"# Pull requests by @{author}",
        "",
        f"{total} sheet rows link a pull request by @{author}.",
        "",
        "## Listed in the tracking issue",
        "",
        f"{len(issue_matches)} of {total} are listed in [{repo}#{number}]({issue_url}).",
        "",
    ]
    if issue_matches:
        lines += [
            "| Package | Sheet status | Pull requests | PR linked in issue | Issue status "
            "| Issue section |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for match in issue_matches:
            linked = "yes" if set(match["prs"]) & match["issue"]["prs"] else "no"
            lines.append(
                f"| {match['package']} | {match['status'] or '—'} | "
                f"{format_prs(match['prs'])} | {linked} | "
                f"{match['issue']['done'] or '—'} | {match['issue']['section'] or '—'} |"
            )
        lines.append("")

    lines += [
        "## Top PyPI packages",
        "",
        f"{len(top_matches)} of {total} are among the [top {top_count:,} PyPI "
        f"packages]({top_url}) by downloads last month.",
        "",
    ]
    if top_matches:
        lines += [
            "| Rank | Package | Downloads | Sheet status | Pull requests |",
            "| --- | --- | --- | --- | --- |",
        ]
        for match in top_matches:
            top = match["top"]
            lines.append(
                f"| {top.rank:,} | {match['package']} | {top.downloads:,} | "
                f"{match['status'] or '—'} | {format_prs(match['prs'])} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=os.path.join(ROOT, ".env"))
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--issue", default=DEFAULT_ISSUE)
    parser.add_argument(
        "--author", help="Pull request author (default: the authenticated gh user)"
    )
    parser.add_argument(
        "--top-pypi", default=TOP_PYPI_URL, help="Top PyPI packages CSV URL"
    )
    parser.add_argument(
        "--output", default=os.path.join(DATA_DIR, "tracking-issue-report.md")
    )
    args = parser.parse_args()

    require_gh()
    author = args.author or authenticated_user()
    sheet_rows, cols = load_sheet(args.env)
    authored = authored_pr_urls(author)
    print(f"Found {len(authored)} pull requests by @{author}")
    rows = authored_rows(sheet_rows, cols, authored)
    print(f"{len(rows)} rows link pull requests by @{author}")

    issue = issue_packages(fetch_issue_body(args.repo, args.issue))
    print(f"Read {len(issue)} packages from {args.repo}#{args.issue}")
    top = fetch_top_packages(args.top_pypi)
    print(f"Read {len(top)} packages from {args.top_pypi}")

    issue_matches = match_issue(rows, issue)
    top_matches = match_top(rows, top)
    report = render_report(
        issue_matches,
        top_matches,
        args.repo,
        args.issue,
        len(rows),
        author,
        args.top_pypi,
        len(top),
    )
    write_text(args.output, report)
    print(
        f"Wrote {args.output} ({len(issue_matches)} in the tracking issue, "
        f"{len(top_matches)} in the top PyPI packages)"
    )


if __name__ == "__main__":
    main()