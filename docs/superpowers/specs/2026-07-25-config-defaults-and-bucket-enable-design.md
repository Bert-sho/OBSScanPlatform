# Configuration Defaults and Bucket Enable Design

## Background

`config/apps.yaml` currently requires the global threshold block, the
application list, and several application identity fields. A missing required
field prevents the configuration from loading, even when an operator only
wants to inspect the effective configuration. Bucket entries currently provide
threshold overrides only; there is no per-bucket switch for excluding a known
bucket from the current scan.

This change makes every modeled YAML field safe to omit, adds a bucket-level
`enable` switch, and keeps invalid operational identities from producing OBS
requests.

## Goals

- Add `applications[].buckets.<bucket-name>.enable`.
- Scan every eligible bucket unless its matching YAML entry explicitly sets
  `enable: false`.
- Provide a model-defined default for every global, application, and bucket
  field when the field is absent.
- Preserve explicit application endpoints while allowing applications without
  an endpoint to inherit the global endpoint.
- Load and expose incomplete configurations safely, but fail an enabled
  application before any OBS request when its operational identity is
  incomplete.
- Document every raw default and effective fallback in English and Chinese.

## Non-goals

- Turning the bucket map into a whitelist.
- Adding a skipped-bucket status or manifest entry.
- Treating explicit `null` as equivalent to a missing non-nullable field.
- Changing bucket ownership/shared-bucket eligibility rules.
- Changing concurrency, request, aggregation, CSV, or manifest schemas.

## Configuration Model

Defaults live on the Pydantic models so loading, programmatic access, and
`GET /config/apps` use one source of truth.

### Top-level defaults

| Field | Missing-field default |
| --- | --- |
| `endpoint` | `""` |
| `scan` | all `ScanSettings` defaults |
| `defaults` | all `Thresholds` defaults |
| `applications` | `[]` |

### Scan defaults

| Field | Missing-field default/effective fallback |
| --- | --- |
| `results_dir` | `"results"` |
| `temp_subdir` | `"_tmp"` |
| `keep_temp_files` | `false` |
| `page_size` | `1000` |
| `bucket_concurrency` | `4` |
| `global_request_concurrency` | `150` |
| `per_bucket_prefix_concurrency` | `null` |
| `objectkeys_concurrency_per_bucket` | `null`; effective limit `30` when both objectkeys fields are absent |
| `metadata_concurrency_per_bucket` | `8` |
| `request_timeout_seconds` | `30` |
| `max_retries` | `3` |
| `retry_base_delay_seconds` | `2` |
| `retry_max_delay_seconds` | `60` |
| `filelist_task_limit_per_bucket` | `100` |
| `metadata_task_limit_per_bucket` | `10000` |
| `aggregation_max_directories_in_memory` | `100000` |

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
| `endpoint` | `""`, then inherit a non-empty global endpoint |
| `apptoken` | `""` |
| `enabled` | `true` |
| `scan_shared_buckets` | `false` |
| `buckets` | `{}` |

Endpoint resolution follows this precedence:

1. A non-empty application endpoint is preserved.
2. Otherwise a non-empty global endpoint is copied into the application model.
3. Otherwise the effective endpoint remains an empty string.

An application endpoint explicitly supplied as `null` remains valid input
because the field is nullable, but it inherits a non-empty global endpoint in
the same way as a missing or empty application endpoint.

### Bucket defaults

| Field | Missing-field default/effective fallback |
| --- | --- |
| `enable` | `true` |
| `large_directory_bytes` | inherit global threshold |
| `large_file_bytes` | inherit global threshold |
| `inactive_directory_days` | inherit global threshold |
| `filelist_depth` | inherit global threshold |

Bucket threshold overrides remain nullable so explicit `null` continues to mean
"do not override the global value." Explicit `null` for non-nullable fields is
a validation error; only absence activates their declared defaults.

## Scan Behavior

Application scanning validates operational identity before constructing an OBS
client or sending a request. An enabled application must have a non-empty
resolved endpoint, `appid`, and `apptoken`. If any are missing, its manifest
entry has `status: failed`, an actionable error, and an empty `buckets` list.
The other enabled applications continue normally. A missing application `name`
does not block scanning.

For a valid application, `listbuckets` still runs once. Each returned bucket
passes through the existing scan-capability and owner/shared eligibility rules.
After those rules, the scanner looks up the exact bucket name in
`application.buckets`:

- no YAML entry: scan;
- YAML entry without `enable`: scan;
- YAML entry with `enable: true`: scan;
- YAML entry with `enable: false`: skip and log the reason.

A disabled bucket does not call bucket endpoint, filelist, metadata, or
objectkeys endpoints, does not aggregate a CSV, and does not appear as a bucket
manifest entry. A configured bucket that is not returned by `listbuckets` has
no effect.

## Error Handling

- YAML syntax and explicit invalid types remain configuration errors.
- Explicit `null` remains invalid for non-nullable fields.
- Incomplete operational application identity is a per-application runtime
  failure, not a configuration-load failure.
- Application errors must not expose `apptoken` values.
- Existing sibling-application isolation and run-status rollup remain in use.

## Testing

Tests will cover:

- an empty YAML file producing every top-level and threshold default;
- partial global, scan, application, and bucket mappings;
- complete effective defaults in masked configuration output;
- global endpoint inheritance, explicit application endpoint precedence, and
  nullable application endpoint input;
- explicit `null` rejection for representative non-nullable fields;
- absent/unconfigured buckets and buckets missing `enable` being scanned;
- `enable: false` excluding only the exact matching bucket;
- incomplete enabled application identity returning a failure without an OBS
  request;
- existing config, scanner, API, CLI, and end-to-end behavior remaining green.

## Documentation

- Add `enable: true` to `config/apps.example.yaml`.
- Update the English `README.md` with the bucket switch, endpoint inheritance,
  operational validation, and a complete configuration-default table.
- Add a standalone Chinese `README.zh-CN.md` covering installation, operation,
  outputs, and all configuration defaults.
- Update `docs/scan-start-guide.md` and `CLAUDE.md` where they describe config
  resolution and bucket filtering.
- Complete the mandatory `docs/current-task.md` and `docs/handoff.md` handoff.

## Success Criteria

- Old YAML files continue scanning every eligible bucket unless they opt out
  with `enable: false`.
- Every modeled field can be omitted without a missing-field validation error.
- Effective application endpoints are visible after global inheritance.
- No OBS request is issued for an enabled application missing endpoint, appid,
  or token.
- Task-relevant tests pass; any unrelated full-suite baseline failures are
  recorded rather than hidden.
- The feature branch is committed and pushed without secrets or unrelated
  changes.
