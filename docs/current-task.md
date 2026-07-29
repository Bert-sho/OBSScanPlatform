# Current Task

## Current task title

Add configurable Parquet bucket overview output

## Current branch

`codex/parquet-overview`

## Task status

`wip`

The requested implementation and focused validation are complete. Repository
policy prevents marking the task `completed` because the full Windows suite
still has five pre-existing environment/portability failures; the feature
suite has 193 passing tests and the Parquet API checks have 7 passing tests
with one Windows symlink test skipped.

## User goal

Add a global YAML setting that selects mutually exclusive CSV or Parquet
bucket overviews, defaulting to Parquet. Preserve the existing CSV behavior.
Write Snappy Parquet parts with at most 50,000 rows, the required ten-field
non-null schema, one-directory object attribution, configurable cutoff depth
(default 4), configurable extension categories, and manifest-authorized part
downloads.

## Completed work

- Added global `scan.overview_format`, `scan.max_depth`, and
  `scan.file_type_map` settings. Parquet and depth 4 are the defaults; YAML
  type-map entries merge over the exact built-in mapping.
- Preserved the legacy CSV aggregation path unchanged when
  `overview_format: csv` is selected.
- Added a bounded external Parquet aggregator that:
  - assigns each object to exactly one path;
  - truncates deeper objects into the configured cutoff directory;
  - calculates count, byte totals, maximum size, latest UTC date, current path
    depth, and sorted JSON file categories;
  - uses the UTC scan-start date when every contributing timestamp is absent
    or invalid;
  - writes Snappy parts of at most 50,000 rows;
  - writes one typed zero-row part for an empty bucket;
  - stages and atomically publishes each bucket output directory.
- Added manifest fields `overview_format`, `overview_path`, and
  `overview_files`; retained `csv_path` only for CSV compatibility.
- Added a manifest-authorized Parquet part download API with traversal and
  symlink protections.
- Added TDD coverage for configuration, aggregation semantics, exact schema,
  compression, splitting, empty buckets, publication failure, scanner
  dispatch, manifests, and downloads.
- Updated example YAML, English/Chinese README files, operator guide, package
  description, architecture notes, approved design, and implementation plan.
- Completed local whole-change review; no Critical or Important issue remains.

## Remaining work

- No implementation work remains for the requested feature.
- The five unrelated Windows full-suite failures require a separate
  portability task if repository-wide validation must become green.
- A representative live OBS scan remains the recommended operational check.

## Key files changed

- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/models.py`
- `src/obs_scan_platform/parquet_aggregation.py`
- `src/obs_scan_platform/scanner.py`
- `src/obs_scan_platform/api.py`
- `tests/test_config.py`
- `tests/test_parquet_aggregation.py`
- `tests/test_scanner.py`
- `tests/test_api.py`
- `tests/test_scan_end_to_end.py`
- `pyproject.toml`
- `config/apps.example.yaml`
- `README.md`
- `README.zh-CN.md`
- `CLAUDE.md`
- `docs/scan-start-guide.md`
- `docs/superpowers/specs/2026-07-29-parquet-overview-design.md`
- `docs/superpowers/plans/2026-07-29-parquet-overview.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_parquet_aggregation.py tests/test_aggregation.py tests/test_external_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_api.py -k parquet -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
```

## Validation result

- Fresh feature suite: `193 passed in 5.02s`.
- Fresh Parquet API suite: `7 passed, 1 skipped, 19 deselected in 1.03s`;
  the skip is the expected Windows symlink privilege case.
- Fresh compilation: exited `0`.
- Fresh full suite: `256 passed, 5 failed, 1 skipped, 1 warning in 6.46s`.
  The five failures exactly match the pre-task Windows baseline: two symlink
  privilege failures, CSV CRLF response normalization, backslash path
  semantics, and CLI path-separator formatting.
- `git diff --check` reported no whitespace errors.

## Known risks

- No live OBS service scan was available; the end-to-end behavior is covered
  with simulated service responses.
- PyArrow is a new runtime dependency and increases installation size.
- A bucket can contain more than 50,000 aggregate paths, so consumers must use
  the ordered `overview_files` list instead of assuming a single part.
- The five existing Windows portability failures remain outside this task.

## Next recommended action

Deploy or install the updated dependency set, run a representative scan with
the default Parquet configuration, inspect `manifest.json`, and download/read
all listed parts. Address the existing Windows test failures in a separate
task.
