# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Install (editable, with dev deps):
```bash
python -m pip install -e ".[dev]"
```

Tests (config in `pyproject.toml`: `testpaths=tests`, `asyncio_mode=auto`, `pythonpath=src` — no manual PYTHONPATH needed):
```bash
pytest                                  # full suite
pytest tests/test_scanner.py            # one file
pytest tests/test_scanner.py::test_name # one test
pytest -k some_expression               # by name substring
```

Run a scan (CLI shows tqdm progress; also logs to `results/<run_id>/scan.log`):
```bash
cp config/apps.example.yaml config/apps.yaml   # first-time setup; do not commit real tokens
obs-scan scan --config config/apps.yaml [--appid <appid>] [--run-id <id>]
```

Run the API (use a single worker — the run guard is in-process, not shared across workers):
```bash
OBS_SCAN_CONFIG=config/apps.yaml uvicorn obs_scan_platform.api:app --reload
```

No linter/formatter is configured.

## Architecture

Python 3.11+ package under `src/obs_scan_platform/`. It scans OBS buckets over an HTTP API and produces per-bucket directory-level CSV rollups. The CLI (`cli.py`) and FastAPI server (`api.py`) are thin fronts over `scanner.run_scan`.

### Per-bucket scan pipeline (`scanner.py`)
For each enabled application → each scan-capable bucket, the ordered flow is:
1. `listbuckets` → build `BucketInfo`s. `should_scan_bucket` filters: must have id/name/vendor/region; shared buckets only when `scan_shared_buckets` is set, otherwise owner-only.
2. `bucket/endpoint` → resolve the per-bucket data endpoint.
3. **filelist discovery** — bounded BFS over directories (see scheduler below).
4. **metadata** for root-level files, then **objectkeys** for discovered prefixes. Discovery + metadata always complete *before* objectkeys for a bucket.
5. `aggregate_bucket` rolls the temp per-object rows into the final directory CSV.

### Filelist discovery scheduler (`filelist_discovery.py`)
`FilelistDiscoveryScheduler` runs level-by-level BFS starting at `/`, capped by `Thresholds.filelist_depth` and `scan.filelist_task_limit_per_bucket`. The task limit gates whether to descend to the next level; it does not truncate already-discovered tasks at the current level. Successfully expanded directories leave the final prefix set, so `objectkeys` scans only the non-overlapping traversal frontier. `record_failed` retains a failed directory as that branch's frontier while pruning descendants and direct files discovered on earlier pages; `record_empty` drops prefixes confirmed empty.

### Concurrency model
Nested `asyncio.Semaphore` layers, all sourced from `ScanSettings`:
- `bucket_concurrency` is the run-global bucket lifecycle limit shared across all applications; `global_request_concurrency` is the single `request_semaphore` created in `Scanner.__init__`, shared by every OBS HTTP call. Applications have no independent scan concurrency limit.
- Within a bucket: `objectkeys_concurrency_per_bucket` and `metadata_concurrency_per_bucket` bound their own worker pools, but each request still passes through the global semaphore. `per_bucket_prefix_concurrency` is the legacy alias for `objectkeys_concurrency_per_bucket` (see `objectkeys_concurrency_limit()`).

### HTTP client (`obs_client.py`)
`OBSClient.get_json` centralizes requests: exponential backoff retries, no retry on 4xx, and treats `success=false` with empty `objects`/`objectkeys` as a valid empty result for the `filelist`/`objectkeys` endpoints. Request bodies and object keys are base64-url encoded (`encode_request_body`, `encode_object_key`).

### Data model & aggregation (`models.py`, `paths.py`, `csv_store.py`, `aggregation.py`)
Objects are written as temp per-object CSVs under `results/<run_id>/_tmp/<appid>/<bucket>/`. `aggregation.aggregate_bucket` reads them back, and `directory_chain_for_object` attributes each object to *every ancestor directory*, so `DirectoryStats` accumulate rollups (size, counts, large/empty/inactive flags via `Thresholds`) written to `results/<run_id>/<appid>/<bucket>.csv`. Unless `scan.keep_temp_files` is enabled, every bucket finalizes its temp directory immediately for `success`, `partial_failed`, and `failed` results while still holding its run-global bucket permit; a cleanup error converts that bucket result to `failed` without abandoning sibling scans. Status uses `_rollup_status` (`success`/`partial_failed`/`failed`); partial failures are collected in `PartialErrorSummary` and surfaced in `manifest.json`.

