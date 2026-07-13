# Current Task

## Current task title

Global bucket concurrency, metadata progress, and immediate temp finalization

## Current branch

`codex/obs-scan-platform`

## Task status

`wip`

The implementation and operator documentation are complete, but the complete Windows test suite still has six accepted pre-existing failures. Final whole-branch review, fresh controller verification, and push remain outstanding, so this task must not be marked `completed`.

## User goal

- Remove independent application scan throttling while keeping legacy `app_concurrency` YAML loadable and ignored.
- Enforce one `bucket_concurrency` limit across all applications for the full bucket lifecycle, including temporary-directory finalization.
- Keep `listbuckets` outside bucket capacity while constraining its HTTP calls with `global_request_concurrency`.
- Log metadata `completed`, `total`, `succeeded`, and `failed`, with `total` equal to the filelist-produced metadata task count and one skipped record when `total=0`.
- Remove each bucket's temporary directory immediately after its final result when `keep_temp_files=false`, before releasing its shared bucket permit.

## Completed work

- Approved and committed the design (`efad134`) and implementation plan (`08d2abe`).
- Removed active `app_concurrency`, started all enabled application enumerations concurrently, and shared one run-wide bucket semaphore (`c4b5a5e`).
- Added deterministic metadata start/progress/finish/skipped records and outcome counters (`e05b43d`).
- Moved retention-disabled cleanup from manifest serialization into the per-bucket semaphore wrapper (`6f36b29`).
- Updated README and the Chinese scan guide with configuration, scheduling, progress-log, and cleanup semantics.
- Ran the complete test suite and documented the six pre-existing Windows failures without changing unrelated tests.

## Remaining work

- Controller: complete the broad review of `efad134^..HEAD` against the approved design and address verified findings.
- Controller: freshly run the relevant and complete verification commands after review.
- Controller: push `codex/obs-scan-platform` and verify local/remote hashes match.
- Separately fix or platform-condition the six pre-existing Windows-only tests before this task can be marked `completed` under the repository validation policy.

## Key files changed

- `config/apps.example.yaml`
- `src/obs_scan_platform/config.py`
- `src/obs_scan_platform/scanner.py`
- `tests/test_config.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `README.md`
- `docs/scan-start-guide.md`
- `docs/superpowers/specs/2026-07-13-global-bucket-concurrency-and-metadata-progress-design.md`
- `docs/superpowers/plans/2026-07-13-global-bucket-concurrency-and-metadata-progress.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
python -m pytest -q
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Task-specific RED/GREEN and regression commands are preserved in `.superpowers/sdd/task-1-report.md`, `.superpowers/sdd/task-2-report.md`, and `.superpowers/sdd/task-3-report.md`.

## Validation result

- Bare `python -m pytest -q`: exited `1` with no pytest output because `python` resolves to the nonfunctional Windows Store alias on this machine.
- Fresh complete suite with bundled Python: `153 passed, 6 failed, 1 warning in 3.07s`.
- Fresh relevant scan suite: `88 passed in 1.84s`.
- The same six failures existed in the pre-task baseline (`147 passed, 6 failed, 1 warning in 2.97s`) and are unrelated to Tasks 1-4:
  - `tests/test_aggregation.py::test_iter_object_rows_rejects_unexpected_header`: Windows path inserted into a regex creates an invalid `\U` escape.
  - `tests/test_api.py::test_runs_list_ignores_symlinked_external_run`: Windows symlink privilege error 1314.
  - `tests/test_api.py::test_runs_list_ignores_symlinked_external_manifest`: Windows symlink privilege error 1314.
  - `tests/test_api.py::test_bucket_csv_downloads_file`: Windows response newline normalization differs (`CRLF` versus `LF`).
  - `tests/test_api.py::test_run_detail_rejects_backslash_segment`: backslash is a path separator on Windows.
  - `tests/test_cli.py::test_scan_success_path`: Windows renders the config path with backslashes.
- Relevant pre-task baseline: `82 passed in 1.22s`.
- Task 1 final relevant suite: `85 passed in 1.61s`.
- Task 2 scanner suite: `73 passed in 1.31s`.
- Task 3 focused/regression evidence: `6 passed in 0.76s`, `9 passed, 65 deselected in 0.77s`, and end-to-end `4 passed in 0.50s`.

## Known risks

- With no application-level throttle, all enabled applications may issue `listbuckets` concurrently. Those calls do not consume bucket permits but can create request pressure up to `global_request_concurrency`.
- `shutil.rmtree` failures are intentionally not suppressed. A filesystem permission, lock, or deletion error can propagate from bucket finalization and prevent normal completion/release behavior.
- Metadata progress records can interleave across concurrent buckets; consumers must group them by `appid` and bucket.
- The six pre-existing Windows test failures mean the branch cannot truthfully be reported as fully passing on this machine.

## Next recommended action

Have the controller perform the broad design review and fresh relevant/full verification, then push the feature branch. Track the unrelated Windows portability failures separately and keep this task `wip` until repository policy permits completion.
