# Comprehensive Project Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a Chinese, self-contained `docs/project-guide.md` that teaches operators how to use OBS Scan Platform and gives developers an accurate architecture, data-flow, module, test, and extension reference.

**Architecture:** Build one layered guide with operator and maintainer reading paths, using current code/configuration as the source of truth. Include copy-paste commands, a complete safe YAML example, API/output references, and two Mermaid diagrams; validate all examples and links without changing runtime behavior or README files.

**Tech Stack:** Markdown, Mermaid, Python 3.11+, Pydantic/PyYAML configuration loader, Typer CLI, FastAPI TestClient, PowerShell, Git.

## Global Constraints

- Create `docs/project-guide.md`; do not modify `README.md` or `README.zh-CN.md`.
- Explanatory prose is Chinese; code, commands, paths, configuration keys, API routes, and schema names remain English.
- The guide is self-contained and must not require README to install, configure, run, inspect, troubleshoot, or understand the system.
- Describe only behavior implemented at current HEAD; no invented dashboard, database, scheduler, built-in authentication, cross-process coordination, or deployment system.
- Use `pyproject.toml`, current source files, `config/apps.example.yaml`, and focused tests as authoritative sources.
- Include two valid Mermaid diagrams: component architecture and scan/data-flow sequence.
- Include the full current configuration contract, CLI commands, API routes, results layout, manifest/log behavior, CSV/Parquet semantics, failure/security behavior, development/test guidance, limitations, and operational recommendations.
- New configuration examples use `aggregation_depth`; legacy `max_depth` appears only in compatibility explanation.
- Never include a real endpoint, token, credential, or company-internal value.
- Update `docs/current-task.md` and `docs/handoff.md`, then commit and push `codex/parquet-overview`.

---

## File Structure

- Create `docs/project-guide.md`: the only new user-facing guide.
- Modify `docs/current-task.md`: current documentation task, evidence, status, and next action.
- Modify `docs/handoff.md`: cross-machine resume state, commits, validation, and push state.
- Do not modify runtime source, tests, README files, or example configuration.

### Task 1: Write the Self-Contained Project Guide

**Files:**
- Create: `docs/project-guide.md`

**Interfaces:**
- Consumes: current repository facts from `pyproject.toml`, `config/apps.example.yaml`, `src/obs_scan_platform/*.py`, and focused tests.
- Produces: one Chinese Markdown guide with the exact heading structure and executable examples below.

- [ ] **Step 1: Capture current CLI, API, configuration, and output facts**

Run these read-only commands before drafting:

```powershell
& '.superpowers\sdd\.venv\Scripts\obs-scan.exe' --help
& '.superpowers\sdd\.venv\Scripts\obs-scan.exe' scan --help
rg -n '@app\.(get|post)|def bucket_|def config_apps|def runs|def run_' src/obs_scan_platform/api.py
rg -n '^class (ScanSettings|Thresholds|BucketOverrides|ApplicationConfig|AppConfigFile)' src/obs_scan_platform/config.py
rg -n 'PARQUET_SCHEMA|MAX_PARQUET_ROWS_PER_FILE|CSV_FIELDS|DIRECTORY' src/obs_scan_platform/aggregation.py src/obs_scan_platform/parquet_aggregation.py
```

Read the relevant current code blocks instead of inferring missing details.

- [ ] **Step 2: Create the guide heading skeleton and navigation**

Create `docs/project-guide.md` with this exact top-level structure:

```markdown
# OBS Scan Platform 项目说明

## 1. 项目定位与适用场景
## 2. 阅读导航
## 3. 当前支持的核心功能
## 4. 技术栈
## 5. 快速开始
## 6. 完整 YAML 配置说明
## 7. CLI 使用说明
## 8. API 使用说明
## 9. 扫描流程与并发模型
## 10. 系统架构与模块职责
## 11. CSV 与 Parquet 聚合规则
## 12. 结果目录、Manifest、日志与临时文件
## 13. 失败语义、安全机制与故障排查
## 14. 开发、测试与扩展指南
## 15. 当前限制与运维建议
```

In “阅读导航”, give operators the section path `1→3→5→6→7/8→12→13`
and developers `1→3→4→9→10→11→14→15`.

- [ ] **Step 3: Write positioning, capabilities, and stack sections**

Explain the complete supported feature set from the approved design. Use a
technology table with columns “技术/版本要求或依赖/项目中的职责” and include:

- Python `>=3.11`;
- FastAPI `>=0.111` and Uvicorn `>=0.30`;
- HTTPX `>=0.27`;
- Pydantic `>=2.7` and PyYAML `>=6.0`;
- Typer `>=0.12` and tqdm `>=4.66`;
- PyArrow `>=16.0`;
- pytest `>=8.2` and pytest-asyncio `>=0.23` as development dependencies.

