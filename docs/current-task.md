# Current Task

## Current task title

Bound filelist metadata tasks with whole-level rollback

## Current branch

`codex/obs-scan-platform`

## Task status

`wip`

The requested behavior is implemented, independently reviewed, and all
task-relevant tests pass. Repository policy prevents `completed` while the
full Windows suite still contains the same six pre-existing portability and
environment failures.

## User goal

Prevent deep filelist traversal from creating an excessive number of metadata
tasks. After each complete BFS level, compare the bucket's cumulative effective
metadata tasks with a configurable limit; if it exceeds the limit, roll the
whole bucket back to the previous frontier and use that level for objectkeys
scanning.

## Completed work

- Added `scan.metadata_task_limit_per_bucket` with default `10000` and example
  configuration coverage.
- Added per-level scheduler checkpoints for the accepted prefix frontier and
  metadata candidates.
- Added cumulative effective metadata counting using the same filtering logic
  as final `RootDiscovery.metadata_files`.
- Enforced strict `>` semantics after the entire BFS level finishes; equality
  is accepted and rollback is whole-bucket rather than branch-local.
- Added root fallback to the sole objectkeys prefix `/`.
- Preserved failed directories and partial-error details during rollback.
- Removed confirmed empty directories and their covered snapshot direct keys
  so rollback cannot re-expose metadata tasks beyond the limit.
- Made empty-directory detection span all pages, including ordinary `files: []`
  and special `objects: {}` responses.
- Added INFO rollback logging, root bucket integration coverage, out-of-order
  same-level concurrency coverage, and pagination terminal-page coverage.
- Updated README and architecture guidance; manifest and CSV schemas remain
  unchanged.
- Completed per-task reviews and final re-review after fixing one Important
  checkpoint edge case; final result is Ready: Yes with no open findings.

## Remaining work

- No work remains within this feature's functional scope.
- The six unrelated Windows portability tests must be fixed or conditioned in
  a separate task before repository policy permits status `completed`.

## Key files changed

- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/filelist_discovery.py`
- `src/obs_scan_platform/scanner.py`
- `config/apps.example.yaml`
- `tests/test_config.py`
- `tests/test_scanner.py`
- `README.md`
- `CLAUDE.md`
- `docs/superpowers/specs/2026-07-16-filelist-metadata-limit-rollback-design.md`
- `docs/superpowers/plans/2026-07-16-filelist-metadata-limit-rollback.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
```

## Validation result

- Relevant configuration/scanner/end-to-end suites: `101 passed in 1.62s`.
- Full suite: `166 passed, 6 failed, 1 warning in 2.69s`.
- The six full-suite failures match the authorized Windows baseline exactly:
  one unescaped Windows path used as a pytest regex, two symlink privilege
  errors, one CRLF/LF response assertion, one backslash path-semantics case,
  and one Windows CLI config-path separator assertion.
- The warning is a Starlette TestClient/httpx deprecation warning.

## Known risks

- The metadata limit is evaluated after a complete BFS level, so a level can
  temporarily discover more candidates in memory before rollback; downstream
  metadata requests are still bounded by the restored result.
- Confirmed empty child listings are treated as authoritative over snapshot
  direct keys covered by that child prefix. This is required to keep the
  restored metadata set consistent with the empty result and within the limit.
- Full-suite green status still depends on separately addressing the six
  Windows-only baseline failures.

## Next recommended action

Deploy or exercise the scanner with representative buckets using the default
limit, then tune `scan.metadata_task_limit_per_bucket` only if operational
request and memory measurements justify it. Track Windows test portability as
a separate maintenance task.
