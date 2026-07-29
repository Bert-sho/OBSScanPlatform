# Current Task

## Current task title

Configure and verify HTTPX keep-alive expiry

## Current branch

`codex/obs-scan-platform`

## Task status

`wip`

Initial implementation commit `c550a20` exposed an Important review finding: supplying a custom `httpx.Limits` made connection caps unbounded. This fix wave explicitly preserves HTTPX 0.28.1's effective `100`/`20` caps. Repository-wide validation retains five documented Windows baseline failures, so this task is not marked `completed`.

## User goal

Expose a positive `scan.keepalive_expiry_seconds` YAML setting, default it to `5.0`, and apply it to each per-application HTTPX client's idle connection-pool expiry without changing timeout, retry, concurrency, or connection-count behavior.

## Completed work

- Added `ScanSettings.keepalive_expiry_seconds: float = Field(default=5.0, gt=0)`.
- Commit `c550a20` added the setting, initial client construction, tests, and user-facing documentation; it remains local and unpushed.
- Review identified that `httpx.Limits(keepalive_expiry=...)` has unbounded connection counts, unlike AsyncClient's effective HTTPX 0.28.1 defaults.
- Extended the scanner boundary test to prove the missing caps (`None != 100`) and then updated client construction to explicitly pass `max_connections=100`, `max_keepalive_connections=20`, and the configured expiry.
- Added configuration tests for the default, a decimal YAML override, and rejection of zero/negative values.
- Added a scanner boundary test that captures the real `AsyncClient` constructor arguments and verifies both the existing timeout and the configured `httpx.Limits.keepalive_expiry`.
- Documented the example setting and its idle-connection lifecycle semantics in both READMEs and the scan-start guide.
- Corrected the committed design and plan so their mechanism and code snippets explicitly retain the effective `100`/`20` caps.
- Completed self-review of this fix wave; controller task/final review remains pending by delegation.

## Remaining work

- Create the local fix-wave commit, then controller: perform task and final review and push the commits if approved.
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
- `docs/superpowers/specs/2026-07-29-httpx-keepalive-expiry-design.md`
- `docs/superpowers/plans/2026-07-29-httpx-keepalive-expiry.md`

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
- Fix-wave RED: the extended scanner boundary test failed with `assert None == 100` for `limits.max_connections`.
- Fix-wave GREEN: focused test `1 passed in 0.37s`; affected suites `122 passed in 1.95s`.
- Fix-wave full suite: `219 passed, 5 failed, 1 warning in 4.24s`; the same five failures are the documented Windows baseline.

## Known risks

- The connection-cap literals match installed HTTPX `0.28.1`; future HTTPX upgrades require confirming that these remain the intended effective AsyncClient defaults.
- The client construction contract is verified at the HTTPX boundary, but no live OBS service scan was run.
- Full-suite Windows baseline failures remain outside this task's scope.

## Next recommended action

Controller should review the committed diff, preserve the documented baseline failures, and push `codex/obs-scan-platform` only after the review gates pass.
