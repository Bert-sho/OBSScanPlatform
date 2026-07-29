# OBS Scan Platform

[中文文档](README.zh-CN.md)

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
        enable: true
        filelist_depth: 8
```

The bucket map is not a whitelist. Every eligible bucket returned by `listbuckets` is scanned unless the exact matching bucket entry explicitly sets `enable: false`:

```yaml
applications:
  - appid: com.camera.pergen
    buckets:
      bucket-to-skip:
        enable: false
```

An absent bucket entry, an entry without `enable`, and `enable: true` all scan the bucket. A bucket with `enable: false` is skipped after the existing scan-capability and owner/shared-bucket checks. It makes no bucket endpoint, filelist, metadata, or objectkeys requests, produces no CSV, and has no bucket entry in the manifest. Configuring a bucket that `listbuckets` does not return has no effect.

`scan.filelist_task_limit_per_bucket` is a threshold for deciding whether to recurse into a deeper level. It does not truncate directory tasks already discovered for the current level.

`scan.metadata_task_limit_per_bucket` defaults to `10000`. After each complete `filelist` BFS level, the scanner checks the cumulative metadata task count. If the count exceeds the limit, discovery for the whole bucket rolls back to the prefix frontier and metadata candidates checkpointed before that level. If the root level overflows, metadata requests are skipped and `objectkeys` scans once with `/` as its prefix.

`objectkeys` scans only the non-overlapping frontier left by `filelist`: successfully expanded and empty directories are excluded, depth/task-limit boundary directories remain, and a failed directory becomes the boundary for its branch.

Recommended request concurrency defaults:

```yaml
scan:
  bucket_concurrency: 4
  global_request_concurrency: 150
  metadata_concurrency_per_bucket: 8
  objectkeys_concurrency_per_bucket: 30
  aggregation_max_directories_in_memory: 100000