State dependency floors, not the local environment's incidental installed
versions, except where a verification note explicitly names the tested runtime.

- [ ] **Step 4: Write quick start and complete YAML configuration**

Include copy-paste commands:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item config\apps.example.yaml config\apps.yaml
obs-scan scan --config config/apps.yaml
$env:OBS_SCAN_CONFIG = "config/apps.yaml"
uvicorn obs_scan_platform.api:app --host 0.0.0.0 --port 8000 --workers 1
```

Provide a complete safe YAML example containing:

- global `endpoint`;
- every `ScanSettings` field with the current default;
- the exact complete built-in `file_type_map`;
- every global threshold;
- one application with placeholder `appid`, `name`, `endpoint`, `apptoken`,
  `enabled`, `scan_shared_buckets`, and two bucket override examples;
- explicit `aggregation_depth: 4` and no active `max_depth` key.

Follow the YAML with concise tables for top-level, scan, threshold,
application, and bucket fields. Explain endpoint inheritance, exact bucket-name
matching, `enable: false`, objectkeys concurrency precedence, positive/non-null
validation, file-type override merge, and legacy depth-name conflict behavior.

- [ ] **Step 5: Write CLI and API usage**

Document CLI examples for all applications, one application, and explicit
`run_id`:

```powershell
obs-scan scan --config config/apps.yaml
obs-scan scan --config config/apps.yaml --appid app.one
obs-scan scan --config config/apps.yaml --run-id manual-20260730
```

Document these API routes in a method/path/purpose/response table:

- `GET /health`
- `GET /config/apps`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/logs`
- `GET /runs/{run_id}/apps/{appid}/buckets/{bucket_name}/csv`
- `GET /runs/{run_id}/apps/{appid}/buckets/{bucket_name}/parquet/{part_name}`
- `POST /runs`

Include curl/PowerShell examples, the `202` trigger response, `409` active-run
conflict, common `404` cases, token masking, and the single-worker requirement.

- [ ] **Step 6: Write workflow, architecture, and module sections**

Add a Mermaid `flowchart` connecting:

```text
CLI/API -> config -> Scanner -> OBSClient -> OBS HTTP API
Scanner -> filelist discovery -> metadata/objectkeys -> temp object CSVs
temp object CSVs -> CSV aggregator or Parquet aggregator -> bucket overview
Scanner -> manifest.json and scan.log
API -> manifest/log/CSV/Parquet downloads
```

Add a Mermaid `sequenceDiagram` covering application validation, listbuckets,
bucket endpoint, bounded filelist BFS, metadata, objectkeys frontier, waiting
aggregation writer, request drain, serialized aggregation, atomic publication,
temporary cleanup, and manifest rollup.

Explain run-global bucket/request concurrency, per-bucket workers, writer
preference, retry pause/resume, run-local/process-local boundaries, and
synchronous aggregation.

Give every module under `src/obs_scan_platform` one responsibility row and use
relative links from `docs/project-guide.md`, for example:

```markdown
[`scanner.py`](../src/obs_scan_platform/scanner.py)
```

- [ ] **Step 7: Write aggregation, output, failures, security, development, and limitations**

For CSV, list the exact output fields from `aggregation.py` and describe direct
directory plus ancestor rollup, threshold flags, inactive days, duplicates,
external merge, and atomic replacement.

For Parquet, list the exact ten non-null fields and types, Snappy compression,
50,000-row maximum, ordered parts, typed empty part, one-directory attribution,
`aggregation_depth` cutoff, deepest-original-directory `max_depth`, file-type
JSON array, UTC date fallback, duplicates, bounded merge, and atomic directory
publication.

Show the results tree containing `manifest.json`, `scan.log`, CSV and Parquet
alternatives, and `_tmp`. Describe manifest timing/status/error/output fields,
`keep_temp_files`, partial errors, cleanup failure, and no saved final object
list.

Add a troubleshooting table for invalid YAML, missing endpoint/appid/apptoken,
no buckets, `partial_failed`, HTTP retry exhaustion, output download 404,
temporary cleanup failure, API 409, and Windows test portability failures.

Add security rules, development commands, test-suite map, safe extension
points, and the exact current limitations from the design.

- [ ] **Step 8: Commit the first complete guide**

Before committing, run `git diff --check`, read the complete new guide, and
confirm it has no real token/endpoint. Then:

```powershell
git add docs/project-guide.md
git commit -m "docs: add comprehensive project guide"
```

