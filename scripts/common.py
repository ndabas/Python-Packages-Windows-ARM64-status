"""Small helpers shared by the scripts in this directory."""

import csv
import os
import re
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
USER_AGENT = "Python-Packages-Windows-ARM64-status/1.0"
# Written by fetch_package_status.py and read by consolidate_results.py.
OPEN_PR_DATES = "open-pr-dates.tsv"
PR_OPENED_COLUMN = "PR Opened"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def normalize_name(name: str) -> str:
    """PEP 503 normalized package name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def write_tsv(path: str, rows: list[list[str]]) -> None:
    """Write rows as TSV without quoting, so formulas such as =HYPERLINK("...") paste
    into Google Sheets intact; csv raises an error for fields containing tabs or newlines.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(
            fh, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE, quotechar=None
        ).writerows(rows)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def append_step_summary(text: str) -> None:
    """Append markdown to the GitHub Actions job summary, when running under Actions."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
