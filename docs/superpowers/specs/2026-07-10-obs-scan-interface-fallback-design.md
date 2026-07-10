# OBS Scan Interface Fallback Design

## Context

The scanner calls five OBS-facing interfaces during each run:

1. `listbuckets`
2. `bucket/endpoint`
3. `bucket/filelist`
4. `object/metadata`
5. `list/bucket/objectkeys`

The current client already retries timeout, connection, 5xx, and `success=false`
responses according to `scan.max_retries`. The missing behavior is endpoint-specific
fallback after retries are exhausted or after non-retryable responses, so one local
failure does not unnecessarily stop higher-level scan work.

The approved product direction is:

- Use endpoint-specific fallback behavior.
- Keep strict failure for prerequisite interfaces.
- Keep partial CSV output for local collection failures.
- Mark incomplete bucket output as `partial_failed`.
- Add an `objectkeys` progress bar and related logs per bucket.

## Success Criteria

- `listbuckets` failure affects only the current application.
- `bucket_endpoint` failure affects only the current bucket.
- Root `filelist` failure affects only the current bucket.
- Child-directory `filelist`, single-file `metadata`, and single-prefix `objectkeys`
  failures are recorded as partial failures while the bucket scan continues.
- Buckets with partial collection failures still produce a final CSV from collected
  data and appear in the manifest with status `partial_failed`.
- Empty bucket, empty directory, and empty `objectkeys` compatibility behavior remains
  unchanged.
- Each bucket can show a second progress bar for `objectkeys` prefix tasks.

## Fallback Matrix

| Interface | Dependency level | Timeout, connection error, or 5xx after retries | 4xx or OBS business error | Final status |
| --- | --- | --- | --- | --- |
| `listbuckets` | Application prerequisite | Current application becomes `failed`; other applications continue | Current application becomes `failed` | Application `failed` |
| `bucket_endpoint` | Bucket prerequisite | Current bucket becomes `failed`; other buckets continue | Current bucket becomes `failed` | Bucket `failed` |
| `filelist` for `/` | Bucket discovery root | Current bucket becomes `failed` | Current bucket becomes `failed` | Bucket `failed` |
| `filelist` for child directories | Local discovery task | Skip that subtree and record failure | Skip that subtree and record failure | Bucket `partial_failed` |
| `metadata` for one object | Local collection task | Skip that object and record failure | Skip that object and record failure | Bucket `partial_failed` |
| `objectkeys` for one prefix | Local collection task | Skip that prefix and record failure | Skip that prefix and record failure | Bucket `partial_failed` |

Existing empty-result exceptions remain valid:

- `filelist` with empty `objects={}` and no real error reason is an empty result.
- `objectkeys` with an empty listing shape and no real error reason is an empty result.

## Result Model

Extend bucket scan results with a bounded partial-failure summary named
`partial_errors`.

Recommended shape:

```json
{
  "partial_errors": {
    "filelist_failed_dirs": 1,
    "metadata_failed_files": 2,
    "objectkeys_failed_prefixes": 3,
    "samples": [
      {
        "endpoint": "objectkeys",
        "target": "logs/2026/",
        "status": 503,
        "reason": "busy"
      }
    ]
  }
}
```

Keep at most 10 samples per bucket. Log every failure in full, but keep the manifest
bounded so large bucket scans do not produce oversized manifests.

If no partial failures occur, omit the field or emit an empty structure consistently
with existing manifest style. Prefer omitting it to keep existing successful manifests
compact.

## Scanner Flow

### Application Level

`_scan_application()` continues to isolate applications and buckets:

- `listbuckets` failure returns an application-level `failed` manifest entry.
- Individual bucket failures continue to become bucket-level results.
- Other buckets and applications continue scanning.

### Bucket Level

`_scan_bucket()` should track a per-bucket partial-failure accumulator.

Hard failures:

- `_get_bucket_endpoint()` fails.
- `_discover_root()` fails while processing root `/`.
- CSV aggregation fails.

