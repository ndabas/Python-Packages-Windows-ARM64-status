"""Package availability in the Anaconda defaults (pkgs/main) win-arm64 channel."""

import urllib.parse
from collections.abc import Iterable

from common import normalize_name

CONDA_REPODATA_URL = "https://repo.anaconda.com/pkgs/main/win-arm64/repodata.json"
CONDA_PACKAGE_URL = "https://anaconda.org/anaconda/{package}"
CONDA_PACKAGES = "conda-packages.txt"

# PyPI names (normalized) whose conda package has a different name; an alias is
# used only when it is in the channel, otherwise the PyPI name is looked up as is.
CONDA_ALIASES = {
    "blis": "cython-blis",
    "duckdb": "python-duckdb",
    "graphviz": "python-graphviz",
    "kaleido": "python-kaleido",
    "lazrs": "lazrs-python",
    "lmdb": "python-lmdb",
    "msgpack": "msgpack-python",
    "opencv-contrib-python": "py-opencv",
    "opencv-python": "py-opencv",
    "opencv-python-headless": "py-opencv",
    "pyqt5": "pyqt",
    "tables": "pytables",
    "torch": "pytorch",
    "xgboost": "py-xgboost",
}


def repodata_names(repodata: dict) -> list[str]:
    """Sorted distinct package names in a conda repodata.json."""
    return sorted(
        {
            info["name"]
            for key in ("packages", "packages.conda")
            for info in (repodata.get(key) or {}).values()
            if info.get("name")
        }
    )


def index_names(names: Iterable[str]) -> dict[str, str]:
    """Conda names keyed by their PEP 503 normalized name.

    Conda names differ from PyPI names only in case and separators for most
    packages; where two conda names normalize alike, the first in sorted order wins.
    """
    index: dict[str, str] = {}
    for name in sorted(names):
        index.setdefault(normalize_name(name), name)
    return index


def read_conda_packages(path: str) -> dict[str, str]:
    """Index the conda package list written by fetch_conda_packages.py."""
    with open(path, encoding="utf-8") as fh:
        return index_names(line.strip() for line in fh if line.strip())


def conda_name(package: str, available: dict[str, str]) -> str:
    """The conda name of a PyPI package in the channel, or '' when it is absent."""
    key = normalize_name(package)
    alias = CONDA_ALIASES.get(key)
    return (alias and available.get(alias)) or available.get(key, "")


def similar_names(package: str, available: dict[str, str]) -> list[str]:
    """Conda names that add a prefix or suffix to a package's normalized name.

    Most conda packages named differently from PyPI do this, such as pytorch for
    torch or msgpack-python for msgpack, so these are alias candidates. The
    addition must be separated by "-", or be a "py" or "python" prefix, so that
    for example ray does not match bitarray.
    """
    key = normalize_name(package)
    glued = {f"py{key}", f"python{key}"}
    return [
        name
        for normalized, name in sorted(available.items())
        if normalized.startswith(f"{key}-") or normalized.endswith(f"-{key}")
        or normalized in glued
    ]


def package_url(name: str) -> str:
    return CONDA_PACKAGE_URL.format(package=urllib.parse.quote(name, safe=""))
