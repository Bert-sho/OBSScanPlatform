# Comprehensive Project Guide Design

## Status

Approved for implementation on 2026-07-30.

## Goal

Create `docs/project-guide.md`, a Chinese, self-contained project guide for both
operators/users and developers/maintainers. It must be independent of
`README.md`: a reader can install, configure, run, inspect, troubleshoot, and
understand the system without opening another document.

## Audience and reading paths

The document serves two audiences in one layered structure:

- Operators and ordinary users follow project positioning, capabilities,
  quick start, YAML, CLI/API, output, security, and troubleshooting sections.
- Developers and maintainers follow technology stack, architecture, data flow,
  concurrency, module responsibilities, testing, and extension sections.

The opening navigation tells each audience which sections to read. Technical
names, configuration keys, commands, API paths, schema fields, and source file
names remain in English; explanatory prose is Chinese.

## File and scope

- Create only one user-facing guide: `docs/project-guide.md`.
- Do not edit or replace `README.md` or `README.zh-CN.md`.
- Update mandatory repository handoff files after the guide is verified.
- Add the normal Superpowers design/plan records for this documentation task.

The guide describes the repository's current implemented behavior only. It
must not promise unimplemented deployment, scheduling, authentication,
database, dashboard, multi-process coordination, or cloud-vendor features.

## Document structure

1. Project positioning and use cases
2. Reading navigation
3. Core capabilities
4. Technology stack
5. Quick start
6. Complete YAML configuration
7. CLI usage
8. API usage
9. Scan workflow and concurrency model
10. Architecture and module responsibilities
11. CSV and Parquet aggregation rules
12. Results, manifest, logs, and temporary files
13. Failure semantics, security, and troubleshooting
14. Development, testing, and extension guidance
15. Current limitations and operational recommendations

## Self-contained content requirements

### Project and capabilities

Explain that OBS Scan Platform scans configured OBS applications and eligible
buckets, discovers directory/file metadata, collects non-overlapping object-key
frontiers, and produces configurable per-bucket CSV or Parquet overviews plus a
run manifest and log.

List current capabilities including:

- multiple applications and bucket include/disable behavior;
- owned/shared bucket eligibility;
- bounded BFS filelist discovery and rollback thresholds;
- metadata and objectkeys collection, pagination, progress, retry, timeout, and
  secret redaction;
- run-global bucket/request concurrency and request/aggregation phase barrier;
- partial-failure reporting and bucket/application/run rollups;
- legacy CSV aggregation and default Snappy Parquet aggregation;
- bounded external merge, 50,000-row Parquet parts, manifest-listed downloads,
  and temporary-data retention control;
- CLI scans and FastAPI run/config/result endpoints.

### Technology stack

Use `pyproject.toml` as the authoritative list: Python 3.11+, FastAPI, Uvicorn,
HTTPX, Pydantic 2, PyYAML, Typer, tqdm, PyArrow, pytest, and pytest-asyncio.
Explain each dependency's role rather than only listing names.

### Usage reference

Provide copy-paste commands for editable installation, test dependency
installation, CLI scan variants, and single-worker API startup. Include a
complete safe YAML template with placeholder endpoint/token values, the full
default file-type map, global thresholds, per-application fields, and bucket
overrides.

Describe every active `ScanSettings`, global threshold, application, and bucket
field with current defaults and effective behavior. Explain the canonical
`aggregation_depth` setting, legacy `max_depth` input compatibility, and
dual-name validation error.

List all current API routes with methods, purpose, expected output, and relevant
error status. Explain that the active-scan guard is process-local and requires
a single Uvicorn worker.

### Architecture and data flow

Include two Mermaid diagrams:

- a component diagram connecting CLI/API, configuration, Scanner, OBSClient,
  discovery, temporary object CSVs, CSV/Parquet aggregation, manifest/logs, and
  download endpoints;
- a scan sequence/flow diagram from application validation and bucket listing
  through filelist, metadata, objectkeys, aggregation barrier, atomic output,
  cleanup, and manifest rollup.

Describe responsibilities of every module under `src/obs_scan_platform` and
link to those source files with repository-relative Markdown links.

### Output semantics

Document the results directory tree, manifest fields/statuses, log purpose, and
`keep_temp_files` behavior.

For CSV, document the unchanged ancestor rollup and existing output columns.
For Parquet, document exact schema, non-null types, Snappy compression,
50,000-row bound, empty typed part, one-directory attribution, cutoff behavior,
file type JSON mapping, UTC date fallback, and `max_depth` as the deepest
original containing-directory level.

### Failures, security, and limitations

Explain `success`, `partial_failed`, and `failed`; retryability; partial error
samples; configuration validation; missing application credentials; cleanup
failures; and troubleshooting steps based on manifest/log evidence.

Document token masking, error/URL sanitization, path traversal protection,
manifest-authorized Parquet parts, and the prohibition on committing secrets.

State current limitations: process-local run guard/barrier, no built-in auth or
scheduler, no database/dashboard, no cross-process coordination, and no live
OBS validation in the automated development environment.

## Source-of-truth policy

The guide is derived from and cross-checked against:

- `pyproject.toml` for dependencies and Python version;
- `src/obs_scan_platform/config.py` and `config/apps.example.yaml` for config;
- `src/obs_scan_platform/cli.py` for CLI behavior;
- `src/obs_scan_platform/api.py` for routes and status behavior;
- `src/obs_scan_platform/scanner.py`, `filelist_discovery.py`,
  `scan_coordination.py`, and `obs_client.py` for scan/concurrency/retry flow;
- `aggregation.py`, `external_aggregation.py`, and
  `parquet_aggregation.py` for output semantics;
- `models.py` for status, manifest, and error structures;
- tests for boundary examples and platform limitations.

If README prose conflicts with code, the current code and passing focused tests
win. Avoid copying historical design text without verifying it against HEAD.

## Verification

- Confirm the guide has all approved top-level sections and both Mermaid
  diagrams.
- Load the embedded/example YAML values through `load_config` where practical,
  and separately load `config/apps.example.yaml`.
- Run CLI `--help` and API/config focused tests to confirm commands/routes.
- Check every repository-relative source link target exists.
- Search active guide prose for obsolete `scan.max_depth` usage and make sure it
  appears only as legacy compatibility text.
- Run `git diff --check` and inspect the complete documentation diff for
  secrets, invented features, and stale defaults.
- Update `docs/current-task.md` and `docs/handoff.md`, commit, and push the
  current feature branch as explicitly requested.
