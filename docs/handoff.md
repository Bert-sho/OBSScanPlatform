# Handoff

## Timestamp

`2026-07-16 20:59:54 +08:00` (Asia/Shanghai)

## Machine/environment

- Workspace: `D:\code\OBSScanPlatform`
- OS/shell: Windows PowerShell
- Branch: `codex/obs-scan-platform`
- Validation used the existing Git-ignored environment at
  `.superpowers\sdd\.venv`.
- The bundled/base Python environment did not provide pytest; no global package
  installation was performed.

## Current branch

`codex/obs-scan-platform`

## Latest commit before this session

`6c8bedd660dd1c8326e655eda26b04b5775bddba`
(`docs: record metadata rollback handoff`)

## Latest commit after this session

- Design: `e1a1cec` (`docs: design bounded csv aggregation`)
- Plan: `53978e0` (`docs: plan bounded csv aggregation`)
- Configuration: `ee52f9b` (`feat: configure aggregation memory bound`)
- Merge primitives: `565b65e` (`feat: add bounded csv summary merge`)
- Source summarization: `f788720` (`feat: summarize object csvs with bounded memory`)
- Bucket integration: `7eab97f` (`fix: bound bucket csv aggregation memory`)
- Operator/architecture docs: `67d5096` (`docs: document bounded csv aggregation`)
- Path-metadata bound: `23f709e` (`fix: bound aggregation path metadata`)
- Streaming discovery: `1a90e3f4d6c63d50d6bc8db209d06bf5633849f4`
  (`fix: stream aggregation file discovery`)
- The final handoff documentation commit is created after this file is written;
  use `git log -1 --oneline` for its immutable hash. The final task response
  records the exact pushed hash.

## Summary of what changed

- Added the validated `scan.aggregation_max_directories_in_memory` setting,
  defaulting to `100000`.
- Replaced whole-bucket object-key deduplication and directory aggregation with
  pure-CSV external aggregation.
- Each detail CSV is read lazily. A bounded directory map is written as sorted
  summary chunks and cleared repeatedly.
- Chunks immediately enter an online hierarchical reducer; completed source
  summaries immediately enter a second bucket reducer. A merge opens no more
  than 32 files, each level keeps at most 31 paths, and finish holds only
  `O(fan_in * levels)` residual paths.
- Source discovery and cleanup use direct context-managed `os.scandir()` rather
  than `Path.glob()`/`rglob()`, avoiding pathlib's hidden per-directory list.
- Final directory rows are streamed in lexical order to the unchanged bucket
  schema through atomic replacement.
- Duplicate object rows are deliberately counted per occurrence. No exact
  object-key set remains.
- Temporary artifact retention, bucket statuses, manifest fields, request versus
  processing timing, and bucket concurrency are unchanged.
- README and CLAUDE guidance document the configuration, aggregation artifacts,
  no-dedup behavior, memory/disk trade-offs, and progress logs.

## Important decisions and rationale

- The user selected no exact deduplication to eliminate the unbounded object-key
  set. This trades protection from duplicate API/detail rows for bounded memory.
- Pure CSV was selected instead of SQLite. It adds no dependency and keeps
  intermediate files inspectable.
- The configured limit counts directory entries rather than bytes. Checking
  after one complete object's ancestor updates preserves correct directory
  attribution and gives a clear maximum overshoot of one object's depth.
- Merge fan-in is fixed at 32 to bound open files and heap rows without adding
  another operator setting.
- Online hierarchical merging was required after final review showed that lists
  of all chunk/source paths could themselves recreate `MemoryError`.
- Direct `os.scandir()` was required after re-review confirmed Python 3.12
  `Path.glob()` internally materializes a directory's entries.
- `keep_temp_files=true` preserves every detail and aggregation artifact;
  `false` deletes successfully consumed work and relies on the existing per-
  bucket finalizer for complete temporary-directory removal.

## Failed attempts or rejected approaches

- Rejected retaining exact object-key deduplication; the user approved counting
  duplicate rows repeatedly.
- Rejected SQLite in favor of the approved pure-CSV design.
- The first implementation bounded object and directory data but retained lists
  of every chunk and source-summary path. Final review classified this as an
  Important violation of complete bounded-memory behavior.
- The first path-metadata fix removed explicit lists but used `Path.glob()`.
  Re-review inspected Python 3.12 and found its hidden `list(scandir_it)`, so the
  implementation was corrected to direct streaming `os.scandir()`.
- No process-RSS assertion was used; RED/GREEN tests instead prove event ordering
  (merges begin before all inputs exist), bounded invalid-input consumption, and
  absence of pathlib glob usage.

## Current test/build status

- Fresh relevant suite: `146 passed in 2.98s`.
- Fresh full suite: `202 passed, 5 failed, 1 warning in 3.56s`.
- The five failures exactly match the authorized Windows baseline categories:
  two symlink privilege failures, CRLF/LF response normalization, backslash path
  semantics, and Windows CLI path rendering.
- `python -m compileall -q src tests` passed.
- `git diff --check 53978e0..HEAD` passed.
- Final independent review of `53978e0..1a90e3f`: Ready; no Critical,
  Important, or Minor findings.
- Task status remains `wip` solely because repository policy forbids
  `completed` while the full suite has failures.

## Uncommitted changes, if any

At the time this handoff was written, only `docs/current-task.md` and
`docs/handoff.md` were uncommitted. After the final commit and push,
`git status --short` must be empty. `.superpowers/sdd` contains Git-ignored
reports, the progress ledger, and the temporary validation environment.

## Exact resume instructions for the next Codex session

```powershell
cd D:\code\OBSScanPlatform
git switch codex/obs-scan-platform
git status --short --branch
git log -12 --oneline

# If no development pytest environment exists on the next machine:
python -m venv .venv
& '.venv\Scripts\python.exe' -m pip install -e '.[dev]'

& '.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_aggregation.py tests/test_external_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.venv\Scripts\python.exe' -m pytest -q

git rev-parse HEAD
git rev-parse origin/codex/obs-scan-platform
```

The relevant suite should pass. The full suite is expected to report the five
documented Windows baseline failures until they are addressed separately. If
local and remote hashes differ, inspect status/log before pushing; do not retry
blindly after an authentication, permissions, network, or divergence error.
