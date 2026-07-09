# OBS Scan Filelist Progress and Configuration Design

## Background

The current OBS scan platform can scan configured applications and buckets, write temporary object CSVs, aggregate final directory CSVs, expose a CLI, and trigger scans from FastAPI. The next change set addresses six operational issues found in the scanner:

- The scan output should not log every request; CLI should show bucket-level `filelist` discovery progress with `tqdm`.
- Each bucket needs customizable recursive `filelist` discovery depth while keeping each bucket near 100 `filelist` tasks.
- Bucket completion logs need total elapsed time per bucket.
- OBS API `endpoint` should be configured once globally instead of repeated per application.
- Empty buckets, including buckets containing only empty folders, should finish successfully and write an empty final CSV.
- Each application needs a switch controlling whether shared buckets are scanned.

This spec follows the approved approach A: keep the current scanner architecture, add focused configuration and scanner behavior changes, and avoid a broad pipeline refactor.

## Goals

- Add global endpoint configuration while preserving compatibility with existing application-level endpoint config.
- Add application-level shared bucket scanning control, defaulting to current behavior.
- Add bucket-customizable recursive `filelist` directory discovery depth.
- Add a global per-bucket `filelist` task limit, defaulting to about 100 tasks.
- Use `tqdm` only for CLI filelist progress; FastAPI scans should only write logs.
- Log per-directory filelist progress to `scan.log` with completed and total task counts.
- Log total elapsed time when each bucket finishes.
- Treat object-empty buckets as successful scans and generate a final CSV with only headers.

## Non-Goals

- Do not redesign the full scan pipeline.
- Do not add database storage.
- Do not add FastAPI request parameters for `appid`, `run_id`, or progress streaming.
- Do not treat `success=false` OBS responses as empty directories.
- Do not add per-request logging.

## Configuration Design

### YAML Shape

Recommended new config:

```yaml
endpoint: http://obs.example

scan:
  results_dir: results
  temp_subdir: _tmp
  keep_temp_files: false
  page_size: 1000
  app_concurrency: 2
  bucket_concurrency: 4
  global_request_concurrency: 50
  per_bucket_prefix_concurrency: 8
  metadata_concurrency_per_bucket: 8
  filelist_task_limit_per_bucket: 100
  request_timeout_seconds: 30
  max_retries: 5
  retry_base_delay_seconds: 2
  retry_max_delay_seconds: 60

defaults:
  large_directory_bytes: 107374182400
  large_file_bytes: 10737418240
  inactive_directory_days: 180
  filelist_depth: 5

applications:
  - appid: com.camera.pergen
    name: Example application
    apptoken: replace-with-real-token
    enabled: true
    scan_shared_buckets: false
    buckets:
      bucket-1191:
        large_directory_bytes: 214748364800
        large_file_bytes: 21474836480
        inactive_directory_days: 365
        filelist_depth: 8
```

### Rules

- Top-level `endpoint` is preferred for all applications.
- Existing application-level `endpoint` remains supported for compatibility.
- Effective endpoint resolution:
  1. Use top-level `endpoint` when present.
  2. Otherwise use `applications[].endpoint` when present.
  3. If neither exists, config validation fails.
- `scan_shared_buckets` is application-level and defaults to `false`.
- `filelist_depth` defaults to `5`.
- Bucket-level `filelist_depth` overrides the default for that bucket.
- `filelist_task_limit_per_bucket` defaults to `100`.

### Model Implications

The current `Thresholds` model only contains directory/file/inactive thresholds. It should be extended or wrapped so bucket-specific settings can include both thresholds and `filelist_depth`.

The simplest implementation is to add `filelist_depth` to `Thresholds` with a default on `defaults` and bucket overrides. This keeps existing `thresholds_for()` behavior compact, but the name `Thresholds` becomes broader. If that feels misleading during implementation, rename it to a more general bucket options model in the implementation plan.

## Shared Bucket Design

Current behavior is hard-coded to scan only owned buckets:

- Include `auth == "owner"`.
- Exclude buckets with `shareFrom != null`.

New behavior:

