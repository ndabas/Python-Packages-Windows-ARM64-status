# Scripts

Run these from the repository root. Sheet-based scripts read `GOOGLE_SHEET_URL`,
`GOOGLE_SHEET_NAME`, `GOOGLE_SHEET_COL_PACKAGE_NAME` and the optional
`GOOGLE_SHEET_GID` from the environment or the repository `.env` file. Outputs go
to `data` by default.

| Script | Purpose |
| --- | --- |
| `fetch_package_status.py` | Writes `sheet-update-report.md`, then `test-win_arm64.txt` and `test-win_arm64-buildtools.txt` from each package's updated status (proposed where the report has one, otherwise the sheet's). Requires `gh`. |
| `report_package_blockers.py` | Writes `package-blockers.tsv` and `package-blocker-report.md`. Requires `packaging`. |
| `Test-PackageInstalls.ps1` | Installs each package from a list and writes `results-<prefix>.csv` and a Markdown log. |
| `consolidate_results.py` | Combines `results-*.csv` with `package-blockers.tsv` into `win_arm64-status-<date>.tsv`. |
| `get_consolidated_results.py` | Downloads `win_arm64-status-<date>.tsv` from the latest full workflow run, reordered to match the sheet. Requires `gh`. |
| `my_contribs.py` | Writes `my-contribs.md`, a weekly summary of your Windows ARM64 PRs. Requires `gh`. |
| `match_tracking_issue.py` | Writes `tracking-issue-report.md`, matching sheet PRs under review against a tracking issue. Requires `gh`. |

Shared helpers live in `common.py` (paths, HTTP, output), `sheet.py` (Google
Sheet access), `pypi.py` (PyPI lookups and wheel classification) and
`github_cli.py` (`gh` wrappers).

The GitHub workflow runs `fetch_package_status.py` and
`report_package_blockers.py`, tests the package lists on each Python version,
then publishes the combined TSV as the `consolidated-results` artifact. When run
manually with **quick** checked, the install tests are skipped and the
`consolidated-results` TSV holds only the `Package Name` and `Potential Blockers`
columns, which line up with the first two columns of a full run's TSV.

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
- `package-blocker-report.md`: ranking, methodology and metadata gaps.

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
sheet row. `Package Name` and `Potential Blockers` are always the first two columns,
followed by the `<prefix>_install_status` and `<prefix>_binary_url` columns from
each result file. Packages are matched on their PEP 503 normalized names, so
duplicate rows all receive the results. Blank, comment and untested rows are left
empty. Tested packages that are not in the TSV are printed as a warning. Use
`--no-results` instead of `--input` to write only the first two columns (as the
workflow's quick mode does), and `--blockers` and `--output` to override the
input TSV and output path.

## Fetching the latest results

To fetch the combined TSV from the latest full (non-quick) workflow run, run:

```powershell
python scripts\get_consolidated_results.py --remove-older
```

It requires `gh` and writes to `data`, overwriting an existing copy. Before
writing, it reorders the TSV's rows, matched on package name, to follow the
current sheet, so it can still be pasted after the sheet has been re-sorted.
Sheet packages missing from the TSV are left blank and TSV packages no longer in
the sheet are dropped, with a warning for each. Options:

- `--remove-older`: delete `win_arm64-status-<date>.tsv` files with an earlier
  date.
- `--dry-run`: report what would be written or removed without changing the
  output directory.
- `--branch`: only consider runs on this branch (default: any, including PRs).
- `--skip-reorder`: keep the original order, and don't read the sheet.
- `--env`, `--output-dir`: as for the blocker report.

The TSV's first column is `Package Name` rather than `Package`, so that pasting
it into the sheet does not create a second column with the sheet's own package
column name.

## Tests

Run the offline tests, which do not access the sheet, PyPI or GitHub:

```powershell
python -m unittest discover -s scripts -p "test_*.py"
```
