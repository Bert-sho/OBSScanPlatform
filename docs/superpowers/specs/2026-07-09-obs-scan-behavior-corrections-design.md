# OBS Scan Behavior Corrections Design

## Context

The previous implementation delivered the main OBS scanner platform, but several runtime behaviors still do not match the latest user requirements:

- Default logs can still expose request links or request details through HTTP exception text.
- Shared buckets are still skipped when `scan_shared_buckets: true` if their `auth` field is not `owner`.
- Recursive `filelist` discovery treats `filelist_task_limit_per_bucket` as a hard cap, but the desired behavior is level-based batching.
- Per-bucket concurrency should mainly cap `objectkeys` requests, not all bucket requests.

The empty-bucket OBS-interface failure is explicitly out of scope for this correction. If an OBS empty bucket endpoint returns an erroneous HTTP/OBS failure, the scanner should continue to treat it as a real failure until the raw interface behavior is clarified.

## Goals

- Remove full request URLs, query strings, and tokens from default `scan.log` and CLI logs.
- Keep useful request failure diagnostics: HTTP status codes such as `404` and `503`, OBS error message summaries, and the logical endpoint category.
- Include scan-capable shared buckets when `scan_shared_buckets: true`.
- Change recursive `filelist` scheduling from hard task truncation to level-based batching.
- Ensure a bucket completes all `filelist` discovery and all metadata collection before starting `objectkeys` collection.
- Increase default total request concurrency to `150`.
- Set the default per-bucket `objectkeys` concurrency to `30`.

## Non-Goals

- Do not convert erroneous OBS empty-bucket failures into successful empty scans.
- Do not print full request URLs in debug or normal logs unless a future explicit debug mode is designed.
- Do not change FastAPI request parameters or add progress streaming.
- Do not redesign final CSV schema.

## Design Summary

Use a small but explicit refactor:

- Keep `OBSClient` as the single request wrapper, but make its surfaced errors safe and structured enough for logging.
- Add a dedicated `filelist` discovery scheduler/planner to own level-based traversal and final-prefix calculation.
- Adjust bucket selection so `scan_shared_buckets: true` means "scan all listbuckets entries that have enough fields to attempt scanning."
- Make `objectkeys` concurrency explicit and default it to `30`, while global request concurrency defaults to `150`.

## Logging and Request Error Sanitization

Default logs must never include:

- Full URL.
- Query parameters.
- Encoded request body.
- Token values.

Request failures should still include safe diagnostics:

```text
OBS request failed endpoint=objectkeys status=503 reason=busy
OBS request failed endpoint=filelist status=404 reason=missing
```

The logical endpoint category can be passed by callers or derived at the callsite. Recommended categories:

- `listbuckets`
- `bucket_endpoint`
- `filelist`
- `metadata`
- `objectkeys`

Implementation should avoid logging `httpx.HTTPStatusError` raw strings directly because those can include request URLs.

The scanner may continue to use `LOGGER.exception()` for stack traces, but the exception message should already be sanitized.

## Shared Bucket Selection

Current behavior effectively requires `auth == "owner"`, which can exclude shared buckets even when `scan_shared_buckets: true`.

New behavior:

- If `scan_shared_buckets` is `false`, keep current self-owned behavior:
  - Include buckets where `auth == "owner"` and `shareFrom` is absent.
- If `scan_shared_buckets` is `true`, include all listbuckets entries that are scan-capable:
  - Required fields: bucket id, bucket name, vendor, region.
  - Do not require `auth == "owner"`.
  - Do not exclude buckets only because `shareFrom` is present.
- If a bucket lacks required fields, skip it and write a concise warning that does not contain request URLs or tokens.
- If a shared bucket later fails on endpoint/object listing APIs, record it as a normal per-bucket failure in the manifest.

## Filelist Scheduling Semantics

`filelist_task_limit_per_bucket` becomes a target threshold for deciding whether to continue deeper, not a hard cap on the current level.

Definitions:

- Root `/` is level 1.
- Directories discovered from root are level 2.
- Directories discovered from level 2 are level 3, and so on.

Scheduling rules:

