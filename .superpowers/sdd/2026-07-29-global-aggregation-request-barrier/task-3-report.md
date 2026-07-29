# Task 3 Report: Wire the global coordinator into scanner aggregation

## Status

Implemented; the delivery commit is prepared pending parent-controller
whole-branch review and push. The repository remains `wip` because the known
Windows full-suite baseline still has five unrelated failures.

## Requirements implemented

- Wrapped the complete existing CSV/Parquet aggregation format dispatch in
  `async with self.phase_coordinator.aggregation()`.
- Preserved `phase_boundary` placement and both aggregation call argument
  lists.
- Extended scanner construction coverage to assert the client receives the
  scanner’s exact coordinator instance.
- Added CSV and Parquet ordering coverage with a recording coordinator.
- Updated end-to-end fake clients and a two-application run to verify every
  client shares the scanner coordinator.
- Documented the fixed process-local, run-local phase behavior in English and
  Chinese operator documentation.

## Files changed

- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `README.md`
- `README.zh-CN.md`
- `docs/scan-start-guide.md`
- `docs/current-task.md`
- `docs/handoff.md`
- `.superpowers/sdd/2026-07-29-global-aggregation-request-barrier/task-3-report.md`

## TDD evidence

### RED

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -q
```

Result: `2 failed, 93 passed in 1.77s`.

Both new tests failed for the intended reason: events were only
`csv-aggregate` or `parquet-aggregate`, proving the writer scope had not
wrapped either dispatch branch.

### GREEN

After adding the one writer scope around the existing dispatch:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_end_to_end.py -q
```

Results: `95 passed in 1.51s`; `4 passed in 0.57s`.

## Validation

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_coordination.py tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py tests/test_aggregation.py tests/test_parquet_aggregation.py -q
# 166 passed in 3.73s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
# 264 passed, 5 failed, 1 skipped, 1 warning in 7.07s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
# exit 0

git diff --check
# exit 0
```

The five full-suite failures exactly match the documented Windows baseline:
two symlink-privilege failures, CSV CRLF response normalization, backslash
path semantics, and CLI path separator formatting. No live OBS service was
available; integration coverage uses controlled HTTP fakes.

## Self-review

- Both aggregation formats are inside the same `Scanner`-owned coordinator
  writer scope.
- The ordering tests would fail if the writer scope is removed.
- The constructor and two-application end-to-end tests ensure one identity is
  shared by application clients.
- Documentation includes all required operational guarantees and states the
  behavior is neither YAML-configurable nor cross-process.
- Independent task review found no Critical or Important issue. The one Minor
  test-strengthening finding was fixed by asserting a client was constructed
  for every configured application before checking shared identity.
- `git diff --check` is clean. The reviewed task diff contains only approved
  coordinator integration, tests, docs, and handoff records. No secrets or
  machine-specific credentials were added.

## Commit and push

Task commit: pending at report creation; resolve with `git log -1 --oneline`
after the commit. Push intentionally not performed; the parent controller owns
final review and push.
