# Current Task

## Current task title

Configure and verify HTTPX keep-alive expiry

## Current branch

`codex/obs-scan-platform`

## Task status

`wip`

Implementation and focused validation are complete. Repository-wide validation retains five documented Windows baseline failures, so this task is not marked `completed`.

## User goal

Expose a positive `scan.keepalive_expiry_seconds` YAML setting, default it to `5.0`, and apply it to each per-application HTTPX client's idle connection-pool expiry without changing timeout, retry, concurrency, or connection-count behavior.

## Completed work

- Added `ScanSettings.keepalive_expiry_seconds: float = Field(default=5.0, gt=0)`.
- Passed the configured value to `httpx.Limits(keepalive_expiry=...)` when constructing the per-application `httpx.AsyncClient`.
- Added configuration tests for the default, a decimal YAML override, and rejection of zero/negative values.
- Added a scanner boundary test that captures the real `AsyncClient` constructor arguments and verifies both the existing timeout and the configured `httpx.Limits.keepalive_expiry`.
- Documented the example setting and its idle-connection lifecycle semantics in both READMEs and the scan-start guide.
- Completed self-review of the final diff; no scope, secret, or machine-specific-path concern was found. Controller task/final review remains pending by delegation.

## Remaining work

- Controller: perform task and final review, then push the implementation commit if approved.
- The five unrelated Windows portability/environment failures need a separate task before repository-wide status can be `completed`.

## Key files changed

- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_config.py`
- `tests/test_scanner.py`
- `config/apps.example.yaml`
- `README.md`
- `README.zh-CN.md`
- `docs/scan-start-guide.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py::test_scan_application_applies_configured_httpx_keepalive_expiry -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
git status --short --branch
git diff --stat
git diff
```

## Validation result

- Configuration RED: `4 failed, 21 passed`; the default/override failed because the field did not exist, and zero/negative values were silently ignored.
- Configuration GREEN: `25 passed in 0.37s`.
- Scanner RED: the boundary test failed with `KeyError: 'limits'` because `AsyncClient` received only `timeout`.
- Scanner GREEN: `1 passed in 0.32s`.
- Affected suites: `122 passed in 1.79s`.
- Full suite: `219 passed, 5 failed, 1 warning in 4.28s`; the five failures match the documented Windows baseline (two symlink privilege failures, CSV CRLF normalization, backslash path semantics, and CLI path separator formatting).
- `compileall` and `git diff --check` passed.

## Known risks

- The client construction contract is verified at the HTTPX boundary, but no live OBS service scan was run.
- Full-suite Windows baseline failures remain outside this task's scope.

## Next recommended action

Controller should review the committed diff, preserve the documented baseline failures, and push `codex/obs-scan-platform` only after the review gates pass.
