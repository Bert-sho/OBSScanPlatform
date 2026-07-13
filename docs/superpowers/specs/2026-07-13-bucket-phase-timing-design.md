# Bucket Phase Timing Design

## Goal

Split each bucket's existing elapsed scan time in `manifest.json` into remote request/data-collection time and local summary-file processing time.

## Manifest Contract

Every bucket manifest entry keeps its existing timing fields and adds:

```json
{
  "elapsed_seconds": 12.5,
  "request_elapsed_seconds": 10.2,
  "processing_elapsed_seconds": 2.3
}
```

- `request_elapsed_seconds`: monotonic wall-clock duration from bucket start through bucket endpoint, filelist, metadata, and objectkeys completion. It includes semaphore waiting, HTTP response time, pagination, retry backoff, and response parsing performed within those collection stages.
- `processing_elapsed_seconds`: monotonic wall-clock duration after collection completes through temporary CSV reading, object-key deduplication, aggregation, and atomic final CSV generation.
- `elapsed_seconds`: remains the total monotonic bucket duration for backward compatibility.
- For normal completed scans, `elapsed_seconds` equals the sum of the two phase durations apart from floating-point representation.

The fields are per bucket. No run-level or application-level phase totals are added because concurrent buckets overlap and their wall-clock phase durations cannot be summed into a meaningful run duration.

## Timing Boundary

`Scanner._scan_bucket()` records one monotonic start and one phase boundary immediately after `_collect_prefixes()` returns. The same boundary timestamp ends request timing and starts processing timing. A single final timestamp ends total and processing timing.

This avoids gaps or double counting between the phases.

## Failure Semantics

- Failure before the phase boundary: all elapsed time is reported as request time and processing time is `0.0`.
- Failure during aggregation/final CSV generation: request time is fixed at the phase boundary and processing time covers the boundary through failure.
- Partial request failures that are isolated and allow aggregation to complete use the normal completed-scan calculation.
- The outer unexpected bucket wrapper uses its observed elapsed duration as request time and `0.0` processing time because no reliable internal phase boundary is available.

All durations are non-negative floats.

## Data Model and Serialization

Add `request_elapsed_seconds` and `processing_elapsed_seconds` to `BucketScanResult`, defaulting to `0.0` for compatibility with existing constructors. `_bucket_result_to_manifest()` serializes both values next to `elapsed_seconds`.

No status, error, CSV path, retention, or concurrency behavior changes.

## Testing

- Deterministic monotonic-clock tests verify successful request/processing split and exact total relationship.
- Failure tests verify request-stage failure yields zero processing time.
- A processing-stage failure test verifies both phases retain their measured duration.
- Manifest and end-to-end tests verify both fields are serialized and non-negative.
- Existing timing fields remain unchanged and covered.

## Documentation

Update `README.md`, `docs/scan-start-guide.md`, `docs/current-task.md`, and `docs/handoff.md` with field definitions and failure semantics.

## Non-Goals

- Summed duration of individual concurrent HTTP requests.
- Per-endpoint timing such as separate filelist, metadata, or objectkeys durations.
- Run-level or application-level phase aggregation.
- CPU profiling or filesystem-operation sub-timings.
