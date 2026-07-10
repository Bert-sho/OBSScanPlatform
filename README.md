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

CLI scans display a `tqdm` progress bar for each bucket's `filelist` directory discovery. Detailed progress is also written to `results/<run_id>/scan.log`.

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

FastAPI-triggered scans do not show terminal progress bars. Use `GET /runs/{run_id}/logs` or read `results/<run_id>/scan.log` to view `filelist` progress and per-bucket elapsed time.

## Results

By default, scan output is written under `results/<run_id>/`.

Each successful bucket writes one directory summary CSV:

```text
results/<run_id>/<appid>/<bucket>.csv
```

The final bucket CSV contains directory-level rollups only. It does not store the full object file list. Per-object temporary CSV files are written under `results/<run_id>/_tmp/` while a bucket is being scanned and are removed after successful bucket scans when `scan.keep_temp_files` is `false`.

Each run also writes:

- `results/<run_id>/manifest.json`
- `results/<run_id>/scan.log`

`scan.log` includes per-bucket filelist progress lines such as `filelist progress ... completed=<completed> total=<total>` and bucket duration fields such as `elapsed_seconds=...`.

Default terminal output and `scan.log` do not print full OBS request URLs, query strings, encoded request bodies, or tokens. Failed OBS requests still include safe diagnostics such as `endpoint=objectkeys status=503 reason=busy`.

Empty buckets and buckets containing only empty folders still finish successfully. They produce a bucket CSV with only the final header row.

Design spec: `docs/superpowers/specs/2026-07-08-obs-scan-platform-design.md`
