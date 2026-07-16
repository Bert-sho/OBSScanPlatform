# Handoff

## Timestamp

`2026-07-16 14:58:03 +08:00` (Asia/Shanghai)

## Machine/environment

- Workspace: `D:\code\OBSScanPlatform`
- OS/shell: Windows PowerShell
- Branch: `codex/obs-scan-platform`
- The current Codex bundled Python lacked pytest during this session.
- Validation used an isolated, Git-ignored environment at
  `.superpowers\sdd\.venv`, created from the bundled Python with
  `python -m venv` and `pip install -e '.[dev]'`.
- Bare `python` resolves to a nonfunctional Windows Store alias.

## Current branch

`codex/obs-scan-platform`

## Latest commit before this session

`8bf988c133b84b0d1b3032b2a8c61741a71ea838`
(`fix: scan only filelist frontier prefixes`)

## Latest commit after this session

- Design: `df421a9` (`docs: design metadata task limit rollback`)
- Plan: `2ab078f` (`docs: plan metadata task limit rollback`)
- Configuration: `ee08ea1` (`feat: add metadata task limit configuration`)
- Core rollback: `120b073` (`feat: rollback filelist levels on metadata overflow`)
- Edge semantics/integration: `bb15b48` (`fix: cover metadata rollback edge semantics`)
- Final review fix: `0306ec29718f7ead8f4d222af64603b75e2f0bda`
  (`fix: keep metadata rollback within limit`)
- The final handoff documentation commit is created after this file is written;
  use `git log -1 --oneline` for that immutable hash. The final task response
  records the exact pushed hash.

## Summary of what changed

- Added `scan.metadata_task_limit_per_bucket` with a default of `10000`.
- Before each filelist BFS level, the scheduler snapshots the accepted prefix
  frontier and direct metadata candidates.
- After every complete level, the scanner counts cumulative effective metadata
  tasks. Counts equal to the limit are accepted; counts greater than the limit
  restore the previous whole-bucket checkpoint.
- Root overflow restores `/` as the single objectkeys prefix and skips metadata.
- Empty directories are removed from both live and rollback frontier state;
  snapshot direct keys covered by a confirmed-empty prefix are also removed.
- Failed directories remain rollback prefixes and retain existing failure
  details/counts.
- Empty-directory detection now considers all pages, so a populated early page
  followed by an empty terminal page remains expanded.
- Added deterministic out-of-order same-level coverage, root bucket integration,
  rollback log assertions, failure preservation, and pagination variants.
- README, CLAUDE guidance, design, and implementation plan were updated. No
  manifest or CSV schema field changed.

## Important decisions and rationale

- The checkpoint is per BFS level rather than per branch because the user chose
  whole-bucket rollback and concurrent branches must produce one deterministic
  frontier.
- The limit uses cumulative effective metadata tasks, not raw filelist rows;
  keys covered by retained objectkeys prefixes are not metadata tasks.
- The check runs only after all tasks in the level finish. This preserves
  existing concurrency and prevents completion order from changing rollback.
- Root uses `/` because no shallower non-overlapping frontier exists.
- A confirmed-empty child is authoritative over snapshot direct keys beneath
  its prefix; otherwise removing the prefix could re-expose hidden tasks and
  violate the configured limit.
- Existing filelist task limit, metadata concurrency, partial-error structures,
  manifest schema, and CSV schema remain unchanged.

## Failed attempts or rejected approaches

- Rejected hard-coding `10000`; the user required a configuration item.
- Rejected branch-local rollback; the user selected whole-bucket rollback.
- Rejected checking only the newest level; the user selected cumulative count.
- The current bundled Python unexpectedly lacked pytest. No global packages
  were installed; a Git-ignored local virtual environment was used instead.
- The first empty-directory integration test exposed that ordinary `files: []`
  did not call `record_empty`; fixed by tracking whether any page contained
  items across the complete directory task.
- Final review found that removing an empty rollback prefix could expose old
  snapshot direct keys and break the limit. A failing combination test proved
  the issue before the snapshot cleanup fix.

## Current test/build status

- Fresh relevant suite:
  `101 passed in 1.62s`.
- Fresh full suite:
  `166 passed, 6 failed, 1 warning in 2.69s`.
- The failures exactly match the pre-task Windows baseline categories:
  unescaped regex path, two symlink privilege failures, CRLF/LF assertion,
  backslash path semantics, and Windows CLI path rendering.
- Final fix review: Ready: Yes; Critical 0, Important 0, Minor 0.
- Task status remains `wip` solely because repository policy forbids
  `completed` while the full suite has failures.

## Uncommitted changes, if any

At the time this handoff was written, only the mandatory task/handoff document
updates were uncommitted. After the final commit and push, `git status --short`
must be empty. `.superpowers/sdd` contains Git-ignored reports, review packages,
the progress ledger, and the temporary test environment.

## Exact resume instructions for the next Codex session

```powershell
cd D:\code\OBSScanPlatform
git switch codex/obs-scan-platform
git status --short --branch
git log -8 --oneline

# If no development pytest environment exists on the next machine:
python -m venv .venv
& '.venv\Scripts\python.exe' -m pip install -e '.[dev]'

& '.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.venv\Scripts\python.exe' -m pytest -q

git rev-parse HEAD
git rev-parse origin/codex/obs-scan-platform
```

The relevant suite should pass. The full suite is expected to report the six
documented Windows baseline failures until they are addressed separately. If
local and remote hashes differ, inspect status/log before pushing; do not retry
blindly after an authentication, permissions, network, or divergence error.
