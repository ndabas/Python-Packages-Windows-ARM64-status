"""Write the package names in the Anaconda pkgs/main win-arm64 channel.

Downloads the channel's repodata.json and writes its distinct package names, one
per line, to conda-packages.txt, which consolidate_results.py uses to fill the
Available on Conda column.
"""

import argparse
import json
import os

from common import DATA_DIR, fetch, write_text
from conda_repo import CONDA_PACKAGES, CONDA_REPODATA_URL, repodata_names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=os.path.join(DATA_DIR, CONDA_PACKAGES),
        help=f"Path of the package list (default: data/{CONDA_PACKAGES})",
    )
    args = parser.parse_args()

    try:
        names = repodata_names(json.loads(fetch(CONDA_REPODATA_URL)))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not read {CONDA_REPODATA_URL}: {exc}") from exc
    if not names:
        raise SystemExit(f"No packages found in {CONDA_REPODATA_URL}")
    write_text(args.output, "".join(f"{name}\n" for name in names))
    print(f"Wrote {args.output} ({len(names)} packages)")


if __name__ == "__main__":
    main()
