"""Read the package list from the Google Sheet configured in the environment or .env."""

import csv
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse

from common import fetch

SHEET_EXPORT_URL = "https://docs.google.com/spreadsheets/d/{id}/export?format=csv&gid={gid}"
SHEET_GVIZ_URL = "https://docs.google.com/spreadsheets/d/{id}/gviz/tq?{query}"
SHEET_HTMLVIEW_URL = "https://docs.google.com/spreadsheets/d/{id}/htmlview"
SHEET_TAB_RE = re.compile(r'\{name:\s*"((?:[^"\\]|\\.)*)"[^{}]*?gid:\s*"(\d+)"')

STATUS_BUILDS_AVAILABLE = "Builds Available"
STATUS_NEEDS_PR = "Needs PR"
STATUS_PR_UNDER_REVIEW = "PR Under Review"
STATUS_PR_MERGED = "PR Merged"


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


def is_package_row(name: str) -> bool:
    """Whether a sheet package cell names a package rather than being blank or a comment."""
    return bool(name) and not name.startswith("#")


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
    sheet_url: str, sheet_name: str, column: str, gid: str = "", keep_all: bool = False
) -> list[dict[str, str]]:
    """Return the sheet rows that carry a package name, de-duplicated on that name.

    With keep_all, return every data row in sheet order, including blank, comment
    and duplicate rows.
    """
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
        if keep_all or (is_package_row(name) and key not in seen):
            seen.add(key)
            rows.append({k: (v or "").strip() for k, v in row.items() if k})
    return rows


def load_sheet(
    env_path: str, keep_all: bool = False
) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Read the Google Sheet configured in the environment or the .env file.

    Returns the sheet rows and the column names keyed by package, status, pr,
    timeline, priority and engagement. See read_sheet for keep_all.
    """
    env = load_env(env_path)

    def setting(name: str, default: str = "", required: bool = True) -> str:
        value = os.environ.get(name) or env.get(name) or default
        if not value and required:
            raise SystemExit(f"Set {name} in the environment or in {env_path}")
        return value

    sheet_name = setting("GOOGLE_SHEET_NAME")
    cols = {
        "package": setting("GOOGLE_SHEET_COL_PACKAGE_NAME"),
        "status": setting("GOOGLE_SHEET_COL_CURRENT_STATUS", "Current Status"),
        "pr": setting("GOOGLE_SHEET_COL_PR_LINK", "GitHub PR Link"),
        "timeline": setting("GOOGLE_SHEET_COL_TIMELINES", "Timelines"),
        "priority": setting("GOOGLE_SHEET_COL_PRIORITY", "Priority"),
        "engagement": setting("GOOGLE_SHEET_COL_ENGAGEMENT", "Engagement"),
    }
    rows = read_sheet(
        setting("GOOGLE_SHEET_URL"),
        sheet_name,
        cols["package"],
        setting("GOOGLE_SHEET_GID", required=False),
        keep_all,
    )
    print(f"Read {len(rows)} rows from sheet {sheet_name!r}")
    return rows, cols
