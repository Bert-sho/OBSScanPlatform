# Handoff

## Timestamp

`2026-07-13 20:23:00 +08:00` (Asia/Shanghai)

## Machine/environment

- Workspace: `D:\code\OBSScanPlatform`
- OS/shell: Windows PowerShell
- Branch: `codex/obs-scan-platform`
- Validation runtime: Codex bundled Python 3.12.13 at `C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`
- The `python` command resolves to `C:\Users\lzh\AppData\Local\Microsoft\WindowsApps\python.exe` and exits without running pytest.

## Current branch

`codex/obs-scan-platform`

## Commit history for this task

- Latest commit before this task: `ff5a0516096752de302b1f641bfa27ae4edea437` (`feat: split bucket phase timing`).
- Design: `efad134ee78395cca1d5515074f3da9df54bf88c` (`docs: define global bucket concurrency`).
- Plan: `08d2abed9a222f5453cf0856f0a05d979c58a15c` (`docs: plan global bucket concurrency`).
- Task 1: `c4b5a5ec44fcc977dcd66cd86b7ccbd7ebfc09f3` (`feat: make bucket concurrency global`).
- Task 2: `e05b43d79afdc84c18b7abb49c0df0ccc855b3fb` (`feat: log metadata task progress`).
- Task 3: `6f36b29cda397a553ec2fbc5361ce5115864da5c` (`fix: finalize bucket temp files immediately`).
- Latest commit after this documentation session: the documentation commit at branch `HEAD`; run `git rev-parse HEAD` after resuming to obtain its immutable hash.

## Summary of what changed

- Removed active `ScanSettings.app_concurrency`; legacy YAML remains loadable because unknown fields continue to be ignored.
- All enabled applications now enumerate concurrently.
- One semaphore created by `Scanner.run()` limits active bucket lifecycles across all applications.
- `listbuckets` stays outside bucket permits while all HTTP calls remain subject to the global request semaphore.
- Metadata stages now log start, one outcome-accounted progress record per task, finish, or one skipped record for zero tasks.
- Retention-disabled bucket directories are deleted immediately after final result creation and before the shared bucket permit is released.
- Manifest conversion is serialization-only; retained temp directories remain represented when `keep_temp_files=true`.
- README and the Chinese operator guide document the exact supported settings and runtime semantics.

## Important decisions and rationale

- Kept a single run-owned bucket semaphore so `bucket_concurrency` is global for one run without coupling independent scanner runs.
- Did not add an application semaphore: applications have no independent scan concurrency limit by approved design.
- Kept `listbuckets` outside bucket capacity because a bucket lifecycle begins only after enumeration, while the request semaphore still bounds HTTP pressure.
- Counted a metadata task as successful only when it yields a valid row; request exceptions and invalid responses count as failed tasks.
- Performed cleanup inside the semaphore wrapper so temp finalization is part of the limited bucket lifecycle.
- Did not suppress `shutil.rmtree` errors, preserving visibility of filesystem finalization failures.
- Kept task status `wip` because the complete suite has six failures, even though they predate and are unrelated to this task; the user explicitly authorized proceeding without fixing them.

## RED/GREEN evidence

- Task 1 RED: three focused tests failed for the old modeled `app_concurrency`, per-application bucket permits, and serialized enumeration (`3 failed in 1.20s`). GREEN: focused/relevant `17 passed in 1.01s`; relevant suite `85 passed in 1.61s`.
- Task 2 RED: both metadata log tests failed because stage/progress/skipped records were absent (`2 failed in 0.77s`). GREEN: focused `2 passed in 0.43s`; metadata/phase/objectkeys regression `17 passed, 56 deselected in 0.57s`; scanner suite `73 passed in 1.31s`.
- Task 3 RED: sibling timing and serialization-side-effect tests failed (`2 failed, 3 passed in 1.00s`). GREEN: focused `6 passed in 0.76s`; retention regression `9 passed, 65 deselected in 0.77s`; end-to-end `4 passed in 0.50s`.
- Full evidence and command lines are in `.superpowers/sdd/task-1-report.md`, `.superpowers/sdd/task-2-report.md`, and `.superpowers/sdd/task-3-report.md`.

## Code-review findings and fixes

- Task 1 independent review: spec PASS and quality PASS; no Critical or Important issues.
- Task 2 task review: spec PASS and quality PASS; no blocking findings.
- Task 3 task review: spec PASS and quality PASS; no blocking findings.
- Broad whole-branch review from `efad134` is controller-owned and remains pending.

## Failed attempts or rejected approaches

- The prescribed bare `python -m pytest -q` exited `1` without pytest output because the Windows Store alias is nonfunctional; reran once with the bundled Python runtime and used that output as authoritative evidence.
- Did not fix six unrelated Windows-only failures, per user authorization and the task's surgical-change boundary.
- Rejected per-application semaphores, metadata tqdm changes, manifest-time cleanup, and suppressed deletion errors because they conflict with the approved design.

## Current test/build status

Fresh complete suite command:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q
```

Result: `153 passed, 6 failed, 1 warning in 3.07s`.

Fresh relevant scan-suite command:

```powershell
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Result: `88 passed in 1.84s`.

The six failures exactly match the pre-task Windows baseline (`147 passed, 6 failed, 1 warning in 2.97s`):

1. `tests/test_aggregation.py::test_iter_object_rows_rejects_unexpected_header` — unescaped Windows path used as a regex.
2. `tests/test_api.py::test_runs_list_ignores_symlinked_external_run` — symlink privilege error 1314.
3. `tests/test_api.py::test_runs_list_ignores_symlinked_external_manifest` — symlink privilege error 1314.
4. `tests/test_api.py::test_bucket_csv_downloads_file` — CRLF/LF assertion difference.
5. `tests/test_api.py::test_run_detail_rejects_backslash_segment` — Windows treats backslash as a separator.
6. `tests/test_cli.py::test_scan_success_path` — Windows renders the path with backslashes.

The warning is Starlette's `httpx` test-client deprecation warning. Relevant scan tests and all Task 1-3 focused evidence passed as listed above. Do not mark the task completed while the full suite fails.

## Uncommitted changes, if any

After the documentation commit, no tracked changes are expected. `.superpowers/sdd/` contains ignored controller/agent workflow reports and ledger files. Confirm exact state with `git status --short --branch`.

## Exact resume instructions for the next Codex session

```powershell
cd D:\code\OBSScanPlatform
git switch codex/obs-scan-platform
git status --short --branch
git log -7 --oneline
git diff --stat efad134^..HEAD

& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q

git push -u origin HEAD
git status --short --branch
git rev-parse HEAD
git rev-parse origin/codex/obs-scan-platform
```

Before pushing, the controller must complete the broad review of `efad134^..HEAD`, record any findings/fixes, and refresh `docs/current-task.md` and this handoff if results differ. If push fails, record the exact command and error here, commit that handoff update if useful, and provide the manual recovery command without blind retries.
