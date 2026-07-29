# Handoff

## Timestamp

`2026-07-29 20:45:18 +08:00` (Asia/Shanghai)

## Machine/environment

- Workspace: `D:\code\OBSScanPlatform`
- OS/shell: Windows PowerShell
- Branch: `codex/parquet-overview`
- Validation environment: Git-ignored `.superpowers\sdd\.venv`

## Current branch

`codex/parquet-overview`

## Latest commit before this session

`8614c0447772d8bf5903fc9b36a1b9eac65a367c` —
`docs: correct aggregation barrier handoff state`

## Latest commit after this session

The local final-fix commit with subject
`fix: make aggregation coordination run-local` is directly atop `8614c04`.
Its hash is intentionally not self-recorded inside its own handoff. It remains
unpushed for parent/controller review.

## Summary of what changed

- Each `Scanner.run` now creates its own `ScanPhaseCoordinator`. The coordinator
  is no longer stored on the reusable `Scanner` instance.
- `run` passes the same coordinator explicitly through every application and
  bucket; all per-application `OBSClient` instances and both aggregation format
  branches use that exact object.
- `OBSClient.get_json` enters request admission before constructing each
  request and stays admitted through send/body/status/business validation and
  JSON parsing. Request-construction failures retain raw propagation.
- Deterministic composed-barrier Scanner coverage uses real OBSClient, HTTPX,
  and coordinator logic with controlled HTTP/aggregation boundaries. It covers
  page append ordering, paused/resumed request admission, consecutive
  cross-application aggregations, and recovery after aggregation failure.
- Documentation now describes coordinator ownership as process-local and
  strictly per `Scanner.run`, including concurrent calls on one Scanner.

## Important decisions and rationale

- Coordinator creation belongs inside `run`, next to the already run-local
  bucket semaphore, so concurrent calls on a shared Scanner cannot share
  request capacity or aggregation state.
- The coordinator is an explicit internal dependency of `_scan_application`
  and `_scan_bucket`; no fallback Scanner attribute can silently reconnect
  separate runs.
- Aggregation concurrency remains fixed at one. Writer preference continues to
  block new attempts/retries once any aggregation waits, and queued writers
  remain consecutive before readers resume.
- Tests use Events/Queues for ordering and timeouts only as hang guards. The
  aggregation entry hook delegates to the real coordinator implementation.
- The final review fix stayed surgical: HTTPX limits/timeouts/keep-alive,
  retry/backoff, page parsing, output generation, and failure semantics were
  not changed.

## Failed attempts or rejected approaches

- Valid run-local RED: the focused concurrent-run test failed with
  `1 failed in 0.60s` because `_scan_bucket` received no run coordinator and
  both runs shared the Scanner-owned coordinator.
- The first request-ordering fixture used an objectkeys response that invoked
  business-reason validation twice. The fixture was corrected to the existing
  empty-filelist success exception before the valid RED; production was not
  changed during that correction.
- Valid request-ordering RED: `1 failed in 0.23s`, with `build-request` before
  `scope-enter`.
- The composed-barrier cases were coverage additions, not a newly discovered
  production defect: all three passed on their first focused run after the
  ownership fix (`3 passed in 0.39s`). No extra coordinator algorithm change
  was made.
- A post-signature affected run produced `26 failed, 99 passed`; every failure
  was an internal direct-call/fake signature still missing the new explicit
  coordinator. Updating those test call sites produced `125 passed in 2.15s`.
- The integration harness was reduced after review: shared choreography was
  factored, scenarios were table-driven, and a bespoke HTTP client was replaced
  by real `httpx.AsyncClient` plus `MockTransport`.

## Review status

- Self-review found no Critical or Important issue. The production diff is
  limited to per-run ownership/propagation and moving request construction
  inside admission.
- Mutation review: restoring the Scanner field or omitting the bucket argument
  fails the simultaneous-run test; moving `build_request` above admission fails
  the OBS ordering test; allowing reader admission before queued writers fails
  the composed ordering cases; omitting coordinator cleanup fails the
  aggregation-error case.
- No tracked `.superpowers` scratch artifact is modified or added. No secrets or
  machine-specific paths appear outside this required handoff environment
  record.
- Parent/controller owns final whole-branch review and push.

## Current test/build status

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_coordination.py tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py tests/test_aggregation.py tests/test_parquet_aggregation.py -q
# 170 passed in 3.63s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
# 268 passed, 5 failed, 1 skipped, 1 warning in 6.89s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
# exit 0

git diff --check
# exit 0; LF-to-CRLF conversion warnings only
```

The five full-suite failures exactly match the pre-existing Windows baseline:
two symlink privilege failures, CSV response newline normalization, backslash
path semantics, and CLI path separator formatting. No task-caused failure is
present.

## Push status

Not pushed by this task, per the final-fix brief. The parent/controller performs
final review and push.

## Uncommitted changes, if any

None expected after the local final-fix commit. The branch remains ahead of
`origin/codex/parquet-overview` and unpushed.

## Exact resume instructions for the next Codex session

```powershell
cd D:\code\OBSScanPlatform
git status --short --branch
git log -8 --oneline
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scan_coordination.py tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py tests/test_aggregation.py tests/test_parquet_aggregation.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
```

If the full suite still reports exactly the five Windows baseline failures,
inspect the final diff/report and push the existing branch. Do not create
another aggregation-barrier implementation wave.