### Task 2: Validate Guide Accuracy and Usability

**Files:**
- Modify: `docs/project-guide.md` only if validation finds an issue.

**Interfaces:**
- Consumes: completed guide from Task 1.
- Produces: source-verified headings, examples, links, commands, and Mermaid blocks.

- [ ] **Step 1: Verify required structure and terminology**

Run:

```powershell
rg -n '^## [1-9]|^## 1[0-5]\.' docs/project-guide.md
rg -n '^```mermaid|aggregation_depth|max_depth|overview_format|partial_failed|ScanPhaseCoordinator' docs/project-guide.md
```

Expected: all 15 numbered sections, exactly two Mermaid fences, canonical
configuration naming, output `max_depth` explanation, and concurrency/failure
coverage.

- [ ] **Step 2: Validate YAML and configuration defaults**

Load the repository example:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -c "from obs_scan_platform.config import load_config; c=load_config('config/apps.example.yaml'); assert c.scan.aggregation_depth == 4; assert 'max_depth' not in c.scan.model_dump(); print(c.scan.model_dump(mode='json'))"
```

Extract the guide's complete YAML block manually into a temporary file under
the ignored `.superpowers` directory, load it with `load_config`, inspect the
masked model dump, and delete only that exact temporary file afterward. Never
write real credentials.

- [ ] **Step 3: Validate CLI and API references**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\obs-scan.exe' --help
& '.superpowers\sdd\.venv\Scripts\obs-scan.exe' scan --help
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_cli.py tests/test_api.py -k "health or config_apps or post_runs or parquet" -q
```

Compare option spelling and route behavior against the guide. Record exact test
counts and known Windows skips/failures.

- [ ] **Step 4: Validate all source links and Mermaid fence balance**

Use a short read-only PowerShell/Python validation command that parses Markdown
targets beginning with `../`, resolves them relative to `docs`, and fails if a
target does not exist. Count opening and closing code fences and require the
document to contain exactly two ` ```mermaid` openings.

- [ ] **Step 5: Perform factual review and correct any findings**

Cross-check dependency floors, all configuration defaults, API routes, CSV and
Parquet fields, statuses, concurrency ownership, security claims, and
limitations against the source files listed in the design. Make surgical prose
corrections only in `docs/project-guide.md`.

If corrections are made, repeat Steps 1-4 and commit:

```powershell
git add docs/project-guide.md
git commit -m "docs: correct project guide details"
```

If no corrections are needed, do not create an empty commit.

### Task 3: Review, Handoff, and Push

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Consumes: verified guide and validation outputs.
- Produces: source-of-truth task/handoff state and a pushed branch.

- [ ] **Step 1: Review the whole documentation task**

Invoke `superpowers:requesting-code-review`. Current collaboration rules do not
authorize subagent delegation, so review locally from base `576e9df` to HEAD.
Check completeness for both audiences, self-containment, code accuracy, safe
examples, link validity, Mermaid readability, no README modifications, and no
unsupported claims.

- [ ] **Step 2: Run completion verification**

Invoke `superpowers:verification-before-completion`, then rerun:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_cli.py tests/test_config.py tests/test_api.py -k "not runs_list_ignores_symlinked_external and not bucket_csv_downloads_file and not run_detail_rejects_backslash_segment" -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
```

Also rerun the guide structure, YAML, link, Mermaid, and secret checks from Task
2 and record exact results.

- [ ] **Step 3: Update mandatory handoff files**

Set `docs/current-task.md` to this guide task with branch, `wip`/`completed`
status according to repository-wide validation policy, user goal, completed
work, remaining work, changed files, exact validation, risks, and next action.

Set `docs/handoff.md` with Asia/Shanghai timestamp, Windows environment, base
`576e9df`, new commits, design decisions, validation/corrections, review status,
push state, uncommitted state, and exact resume instructions.

- [ ] **Step 4: Inspect and commit final documentation state**

Run the repository-required pre-commit checks:

```powershell
git status --short --branch
git diff --stat
git diff
git diff --check
```

Inspect the full task range for secrets and unrelated changes. Then:

```powershell
git add .
git commit -m "docs: finalize comprehensive project guide handoff"
```

- [ ] **Step 5: Push and record delivery**

```powershell
git push -u origin HEAD
```

On success, update both handoff files with the pushed hash/output, commit the
push record, run `git push` again, fetch, and compare:

```powershell
git fetch origin
git rev-parse HEAD
git rev-parse origin/codex/parquet-overview
git status --short --branch
```

Expected: identical hashes and a clean tracked working tree. If push fails,
follow the AGENTS.md push-failure procedure without blind retries.