### Config (`config.py`)
Single `AppConfigFile` loaded from YAML. `endpoint` is global or per-application. `thresholds_for` merges `defaults` with per-bucket `BucketOverrides` (a bucket may override e.g. `filelist_depth` without repeating other thresholds). `masked_dict` masks `apptoken` for API responses.

### Security / redaction
`_sanitize_reason` (models.py) strips URLs and secret query params (`token`, `csb-token`, `apikey`, etc.) from anything logged or written to manifests. Never log raw request URLs, bodies, or tokens. API path params go through `_safe_segment`/`_safe_child` to block traversal.

## Project workflow

Use Superpowers for non-trivial software engineering tasks.

- Unclear feature or product work: use brainstorming.
- Multi-file implementation: use writing-plans before editing.
- Bug, crash, failing test, or build error: use systematic-debugging.
- Behavior-changing implementation: use test-driven-development.
- Before completion: use requesting-code-review and verification-before-completion.

Do not move from design to implementation without explicit user approval when the task started with brainstorming.

## Karpathy-style coding discipline

These rules apply inside every Superpowers workflow.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

## Documentation handoff requirement

At the end of every development task, update both files:

- `docs/current-task.md`
- `docs/handoff.md`

These files are mandatory even if the code change is small.

### `docs/current-task.md` must include

- Current task title
- Current branch
- Task status: `not-started`, `in-progress`, `blocked`, `completed`, or `wip`
- User goal
- Completed work
- Remaining work
- Key files changed
- Validation commands run
- Validation result
- Known risks
- Next recommended action

### `docs/handoff.md` must include

- Timestamp
- Machine/environment if relevant
- Current branch
- Latest commit before this session
- Latest commit after this session
- Summary of what changed
- Important decisions and rationale
- Failed attempts or rejected approaches
- Current test/build status
- Uncommitted changes, if any
- Exact resume instructions for the next Codex session

The handoff must be written for a future AI coding agent, not only for a human reader. It must be specific, actionable, and self-contained.

## Git workflow

Before committing:

```bash
git status
git diff --stat
git diff
```

Run the relevant validation commands before committing whenever possible.

After validation and handoff updates:

```bash
git add .
git commit -m "<type>: <short task summary>"
git push -u origin HEAD
```

Use conventional commit types when possible:

- `feat:` for new features
- `fix:` for bug fixes
- `refactor:` for refactors
- `test:` for tests
- `docs:` for documentation-only changes
- `chore:` for setup, tooling, or maintenance
- `wip:` only when the task is intentionally incomplete

Do not push directly to `main` or `master` unless the user explicitly asks.
Prefer working on a feature branch.

If currently on `main` or `master`, create a branch before making non-trivial changes:

```bash
git switch -c codex/<short-task-name>
```

## Handling failed validation

If validation fails:

1. Try to fix failures that are directly caused by the current task.
2. Do not hide or ignore failures.
3. Record the failure in `docs/current-task.md`.
4. Record the failure and exact failing command in `docs/handoff.md`.
5. If the task cannot be completed safely, commit with `wip:` only if preserving work is useful.
6. Clearly report that the branch contains failing validation.

Never mark a task as `completed` if validation failed.

## GitHub push failure handling

If `git push` fails because of authentication, network, permissions, protected branch rules, or remote divergence:

1. Do not keep retrying blindly.
2. Record the push failure in `docs/handoff.md`.
3. Show the exact command that failed.
4. Show the exact error summary.
5. Leave the repository in a clean or clearly documented state.
6. Tell the user what command to run manually.

## Security boundaries

Never commit secrets.

Do not commit:

- `.env`
- API keys
- tokens
- private SSH keys
- credentials
- local database dumps
- company-internal secrets
- generated dependency folders such as `node_modules`
- Python virtual environments such as `.venv`
- build artifacts unless explicitly required

Before committing, inspect the diff for secrets or machine-specific paths.

## End-of-task checklist

Before ending any development task, Codex must verify:

- [ ] Relevant code changes are complete.
- [ ] Relevant tests or checks were run, or failure reason is documented.
- [ ] `docs/current-task.md` is updated.
- [ ] `docs/handoff.md` is updated.
- [ ] `git status` was checked.
- [ ] Diff was reviewed for unrelated changes and secrets.
- [ ] Changes were committed.
- [ ] Current branch was pushed to GitHub, or push failure was documented.
- [ ] Final response includes branch name, commit hash, validation result, and push status.
Design spec: `docs/superpowers/specs/2026-07-08-obs-scan-platform-design.md`.
