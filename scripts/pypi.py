"""PyPI JSON API lookups and win_arm64 wheel classification."""

import csv
import json
import urllib.error
import urllib.parse
from dataclasses import dataclass

from common import fetch, normalize_name

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
TARGET_PLATFORM = "win_arm64"
# hugovk's monthly list of the 15,000 most-downloaded PyPI projects of the last month.
TOP_PYPI_URL = "https://hugovk.dev/top-pypi-packages/top-pypi-packages.csv"


@dataclass(frozen=True)
class TopPackage:
    """A project in the top PyPI packages list."""

    name: str
    rank: int
    downloads: int


def parse_top_packages(text: str) -> dict[str, TopPackage]:
    """Parse the download_count,project CSV, keyed by normalized name and ranked
    from 1 by download count."""
    rows = []
    for row in csv.DictReader(text.splitlines()):
        name = (row.get("project") or "").strip()
        if name:
            rows.append((name, int(row.get("download_count") or 0)))
    rows.sort(key=lambda row: -row[1])
    packages: dict[str, TopPackage] = {}
    for name, downloads in rows:
        packages.setdefault(
            normalize_name(name), TopPackage(name, len(packages) + 1, downloads)
        )
    return packages


def fetch_top_packages(url: str = TOP_PYPI_URL) -> dict[str, TopPackage]:
    packages = parse_top_packages(fetch(url).decode("utf-8-sig"))
    if not packages:
        raise SystemExit(f"No packages read from {url}")
    return packages


@dataclass(frozen=True)
class WheelStatus:
    """Wheels published for a package's latest release."""

    platform_independent: bool
    has_target_wheel: bool

    @property
    def has_wheel(self) -> bool:
        """Whether the release installs on the target platform without a source build."""
        return self.platform_independent or self.has_target_wheel


def fetch_metadata(package: str) -> dict:
    """Fetch the latest release's metadata and distribution files."""
    url = PYPI_JSON_URL.format(package=urllib.parse.quote(package, safe=""))
    return json.loads(fetch(url))


def lookup(package: str) -> tuple[dict | None, str]:
    """Fetch metadata, returning (None, error message) when the lookup fails."""
    try:
        return fetch_metadata(package), ""
    except urllib.error.HTTPError as exc:
        exc.close()
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        return None, str(exc)


def wheel_status(data: dict) -> WheelStatus:
    """Inspect a PyPI response's wheels for platform independence / win_arm64."""
    platform_independent = False
    has_target_wheel = False
    for item in data.get("urls", []):
        if item.get("packagetype") != "bdist_wheel":
            continue
        parts = item.get("filename", "").removesuffix(".whl").split("-")
        if len(parts) < 3:
            continue
        python_tag, abi_tag, platform_tag = parts[-3], parts[-2], parts[-1]
        # Each tag may carry several dot-separated values.
        platform_tags = platform_tag.split(".")
        runs_on_cpython = any(tag.startswith(("py", "cp")) for tag in python_tag.split("."))
        if runs_on_cpython and abi_tag == "none" and "any" in platform_tags:
            platform_independent = True
        if TARGET_PLATFORM in platform_tags:
            has_target_wheel = True
    return WheelStatus(platform_independent, has_target_wheel)
