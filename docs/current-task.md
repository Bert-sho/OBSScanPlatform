# Current Task

## Current task title

Global aggregation request barrier — final review fixes

## Current branch

`codex/parquet-overview`

## Task status

`wip`

The requested implementation and focused validation are complete. Repository
policy keeps the task at `wip` because the full Windows suite retains the same
five documented baseline failures.

## User goal

Give every individual `Scanner.run` its own aggregation/request coordinator,
including concurrent runs on one `Scanner`; share that exact coordinator across
the run's applications, clients, and buckets; keep request construction and
response validation inside admission; and prove the composed barrier behavior
through deterministic Scanner integration tests.

## Completed work

- Moved `ScanPhaseCoordinator` construction from `Scanner.__init__` into
  `Scanner.run`, then passed the run coordinator explicitly through
  `_scan_application` and `_scan_bucket` to every `OBSClient` and aggregation.
- Moved `http.build_request(...)` inside `request_attempt()` while preserving
  raw request-construction error propagation and all existing retry behavior.
- Added a simultaneous-two-run regression proving distinct run coordinators and
  one shared identity for all application clients and buckets in each run.
- Added event/queue-controlled Scanner integration cases using real
  `OBSClient`, `httpx.AsyncClient`, and `ScanPhaseCoordinator` behavior. They
  prove current-page conversion/CSV append precedes aggregation, request
  admission pauses for waiting/active aggregation, cross-application writers
  stay consecutive, and aggregation failure reopens admission.
- Extended OBS request ordering coverage through request construction, send,
  JSON parsing, business validation, and scope exit.
- Kept HTTPX limits/timeouts/keep-alive, request concurrency, retry/backoff,
  pagination, aggregation arguments, output, and failure behavior unchanged.
- Corrected task/handoff architecture, file inventory, validation, and unpushed
  state.

## Remaining work

- Parent/controller final review and GitHub push.
- A representative live OBS scan remains an operational follow-up.
- The five unrelated Windows portability failures require a separate task.

## Key files changed or directly relevant

- `src/obs_scan_platform/scan_coordination.py`
- `src/obs_scan_platform/obs_client.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_scan_coordination.py`
- `tests/test_obs_client.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `tests/test_aggregation.py`
- `tests/test_parquet_aggregation.py`
- `README.md`
- `README.zh-CN.md`
- `docs/scan-start-guide.md`
- `docs/superpowers/specs/2026-07-29-global-aggregation-request-barrier-design.md`
- `docs/superpowers/plans/2026-07-29-global-aggregation-request-barrier.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
# Run-local ownership RED / GREEN
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_end_to_end.py::test_concurrent_runs_on_one_scanner_use_distinct_run_coordinators -q

# Request-construction ordering RED / GREEN
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_obs_client.py::test_get_json_builds_request_and_validates_response_inside_request_attempt -q

# Event-controlled composed Scanner barrier cases
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_end_to_end.py::test_concurrent_runs_on_one_scanner_use_distinct_run_coordinators tests/test_scan_end_to_end.py::test_scanner_composed_barrier_orders_processing_aggregations_and_requests -q

# Required focused suite
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_coordination.py tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py tests/test_aggregation.py tests/test_parquet_aggregation.py -q

& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
```

## Validation result

- Run-local RED: `1 failed in 0.60s`; bucket orchestration received no explicit
  run coordinator and both runs used the Scanner-owned coordinator. GREEN:
  `1 passed in 0.49s`.
- Request-ordering RED: `1 failed in 0.23s`; `build-request` appeared before
  `scope-enter`. GREEN: `1 passed in 0.16s`.
- Final four new end-to-end cases: `4 passed in 0.45s`.
- Required focused suite: `170 passed in 3.63s`.
- Full suite: `268 passed, 5 failed, 1 skipped, 1 warning in 6.89s`.
  Failures exactly match the documented Windows baseline: two symlink
  privilege cases, CSV CRLF normalization, backslash path semantics, and CLI
  path separator rendering.
- `compileall` and `git diff --check` exited `0`; diff-check emitted only the
  repository's LF-to-CRLF conversion warnings.

## Known risks

- No live OBS service was available; external HTTP and aggregation boundaries
  use controlled fakes while Scanner, OBSClient, HTTPX request construction,
  and coordinator orchestration remain real.
- Coordination is intentionally per `Scanner.run` in one process; separate
  runs and processes never share capacity or aggregation gating.
- The full suite remains non-green only for the five unchanged Windows
  baseline failures above.

## Next recommended action

Review the final local commit and ignored final-fix report, then push
`codex/parquet-overview` if no issue remains. Do not rerun or suppress the five
unrelated Windows failures as part of this task.
