# OBS Scan Platform

Python backend for scanning OBS bucket usage with controlled concurrency and directory-level CSV aggregation.

## Development Setup

Install the package with development dependencies:

```bash
python -m pip install -e ".[dev]"
```

Copy the example configuration before running scans:

```bash
cp config/apps.example.yaml config/apps.yaml
```

Edit `config/apps.yaml` with the real OBS API endpoint, application IDs, app tokens, bucket overrides, and scan concurrency settings. Do not commit real tokens.

The recommended config shape uses one top-level OBS API endpoint:

```yaml
endpoint: http://obs.example
```

Each application can opt into shared bucket scanning:

```yaml
applications:
  - appid: com.camera.pergen
    name: Example application
    apptoken: replace-with-real-token
    scan_shared_buckets: false
```

When `scan_shared_buckets: true` is set, scan-capable shared buckets with complete bucket id, name, vendor, and region fields are included.

Directory discovery uses bounded recursive `filelist` splitting. The default depth is `5`, and each bucket is capped at about `100` filelist directory tasks by `scan.filelist_task_limit_per_bucket`. A bucket can override only its filelist depth without repeating the size and inactivity thresholds:

```yaml
defaults:
  large_directory_bytes: 107374182400
  large_file_bytes: 10737418240
  inactive_directory_days: 180
  filelist_depth: 5

applications:
  - appid: com.camera.pergen
    name: Example application
    apptoken: replace-with-real-token
    buckets:
      bucket-1191:
        filelist_depth: 8
```

`scan.filelist_task_limit_per_bucket` is a threshold for deciding whether to recurse into a deeper level. It does not truncate directory tasks already discovered for the current level.

Recommended request concurrency defaults:

```yaml
scan:
  global_request_concurrency: 150
  objectkeys_concurrency_per_bucket: 30
```

`objectkeys_concurrency_per_bucket` limits only per-bucket `objectkeys` prefix workers. `filelist` and metadata requests still share the global request limit. The legacy `scan.per_bucket_prefix_concurrency` setting remains compatible, but new configs should use `scan.objectkeys_concurrency_per_bucket`.

For each bucket, scanning completes all `filelist` discovery and metadata requests before starting `objectkeys` collection.

## CLI Scans

Run a scan for all enabled applications:

```bash
obs-scan scan --config config/apps.yaml
```

CLI scans display `tqdm` progress bars for each bucket's `filelist` directory discovery and `objectkeys` prefix collection. Detailed progress is also written to `results/<run_id>/scan.log`.

Run a scan for one application ID:

```bash
obs-scan scan --config config/apps.yaml --appid com.camera.pergen
```

Use `--run-id` when a stable output directory name is needed:

```bash
obs-scan scan --config config/apps.yaml --run-id run-1
```

## API Server

Start the API with a configured app file:

```bash
OBS_SCAN_CONFIG=config/apps.yaml uvicorn obs_scan_platform.api:app --reload
```

Useful endpoints:

- `GET /health`
- `GET /config/apps`
- `POST /runs`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/logs`
- `GET /runs/{run_id}/apps/{appid}/buckets/{bucket_name}/csv`

The manual scan trigger uses an in-process `active_scan` guard. Run the API with a single worker for this version; multiple API workers do not share that guard.

FastAPI-triggered scans do not show terminal progress bars. They write the same filelist and objectkeys progress to `results/<run_id>/scan.log`; use `GET /runs/{run_id}/logs` or read that file directly.

## Results

By default, scan output is written under `results/<run_id>/`.

Each successfully aggregated bucket writes one directory summary CSV, including a bucket that finishes `partial_failed`:

```text
results/<run_id>/<appid>/<bucket>.csv
```

The final bucket CSV contains directory-level rollups only. It does not store the full object file list. Per-object temporary CSV files are written under `results/<run_id>/_tmp/` while a bucket is being scanned. When `scan.keep_temp_files` is `false` (the default), temporary files are removed for `success`, `partial_failed`, and `failed` buckets; when it is `true`, temporary files are retained for every bucket status.

Each run also writes:

- `results/<run_id>/manifest.json`
- `results/<run_id>/scan.log`

`scan.log` includes per-bucket filelist progress lines and objectkeys progress fields `completed`, `total`, `succeeded`, `failed`, `pages`, and `objects`. Every bucket manifest entry includes `started_ms`, `ended_ms`, `started_at`, `ended_at`, and monotonic `elapsed_seconds` timing fields.

Normal successful requests suppress full OBS URLs. Every failed attempt logs its unredacted prepared URL and up to 2048 response characters, together with attempt counters, status, reason, truncation metadata, and exception type. The default retry policy makes up to three retries after the initial request (four attempts total) for retryable failures.

Treat `scan.log` and `manifest.json` as sensitive data: failed URLs can contain tokens, encoded request bodies, object keys, and pagination cursors, and failed response bodies can contain service details. Restrict access and do not commit or share these files without review.

The five request fallback boundaries are:

- `listbuckets`: the current application fails because its bucket set is unknown; other applications continue.
- `bucket_endpoint`: the current bucket fails and has no CSV; other buckets continue.
- `filelist`: only the failed directory's remaining pages stop, including for root `/`; successful earlier pages and other discovered directory tasks remain, and the bucket becomes `partial_failed`.
- `metadata`: only the failed object is skipped; other objects continue and the bucket becomes `partial_failed`.
- `objectkeys`: only the failed prefix's remaining pages stop; rows from earlier successful pages and other prefixes remain, and the bucket becomes `partial_failed`.

Before consuming a CSV, inspect its bucket entry in `manifest.json`. `status=success` means aggregation finished without a final recoverable request failure. `status=partial_failed` means the CSV is incomplete: `error` gives a concise summary, `partial_errors` gives compatibility counters and bounded samples, and `errors` contains every detailed final request failure. A partial CSV preserves successful rows and excludes data available only through failed requests; it must not be treated as complete without evaluating those fields.

Empty buckets and buckets containing only empty folders still finish successfully. They produce a bucket CSV with only the final header row.

Design spec: `docs/superpowers/specs/2026-07-08-obs-scan-platform-design.md`
