# Current Task

## Current task title

Global aggregation request barrier

## Current branch

`codex/parquet-overview`

## Task status

`wip`

The implementation and affected suites are complete. Repository policy keeps
the task at `wip` because the full Windows suite retains five documented
baseline failures.

## User goal

Use one scan-wide phase coordinator so every CSV and Parquet bucket
aggregation waits for admitted OBS attempts to finish, prevents new attempts
and retries while aggregation is pending, and serializes all queued
aggregations before request admission resumes.

## Completed work

- Task 1 created `ScanPhaseCoordinator` in commit `7ba14f8`.
- Task 2 scoped every OBS HTTP attempt with that coordinator in commit
  `783a111`.
- Task 3 wraps the complete scanner CSV/Parquet dispatch in
  `self.phase_coordinator.aggregation()` without changing the aggregator
  argument lists or phase-boundary placement.
- Added CSV and Parquet ordering tests that prove aggregation occurs inside
  the global writer scope, plus end-to-end coverage that every application
  client receives the same coordinator instance.
- Documented the fixed single-aggregation behavior in both READMEs and the
  scan-start guide.

## Remaining work

- Controller review, final whole-branch verification, and push are owned by
  the parent task.
- A representative live OBS scan remains an operational follow-up.
- The five Windows portability failures require a separate task.

## Key files changed

- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `README.md`
- `README.zh-CN.md`
- `docs/scan-start-guide.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
# RED
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -q

# GREEN and integration
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_coordination.py tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py tests/test_aggregation.py tests/test_parquet_aggregation.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
```

## Validation result

- RED: `2 failed, 93 passed`; both new ordering tests observed only the
  aggregator event, proving the writer scope was absent.
- GREEN: scanner `95 passed`; end-to-end `4 passed`; combined affected suite
  `166 passed`.
- Full suite: `264 passed, 5 failed, 1 skipped, 1 warning in 7.07s`.
  The failures are the five known Windows-only baseline cases: two symlink
  privilege tests, CSV CRLF response normalization, backslash path semantics,
  and CLI path separator formatting.
- `compileall` and `git diff --check` exited `0`.
- Task-level review found no Critical or Important issue. Its one Minor test
  gap (prove both configured application clients were constructed) was fixed.

## Known risks

- No live OBS service was available; coverage uses controlled HTTP fakes.
- The coordinator applies only to one `Scanner` run in one process; it does
  not coordinate independent processes or scan runs.
- Full-suite Windows baseline failures remain unrelated to this task.

## Next recommended action

Review the task commit and report, run the parent’s final whole-branch checks,
then push `codex/parquet-overview` if the review finds no issue.
