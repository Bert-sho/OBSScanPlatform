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

Edit `config/apps.yaml` with real application IDs, OBS API endpoints, and app tokens. Do not commit real tokens.

## CLI Scans

Run a scan for all enabled applications:

```bash
obs-scan scan --config config/apps.yaml
```

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

Design spec: `docs/superpowers/specs/2026-07-08-obs-scan-platform-design.md`
