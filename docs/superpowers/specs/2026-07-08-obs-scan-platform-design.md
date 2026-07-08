# OBS Scan Platform Design

## Background

Build a Python backend platform for periodically scanning department OBS storage usage. The department has multiple applications, and each application owns multiple OBS buckets. The first version focuses on controlled scanning, directory-level aggregation, CSV output, logs, and lightweight management APIs.

The repository currently contains OBS API examples in `OBS_API.txt`. The first version does not use a database. Application configuration is file-based, and scan results are written to CSV files.

## Scope

In scope:

- Maintain application scan configuration in a YAML or JSON file.
- Provide a CLI scanner that can be run manually or by cron/systemd.
- Print real-time scan logs in the CLI and write the same logs to disk.
- Provide FastAPI management APIs for configuration visibility, scan history, logs, CSV download, and manual scan triggering.
- Scan only application-owned buckets and skip shared buckets.
- Support bucket-level custom thresholds for large directories, large files, and inactive directories.
- Generate one final directory aggregation CSV per bucket:
  `results/<run_id>/<appid>/<bucket>.csv`
- Limit total concurrent OBS HTTP requests to a configurable value, defaulting to about 50.
- Use low-memory two-stage scanning: collect temporary object rows first, then aggregate directories after bucket collection completes.

Out of scope for the first version:

- Database storage for application configuration or scan results.
- Persistent exception workflow states such as "no abnormality" or "processed".
- Full frontend pages.
- Long-term storage of every object as the final output.
- Automatic deletion or modification of OBS objects.

## Architecture

The first version has three main parts:

1. CLI scanner
   - Starts scans from the command line.
   - Can be called manually or by cron/systemd.
   - Prints progress logs in real time.
   - Writes logs to `results/<run_id>/scan.log`.
   - Writes scan outputs under `results/<run_id>/`.

2. Scan core
   - Loads application and threshold configuration.
   - Calls OBS APIs described in `OBS_API.txt`.
   - Lists buckets with `listbuckets`.
   - Filters out shared buckets.
   - Gets bucket proxy endpoints.
   - Scans each bucket using root directory discovery plus prefix-based `objectkeys` scanning.
   - Writes temporary object rows during collection.
   - Aggregates temporary rows into the final directory CSV.

3. FastAPI management API
   - Exposes lightweight management endpoints.
   - Reads config files and `results` manifests.
   - Can trigger a manual scan.
   - Does not own scheduling in the first version.

The CLI and FastAPI API reuse the same scan core. Scheduling stays outside the web process in the first version.

## Configuration

Use `config/apps.yaml` or an equivalent JSON file. YAML is preferred for readability.

Example:

```yaml
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
  request_timeout_seconds: 30
  max_retries: 5
  retry_base_delay_seconds: 2
  retry_max_delay_seconds: 60

defaults:
  large_directory_bytes: 107374182400
  large_file_bytes: 10737418240
  inactive_directory_days: 180

applications:
  - appid: com.camera.pergen
    name: Example application
    endpoint: http://example.com
    apptoken: cbb156df-d7c0-43e7-aad4-175ef5e921df
    enabled: true
    buckets:
      bucket-1191:
        large_directory_bytes: 214748364800
        large_file_bytes: 21474836480
        inactive_directory_days: 365
```

Configuration rules:

- `applications` contains all applications to scan.
- Disabled applications are skipped.
- `defaults` contains global fallback thresholds.
- `applications[].buckets.<bucket_name>` overrides thresholds for a specific bucket.
- If a bucket has no override, it uses global default thresholds.
- `apptoken` may be stored in config for the first version, but real tokens should not be committed.
- API responses that expose config must mask `apptoken`.

## OBS API Usage

The scan core uses these API capabilities from `OBS_API.txt`:

- `listbuckets`
  - Lists buckets for an application.
  - Uses app-level `appid` and `apptoken`.

- `bucket/endpoint`
  - Gets the bucket proxy endpoint.
  - Needed before calling `metadata` and `objectkeys`.

- `bucket/filelist`
  - Used only for complete root directory discovery.
  - The returned file size is display text, not exact bytes, so it is not used for threshold analysis.

- `object/metadata`
  - Used for files directly under the bucket root.
  - Provides exact byte size and modify time.

- `list/bucket/objectkeys`
  - Used to scan all objects under a discovered top-level directory prefix.
  - Returns object key, byte size, and modify time.
  - Uses `nextmarker` pagination.

## Bucket Filtering

The first version scans only application-owned buckets.

Filtering rule:

- Include buckets where `auth == "owner"`.
- Exclude buckets with `shareFrom != null`.
- If the API contains ambiguous bucket ownership fields, log the bucket and skip it unless it is clearly owned.

## Scan Strategy