- If `application.scan_shared_buckets == false`, keep current behavior.
- If `application.scan_shared_buckets == true`, include both owned buckets and shared buckets returned by `listbuckets`.
- Shared buckets use the same downstream scan path as owned buckets.
- If a shared bucket lacks required fields such as bucket id, vendor, or region, it fails as a normal bucket failure and is recorded in the manifest.

## Recursive Filelist Discovery Design

### Purpose

`filelist` is used for directory discovery and direct-file discovery in expanded directories. It is not the main object scanning mechanism for large prefixes. `objectkeys` remains responsible for scanning objects under final directory prefixes.

### Key Rule To Avoid Missing or Duplicating Objects

If a directory is expanded with `filelist`, do not also scan that same directory with `objectkeys`, because scanning both the parent and child prefixes can duplicate object rows.

Instead:

- Files directly returned by `filelist` for an expanded directory are collected through `metadata`.
- Child directories that are selected for further expansion are queued for `filelist`.
- Child directories that are not expanded become final `objectkeys` prefixes.

This ensures:

- Direct files in expanded directories are not missed.
- Objects under unexpanded directories are covered by `objectkeys`.
- Parent and child `objectkeys` prefixes do not overlap.

### Algorithm

For each bucket:

1. Create a queue with root directory `/` at depth `0`.
2. Initialize counters:
   - `filelist_completed = 0`
   - `filelist_total = 1`
   - `filelist_limit = config.scan.filelist_task_limit_per_bucket`
3. Pop directory tasks from the queue and call `bucket/filelist`.
4. For each file returned by `filelist`:
   - Call `metadata` for exact byte size and modify time.
   - Write the resulting object row to temporary CSV.
5. For each directory returned by `filelist`:
   - If `current_depth < filelist_depth` and `filelist_total < filelist_limit`, enqueue it for another `filelist` call and increment `filelist_total`.
   - Otherwise add it to the final `objectkeys` prefix set.
6. After a directory task finishes, increment `filelist_completed` and emit progress.
7. After all filelist tasks complete, scan final prefixes with `objectkeys`.
8. Aggregate temporary object rows into the final bucket CSV.

### Depth Meaning

`filelist_depth` controls how deeply the scanner splits directory work before handing prefixes to `objectkeys`.

It is not a final object scan depth limit. Objects are still fully scanned under each final `objectkeys` prefix.

Examples:

- `filelist_depth: 0`
  - Only root `/` is filelisted.
  - Root files use `metadata`.
  - Root child directories become `objectkeys` prefixes.
- `filelist_depth: 5`
  - The scanner can expand directories up to five levels deep.
  - Deeper or over-limit directories become `objectkeys` prefixes.

### Task Limit Behavior

`filelist_task_limit_per_bucket` limits the number of directories actually processed by `filelist`.

When the limit is reached:

- Stop enqueuing more filelist tasks.
- Add newly discovered child directories to the final `objectkeys` prefix set.
- Do not drop directories.

This keeps the bucket near 100 filelist tasks without missing objects.

## Progress and Logging Design

### CLI Progress

CLI scans should display `tqdm` progress for each bucket's `filelist` discovery.

The progress bar should track:

- Completed filelist tasks.
- Current total discovered filelist tasks.
- Bucket name.

The total can grow while discovery is running because more directories are discovered dynamically.

FastAPI scans should not display `tqdm`.

### scan.log Progress

Every completed `filelist` directory task should write a compact progress log:

```text
filelist progress appid=app.one bucket=bucket-a completed=12 total=37 limit=100 depth=3/5
```

Required fields:

- `appid`
- `bucket`
- `completed`
- `total`
- `limit`
- `depth`

`total` is the current discovered filelist task count. It may increase over time.

### Bucket Duration Logs

Each bucket should log elapsed time on completion:

```text
bucket finish appid=app.one bucket=bucket-a status=success elapsed_seconds=123.45 filelist_completed=37 filelist_total=37
```

On failure:

```text
bucket finish appid=app.one bucket=bucket-a status=failed elapsed_seconds=8.92 filelist_completed=4 filelist_total=12 error="..."
```

For empty buckets:

