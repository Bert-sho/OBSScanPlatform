# Metadata bucketId Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the metadata endpoint query parameter from the misspelled `bucketld` to the API-contract field `bucketId`.

**Architecture:** Keep the existing metadata request flow and values unchanged. Protect the exact outbound query contract with a focused scanner test, then make the single production-key correction; the objectkeys endpoint remains outside this change.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, Git

## Global Constraints

- Change only the `/rest/boto3/s3/object/metadata` request parameter name.
- Preserve `bucketid` as the bucket-name field and use `bucketId` for the bucket internal ID.
- Do not change the existing `bucketld` contract used by the objectkeys endpoint.
- Update `docs/current-task.md` and `docs/handoff.md` before committing.
- Commit and push the current feature branch; do not push directly to `main` or `master`.

---

### Task 1: Correct and verify the metadata query contract

**Files:**
- Modify: `tests/test_scanner.py:1795`
- Modify: `src/obs_scan_platform/scanner.py:752`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`
- Create: `docs/superpowers/plans/2026-07-28-metadata-bucket-id.md`

**Interfaces:**
- Consumes: `Scanner._collect_metadata_files(...)` and `FakeClient.calls`.
- Produces: metadata requests whose params contain `bucketId=<bucket internal ID>` and omit `bucketld`.

- [ ] **Step 1: Write the failing regression test**

Rename the existing metadata request test to describe both bucket fields and replace the incorrect assertion with:

```python
params = call["params"]
assert params["bucketid"] == bucket.name
assert params.get("bucketId") == bucket.bucket_id
assert "bucketld" not in params
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py::test_collect_metadata_files_uses_correct_bucket_query_fields_and_writes_csv -q
```

Expected: FAIL because `params.get("bucketId")` is `None` while the implementation still sends `bucketld`.

- [ ] **Step 3: Apply the minimal production fix**

Change only the metadata params entry in `Scanner._collect_metadata_files`:

```python
"bucketId": bucket.bucket_id,
```

- [ ] **Step 4: Run focused and relevant tests and verify GREEN**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py::test_collect_metadata_files_uses_correct_bucket_query_fields_and_writes_csv -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: the focused test and scanner/end-to-end suites pass.

- [ ] **Step 5: Update task and handoff documentation**

Record the task goal, branch, exact field-name decision, validation commands/results, changed files, known Windows baseline failures, before-session commit `a4bf0c36995c7f89eb6b1f38d515ea7e672e61fd`, and exact resume instructions in `docs/current-task.md` and `docs/handoff.md`.

- [ ] **Step 6: Review and run completion verification**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
git status --short --branch
git diff --stat
git diff
```

Expected: the field regression is green; any pre-existing Windows full-suite failures are reported rather than hidden; compileall and diff checks pass.

- [ ] **Step 7: Commit and push**

```powershell
git add .
git commit -m "fix: correct metadata bucketId parameter"
git push -u origin HEAD
```

Expected: commit succeeds on `codex/obs-scan-platform` and local HEAD equals `origin/codex/obs-scan-platform` after push.
