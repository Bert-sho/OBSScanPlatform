# Current Task

## Current task title

Task 2: Sanitized OBS Request Errors

## Current branch

`codex/obs-scan-platform`

## Task status

`completed`

## User goal

Implement Task 2 sanitized OBS request errors for the OBS scan platform:

- add endpoint-labeled, sanitized `OBSRequestError` failures
- keep retry semantics: fail fast for HTTP 4xx, retry HTTP 5xx and OBS `success=false`
- avoid leaking full URLs, query strings, encoded request bodies, or tokens in default errors/logs
- pass endpoint labels from scanner call sites

## Completed work

- Added sanitized OBS client regression tests for 404, 503-after-retry, and `success=false` error cases.
- Updated the existing 404 behavior test to expect `OBSRequestError` instead of `httpx.HTTPStatusError`.
- Implemented structured `OBSRequestError(endpoint, status_code, reason)` in `src/obs_scan_platform/obs_client.py`.
- Sanitized HTTP and JSON failure reasons so default request failures no longer include raw request URLs, tokens, or encoded bodies.
- Preserved retry behavior: HTTP 4xx fails fast, HTTP 5xx retries, OBS JSON `success=false` retries through `max_retries`.
- Passed endpoint labels from scanner OBS call sites and updated fake test clients to accept the new keyword argument.
- Updated `docs/current-task.md` and `docs/handoff.md` for Task 2.

## Remaining work

- None for Task 2.

## Key files changed

- `src/obs_scan_platform/obs_client.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_obs_client.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

- `pytest tests/test_obs_client.py -v` (RED)
- `pytest tests/test_obs_client.py -v` (GREEN)
- `pytest tests/test_scanner.py -v`
- `pytest tests/test_scan_end_to_end.py -v`

## Validation result

- RED: `pytest tests/test_obs_client.py -v` failed with 4 expected failures because `OBSClient.get_json()` did not yet accept `endpoint=`.
- GREEN: `pytest tests/test_obs_client.py -v` passed with 10/10 tests green after the fix.
- `pytest tests/test_scanner.py -v`: 23 passed
- `pytest tests/test_scan_end_to_end.py -v`: 2 passed

## Known risks

- The new sanitization trims failure reasons to 200 characters. If operators later need richer diagnostics, expand them carefully without reintroducing secret-bearing request details.
- I did not run the full `pytest -q` suite in this task; validation was targeted to the OBS client, scanner, and end-to-end scanner tests touched by the change.

## Next recommended action

- Continue with the next task in `docs/superpowers/plans/2026-07-09-obs-scan-behavior-corrections.md`, starting from the current branch head after confirming push status.
