"""Weekly summary of my Windows ARM64 pull requests, plus the forks left to delete.

Uses the GitHub CLI (`gh`) to search for the author's PRs, groups them into ISO
weeks by creation date (opened) and merge date (merged), and writes a markdown
report. Merged PRs whose head repository still exists under the author's account
are listed as forks that still need deleting, and open PRs that no longer merge
cleanly are listed as needing a rebase. Merged PRs that other people have
commented on since the merge are listed, and their comments written to a second
markdown file.
"""

import argparse
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from common import DATA_DIR, write_text
from github_cli import gh_list, require_gh, search_issues
from github_cli import pull_request as fetch_pull_request

SEARCH_TERMS = "windows arm64"
DEFAULT_AUTHOR = "ndabas"
DEFAULT_SINCE = "2026-08-01"


@dataclass
class Comment:
    author: str
    created_at: datetime
    url: str
    body: str
    kind: str


def parse_time(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def week_start(timestamp: str) -> date:
    """Monday of the ISO week the timestamp falls in."""
    day = parse_time(timestamp).date()
    return day - timedelta(days=day.weekday())


def pr_label(item: dict) -> str:
    return f"{repo_slug(item)}#{item['number']}"


def table_cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def repo_slug(item: dict) -> str:
    return item.get("repository_url", "").removeprefix(
        "https://api.github.com/repos/"
    )


def pull_request(item: dict) -> dict:
    """Fetch the full pull request record behind a search result."""
    return fetch_pull_request(repo_slug(item), item["number"])


def surviving_fork(item: dict, author: str, index: int, total: int) -> str:
    """Return the fork slug if the merged PR's head repo still exists, else ''."""
    slug = repo_slug(item)
    number = item["number"]
    head_repo = (pull_request(item).get("head") or {}).get("repo") or {}
    fork = head_repo.get("full_name", "")
    keep = fork if fork.split("/")[0].lower() == author.lower() else ""
    print(f"[{index}/{total}] {slug}#{number}: {keep or 'fork deleted'}", flush=True)
    return keep


def conflict_state(item: dict, index: int, total: int) -> str:
    """Return the mergeable state of an open PR when it conflicts, else ''."""
    slug = repo_slug(item)
    number = item["number"]
    pr = pull_request(item)
    mergeable = pr.get("mergeable")
    state = pr.get("mergeable_state") or "unknown"
    # GitHub computes mergeability lazily, so null means "ask again later".
    conflicted = state if mergeable is False else ""
    status = conflicted or ("not computed yet" if mergeable is None else "mergeable")
    print(f"[{index}/{total}] {slug}#{number}: {status}", flush=True)
    return conflicted


def post_merge_comments(item: dict, author: str, index: int, total: int) -> list[Comment]:
    """Comments, review comments and reviews by others made after the PR merged."""
    slug = repo_slug(item)
    number = item["number"]
    merged_at = parse_time(item["pull_request"]["merged_at"])
    sources = (
        ("comment", f"repos/{slug}/issues/{number}/comments", "created_at"),
        ("review comment", f"repos/{slug}/pulls/{number}/comments", "created_at"),
        ("review", f"repos/{slug}/pulls/{number}/reviews", "submitted_at"),
    )
    comments: list[Comment] = []
    for kind, path, time_field in sources:
        for entry in gh_list(path):
            login = (entry.get("user") or {}).get("login", "")
            body = (entry.get("body") or "").strip()
            timestamp = entry.get(time_field)
            # Reviews holding only inline comments have an empty body of their own.
            if not timestamp or not body or login.lower() == author.lower():
                continue
            created_at = parse_time(timestamp)
            if created_at > merged_at:
                if kind == "review comment" and entry.get("path"):
                    kind_label = f"review comment on `{entry['path']}`"
                else:
                    kind_label = kind
                comments.append(
                    Comment(login, created_at, entry.get("html_url", ""), body, kind_label)
                )
    comments.sort(key=lambda comment: comment.created_at)
    count = f"{len(comments)} comment{'s' if len(comments) != 1 else ''}"
    print(f"[{index}/{total}] {slug}#{number}: {count} after merge", flush=True)
    return comments


def latest_activity_first(
    commented: list[tuple[dict, list[Comment]]],
) -> list[tuple[dict, list[Comment]]]:
    """PRs ordered by their newest comment, most recent first."""
    return sorted(
        commented,
        key=lambda pair: (
            -max(c.created_at for c in pair[1]).timestamp(),
            pr_label(pair[0]).lower(),
        ),
    )


def render_comments_report(
    author: str, commented: list[tuple[dict, list[Comment]]]
) -> str:
    lines = [
        f"# Comments on {author}'s merged {SEARCH_TERMS} pull requests after merging",
        "",
        f"{len(commented)} merged pull requests have comments from others since they "
        "were merged, most recent activity first.",
        "",
    ]
    for item, comments in latest_activity_first(commented):
        merged_at = parse_time(item["pull_request"]["merged_at"])
        latest = max(c.created_at for c in comments)
        lines += [
            f"## [{pr_label(item)}]({item['html_url']}): {table_cell(item.get('title', ''))}",
            "",
            f"Merged {merged_at:%Y-%m-%d %H:%M} UTC; latest comment "
            f"{latest:%Y-%m-%d %H:%M} UTC.",
            "",
        ]
        for comment in comments:
            lines += [
                f"### @{comment.author}, {comment.created_at:%Y-%m-%d %H:%M} UTC "
                f"([{comment.kind}]({comment.url}))",
                "",
            ]
            lines += [f"> {line}".rstrip() for line in comment.body.splitlines()]
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--author", default=DEFAULT_AUTHOR)
    parser.add_argument("--since", default=DEFAULT_SINCE, help="YYYY-MM-DD cut-off")
    parser.add_argument("--output", default=os.path.join(DATA_DIR, "my-contribs.md"))
    parser.add_argument(
        "--comments-output",
        default=os.path.join(DATA_DIR, "my-contribs-post-merge-comments.md"),
        help="Where to write comments made on merged pull requests after merging",
    )
    args = parser.parse_args()

    require_gh()

    base = f"is:pr author:{args.author} archived:false {SEARCH_TERMS}"
    items = search_issues(f"{base} created:>={args.since}")
    # The search API reports merged_at alongside each pull request.
    merged = [item for item in items if (item.get("pull_request") or {}).get("merged_at")]
    open_prs = [item for item in items if item.get("state") == "open"]
    print(
        f"Found {len(items)} pull requests, {len(merged)} of them merged, "
        f"{len(open_prs)} still open"
    )

    weeks: dict[date, dict[str, int]] = {}
    for item in items:
        weeks.setdefault(week_start(item["created_at"]), {"opened": 0, "merged": 0})[
            "opened"
        ] += 1
    for item in merged:
        merged_at = item["pull_request"]["merged_at"] or item["updated_at"]
        weeks.setdefault(week_start(merged_at), {"opened": 0, "merged": 0})[
            "merged"
        ] += 1

    # A fork is still needed while any pull request against that repo is open.
    open_repos = {repo_slug(item) for item in open_prs}
    candidates = [item for item in merged if repo_slug(item) not in open_repos]

    forks: list[tuple[str, str, str]] = []
    for index, item in enumerate(candidates, start=1):
        fork = surviving_fork(item, args.author, index, len(candidates))
        if fork:
            forks.append(
                (fork, f"{repo_slug(item)}#{item['number']}", item["html_url"])
            )

    conflicts: list[tuple[str, str, str]] = []
    for index, item in enumerate(open_prs, start=1):
        state = conflict_state(item, index, len(open_prs))
        if state:
            conflicts.append(
                (f"{repo_slug(item)}#{item['number']}", item["html_url"], state)
            )

    print(f"Checking {len(merged)} merged pull requests for comments after merging")
    commented: list[tuple[dict, list[Comment]]] = []
    for index, item in enumerate(merged, start=1):
        comments = post_merge_comments(item, args.author, index, len(merged))
        if comments:
            commented.append((item, comments))
    commented = latest_activity_first(commented)

    lines = [
        f"# {args.author}'s {SEARCH_TERMS} pull requests since {args.since}",
        "",
        "| Week starting | Opened | Merged |",
        "| --- | ---: | ---: |",
    ]
    for start in sorted(weeks):
        counts = weeks[start]
        lines.append(f"| {start:%Y-%m-%d} | {counts['opened']} | {counts['merged']} |")
    lines += [
        f"| **Total** | **{len(items)}** | **{len(merged)}** |",
        "",
        "## Open pull requests with merge conflicts",
        "",
    ]
    if conflicts:
        lines += [
            f"- [{pr}]({url}) (`{state}`)" for pr, url, state in sorted(set(conflicts))
        ]
    else:
        lines.append("None - every open pull request still merges cleanly.")
    lines += [
        "",
        "## Merged pull requests with comments after merging",
        "",
    ]
    if commented:
        link = os.path.relpath(
            args.comments_output, os.path.dirname(os.path.abspath(args.output))
        ).replace(os.sep, "/")
        lines += [f"See [{os.path.basename(args.comments_output)}]({link}).", ""]
        lines += [
            f"- [{pr_label(item)}]({item['html_url']}) "
            f"({len(comments)} comment{'s' if len(comments) != 1 else ''}, latest "
            f"{max(c.created_at for c in comments):%Y-%m-%d})"
            for item, comments in commented
        ]
    else:
        lines.append("None - nobody else has commented on a merged pull request after merging.")
    lines += [
        "",
        "## Forks still to delete",
        "",
    ]
    if forks:
        lines += [
            f"- [{fork}](https://github.com/{fork}) (merged in [{pr}]({url}))"
            for fork, pr, url in sorted(set(forks))
        ]
    else:
        lines.append("None - every merged pull request's fork has been deleted.")
    lines.append("")

    report = "\n".join(lines)
    write_text(args.output, report)
    if commented:
        write_text(args.comments_output, render_comments_report(args.author, commented))
        print(f"Wrote {args.comments_output} ({len(commented)} pull requests)")
    elif os.path.exists(args.comments_output):
        # Don't leave comments from an earlier run looking current.
        os.remove(args.comments_output)
        print(f"Removed {args.comments_output}; no comments after merging")
    print(
        f"Wrote {args.output} ({len(weeks)} weeks, {len(forks)} forks to delete, "
        f"{len(conflicts)} conflicting open pull requests)"
    )


if __name__ == "__main__":
    main()
