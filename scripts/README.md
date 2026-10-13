# Scripts

Run these from the repository root. Sheet-based scripts read `GOOGLE_SHEET_URL`,
`GOOGLE_SHEET_NAME`, `GOOGLE_SHEET_COL_PACKAGE_NAME` and the optional
`GOOGLE_SHEET_GID` from the environment or the repository `.env` file. Outputs go
to `data` by default.

| Script | Purpose |
| --- | --- |
| `fetch_package_status.py` | Writes `test-win_arm64.txt`, `test-win_arm64-buildtools.txt` and `sheet-update-report.md`. Requires `gh`. |
| `report_package_blockers.py` | Writes `package-blockers.tsv` and `package-blocker-report.md`. Requires `packaging`. |
| `Test-PackageInstalls.ps1` | Installs each package from a list and writes `results-<prefix>.csv` and a Markdown log. |
| `consolidate_results.py` | Combines `results-*.csv` with `package-blockers.tsv` into `win_arm64-status-<date>.tsv`. |
| `my_contribs.py` | Writes `my-contribs.md`, a weekly summary of your Windows ARM64 PRs. Requires `gh`. |
| `match_tracking_issue.py` | Writes `tracking-issue-report.md`, matching sheet PRs under review against a tracking issue. Requires `gh`. |

Shared helpers live in `common.py` (paths, HTTP, output), `sheet.py` (Google
Sheet access), `pypi.py` (PyPI lookups and wheel classification) and
`github_cli.py` (`gh` wrappers).

The GitHub workflow runs `fetch_package_status.py` and
`report_package_blockers.py`, tests the package lists on each Python version,
then publishes the combined TSV as the `consolidated-results` artifact.

## Package blocker report

```powershell
python -m pip install -r scripts\requirements.txt
python scripts\report_package_blockers.py
```

It does not require the GitHub CLI or change the sheet. Options:

- `--env`: configuration file (default: repository `.env`).
- `--output-dir`: destination directory (default: `data`).
- `--jobs`: concurrent PyPI requests (default: 3; must be positive).
- `--min-downstream`: omit blockers with fewer downstream packages from the
  Markdown report (default: 2).

Outputs:

- `package-blockers.tsv`: one line per sheet data row, in sheet order, so the
  columns can be pasted alongside the sheet. Blank, `#` comment and duplicate
  rows are kept. `Potential Blockers` lists, comma-separated, the blockers the
  package depends on directly or transitively within the sheet. A package never
  lists itself; the column is empty when no upstream blockers are found. Set
  `GOOGLE_SHEET_GID` so rows hidden by a sheet filter are not omitted.
- `package-blocker-report.md`: ranking, methodology and metadata gaps. Also
  appended to `GITHUB_STEP_SUMMARY` when set.

A blocker is a sheet package whose latest PyPI release has neither a pure-Python
wheel nor a `win_arm64` wheel, using the same wheel checks as
`fetch_package_status.py`. Availability is queried afresh, not inferred from PR
status. Python-version compatibility is not checked.

All declared runtime dependencies count, including extras and conditional
dependencies for other platforms. Constraints are not resolved. The ranking
therefore describes **potential impact**, not verified failures or guaranteed
unlocks. Counts can overlap between blockers.

Transitive paths follow dependencies through sheet packages only, including
packages that already have wheels. Each downstream sheet package is counted once
per blocker, and a blocker never counts itself. External dependency metadata is
not fetched.

Missing `requires_dist` is reported as unknown, not assumed to mean no
dependencies; an explicit empty list means no declared dependencies. Lookup
failures and invalid requirements are printed as warnings and listed in the
report, which is still produced. Unknown wheel availability does not qualify a
package as a blocker. Build dependencies, native libraries and free-text sheet
comments are not inferred.

## Combined results

Download the `results-*` artifacts into a `results` directory, then run:

```powershell
python scripts\consolidate_results.py --input results
```

The output has one line per line of `package-blockers.tsv`, and so one line per
sheet row: `Package` and `Potential Blockers`, then the
`<prefix>_install_status` and `<prefix>_binary_url` columns from each result
file. Packages are matched on their PEP 503 normalized names, so duplicate rows
all receive the results. Blank, comment and untested rows are left empty. Tested
packages that are not in the TSV are printed as a warning. Use `--blockers` and
`--output` to override the input TSV and output path.

## Tests

Run the offline tests, which do not access the sheet, PyPI or GitHub:

```powershell
python -m unittest discover -s scripts -p "test_*.py"
```