1. Start with root `/`.
2. Process all tasks in the current level.
3. Collect all directories discovered for the next level.
4. If the next level is within `filelist_depth`, schedule the whole next level when the scanner is still allowed to go deeper.
5. If scheduling the next level causes total filelist tasks to exceed `filelist_task_limit_per_bucket`, still process that entire level.
6. Once the total planned/completed filelist tasks reaches or exceeds the target threshold, do not schedule any deeper level.

Example:

- `filelist_task_limit_per_bucket = 100`
- Root discovers 120 second-level directories.
- All 120 second-level directories are scheduled and processed.
- After level 2 completes, no level 3 filelist tasks are scheduled.
- Final `objectkeys` prefixes should cover the needed data without duplicate scans or missing direct files.

## Per-Bucket Scan Phase Order

For each bucket, the scan phases must be ordered:

1. Get bucket endpoint.
2. Complete all `filelist` discovery.
3. Complete all metadata requests for files discovered directly by `filelist` in the metadata phase.
4. Start `objectkeys` collection only after phases 2 and 3 finish.
5. Aggregate temporary object rows into the final bucket CSV.

This makes the bucket workflow easier to reason about and avoids interleaving objectkeys work with ongoing filelist discovery.

## Metadata Collection

Files returned by `filelist` that need exact byte sizes must be collected through the existing metadata API path before `objectkeys` begins.

The root-file metadata behavior remains required. The filelist scheduler must also return metadata candidates for direct files discovered in expanded non-root directories when those files will not be covered by a selected parent `objectkeys` prefix.

Final `objectkeys` prefixes should be non-overlapping. If a parent prefix is selected for `objectkeys`, descendant prefixes do not need separate objectkeys workers, and direct files under that parent are covered by objectkeys instead of metadata.

## Objectkeys Concurrency

Configuration defaults should become:

```yaml
scan:
  global_request_concurrency: 150
  objectkeys_concurrency_per_bucket: 30
```

Compatibility:

- Keep accepting existing `per_bucket_prefix_concurrency`.
- Prefer `objectkeys_concurrency_per_bucket` when present.
- If only the old field is present, use it as the objectkeys per-bucket concurrency.
- Update examples and docs to recommend the new name.

`filelist` and metadata requests remain globally limited by `global_request_concurrency`. The per-bucket `30` limit primarily applies to concurrent `objectkeys` prefix workers.

## Filelist Planner Boundary

Introduce a small dedicated unit, for example:

```python
class FilelistDiscoveryScheduler:
    ...
```

Responsibilities:

- Track levels.
- Track planned and completed filelist task counts.
- Decide whether to schedule the next level.
- Return discovered final prefixes and metadata candidates.
- Emit progress events or return counters that scanner logs.

The scheduler should not perform final CSV aggregation and should not know about FastAPI or CLI.

## Testing Strategy

Add or update tests for:

- OBS request failure logs/errors include `status=404/503` and logical endpoint category but not full URL/query/token.
- `scan_shared_buckets=false` still excludes shared buckets.
- `scan_shared_buckets=true` includes scan-capable non-owner shared buckets.
- Buckets missing required fields are skipped with a safe warning.
- Filelist level batching:
  - A level with fewer than 100 directories is processed as a full level.
  - A level with more than 100 directories is still processed as a full level.
  - No deeper level is scheduled once the threshold has been reached or exceeded.
- Per-bucket `objectkeys` concurrency is capped at `30`.
- Global request concurrency default is `150`.
- Bucket scan phase order: filelist completes, metadata completes, then objectkeys starts.

## Acceptance Criteria

- No default log line contains full request URLs, query strings, encoded request bodies, or tokens.
- HTTP/OBS failures still expose safe status/reason diagnostics.
- `scan_shared_buckets: true` scans non-owner shared buckets when required fields exist.
- Level-based filelist scheduling matches the user example: all second-level tasks are included even if they exceed 100, and deeper levels are not scheduled after the threshold is reached.
- `objectkeys` concurrency per bucket defaults to `30`.
- Global request concurrency defaults to `150`.
- Empty-bucket OBS interface failures remain real failures in this scope.