Partial failures:

- Child `filelist` task fails.
- One `metadata` object fails.
- One `objectkeys` prefix fails.

If aggregation succeeds and the accumulator has any entries, return:

- `status=ScanStatus.PARTIAL_FAILED`
- `csv_path=<final csv path>`
- `error=None`
- bounded `partial_errors` details in the manifest

Reserve the existing `error` field for hard failures where the bucket cannot produce a
complete or partial CSV.

If aggregation cannot run or fails, return `failed`.

## Filelist Behavior

`_process_filelist_task()` needs to distinguish root from child tasks:

- Root task path `/`: propagate exceptions so the bucket fails.
- Child task: catch request/parsing failures, record a partial error, mark the task
  completed, and do not schedule descendants from that subtree.

For progress correctness, both successful and skipped child tasks must call the same
completion path. This prevents the existing `filelist` tqdm progress bar from stalling.

## Metadata Behavior

`_collect_metadata_files()` should handle failures per object key:

- Catch `OBSRequestError`, timeout-derived request errors, and unexpected per-object
  parsing failures around the single metadata call.
- Record the object key as a partial failure.
- Continue draining the queue.
- Append rows only for successful metadata responses.

Metadata failures should not prevent `objectkeys` collection.

## Objectkeys Behavior

`_collect_prefixes()` and `_collect_prefix()` should handle failures per prefix:

- A prefix is successful only after all its `objectkeys` pages complete.
- If any request for that prefix fails after client retries, record the prefix failure
  and stop that prefix.
- Other prefix workers continue.
- Rows collected before a later page failure may remain in temp CSVs. The bucket status
  still becomes `partial_failed`, so downstream users know the result is incomplete.

## Objectkeys Progress And Logs

Add an `objectkeys` tqdm progress bar per bucket when `show_progress=True`.

Recommended behavior:

- Description: `<appid>/<bucket_name> objectkeys`
- Unit: `prefix`
- Total: `len(discovery.prefixes)`
- Progress advances once per prefix, whether the prefix succeeds or fails.
- If there are zero prefixes, do not create a progress bar.

Recommended logs:

```text
objectkeys start appid=... bucket=... total=...
objectkeys progress appid=... bucket=... completed=3 total=20 failed=1
objectkeys prefix failure appid=... bucket=... prefix=... error=...
objectkeys finish appid=... bucket=... completed=20 total=20 failed=2
objectkeys skipped appid=... bucket=... total=0
```

Use sanitized error strings that do not include request URLs or tokens.

## Testing Plan

Add focused tests before implementation:

- `listbuckets` request failure marks only the current application failed.
- `bucket_endpoint` request failure marks only the current bucket failed.
- Root `filelist` request failure marks the bucket failed.
- Child `filelist` request failure skips that subtree, completes progress, aggregates
  existing data, and marks the bucket `partial_failed`.
- `metadata` failure for one object skips that object, keeps other objects, produces
  CSV, and marks the bucket `partial_failed`.
- `objectkeys` failure for one prefix skips that prefix, keeps other prefixes, produces
  CSV, and marks the bucket `partial_failed`.
- Empty `filelist` and empty `objectkeys` responses keep their current non-error
  behavior.
- `objectkeys` progress logs report start, progress, prefix failure, and finish.

## Out Of Scope

- Adding per-endpoint fallback behavior as user-facing configuration.
- Changing global retry timing or concurrency defaults.
- Changing API routes, CLI behavior, or CSV schema beyond manifest status/details.
- Fixing unrelated Windows-specific full-suite test failures.

## Approval

The user approved:

- Option C: endpoint-specific fallback strategy.
- Option A: keep partial CSV output with bucket status `partial_failed`.
- Recommended approach 2: strict prerequisites plus partial collection fallback.
- `objectkeys` progress measured by prefix count.
- Bounded manifest partial-error summary with up to 10 samples.
