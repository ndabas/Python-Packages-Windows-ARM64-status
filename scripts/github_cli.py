"""GitHub access through the GitHub CLI (`gh`)."""

import json
import re
import shutil
import subprocess

from common import ROOT

PR_URL_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", re.I)


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command, decoding its output as UTF-8 rather than the console codepage."""
    return subprocess.run(
        command,
        capture_output=True,
        check=check,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
    )


def require_gh() -> None:
    if not shutil.which("gh"):
        raise SystemExit("The GitHub CLI (gh) is required; see https://cli.github.com")


def gh(*args: str) -> dict | list:
    """Run a gh command that prints JSON and return the parsed result."""
    result = run(["gh", *args], check=False)
    if result.returncode != 0:
        message = (result.stderr.strip().splitlines() or ["gh failed"])[-1]
        raise SystemExit(f"gh {' '.join(args)}: {message}")
    return json.loads(result.stdout)


def pull_request(repo: str, number: str | int) -> dict:
    """Fetch a pull request; repo is an owner/name slug."""
    data = gh("api", f"repos/{repo}/pulls/{number}")
    assert isinstance(data, dict)
    return data


def search_issues(query: str) -> list[dict]:
    """Page through the issue search API and return every matching item."""
    items: list[dict] = []
    page = 1
    while True:
        data = gh(
            "api",
            "-X",
            "GET",
            "search/issues",
            "-f",
            f"q={query}",
            "-F",
            "per_page=100",
            "-F",
            f"page={page}",
        )
        assert isinstance(data, dict)
        batch = data.get("items", [])
        items.extend(batch)
        # The search API caps out at 1000 results.
        if len(batch) < 100 or len(items) >= min(data.get("total_count", 0), 1000):
            return items
        page += 1


def pr_refs(links: str) -> list[tuple[str, str, str, str]]:
    """Every GitHub PR URL found in a whitespace separated cell, split into parts."""
    return [
        (f"https://github.com/{owner}/{repo}/pull/{number}", owner, repo, number)
        for owner, repo, number in PR_URL_RE.findall(links)
    ]
