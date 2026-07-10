# OBS Scan Shared Bucket Regressions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix remaining URL request logging, shared-bucket empty `objectkeys` failures, and app-level resilience when an individual bucket fails unexpectedly.

**Architecture:** Keep the existing scanner flow. Harden logging with a filter that drops noisy `httpx/httpcore` request records even if logger levels are later reset. Treat only empty `objectkeys` 200/`success=false` payloads as terminal empty pages. Add a defensive bucket wrapper so unexpected per-bucket exceptions become failed bucket manifest entries instead of aborting the whole application.

**Tech Stack:** Python 3.11+, asyncio, httpx, pytest, pytest-asyncio.

## Global Constraints

- Use `superpowers:systematic-debugging` and TDD.
- Keep changes surgical and scoped to reported scan behavior.
- Do not hide real OBS permission/error messages.
- Update `docs/current-task.md` and `docs/handoff.md`.

---

### Task 1: Robust HTTPX URL Log Suppression

**Files:**
- Modify: `src/obs_scan_platform/logging_config.py`
- Modify: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `configure_logging(log_path: Path | None = None) -> None`
- Produces: logging configuration that drops `httpx` / `httpcore` INFO request records even if logger levels are reset later.

- [x] Add failing test that calls `configure_logging()`, resets `logging.getLogger("httpx").setLevel(logging.INFO)`, emits `HTTP Request: GET http://secret.example`, and asserts the URL is absent from captured output/log file.
- [x] Implement a small logging filter that removes request URL records from handlers and relevant loggers.
- [x] Run the new test and focused scanner tests.

### Task 2: Empty Objectkeys Success-False Handling

**Files:**
- Modify: `src/obs_scan_platform/obs_client.py`
- Modify: `tests/test_obs_client.py`
- Modify: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `OBSClient.get_json(..., endpoint="objectkeys")`
- Produces: empty `objectkeys` response shapes with 200/`success=false` and no reason are returned as empty terminal pages.

- [x] Add failing test for `OBSClient.get_json()` with `endpoint="objectkeys"` and `{"success": false, "objectKeys": [], "truncated": "false"}`.
- [x] Add failing scanner test where a shared bucket reaches `objectkeys` and receives that empty response but still succeeds with a header-only CSV.
- [x] Implement minimal endpoint-specific empty-list handling for `objectkeys`, without changing failures that include a real reason such as `permission denied`.
- [x] Add reviewer-requested regression coverage for nested `result.message`.
- [x] Run targeted and focused tests.

### Task 3: App-Level Bucket Failure Isolation

**Files:**
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `_scan_application(...)`
- Produces: an unexpected single-bucket exception is represented as a failed `BucketScanResult`; other buckets in the same application still appear in the manifest.

- [x] Add failing test by monkeypatching `_scan_bucket()` to raise for one bucket and return success for another.
- [x] Implement a defensive wrapper around bucket scans in `_scan_application`.
- [x] Run targeted and focused tests.

### Task 4: Handoff and Validation

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

- [x] Run focused scanner validation.
- [x] Run full suite and document known Windows/platform failures if still present.
- [ ] Review diff for secrets/generated files.
- [ ] Commit and push.
