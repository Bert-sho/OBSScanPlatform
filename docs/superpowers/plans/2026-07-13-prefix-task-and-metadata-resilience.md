# Prefix Task and Metadata Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make objectkeys progress and temporary CSVs correspond to every prefix discovered by filelist, while preventing an individual metadata task failure from terminating the bucket scan.

**Architecture:** Preserve the complete sorted prefix set at the filelist scheduler boundary instead of collapsing it to first-level directories; the existing objectkeys queue and `prefix_temp_filename()` then provide one progress task and one cache file per prefix. Broaden only metadata worker isolation so any per-object exception is recorded and scanning continues, leaving filelist and objectkeys unexpected-exception behavior unchanged.

**Tech Stack:** Python 3.11+, asyncio, pytest, pytest-asyncio

## Global Constraints

- Use test-first red-green cycles for each behavior change.
- Keep changes surgical; do not refactor adjacent scanner behavior.
- Update `docs/current-task.md` and `docs/handoff.md`, then commit and push the feature branch.

---

### Task 1: Preserve every filelist-discovered prefix task

**Files:**
- Modify: `src/obs_scan_platform/filelist_discovery.py`
- Modify: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `FilelistDiscoveryScheduler._discovered_prefixes: set[str]`
- Produces: `FilelistDiscoveryScheduler.result() -> RootDiscovery` with every non-empty discovered prefix sorted in `RootDiscovery.prefixes`

- [ ] **Step 1: Write failing discovery and integration tests**

Add a nested discovery assertion expecting `['alpha/', 'alpha/beta/']`, and a bucket-level test asserting objectkeys receives both prefixes, logs `total=2`, and creates both `prefix_temp_filename()` CSV files.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_scanner.py -k "recurses_to_filelist_depth_and_finds_nested_prefixes or bucket_uses_each_filelist_prefix_as_objectkeys_task" -q`

Expected: FAIL because `RootDiscovery.prefixes` currently collapses nested prefixes to `['alpha/']`.

- [ ] **Step 3: Implement the minimum scheduler change**

Change `result()` to use `sorted(self._discovered_prefixes)` and remove the now-unused `_top_level_prefixes()` helper. Keep metadata exclusion based on the resulting prefix list.

- [ ] **Step 4: Run tests to verify GREEN**

Run the same focused pytest command and expect all selected tests to pass.

### Task 2: Isolate all individual metadata task failures

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `tests/test_scanner.py`

**Interfaces:**
- Consumes: exceptions raised while fetching or parsing one metadata object
- Produces: a `PartialErrorSummary` metadata entry, a sanitized warning, continued queue processing, and subsequent objectkeys collection

- [ ] **Step 1: Write failing metadata resilience tests**

Replace the unexpected-metadata propagation expectation with assertions that a parser failure is recorded, a later metadata object is written, and `_scan_bucket()` still reaches objectkeys and returns `partial_failed`.

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_scanner.py -k "metadata_unexpected_exception or bucket_continues_to_objectkeys_after_metadata_task_failure" -q`

Expected: FAIL because non-`OBSRequestError` metadata exceptions currently escape `_collect_metadata_files()`.

- [ ] **Step 3: Implement minimum exception isolation**

Catch `Exception` at the individual metadata item boundary, record it through `PartialErrorSummary.record('metadata', ...)`, sanitize the warning, and continue. Do not catch `BaseException`, so cancellation semantics remain intact.

- [ ] **Step 4: Run tests to verify GREEN**

Run the same focused pytest command and expect all selected tests to pass.

### Task 3: Validate, review, document, commit, and push

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Produces: reproducible validation and Git handoff state for the next Codex session

- [ ] **Step 1: Run scanner and end-to-end suites**

Run: `pytest tests/test_scanner.py tests/test_scan_end_to_end.py -q`

Expected: all selected tests pass.

- [ ] **Step 2: Request code review and address Critical/Important findings**

Review the diff against the three user requirements, with special attention to duplicate prefix behavior, cancellation, partial failure status, and temp-file naming.

- [ ] **Step 3: Update mandatory handoff documents**

Record branch, starting commit `b7b9512`, changed files, exact commands/results, risks, and resume instructions in both mandatory documentation files.

- [ ] **Step 4: Run fresh completion verification**

Run: `pytest tests/test_scanner.py tests/test_scan_end_to_end.py -q`

Then inspect `git status`, `git diff --stat`, and `git diff` for unrelated changes, secrets, and machine-specific paths.

- [ ] **Step 5: Commit and push**

Run `git add .`, commit with `fix: align prefix tasks and isolate metadata failures`, then `git push -u origin HEAD`.