Use a hybrid strategy: complete root discovery plus prefix-concurrent object scanning.

1. List application buckets.
2. Filter out shared buckets.
3. Get the endpoint for each owned bucket.
4. Fully scan the bucket root directory with `filelist("/")`.
   - Continue until root pagination is exhausted.
   - Collect first-level directory prefixes.
   - Collect files directly under `/`.
5. For root-level files, call `metadata` for exact `size` and `lastModifyTime`.
6. For each first-level directory prefix, scan with `objectkeys(prefix, nextmarker)`.
   - Each prefix advances sequentially by `nextmarker`.
   - Multiple prefixes in the same bucket can run concurrently.
   - Prefix concurrency is limited by `per_bucket_prefix_concurrency`.
7. During collection, write only necessary object rows to temporary CSV files.
8. After all bucket collection work completes, aggregate temporary object rows into the final bucket CSV.
9. Write a manifest and logs for the run.

This strategy avoids keeping all objects in memory while still allowing controlled per-bucket parallelism.

## Concurrency Model

Use `asyncio` and an async HTTP client such as `httpx.AsyncClient`.

Concurrency controls:

- `app_concurrency`
  - Maximum applications scanned at the same time.

- `bucket_concurrency`
  - Maximum buckets scanned at the same time.

- `global_request_concurrency`
  - Maximum simultaneous OBS HTTP requests across all applications and buckets.
  - Default: 50.

- `per_bucket_prefix_concurrency`
  - Maximum top-level directory prefixes scanned at the same time in one bucket.

- `metadata_concurrency_per_bucket`
  - Maximum concurrent metadata requests for root-level files in one bucket.

Every `filelist`, `metadata`, and `objectkeys` request must acquire the global request semaphore before sending.

The scanner should not open uncontrolled per-object tasks. Root-level metadata tasks and prefix scan tasks must be bounded by semaphores.

## Temporary Files

The collection phase writes temporary object-level rows. These files are implementation artifacts, not final scan outputs.

Temporary files live under the current run directory. Suggested layout:

```text
results/<run_id>/_tmp/<appid>/<bucket>/root_files.csv
results/<run_id>/_tmp/<appid>/<bucket>/<prefix_hash>.csv
```

Temporary CSV fields:

```csv
object_key,size_bytes,last_modified_ms
```

Rules:

- Temporary rows include only fields needed for aggregation.
- Do not store `md5`, `storageType`, or display size unless later required.
- On successful bucket aggregation, delete the bucket temporary directory by default.
- If collection or aggregation fails, keep the temporary directory for troubleshooting.
- Support `keep_temp_files: true` for manual debugging.

## Directory Aggregation

Each object row contributes to its containing directory and all parent directories up to `/`.

Examples:

- `file.txt` contributes to `/`.
- `a/file.txt` contributes to `/a/` and `/`.
- `a/b/file.txt` contributes to `/a/b/`, `/a/`, and `/`.

Object keys may or may not begin with `/`. Normalize them before path processing.

For each directory, aggregate:

- Object count.
- Total file size in bytes.
- Maximum file size.
- Empty file count.
- Large file count.
- Latest modify time.

Inactive directory detection uses the latest file modify time under that directory tree. If a directory has no valid modify time, leave `latest_modified_ms` and `inactive_days` empty and do not mark it inactive.

## Final CSV Output

Each bucket produces one final CSV:

```text
results/<run_id>/<appid>/<bucket>.csv
```

Each row represents one directory after recursive aggregation.

Fields:

```csv
run_id,
appid,
bucket_name,
bucket_id,
directory_path,
depth,
object_count,
total_size_bytes,
max_file_size_bytes,
empty_file_count,
large_file_count,
latest_modified_ms,
inactive_days,
is_large_directory,
has_large_file,
has_empty_file,
is_inactive_directory
```

Thresholds are bucket-level parameters and must not be repeated on every directory row.

## Thresholds and Abnormal Rules

The scanner resolves thresholds once per bucket:

- Bucket-specific value if configured.
- Otherwise global default.

Rules:

- Large directory:
  - `total_size_bytes >= large_directory_bytes`

- Large file exists under directory:
  - `large_file_count > 0`

- Empty file exists under directory:
  - `empty_file_count > 0`

- Inactive directory:
  - `inactive_days >= inactive_directory_days`

The first version records these booleans in the final CSV but does not maintain persistent exception workflow state.

## Manifest

Each run writes:

```text
results/<run_id>/manifest.json
```

The manifest records:

- `run_id`
- start and end timestamps
- overall status
- config file path
- per-application status
- per-bucket status
- bucket thresholds
- final CSV path
- temporary directory path when retained
- failure reason, if any

Example bucket entry:

```json
{
  "bucket_name": "bucket-1191",
  "bucket_id": "20583467-6766-46b9-be29-0b0eba7bf298",
  "status": "success",
  "csv_path": "results/20260708_213000/com.camera.pergen/bucket-1191.csv",
  "thresholds": {
    "large_directory_bytes": 214748364800,
    "large_file_bytes": 21474836480,
    "inactive_directory_days": 365
  }
}
```

## Logging

CLI logs must print in real time and also write to:

```text
results/<run_id>/scan.log
```

Log content should include:

- Run id and config path.
- Start and end time.
- Application start, success, and failure.
- Bucket start, success, partial failure, and failure.
- Bucket thresholds.
- Root `filelist` page progress.
- Root metadata progress.
- Prefix `objectkeys` page count, object count, and rate.
- Retry attempts, 503 backoff, timeouts, and final request errors.
- Final CSV path and directory row count.

## Error Handling

All OBS requests go through one retry-aware request wrapper.

Retryable cases:

- Timeout.
- Connection error.
- HTTP 5xx.
- HTTP 503.
- API response with `success=false`, unless the error is clearly non-retryable.

Retry behavior:

- Use exponential backoff.
- Use `retry_base_delay_seconds`.
- Cap at `retry_max_delay_seconds`.
- Stop after `max_retries`.

Failure isolation:

- One application failure does not stop other applications.
- One bucket failure does not stop other buckets.
- One prefix failure allows sibling prefixes to finish, but the bucket must not be marked as successful.
- If a bucket is incomplete, do not publish a final success CSV for it.
- If aggregation fails after collection succeeds, retain temporary files.

Final CSV writes must use a temporary output file and atomic rename to avoid exposing partial final CSV files.

## CLI

Provide a CLI entry point for manual or scheduled scans.

Example commands:

```bash
obs-scan scan --config config/apps.yaml
obs-scan scan --config config/apps.yaml --appid com.camera.pergen
obs-scan scan --config config/apps.yaml --run-id 20260708_213000
```

CLI requirements:

- Print real-time logs.
- Exit with non-zero status if any selected application or bucket fails.
- Write manifest and scan log.
- Allow scanning all enabled apps or one selected app.

## FastAPI Management API

First-version API endpoints:

- `GET /health`
  - Health check.

- `GET /config/apps`
  - Returns configured applications and bucket threshold overrides.
  - Masks `apptoken`.

- `GET /runs`
  - Lists historical run manifests under `results`.

- `GET /runs/{run_id}`
  - Returns run details from `manifest.json`.

- `GET /runs/{run_id}/logs`
  - Returns `scan.log`.

- `GET /runs/{run_id}/apps/{appid}/buckets/{bucket_name}/csv`
  - Downloads the final bucket CSV.

- `POST /runs`
  - Triggers one manual scan.
  - First version allows only one active API-triggered scan at a time.

## Testing Strategy

Unit and integration-style tests should avoid real OBS dependencies by using mocked HTTP responses and temporary directories.

Test areas:

- Configuration parsing.
  - Defaults.
  - Bucket-level override.
  - Disabled applications.
  - Token masking.

- Root discovery.
  - Full root pagination.
  - First-level directory extraction.
  - Root-level file extraction.

- Metadata handling.
  - Root file metadata is used for exact byte size.
  - Display size from `filelist` is not used for thresholds.

- Object row collection.
  - `objectkeys` pagination with `nextmarker`.
  - Temporary CSV writes.
  - Dirty `size` or `lastModifyTime` values are logged and handled.

- Directory aggregation.
  - Parent directory rollup.
  - Root directory rollup.
  - Object count.
  - Total bytes.
  - Maximum file size.
  - Empty file count.
  - Large file count.
  - Inactive directory detection.

- Retry behavior.
  - 503 backoff.
  - Max retry failure.
  - Failure isolation.

- Final output.
  - Final CSV fields.
  - Thresholds appear in manifest, not every CSV row.
  - Temporary files are deleted on success and retained on failure.

- API behavior.
  - Run listing.
  - Run details.
  - Log retrieval.
  - CSV download.

- CLI behavior.
  - Argument parsing.
  - Real-time logging setup.
  - Non-zero exit on failed scans.

## Success Criteria

The first version is successful when:

- A user can define multiple applications and bucket thresholds in a config file.
- A user can start a scan from the command line.
- CLI logs are printed in real time and saved to `scan.log`.
- Shared buckets are skipped.
- OBS request concurrency never exceeds the configured global limit.
- Root directory discovery is complete.
- Root-level files use `metadata` for exact byte size.
- Top-level prefixes are scanned with bounded concurrency.
- Temporary object rows are written during collection.
- Each successfully scanned bucket produces one final directory aggregation CSV.
- Each run writes a manifest usable by the API.
- FastAPI can list runs, show details, return logs, and download final CSV files.
