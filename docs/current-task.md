# Current Task

## Current task title

Bound bucket CSV aggregation memory

## Current branch

`codex/obs-scan-platform`

## Task status

`wip`

The requested aggregation behavior is implemented, independently reviewed, and
all task-relevant tests pass. Repository policy prevents `completed` while the
full Windows suite still contains five pre-existing portability/environment
failures.

## User goal

Prevent `MemoryError` while aggregating a bucket after request collection. The
implementation must use pure CSV files, keep memory bounded across directory
statistics and temporary-path metadata, preserve the final CSV and manifest
schemas, and continue honoring `scan.keep_temp_files`.

## Completed work

- Added `scan.aggregation_max_directories_in_memory` with default `100000` and
  positive-value validation.
- Removed the bucket-wide `seen_object_keys` set and directory-statistics map.
  Duplicate detail rows are intentionally counted once per occurrence, as
  approved by the user.
- Added associative directory-summary CSVs and atomic sibling-`.tmp` writes.
- Added online hierarchical reducers for both per-source chunks and bucket-wide
  source summaries. Each merge opens at most 32 inputs, and each level retains
  at most 31 pending paths.
- Flushes the current directory map after reaching the configured limit. One
  triggering object's ancestor chain may exceed the configured count by that
  object's directory depth.
- Replaced `Path.glob()`/`Path.rglob()` in the production aggregation path with
  context-managed `os.scandir()` so source discovery and cleanup do not
  materialize all directory entries.
- Bounded invalid merge-input consumption to `fan_in + 1` paths.
- Streams the unchanged `FINAL_FIELDS` schema in lexical directory order and
  atomically replaces the final bucket CSV.
- Preserved top-level prefix detail CSV and `metadata_files.csv` handling,
  scanner timing/status behavior, bucket concurrency, and manifest schema.
- Preserved retention semantics: `keep_temp_files=true` keeps detail, chunk,
  source-summary, and bucket-run artifacts; `false` removes generated work and
  the bucket finalizer removes the bucket temporary directory for every status.
- Added aggregation start/source/merge/finish logging and operator/architecture
  documentation.
- Completed TDD cycles, per-task reviews, two final-review fixes, and a final
  independent review with no Critical, Important, or Minor findings.

## Remaining work

- No code work remains within this feature's scope.
- The five unrelated Windows portability/environment tests must be addressed in
  a separate task before repository policy permits status `completed`.

## Key files changed

- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/csv_store.py`
- `src/obs_scan_platform/external_aggregation.py`
- `src/obs_scan_platform/aggregation.py`
- `src/obs_scan_platform/scanner.py`
- `config/apps.example.yaml`
- `tests/test_config.py`
- `tests/test_external_aggregation.py`
- `tests/test_aggregation.py`
- `tests/test_scanner.py`
- `README.md`
- `CLAUDE.md`
- `docs/superpowers/specs/2026-07-16-bounded-csv-aggregation-design.md`
- `docs/superpowers/plans/2026-07-16-bounded-csv-aggregation.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_aggregation.py tests/test_external_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check 53978e0..HEAD
```

## Validation result

- Relevant configuration/aggregation/scanner/end-to-end suites:
  `146 passed in 2.98s`.
- Full suite: `202 passed, 5 failed, 1 warning in 3.56s`.
- The five full-suite failures are the authorized pre-existing Windows baseline:
  two symlink privilege errors, one CRLF/LF response assertion, one backslash
  path-semantics case, and one Windows CLI config-path separator assertion.
- Compilation and diff whitespace checks passed.
- Final independent review: Ready; Critical 0, Important 0, Minor 0.

## Known risks

- Aggregation no longer performs exact object-key deduplication. Duplicate rows
  from overlapping or anomalous API results increase counts and sizes.
- `aggregation_max_directories_in_memory` bounds directory entries, not bytes;
  one object may temporarily add its complete ancestor chain before flushing.
- Online reducers retain `O(32 * merge_levels)` path metadata and use temporary
  disk proportional to summary volume. `keep_temp_files=true` intentionally
  retains all aggregation artifacts and can require substantial disk capacity.
- Aggregation remains synchronous inside the bucket lifecycle and continues to
  hold the existing bucket permit during processing.
- Source discovery uses two streaming directory passes (count, then process),
  relying on the existing lifecycle guarantee that request writers have already
  finished before aggregation starts.
- Full-suite green status still depends on separately fixing the five Windows
  baseline failures.

## Next recommended action

Run a representative large-bucket scan with the default limit, monitor memory,
disk use, and aggregation progress logs, and tune
`scan.aggregation_max_directories_in_memory` only from measured results. Track
the Windows-only test failures as a separate portability task.
