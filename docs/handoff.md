# Handoff

## Timestamp

`2026-07-29 10:19:28 +08:00` (Asia/Shanghai)

## Machine/environment

- Workspace: `D:\code\OBSScanPlatform`
- OS/shell: Windows PowerShell
- Branch: `codex/obs-scan-platform`
- HTTPX under test: `0.28.1`
- Validation environment: Git-ignored `.superpowers\sdd\.venv`

## Current branch

`codex/obs-scan-platform`

## Latest commit before this session

`59a45d7a4342839fedebbb3fcdccaffcb136d9a7`
(`docs: record objectkeys fix push`)

## Latest commits after this session

- `935ca97` — `docs: design HTTPX keepalive expiry setting`
- `7795ac4` — `docs: plan HTTPX keepalive expiry setting`
- `c550a2018185b003c25ce5f4c52d20afbe40c194` —
  `fix: configure HTTPX keepalive expiry`
- `89818f31e602dbf4a4bcec12e87e2abd459c0543` —
  `fix: preserve HTTPX connection limits`
- The final handoff commit is the commit containing this file; resolve its exact
  hash with `git log -1 --oneline` after fetching the branch.

## Summary of what changed

- Added positive YAML setting `scan.keepalive_expiry_seconds`, default `5.0`.
- The per-application shared `httpx.AsyncClient` receives:
  - the existing request timeout;
  - `httpx.Limits.keepalive_expiry` from YAML;
  - explicit `max_connections=100`;
  - explicit `max_keepalive_connections=20`.
- Connection reuse and existing retry/error handling remain enabled.
- Added configuration and scanner-boundary tests.
- Updated example and operator documentation in English and Chinese.
- Updated the approved design after review proved that a custom Limits object
  with only `keepalive_expiry` would make connection caps unbounded.

## Important decisions and rationale

- `Field(default=5.0, gt=0)` accepts positive integers/decimals, provides
  backward-compatible omission behavior, and rejects zero/negative values.
- HTTPX 0.28.1's normal AsyncClient uses effective caps `100` and `20`.
  Supplying only `keepalive_expiry` creates a Limits object whose caps are
  `None`, so the fix supplies `100`/`20` explicitly.
- Five seconds is an idle pool expiry, not an active-request deadline.
  `scan.request_timeout_seconds` still controls request timeouts.
- The observed `RemoteProtocolError` is consistent with peer disconnect/stale
  reuse but does not prove the server timeout; this is a mitigation, while the
  existing retry path remains the fallback.

## Failed attempts or rejected approaches

- TDD configuration RED produced `4 failed, 21 passed` before the field existed.
- TDD scanner RED produced `KeyError: 'limits'` before limits were supplied.
- The initial implementation passed only `keepalive_expiry`; final review
  correctly rejected it because its connection caps became `None`/unbounded.
- Fix-wave RED produced `assert None == 100`, then passed after explicitly
  restoring the `100`/`20` caps.
- `Connection: close`, disabled pooling, retry changes, and server timeout
  changes were deliberately rejected as outside the approved design.

## Review status

- Task review: initial runtime behavior had no Critical/Important finding.
- Final whole-plan review: one Important connection-cap regression and one
  Minor stale-handoff issue.
- Fix commit `89818f3`: connection-cap finding addressed.
- Scoped re-review: no new Critical/Important breakage; stale handoff wording
  was the only remaining item and is corrected by this final record.

## Current test/build status

Fresh completion verification:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
# 122 passed in 1.83s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
# exit 0

& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
# 219 passed, 5 failed, 1 warning in 4.37s
```

The five full-suite failures match the pre-session Windows baseline:

- two tests require Windows symlink privilege;
- one CSV response assertion differs only by CRLF normalization;
- one backslash-path test follows Windows path semantics;
- one CLI assertion expects a POSIX separator.

No affected-suite regression was observed. Project policy keeps
`docs/current-task.md` at `wip` because the full suite is not green.

## Push status

`git push -u origin HEAD` succeeded for `codex/obs-scan-platform` through
`89818f3`:

```text
59a45d7..89818f3  HEAD -> codex/obs-scan-platform
```

The final documentation commit containing this handoff is pushed immediately
after creation, followed by an explicit local/remote HEAD equality check.

## Uncommitted changes, if any

At the time this record was prepared, only `docs/current-task.md` and
`docs/handoff.md` contained the final push/review-state update. They are
committed and pushed as the final completion action. Expected final working
tree state: clean.

## Exact resume instructions for the next Codex session

```powershell
cd D:\code\OBSScanPlatform
git fetch origin
git switch codex/obs-scan-platform
git pull --ff-only
git status --short --branch
git log -5 --oneline
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Confirm the branch is clean and local HEAD equals
`origin/codex/obs-scan-platform`. For operational validation, run a
representative OBS scan with `scan.keepalive_expiry_seconds: 5.0` and compare
`RemoteProtocolError` retry frequency. Do not treat this setting as a request
timeout or proof of the server's keep-alive value. Handle the five Windows
baseline test failures only in a separate task.
