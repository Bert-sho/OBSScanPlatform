# Current Task

## Current task title

Configuration defaults and per-bucket scan enable control

## Current branch

`codex/obs-scan-platform`

## Task status

`wip`

Feature work is complete and reviewed. This status remains `wip` solely because repository policy forbids `completed` while the full suite has five known Windows baseline failures.

## User goal

Make configuration sections safe to omit by supplying operational defaults; resolve application endpoints predictably; skip explicitly disabled buckets; and reject incomplete applications before they make network requests.

## Completed work

- Added defaults for every scan setting, threshold, application field, bucket override, and the top-level applications list, so empty and partial YAML configurations load where their required scan identity is not yet used.
- Preserved endpoint precedence: a nonblank application endpoint wins; otherwise a nonblank global endpoint is inherited; blank/whitespace values resolve to an empty endpoint.
- Added `buckets.<bucket-name>.enable`, defaulting to `true`. A bucket is scanned unless it has a matching override with `enable: false`; disabled buckets are filtered after normal scan-eligibility checks and before bucket work begins.
- Added an early application preflight that reports missing nonblank `endpoint`, `appid`, or `apptoken` before list-buckets or other API requests.
- Added targeted configuration, scanner, and end-to-end tests, corrected old global-first scanner assertions, and documented the YAML contract and examples.
- Completed final whole-feature review of `50c1144..c55abcb`: Ready; Critical 0, Important 0, Minor 0.
- Successfully pushed `codex/obs-scan-platform` through `99b6c75271fa11902b2706e5c0b766b3fa5bad52`.

## Remaining work

- No feature work remains.
- Address the five unrelated Windows portability/environment failures in a separate task before repository policy permits status `completed`.

## Key files changed

- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_config.py`
- `tests/test_scanner.py`
- `README.md`
- `README.zh-CN.md`
- `CLAUDE.md`
- `config/apps.example.yaml`
- `docs/scan-start-guide.md`
- `docs/superpowers/specs/2026-07-25-config-defaults-and-bucket-enable-design.md`
- `docs/superpowers/plans/2026-07-25-config-defaults-and-bucket-enable.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py tests/test_api.py tests/test_cli.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check 8eaf918..HEAD
```

## Validation result

- Task-relevant config/scanner/end-to-end tests: `118 passed in 1.73s`.
- Combined config/scanner/end/api/cli tests: `135 passed, 5 failed, 1 warning in 2.73s`.
- Full suite: `215 passed, 5 failed, 1 warning in 6.76s`.
- The same five pre-existing Windows categories remain: two symlink privilege failures, CRLF response normalization, backslash path semantics, and CLI config-path separator behavior.
- `compileall` passed.
- `git diff --check 8eaf918..HEAD` passed.
- Final review: Ready; Critical 0, Important 0, Minor 0.

## Known risks

- The application-level preflight intentionally checks only the nonblank scan identity (`endpoint`, `appid`, `apptoken`); malformed API responses and bucket-level conditions remain runtime concerns.
- Endpoint normalization treats whitespace-only endpoint values as missing.
- A disabled bucket produces no scan result because it is deliberately absent from the selected bucket list.
- Full-suite green status is blocked by the documented Windows baseline, not by this feature.

## Next recommended action

Create or schedule a separate Windows portability task for the five baseline failures; that work is the only remaining prerequisite for repository status `completed`.
