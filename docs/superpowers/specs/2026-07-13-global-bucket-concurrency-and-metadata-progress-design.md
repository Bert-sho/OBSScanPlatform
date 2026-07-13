# Global Bucket Concurrency and Metadata Progress Design

## Context

The scanner currently limits application scans with `scan.app_concurrency` and creates a separate `scan.bucket_concurrency` semaphore inside each application scan. As a result, the configured bucket limit is applied per application rather than across the entire run.

Filelist and objectkeys already emit per-bucket progress logs. Metadata collection only logs individual failures, so operators cannot see how many metadata tasks a bucket has or how far that stage has progressed.

Temporary bucket files are currently removed while application manifest entries are assembled. An early-finishing bucket therefore keeps its temporary directory until every bucket in the same application has finished.

## Goals

- Remove the application concurrency limit.
- Make `scan.bucket_concurrency` the global maximum number of active bucket scans across all applications in one run.
- Add per-bucket metadata task progress logs.
- Delete each bucket's temporary directory immediately after that bucket reaches its final result when `scan.keep_temp_files` is `false`.
- Preserve existing request limits, per-bucket worker limits, CSV output, manifest structure, status rollups, timing semantics, and partial-failure behavior.

## Non-goals

- Do not add a metadata tqdm progress bar.
- Do not change the final CSV schema or manifest schema.
- Do not change `global_request_concurrency`, `metadata_concurrency_per_bucket`, or `objectkeys_concurrency_per_bucket` semantics.
- Do not rewrite historical specifications or implementation plans that describe the earlier concurrency model.

## Selected Approach

Use one shared bucket semaphore created for the run and passed to every application scan. Applications remain the unit of bucket enumeration, client lifetime, status rollup, and manifest grouping, but they no longer acquire an application semaphore.

This is preferred over flattening every application's buckets into a new run-level scheduler because it preserves the existing application error boundary and manifest assembly. It is also preferred over retaining an effectively unlimited application semaphore because that would leave a misleading concurrency layer and configuration field.

## Run and Bucket Scheduling

`Scanner.run()` will:

1. Select all enabled applications, applying the optional `appid` filter as it does today.
2. Create one `asyncio.Semaphore` with capacity `scan.bucket_concurrency`.
3. Start every selected application scan directly with `asyncio.gather`, without an application semaphore.
4. Pass the shared bucket semaphore to each application scan.

Each application will continue to create its own `OBSClient` and request its own bucket list. `listbuckets` calls do not consume bucket permits, but all HTTP calls remain subject to the existing shared `global_request_concurrency` semaphore.

After enumeration, every bucket must acquire the shared bucket semaphore before entering its scan. A bucket permit covers the bucket's entire active lifecycle:

- bucket endpoint lookup;
- filelist discovery;
- metadata collection;
- objectkeys collection;
- aggregation and final CSV generation;
- final result determination; and
- temporary-directory retention or deletion.

The permit is released only after bucket finalization is complete. Therefore, `bucket_concurrency=N` means that no more than N bucket tasks are active across all applications in the run.

Application-level failure isolation and manifest grouping remain unchanged. A bucket exception remains isolated into a failed bucket result where the existing wrapper already provides that behavior.

## Configuration Migration

Remove `app_concurrency` from `ScanSettings` and from the recommended example configuration. Remove current operator documentation that presents it as an active limit.

Pydantic's existing extra-field behavior ignores unknown configuration fields. An older YAML file that still contains `app_concurrency` will therefore continue to load, but the field will have no effect. Masked/current configuration output will no longer include it.

The meaning and defaults of all remaining concurrency settings stay unchanged:

- `bucket_concurrency`: global active bucket limit for the run;
- `global_request_concurrency`: global simultaneous OBS request limit;
- `metadata_concurrency_per_bucket`: metadata worker count within one active bucket; and
- `objectkeys_concurrency_per_bucket`: objectkeys worker count within one active bucket.

## Metadata Progress Logging

For a non-empty metadata task list, emit:

```text
metadata start appid=<appid> bucket=<bucket> total=<total>
metadata progress appid=<appid> bucket=<bucket> completed=<completed> total=<total> succeeded=<succeeded> failed=<failed>
metadata finish appid=<appid> bucket=<bucket> completed=<total> total=<total> succeeded=<succeeded> failed=<failed>
```

Definitions:

- `total` is the number of file keys produced by filelist discovery and passed to metadata collection.
- `completed` increments exactly once when a metadata task finishes.
- `succeeded` increments only when the response produces a valid `ObjectRow`.
- `failed` increments for request failures, unexpected exceptions, or responses that do not produce a valid `ObjectRow`.
- The invariant is `completed = succeeded + failed <= total`.

Each task writes its progress line from its completion path, after its outcome is known. Existing per-object warning logs remain for failed tasks, and a failed metadata task does not cancel remaining metadata tasks or the following objectkeys stage.

For an empty task list, emit only:

```text
metadata skipped appid=<appid> bucket=<bucket> total=0
```

Metadata progress logs must not include tokens, full URLs, encoded request bodies, or object keys. This change adds log output only; it does not add a metadata tqdm bar.

## Immediate Temporary-Directory Finalization

Move bucket temporary-directory handling out of application manifest assembly and into the bucket wrapper's finalization path.

After `_scan_bucket()` returns a `success`, `partial_failed`, or `failed` result, or after the outer bucket wrapper constructs a failed result for an unexpected exception:

- when `keep_temp_files` is `false`, remove that bucket's `_tmp/<appid>/<bucket>/` directory immediately if it exists;
- when `keep_temp_files` is `true`, leave the directory untouched so the later manifest entry can expose `temp_dir` as it does today.

Temporary-directory handling occurs before releasing the shared bucket permit. It no longer waits for other buckets in the application or other applications in the run.

The existing filesystem-error policy is retained: a deletion failure is not silently reported as success. It is logged and allowed to surface through the existing scan failure boundary.

Manifest conversion becomes serialization-only with respect to cleanup. It still includes `temp_dir` only when retention is enabled.

## Error Handling and Invariants

- Application bucket-list failures continue to produce a failed application entry without preventing other application scans from completing.
- Metadata task failures continue to contribute to `PartialErrorSummary` and do not terminate the bucket before objectkeys.
- The global request semaphore remains the final cap on simultaneous HTTP calls even though all applications can enumerate concurrently.
- Bucket finalization happens exactly once per acquired bucket permit.
- A permit is released on every success and failure path.
- Metadata counters are updated in the single asyncio event loop without an `await` between deciding an outcome and updating/logging its counters.

## Testing Strategy

Add TDD coverage for:

1. Two applications sharing one bucket semaphore, proving the peak number of active bucket scans never exceeds `bucket_concurrency`.
2. Concurrent application enumeration with no application limit.
3. Removal of `app_concurrency` from modeled and masked configuration while accepting an old YAML field as ignored input.
4. Metadata progress for all-success tasks.
5. Metadata progress for mixed success, exception, and invalid-response outcomes, including exact counter invariants.
6. The empty metadata `skipped` log.
7. Continuation from failed metadata tasks into objectkeys.
8. Immediate per-bucket temp deletion for `success`, `partial_failed`, and `failed` results when retention is disabled, before other buckets finish.
9. Temp retention and manifest `temp_dir` output for all bucket statuses when retention is enabled.
10. Existing manifest, CSV aggregation, timing, and request-concurrency regression suites.

## Documentation

Update the example YAML, README, and Chinese scan-start guide to state that applications have no independent scan limit and that `bucket_concurrency` is shared globally across all applications. Document metadata progress fields alongside filelist and objectkeys progress.
