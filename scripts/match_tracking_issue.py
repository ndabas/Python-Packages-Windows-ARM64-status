"""Match the sheet's pull requests under review against a GitHub tracking issue.

Reads the same Google Sheet settings as fetch_package_status.py, keeps the rows whose
status is "PR Under Review" with a "Done" timeline, and writes a markdown report of
those packages that are also listed in the tracking issue's tables. The GitHub CLI
(`gh`) is required to read the issue.
"""

import argparse
import os
import re

from common import DATA_DIR, ROOT, normalize_name, write_text
from github_cli import gh, pr_refs, require_gh
from sheet import STATUS_PR_UNDER_REVIEW, TIMELINE_DONE, load_sheet

DEFAULT_REPO = "khmyznikov/PyEnv-WoA-State"
DEFAULT_ISSUE = "1"
ISSUE_NAME_RE = re.compile(r"^\*\*\[?([^\]*]+?)\]?(?:\([^)]*\))?\*\*$")


def fetch_issue_body(repo: str, number: str) -> str:
    data = gh("issue", "view", number, "-R", repo, "--json", "body")
    assert isinstance(data, dict)
    return data["body"]


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


def render_report(matches: list[dict], repo: str, number: str, total: int) -> str:
    issue_url = f"https://github.com/{repo}/issues/{number}"
    lines = [
        "# Pull requests under review listed in the tracking issue",
        "",
        f"{len(matches)} of {total} sheet rows with status {STATUS_PR_UNDER_REVIEW!r} "
        f"and timeline {TIMELINE_DONE!r} are listed in [{repo}#{number}]({issue_url}).",
        "",
    ]
    if not matches:
        return "\n".join(lines)

    lines += [
        "| Package | Pull requests | PR linked in issue | Issue status | Issue section |",
        "| --- | --- | --- | --- | --- |",
    ]
    for match in matches:
        prs = "<br>".join(
            f"[{url.split('/')[3]}/{url.split('/')[4]}#{url.rsplit('/', 1)[1]}]({url})"
            for url in match["prs"]
        )
        linked = "yes" if set(match["prs"]) & match["issue"]["prs"] else "no"
        lines.append(
            f"| {match['package']} | {prs or '—'} | {linked} | "
            f"{match['issue']['done'] or '—'} | {match['issue']['section'] or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=os.path.join(ROOT, ".env"))
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--issue", default=DEFAULT_ISSUE)
    parser.add_argument(
        "--output", default=os.path.join(DATA_DIR, "tracking-issue-report.md")
    )
    args = parser.parse_args()

    require_gh()
    sheet_rows, cols = load_sheet(args.env)
    under_review = [
        row
        for row in sheet_rows
        if row.get(cols["status"], "").lower() == STATUS_PR_UNDER_REVIEW.lower()
        and row.get(cols["timeline"], "").lower() == TIMELINE_DONE.lower()
    ]
    print(f"{len(under_review)} rows have open pull requests")

    issue = issue_packages(fetch_issue_body(args.repo, args.issue))
    print(f"Read {len(issue)} packages from {args.repo}#{args.issue}")

    matches = [
        {
            "package": row[cols["package"]],
            "prs": [url for url, *_ in pr_refs(row.get(cols["pr"], ""))],
            "issue": issue[normalize_name(row[cols["package"]])],
        }
        for row in under_review
        if normalize_name(row[cols["package"]]) in issue
    ]
    matches.sort(key=lambda match: match["package"].lower())

    report = render_report(matches, args.repo, args.issue, len(under_review))
    write_text(args.output, report)
    print(f"Wrote {args.output} ({len(matches)} packages in both lists)")


if __name__ == "__main__":
    main()
