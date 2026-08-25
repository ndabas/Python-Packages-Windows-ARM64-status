"""Report win_arm64 wheel availability for a package list held in a Google Sheet.

Reads GOOGLE_SHEET_URL / GOOGLE_SHEET_NAME / GOOGLE_SHEET_COL_PACKAGE_NAME from a
.env file, queries the PyPI JSON API for each package, and writes a status CSV plus
the two package lists consumed by the test/build matrices.
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
USER_AGENT = "Python-Packages-Windows-ARM64-status/1.0"
TARGET_PLATFORM = "win_arm64"


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


def sheet_csv_url(sheet_url: str, sheet_name: str) -> str:
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
    if not match:
        raise SystemExit(f"Could not find a spreadsheet id in {sheet_url!r}")
    query = urllib.parse.urlencode({"tqx": "out:csv", "sheet": sheet_name})
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/gviz/tq?{query}"


def read_packages(sheet_url: str, sheet_name: str, column: str) -> list[str]:
    text = fetch(sheet_csv_url(sheet_url, sheet_name)).decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames or column not in reader.fieldnames:
        raise SystemExit(
            f"Column {column!r} not in sheet {sheet_name!r}; found {reader.fieldnames}"
        )

    packages: list[str] = []
    seen: set[str] = set()
    for row in reader:
        name = (row.get(column) or "").strip()
        key = name.lower()
        if name and not name.startswith("#") and key not in seen:
            seen.add(key)
            packages.append(name)
    return packages


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
        if len(parts) < 2:
            continue
        abi_tag, platform_tag = parts[-2], parts[-1]

        if abi_tag == "none":
            platform_independent = True
        # A wheel filename may carry several dot-separated platform tags.
        if TARGET_PLATFORM in platform_tag.split("."):
            arm64_wheels.append(item["filename"])
        if platform_tag != "any":
            platform_specific = True

    has_arm64 = bool(arm64_wheels)
    row["platform_independent"] = str(platform_independent).lower()
    row[f"has_{TARGET_PLATFORM}"] = str(has_arm64).lower()
    row[f"{TARGET_PLATFORM}_wheels"] = " ".join(arm64_wheels)
    if platform_independent and platform_specific and not has_arm64:
        row["needs_platform_specific"] = "true"
    return row


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
    args = parser.parse_args()

    env = load_env(args.env)

    def setting(name: str) -> str:
        value = os.environ.get(name) or env.get(name)
        if not value:
            raise SystemExit(f"Set {name} in the environment or in {args.env}")
        return value

    sheet_url = setting("GOOGLE_SHEET_URL")
    sheet_name = setting("GOOGLE_SHEET_NAME")
    column = setting("GOOGLE_SHEET_COL_PACKAGE_NAME")

    packages = read_packages(sheet_url, sheet_name, column)
    print(f"Read {len(packages)} packages from sheet {sheet_name!r}")

    rows = []
    for index, package in enumerate(packages, start=1):
        row = classify(package)
        rows.append(row)
        status = row["error"] or (
            f"{TARGET_PLATFORM}" if row[f"has_{TARGET_PLATFORM}"] == "true"
            else "pure-python" if row["platform_independent"] == "true"
            else "needs build"
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

    if any(r["error"] for r in rows):
        print("Some packages could not be queried; see the error column.", file=sys.stderr)


if __name__ == "__main__":
    main()
