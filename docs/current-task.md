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
- Added a focused connect-error sanitization regression test that proves raw URLs, tokens, and request bodies stay out of the final error string while the reason falls back to `ConnectError`.
- Added a non-JSON 503 regression test that proves upstream HTML/text error bodies are reduced to a safe reason phrase instead of echoing raw request URLs, query strings, request bodies, or tokens.
- Updated the existing 404 behavior test to expect `OBSRequestError` instead of `httpx.HTTPStatusError`.
- Implemented structured `OBSRequestError(endpoint, status_code, reason)` in `src/obs_scan_platform/obs_client.py`.
- Sanitized HTTP and JSON failure reasons so default request failures no longer include raw request URLs, tokens, or encoded bodies, and non-JSON failures now fall back to `response.reason_phrase` or `HTTP {status_code}`.
- Preserved retry behavior: HTTP 4xx fails fast, HTTP 5xx retries, and OBS JSON `success=false` retries through `max_retries`.
- Passed endpoint labels from scanner OBS call sites and updated fake test clients to accept the new keyword argument.
- Updated `docs/current-task.md`, `docs/handoff.md`, and the Task 2 report for the review-fix pass.

## Remaining work

- None for Task 2.

## Key files changed

- `src/obs_scan_platform/obs_client.py`
- `tests/test_obs_client.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `docs/current-task.md`
- `docs/handoff.md`
- `.superpowers/sdd/task-2-report.md`

## Validation commands run

- `pytest tests/test_obs_client.py -v` (RED)
- `pytest tests/test_obs_client.py -v` (GREEN)
- `pytest tests/test_obs_client.py -v` (review-fix regression pass, 12/12 green)
- `pytest tests/test_scanner.py -v`
- `pytest tests/test_scan_end_to_end.py -v`
- `/opt/homebrew/bin/git diff --check`
- `rg -n "Pending|ready to commit|needs to be written" docs/handoff.md`

## Validation result

- RED: `pytest tests/test_obs_client.py -v` failed with 4 expected failures because `OBSClient.get_json()` did not yet accept `endpoint=`.
- GREEN: `pytest tests/test_obs_client.py -v` passed with 12/12 tests green after the fix.
- `pytest tests/test_scanner.py -v`: 23 passed
- `pytest tests/test_scan_end_to_end.py -v`: 2 passed
- Review-fix regression coverage is already green with the current production code; no production change was required for the new connect-error sanitization test.
- `pytest tests/test_obs_client.py -v`: 12 passed
- `git diff --check`: clean
- `rg -n "Pending|ready to commit|needs to be written" docs/handoff.md`: no matches

## Known risks

- The new sanitization trims failure reasons to 200 characters. If operators later need richer diagnostics, expand them carefully without reintroducing secret-bearing request details.
- I did not run the full `pytest -q` suite in this task; validation was targeted to the OBS client, scanner, and end-to-end scanner tests touched by the change.

## Next recommended action

- Continue with the next task in `docs/superpowers/plans/2026-07-09-obs-scan-behavior-corrections.md` from the current branch head; Task 2 review findings are closed.