```

Applications have no independent scan concurrency limit. `bucket_concurrency` is one run-wide limit shared across all applications and covers each bucket's full lifecycle through temporary-directory finalization. Application `listbuckets` calls do not consume bucket capacity, but they do consume `global_request_concurrency` capacity along with every other HTTP request.

`metadata_concurrency_per_bucket` and `objectkeys_concurrency_per_bucket` limit their respective per-bucket workers. `filelist` and metadata requests still share the global request limit. The legacy `scan.per_bucket_prefix_concurrency` setting remains compatible, but new configs should use `scan.objectkeys_concurrency_per_bucket`.

`scan.aggregation_max_directories_in_memory` defaults to `100000` and must be positive. It limits the directory-statistics dictionary used for each aggregation chunk, not an exact byte count. A single object is attributed to its containing directory and every ancestor before the limit is checked, so a chunk can exceed the configured count by at most that triggering object's directory depth.

For each bucket, scanning completes all `filelist` discovery and metadata requests before starting `objectkeys` collection.

## Configuration Contract

All modeled fields may be omitted. Defaults are applied by the configuration models and are visible through the masked `GET /config/apps` response. A missing field uses the defaults below; explicit `null` is still invalid for non-nullable fields.

### Top-level defaults

| Field | Missing-field default |
| --- | --- |
| `endpoint` | `""` |
| `scan` | All scan defaults below |
| `defaults` | All global threshold defaults below |
| `applications` | `[]` |

### Scan defaults

| Field | Raw missing-field default | Effective behavior |
| --- | --- | --- |
| `results_dir` | `"results"` | — |
| `temp_subdir` | `"_tmp"` | — |
| `keep_temp_files` | `false` | — |
| `page_size` | `1000` | — |
| `bucket_concurrency` | `4` | — |
| `global_request_concurrency` | `150` | — |
| `per_bucket_prefix_concurrency` | `null` | Legacy objectkeys concurrency alias |
| `objectkeys_concurrency_per_bucket` | `null` | Uses this value when set; otherwise uses `per_bucket_prefix_concurrency`; when both are absent/`null`, the effective limit is `30` |
| `metadata_concurrency_per_bucket` | `8` | — |
| `request_timeout_seconds` | `30` | — |
| `keepalive_expiry_seconds` | `5.0` | Expires idle pooled connections; must be positive |
| `max_retries` | `3` | Three retries after the initial attempt |
| `retry_base_delay_seconds` | `2` | — |
| `retry_max_delay_seconds` | `60` | — |
| `filelist_task_limit_per_bucket` | `100` | — |
| `metadata_task_limit_per_bucket` | `10000` | — |
| `aggregation_max_directories_in_memory` | `100000` | Must be positive |

`keepalive_expiry_seconds` controls how long idle pooled connections may remain reusable. Its `5.0`-second default expires idle pooled connections; it does not terminate an active request after five seconds, keeps connection reuse enabled, and works alongside the existing retry path.

### Global threshold defaults

| Field | Missing-field default |
| --- | --- |
| `large_directory_bytes` | `107374182400` (100 GiB) |
| `large_file_bytes` | `10737418240` (10 GiB) |
| `inactive_directory_days` | `180` |
| `filelist_depth` | `5` |

### Application defaults

| Field | Missing-field default |
| --- | --- |
| `appid` | `""` |
| `name` | `""` |
| `endpoint` | `""`, then inherit a non-empty top-level endpoint |
| `apptoken` | `""` |
| `enabled` | `true` |
| `scan_shared_buckets` | `false` |
| `buckets` | `{}` |

Endpoint resolution has exact precedence: a non-empty application `endpoint` wins; otherwise the application inherits a non-empty top-level `endpoint`; otherwise its effective endpoint remains empty. Application `endpoint: null` is accepted and inherits like an absent or empty value. The top-level endpoint is also nullable, but explicit `null` does not supply an operational endpoint.

Configuration loading intentionally accepts incomplete applications. Immediately before scanning each enabled application—and before constructing an OBS client or sending any request—the scanner requires a non-empty resolved `endpoint`, `appid`, and `apptoken`. Missing fields make only that application manifest entry `failed`, with an actionable `error` and `buckets: []`; sibling enabled applications continue. A missing `name` does not block scanning, and `enabled: false` applications are not scanned.

### Bucket defaults

| Field | Missing-field default/effective behavior |
| --- | --- |
| `enable` | `true` |
| `large_directory_bytes` | `null`; inherit global `large_directory_bytes` (`107374182400` by default) |
| `large_file_bytes` | `null`; inherit global `large_file_bytes` (`10737418240` by default) |
| `inactive_directory_days` | `null`; inherit global `inactive_directory_days` (`180` by default) |
| `filelist_depth` | `null`; inherit global `filelist_depth` (`5` by default) |

Bucket threshold overrides are nullable: explicit `null` means “do not override the global threshold.” This is different from non-nullable fields, where explicit `null` is a validation error and only omission activates the declared default.

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

The final bucket CSV contains directory-level rollups only. Its path and schema are unchanged, and the manifest path and schema are also unchanged. Per-object temporary CSV files are written under `results/<run_id>/_tmp/` while a bucket is being scanned. Aggregation no longer retains all object keys or all bucket directories in memory: each top-level prefix detail CSV, plus `metadata_files.csv` when present, is converted to a sorted source summary. Directory entries are accumulated until `scan.aggregation_max_directories_in_memory` is reached, written as a sorted chunk, and cleared. Each completed chunk and source summary immediately enters an online hierarchical merge with a maximum fan-in of 32, so aggregation does not retain paths for every chunk or source before producing the final bucket CSV. Duplicate object-key rows are intentionally not deduplicated: every occurrence contributes to the rollups.

Aggregation work files are placed below the bucket temporary directory in `_aggregation/chunks`, `_aggregation/prefixes`, and `_aggregation/bucket-runs`. When `scan.keep_temp_files` is `false` (the default), consumed aggregation runs may be removed during processing, and each bucket's whole temporary directory is removed immediately after its final result and before its shared bucket permit is released, for `success`, `partial_failed`, and `failed` buckets. When it is `true`, detail CSVs and all chunk, source-summary, and bucket-run artifacts are retained for every bucket status; this can require substantial disk capacity.

Each run also writes:

- `results/<run_id>/manifest.json`
- `results/<run_id>/scan.log`

`scan.log` includes per-bucket filelist progress lines, metadata progress fields `completed`, `total`, `succeeded`, and `failed`, and objectkeys progress fields `completed`, `total`, `succeeded`, `failed`, `pages`, and `objects`. Metadata `total` is the number of metadata tasks produced by filelist; when that count is zero, the scanner writes one `metadata skipped ... total=0` record. Aggregation records add `aggregation start` fields `sources` and `directory_limit`, `aggregation source progress` fields `completed`, `total`, and `chunks`, an `aggregation merge` record with `inputs` and `fan_in`, and an `aggregation finish` field `directories`; every record also identifies the application and bucket. Every bucket manifest entry includes `started_ms`, `ended_ms`, `started_at`, `ended_at`, and monotonic `elapsed_seconds` timing fields. The total is split into `request_elapsed_seconds` (bucket endpoint, filelist, metadata, and objectkeys collection, including waits/retries/parsing) and `processing_elapsed_seconds` (temporary CSV reading, chunking, external merging, aggregation, and atomic final CSV generation). Aggregation remains in the processing phase. A request-stage failure reports zero processing time; a processing-stage failure preserves both measured phases. Because buckets run concurrently, per-bucket phase durations must not be summed as the run's wall-clock duration.

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
