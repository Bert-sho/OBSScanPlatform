# Objectkeys bucketId Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the objectkeys endpoint query parameter from the misspelled `bucketld` to the API-contract field `bucketId`.

**Architecture:** Keep the existing objectkeys request flow, pagination, and values unchanged. Protect the outbound request boundary with focused and end-to-end assertions, then make the single production-key correction; the already-correct metadata endpoint remains unchanged.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, Git

## Global Constraints

- Change only the `/rest/boto3/s3/list/bucket/objectkeys` internal-ID parameter name.
- Preserve `bucketid` as the bucket-name field and use `bucketId` for the bucket internal ID.
- Preserve the metadata endpoint's existing `bucketId` contract without modifying its request construction.
- Preserve objectkeys pagination and every other query field unchanged.
- Update `docs/current-task.md` and `docs/handoff.md` before committing.
- Commit and push the current feature branch; do not push directly to `main` or `master`.

---

### Task 1: Correct and verify the objectkeys query contract

**Files:**
- Modify: `tests/test_scanner.py:1496`
- Modify: `tests/test_scanner.py:2127`
- Modify: `tests/test_scan_end_to_end.py:121`
- Modify: `tests/test_scan_end_to_end.py:257`
- Modify: `tests/test_scan_end_to_end.py:674`
- Modify: `src/obs_scan_platform/scanner.py:931`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`
- Create: `docs/superpowers/plans/2026-07-28-objectkeys-bucket-id.md`

**Interfaces:**
- Consumes: `Scanner._collect_prefix(...)`, `FakeClient.calls`, and the end-to-end fake OBS clients.
- Produces: objectkeys requests whose params contain `bucketId=<bucket internal ID>` and omit `bucketld`.

- [ ] **Step 1: Write the failing focused regression test**

In `test_collect_prefix_stops_when_truncated_string_false`, replace the obsolete internal-ID assertion with:

```python
params = call["params"]
assert params["bucketid"] == bucket.name
assert params.get("bucketId") == bucket.bucket_id
assert "bucketld" not in params
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py::test_collect_prefix_stops_when_truncated_string_false -q
```

Expected: FAIL because `params.get("bucketId")` is `None` while the implementation still sends `bucketld`.

- [ ] **Step 3: Apply the minimal production fix**

Change only the objectkeys params entry in `Scanner._collect_prefix`:

```python
"bucketId": bucket.bucket_id,
```

- [ ] **Step 4: Update all objectkeys boundary assertions**

For each existing objectkeys assertion in `tests/test_scanner.py` and `tests/test_scan_end_to_end.py`, assert the bucket internal ID through `bucketId` and assert that `bucketld` is absent. Leave metadata assertions unchanged.

- [ ] **Step 5: Run focused and relevant tests and verify GREEN**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py::test_collect_prefix_stops_when_truncated_string_false -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: the focused test and scanner/end-to-end suites pass.

- [ ] **Step 6: Update task and handoff documentation**

Record the task goal, branch, exact field-name decision, validation commands/results, changed files, known Windows baseline failures, before-session commit `eccf3fef8116ae0e5d2053d4e6620647d92a43ca`, and exact resume instructions in `docs/current-task.md` and `docs/handoff.md`.

- [ ] **Step 7: Review and run completion verification**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
git status --short --branch
git diff --stat
git diff
```

Expected: the objectkeys regression is green; any pre-existing Windows full-suite failures are reported rather than hidden; compileall and diff checks pass.

- [ ] **Step 8: Commit and push**

```powershell
git add .
git commit -m "fix: correct objectkeys bucketId parameter"
git push -u origin HEAD
```

Expected: commit succeeds on `codex/obs-scan-platform` and local HEAD equals `origin/codex/obs-scan-platform` after push.