```text
bucket empty appid=app.one bucket=bucket-a objects=0
bucket finish appid=app.one bucket=bucket-a status=success elapsed_seconds=2.31 filelist_completed=5 filelist_total=5
```

### Request Logging

The scanner and OBS client should not log every HTTP request by default. Existing request-level details should remain absent unless a future debug mode is explicitly introduced.

## Empty Bucket Handling

An empty bucket means the scan finishes with zero valid object rows. This includes:

- Root has no files and no directories.
- Root or nested directories exist, but all directories are empty.

Handling:

- The bucket result is `success`.
- The final CSV is written with only the header row.
- Manifest includes a normal `csv_path`.
- `scan.log` records `bucket empty ... objects=0`.
- Temporary directory cleanup follows existing `keep_temp_files` rules.

Not empty handling:

- `success=false` OBS responses are still errors.
- HTTP errors and retry exhaustion are still errors.
- Files whose metadata cannot produce a valid byte size should not silently redefine the bucket as empty. They should follow the existing error/skip behavior decided in implementation tests.

## CLI and FastAPI Behavior

### CLI

`obs-scan scan` should enable `tqdm` filelist progress.

Existing CLI arguments remain:

- `--config`
- `--appid`
- `--run-id`

### FastAPI

FastAPI `POST /runs` should continue triggering the same scan core.

FastAPI scans should:

- Not show `tqdm`.
- Write progress and duration to `scan.log`.
- Keep current API response behavior.

## Dependency Update

Add `tqdm` to project dependencies because CLI progress is part of the runtime scanner behavior, not only a development tool.

No optional UI dependency split is needed for this version.

## Documentation Updates

Update:

- `config/apps.example.yaml`
- `README.md`
- `docs/scan-start-guide.md`

The docs should explain:

- Top-level `endpoint`.
- Application-level `scan_shared_buckets`.
- Default `filelist_depth: 5`.
- `filelist_task_limit_per_bucket: 100`.
- CLI shows filelist progress.
- FastAPI writes progress only to `scan.log`.

## Testing Strategy

Add or update focused tests:

### Config Tests

- Top-level endpoint is used for applications.
- Application endpoint remains a fallback when top-level endpoint is missing.
- Config validation fails when no endpoint exists.
- `scan_shared_buckets` defaults to `false`.
- `filelist_depth` defaults to `5` and can be overridden per bucket.
- `filelist_task_limit_per_bucket` defaults to `100`.

### Scanner Tests

- Shared bucket is skipped when `scan_shared_buckets=false`.
- Shared bucket is included when `scan_shared_buckets=true`.
- Recursive filelist expands directories up to configured depth.
- Filelist task limit stops further expansion and turns remaining directories into `objectkeys` prefixes.
- Direct files from expanded directories call `metadata`.
- Final objectkeys prefixes do not overlap with expanded parent prefixes.
- Empty root bucket succeeds and writes header-only CSV.
- Bucket containing only empty directories succeeds and writes header-only CSV.
- Bucket finish log includes elapsed seconds.
- Filelist progress log includes completed and total task counts.

### CLI Tests

- CLI scan enables progress mode without changing scan result behavior.
- CLI help remains stable.

### API Tests

- API-triggered scans keep progress logging but do not enable CLI progress mode.

## Migration and Compatibility

Existing configs with application-level `endpoint` remain valid.

Recommended new configs should move endpoint to the top level. Documentation and example config should use the new shape.

Existing bucket threshold overrides continue to work. Buckets that do not specify `filelist_depth` use the default value of `5`.

## Approved Decisions

- Use approach A: recursive `filelist` for directory discovery only, `objectkeys` for final object collection.
- Default `filelist_depth` is `5`.
- Default `filelist_task_limit_per_bucket` is `100`.
- CLI uses `tqdm`; FastAPI does not.
- `scan.log` records completed and total filelist task counts.
- Empty buckets write header-only CSV and succeed.
- Only explicit empty list responses count as empty; `success=false` remains an error.
- Top-level endpoint is preferred, with application endpoint fallback.
- `scan_shared_buckets` is application-level and defaults to `false`.
