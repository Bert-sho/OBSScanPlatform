# Filelist Frontier Prefixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make objectkeys scan only the effective filelist traversal frontier, without overlapping parent and child prefix tasks.

**Architecture:** `FilelistDiscoveryScheduler` keeps discovered folders as candidate final prefixes and removes a directory only after its own filelist traversal succeeds. Scanner signals successful expansion separately from generic task completion; a failed directory stays as the branch boundary while its discovered descendants are pruned.

**Tech Stack:** Python 3.11, asyncio, pytest

## Global Constraints

- Preserve current level-based depth and whole-level task-limit behavior.
- Preserve metadata handling for files directly returned by expanded directories.
- Preserve failed-directory fallback and empty-directory behavior.
- Do not change aggregation deduplication or temporary CSV naming.

---

### Task 1: Define Frontier Behavior with Regression Tests

**Files:**
- Modify: `tests/test_scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `Scanner._discover_root(...) -> RootDiscovery`
- Produces: assertions that `RootDiscovery.prefixes` and objectkeys temp files contain only traversal-frontier prefixes

- [ ] **Step 1: Change the nested discovery regression expectations**

Update the two-level discovery test so `alpha/` is excluded after successful
expansion and only `alpha/beta/` is returned. Update the bucket objectkeys test
to expect one request and one child-prefix temporary CSV.

- [ ] **Step 2: Add failed-directory frontier coverage**

Ensure a directory whose own filelist request fails stays in
`RootDiscovery.prefixes`, while successfully expanded parents do not. For a
later-page failure, assert that descendants discovered on earlier pages are not
requested separately.

- [ ] **Step 3: Run focused tests to verify RED**

Run:

```powershell
python -m pytest tests/test_scanner.py -k "prefix or recurse or depth or task_limit or child_filelist_failure" -q
```

Expected: frontier assertions fail because `result()` currently returns every
discovered parent and child prefix.

### Task 2: Implement Candidate-Frontier State Transitions

**Files:**
- Modify: `src/obs_scan_platform/filelist_discovery.py`
- Modify: `src/obs_scan_platform/scanner.py`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `FilelistTask`, folder discoveries, and successful/failed filelist completion
- Produces: `FilelistDiscoveryScheduler.record_expanded(task: FilelistTask) -> None`, `record_failed(task: FilelistTask) -> None`, and non-overlapping `RootDiscovery.prefixes`

- [ ] **Step 1: Add successful-expansion transition**

Add `record_expanded(task)` to remove `f"{task.path.strip('/')}/"` from the
candidate prefix set when the path is not root.

- [ ] **Step 2: Signal expansion only after a successful paginated request**

In `_process_filelist_task`, call `record_expanded(task)` only after the request
loop completes normally. Do not call it from the request-error handler. Keep
`mark_completed(task)` in `finally` so progress remains accurate.

On an `OBSRequestError`, call `record_failed(task)` to retain the failed prefix
but remove its descendant prefixes, direct files, and queued tasks.

- [ ] **Step 3: Run focused tests to verify GREEN**

Run the focused command from Task 1. Expected: all selected tests pass.

- [ ] **Step 4: Run scanner and aggregation suites**

Run:

```powershell
python -m pytest tests/test_scanner.py tests/test_aggregation.py tests/test_scan_end_to_end.py -q
```

Expected: all relevant behavior passes, apart from any already documented
environment-specific failures unrelated to this change.

### Task 3: Review, Handoff, and Publish

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Consumes: verified code/test results and Git state
- Produces: self-contained continuation notes and a pushed conventional commit

- [ ] **Step 1: Request code review**

Review the patch against the design, focusing on pagination failure, empty
directories, uneven trees, task-limit boundaries, metadata coverage, and
parent/child overlap.

- [ ] **Step 2: Run completion verification**

Run the relevant suites, then the repository's full validation command. Record
all pass/fail counts and distinguish pre-existing environment failures.

- [ ] **Step 3: Update mandatory handoff files**

Record branch, commits, changed files, commands/results, known risks, and exact
resume instructions in `docs/current-task.md` and `docs/handoff.md`.

- [ ] **Step 4: Inspect, commit, and push**

Run `git status`, `git diff --stat`, and `git diff`; inspect for unrelated edits,
secrets, and machine-specific paths. Stage the intended files, commit with
`fix: scan only filelist frontier prefixes`, then push with
`git push -u origin HEAD`.
