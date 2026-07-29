# Current Task

## Current task title

Configure and verify HTTPX keep-alive expiry

## Current branch

`codex/obs-scan-platform`

## Task status

`wip`

The requested implementation, reviews, and initial push are complete. Project
policy prevents marking the task `completed` because the full Windows suite
still has five pre-existing environment/portability failures; all 122 tests in
the affected configuration, scanner, and end-to-end suites pass.

## User goal

Expose a positive `scan.keepalive_expiry_seconds` YAML setting, default it to
`5.0`, and apply it to each per-application HTTPX client's idle
connection-pool expiry without changing timeout, retry, concurrency,
connection reuse, or effective connection-count behavior.

## Completed work

- Added `ScanSettings.keepalive_expiry_seconds: float = Field(default=5.0, gt=0)`.
- Applied the configured value through `httpx.Limits.keepalive_expiry`.
- Explicitly retained HTTPX 0.28.1's effective client caps:
  `max_connections=100` and `max_keepalive_connections=20`.
- Preserved `request_timeout_seconds`, retry policy, concurrency controls, and
  connection reuse; no `Connection: close` header was added.
- Added TDD coverage for the default, decimal YAML override, rejection of zero
  and negative values, timeout preservation, keep-alive expiry, and both
  connection caps.
- Updated example YAML, English/Chinese READMEs, scan-start guide, design, and
  implementation plan.
- Task review found no Critical/Important issue in the initial runtime change.
- Final review found the custom-Limits connection-cap regression; commit
  `89818f31e602dbf4a4bcec12e87e2abd459c0543` fixed it.
- Scoped re-review confirmed the connection-cap finding was addressed with no
  new Critical/Important breakage. This final record corrects the remaining
  handoff-state wording.
- `git push -u origin HEAD` successfully pushed the branch through
  `89818f3`; the final documentation commit is pushed as the last completion
  action.

## Remaining work

- No implementation or review work remains for the requested keep-alive change.
- The five unrelated Windows full-suite failures require a separate portability
  task if repository-wide validation must become green.
- A live OBS scan remains the recommended operational confirmation.

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

- Configuration RED: `4 failed, 21 passed`; the field did not yet exist and
  non-positive extra values were ignored.
- Configuration GREEN: `25 passed`.
- Initial scanner RED: `KeyError: 'limits'`.
- Initial scanner GREEN: `1 passed`.
- Fix-wave RED: `assert None == 100` exposed the accidental unbounded
  connection cap.
- Fix-wave GREEN: focused scanner test `1 passed`.
- Fresh affected-suite verification: `122 passed in 1.83s`.
- Fresh compilation: `python -m compileall -q src tests` exited `0`.
- Fresh full suite: `219 passed, 5 failed, 1 warning in 4.37s`; failures are
  the unchanged Windows baseline (two symlink privilege failures, CSV CRLF
  normalization, backslash-path semantics, and CLI path-separator formatting).
- Diff checks and changed-line secret-pattern scan passed.

## Known risks

- The explicit `100`/`20` caps match installed HTTPX `0.28.1`; revalidate
  them when upgrading HTTPX.
- The boundary contract is covered by automated tests, but no live OBS service
  scan was available.
- A five-second client expiry reduces stale idle-connection reuse; it cannot
  guarantee that every server or network disconnect is prevented.

## Next recommended action

Use the default or set `scan.keepalive_expiry_seconds: 5.0` explicitly, run a
representative OBS scan, and monitor whether `RemoteProtocolError` retries
decrease. Treat the existing five Windows test failures as a separate task.
