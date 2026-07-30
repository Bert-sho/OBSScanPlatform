# Current Task

## Current task title

Correct Parquet maximum file depth and rename aggregation cutoff configuration

## Current branch

`codex/parquet-overview`

## Task status

`wip`

The requested implementation, documentation, review, and focused verification
are complete. Repository policy keeps the task at `wip` because the full
Windows suite still contains the same five pre-existing portability failures.

## User goal

- Make Parquet `max_depth` report the deepest original containing-directory
  level represented by each row. The filename is excluded, so
  `/a/b/file.txt` is depth 2 and `/a/b/c/d/e/file.txt` is depth 5.
- Rename the canonical global YAML cutoff setting from `max_depth` to
  `aggregation_depth`.
- Accept legacy `max_depth` only when the canonical name is absent; reject both
  names together and serialize only `aggregation_depth`.

## Completed work

- Added canonical `scan.aggregation_depth`, non-negative with default `4`.
- Added before-validation migration for legacy `scan.max_depth` and an explicit
  error when both names are present, including equal values.
- Ensured model dumps and `GET /config/apps` expose only
  `aggregation_depth`.
- Added original file-directory depth calculation before output-path
  truncation.
- Added `max_file_depth` to the bounded Parquet summary state and internal CSV
  format; every chunk and merge round combines it with `max()`.
- Changed the unchanged Parquet `max_depth int32` column to use the aggregated
  deepest original file depth instead of the output path depth.
- Renamed Parquet-specific cutoff parameters to `aggregation_depth` and updated
  scanner dispatch without changing its run-local aggregation coordinator.
- Preserved CSV aggregation, all other Parquet metrics, Snappy compression,
  50,000-row splitting, schema order/nullability, empty-bucket behavior,
  output publication, manifests, and downloads.
- Added RED→GREEN tests for canonical/legacy configuration, conflict and
  negative validation, API serialization, direct/root/deep file depth, bounded
  merge propagation, and scanner wiring.
- Updated example YAML, English/Chinese README files, scan-start guide,
  architecture notes, approved design, and implementation plan.
- Completed a local whole-branch review with no remaining Critical or Important
  finding.
- Pushed `codex/parquet-overview` to GitHub through `45588ef`; the final
  push-status record is committed and pushed as the last delivery action.

## Remaining work

- No requested implementation or review work remains.
- Run a representative live OBS scan when an environment is available.
- Address the five unrelated Windows portability failures in a separate task.

## Key files changed

- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/parquet_aggregation.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_config.py`
- `tests/test_api.py`
- `tests/test_parquet_aggregation.py`
- `tests/test_scanner.py`
- `config/apps.example.yaml`
- `README.md`
- `README.zh-CN.md`
- `docs/scan-start-guide.md`
- `CLAUDE.md`
- `docs/superpowers/specs/2026-07-30-parquet-max-depth-semantics-design.md`
- `docs/superpowers/plans/2026-07-30-parquet-max-depth-semantics.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_parquet_aggregation.py tests/test_aggregation.py tests/test_external_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_api.py -k "config_apps or parquet" -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
git diff --check
```

## Validation result

- Fresh feature suite: `207 passed in 5.61s`.
- Fresh configuration/Parquet API suite: `9 passed, 1 skipped, 18 deselected
  in 1.10s`; the skip is the Windows symlink privilege case.
- Fresh compile validation: exit `0`.
- Fresh full suite: `278 passed, 5 failed, 1 skipped, 1 warning in 7.18s`.
- The five failures exactly match the pre-task Windows baseline: two symlink
  privilege failures, CSV CRLF response normalization, backslash path
  semantics, and CLI path-separator rendering.
- `git diff --check` passed and the working tree contained no uncommitted
  implementation changes before this handoff update.

## Known risks

- Existing Parquet consumers that incorrectly interpreted `max_depth` as the
  output `path` depth will observe intentionally larger values at the cutoff.
- The legacy YAML name remains accepted for compatibility but has no separate
  deprecation deadline in this task.
- No live OBS service was available; automated tests cover real configuration,
  aggregation, PyArrow output, scanner, and API boundaries with controlled OBS
  responses.
- The five existing Windows portability failures remain outside this task.

## Next recommended action

Use `aggregation_depth: 4` in new YAML configurations, run a representative
Parquet scan, and confirm cutoff rows report the deepest original file level.
Treat Windows portability cleanup as a separate task.
