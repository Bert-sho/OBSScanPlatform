# OBS Request Fallback, Failure Diagnostics, and Objectkeys Progress Design

## Status

Approved in conversation on 2026-07-12. This document defines design only; implementation is a separate task gated by a reviewed implementation plan.

## Baseline and problem statement

The original local snapshot had two coupled failure modes:

1. CLI scans emitted a full `httpx` request URL for every successful request because root logging was configured at `INFO` and received the `httpx` request logger.
2. `OBSClient.get_json()` raised after a final request failure, while scanner phases only caught errors at the whole-bucket boundary. A failed `filelist`, `metadata`, or `objectkeys` request therefore stopped the bucket instead of preserving partial results.

The shared branch advanced while this design was being discussed. Commit `5930bee` already contains a first fallback revision: successful `httpx` URLs are suppressed, child filelist/metadata/objectkeys failures can produce partial output, `partial_errors` is recorded, and basic objectkeys prefix progress exists.

This design is the approved next revision on top of that implementation. It supersedes conflicting behavior in `docs/superpowers/specs/2026-07-10-obs-scan-interface-fallback-design.md`, specifically sanitized failure diagnostics, root filelist hard failure, bounded-only error detail, and basic progress counters.

Operators also need:

- full failed-request URLs and response bodies in logs without redaction;
- endpoint-specific fallback behavior for all five OBS interfaces;
- detailed per-bucket errors in `manifest.json`;
- per-bucket objectkeys task progress in CLI tqdm output and `scan.log`;
- per-bucket start time, end time, and elapsed time in `manifest.json`.

## Goals

- Preserve the existing suppression of URLs for successful requests.
- Log every failed request attempt with the full URL and response details.
- Retry transient failures up to three times after the initial attempt.
- Isolate recoverable failures at the smallest useful scanning unit.
- Continue producing a partial bucket CSV when `filelist`, `metadata`, or `objectkeys` work is incomplete.
- Preserve hard-failure boundaries where scanning cannot proceed.
- Retain the existing manifest `error` and `partial_errors` fields while adding structured error details.
- Record objectkeys task completion accurately under concurrency.
- Add timing fields to every bucket result.

## Non-goals

- Do not change OBS endpoint paths or request parameters.
- Do not make fallback policies configurable in this version.
- Do not change the final bucket CSV schema.
- Do not add new API routes or request parameters.
- Do not redact tokens, encoded request bodies, object keys, or other values from failure URLs or bodies. The resulting logs and manifests must be treated as sensitive data.

## Architecture

Use a two-layer design:

1. `OBSClient` owns request preparation, retries, response parsing, failed-attempt logging, and a structured final exception.
2. `Scanner` owns endpoint semantics. It decides whether a final request failure stops an application, stops a bucket, or is collected as a recoverable bucket error.

This keeps transport behavior consistent without teaching the HTTP client what an OBS bucket, directory, object, or prefix means.

### Request failure representation

`OBSRequestError` will carry transport-level details rather than only a formatted message:

- logical endpoint name;
- final prepared request URL;
- HTTP status code, or `None` when no response exists;
- detailed reason;
- response body text;
- whether the response body was truncated;
- original response-body character count;
- exception type;
- total attempts made.

The response body retained in logs, exceptions, and manifests is limited to 2048 characters. When the original body is longer, the first 2048 characters are stored with `response_body_truncated=true` and the original character count.

Scanner call sites add business context when converting a final request error into a manifest error:

- `bucket_endpoint`: bucket name and bucket ID;
- `filelist`: directory path and pointer;
- `metadata`: object key;
- `objectkeys`: prefix and next marker.

## Logging behavior

### Successful requests

The scanner will prevent `httpx` INFO request messages from propagating into normal CLI and file output. Successful requests will not print full URLs.

Existing scanner lifecycle and progress INFO logs remain unchanged except for richer objectkeys progress records.

### Failed attempts

Every failed attempt logs a single structured message containing:

- endpoint;
- `attempt` and `max_attempts`;
- full prepared request URL without redaction;
- HTTP status when present;
- reason;
- response body up to 2048 characters;
- truncation flag and original character count;
- exception type.

For timeouts and connection failures, there is no response. The log uses:

- `status=unknown`;
- `response_body=<no response>`;
- a reason containing the exception type and exception text.

Recovered transient failures remain in `scan.log` but do not appear in `manifest.json`. Only failures that remain after retry handling are added to manifest errors.

## Retry policy

`max_retries` defaults to `3`. It means one initial attempt followed by at most three retries, for a maximum of four attempts.

Retry these failures:

- connection errors;
- timeouts;
- HTTP 408;
- HTTP 429;
- all HTTP 5xx responses;
- HTTP success responses whose JSON contains boolean `false` or case-insensitive string `"false"` in `success`;
- responses that cannot be parsed as JSON when JSON is required.

Do not retry other HTTP 4xx responses, including 400, 401, 403, and 404.

Existing exponential backoff settings continue to control delays. Every attempt still acquires the global request semaphore independently.

## Endpoint fallback policy

| Endpoint | Failure boundary | Fallback behavior | Result state |
|---|---|---|---|
| `listbuckets` | Application | Stop the current application because the bucket set is unknown. Other applications continue. | Application `failed` |
| `bucket_endpoint` | Bucket | Stop the current bucket because data endpoints cannot be called. Other buckets continue. | Bucket `failed` |
| `filelist` | Directory page, including root `/` | Keep previously discovered files and folders, stop the failed directory's remaining pagination, and continue other already discovered directory tasks. A root failure leaves no newly discovered root work but still proceeds to aggregation. | Bucket `partial_failed` |
| `metadata` | Object | Skip the failed object and continue other object metadata tasks. | Bucket `partial_failed` |
| `objectkeys` | Prefix page | Keep data from successful earlier pages, stop the failed prefix's remaining pagination, and continue other prefixes. | Bucket `partial_failed` |

All HTTP errors, exhausted transient errors, `success=false` responses, and JSON parsing failures enter this policy through `OBSRequestError`.

### Partial CSV rules

- Recoverable final failures do not skip aggregation.
- Successfully collected rows remain in temporary CSV files and are included in the final bucket CSV.
- A bucket with recoverable failures but no valid rows still produces a header-only CSV.
- Such a bucket is always `partial_failed`, even if every recoverable request failed.
- A `bucket_endpoint` hard failure produces no bucket CSV.

### Concurrent worker behavior

Metadata and objectkeys workers catch recoverable final request errors inside each work item. A worker records the error, updates its task outcome, calls `queue.task_done()`, and continues consuming work. The surrounding `asyncio.gather()` therefore waits for all workers rather than returning on the first request exception.

Unexpected programming or filesystem exceptions are not silently converted into request fallback. They continue to the bucket-level hard-failure handler.

## Objectkeys progress

An objectkeys task is one final non-overlapping prefix. Pagination does not add to the task total.

Each bucket tracks:

- `total`: number of objectkeys prefixes;
- `completed`: prefixes whose processing ended, whether successful or failed;
- `succeeded`: prefixes that completed all pages;
- `failed`: prefixes stopped by a final request failure;
- `pages`: cumulative successfully received objectkeys pages;
- `objects`: cumulative valid object rows written from those pages.

The invariant is:

```text
succeeded + failed == completed <= total
```

After every prefix finishes, write:

```text
objectkeys progress appid=app.one bucket=bucket-a completed=43 total=100 succeeded=42 failed=1 pages=87 objects=12560
```

CLI scans display one tqdm bar per bucket during its objectkeys phase. Its description and postfix expose the same counters. A failed prefix increments both `completed` and `failed`, ensuring the bar reaches its total. FastAPI-triggered scans do not create tqdm bars but write the same progress logs.

If a bucket has no objectkeys prefixes, it does not create a bar and proceeds normally.

## Bucket status and timing

Bucket timing begins after the bucket acquires the configured bucket-concurrency slot and immediately before its first scan operation. Timing ends after aggregation completes or after a hard failure is finalized.

Every bucket manifest entry, including `success`, `partial_failed`, and `failed`, contains:

```json
{
  "started_ms": 1783785600123,
  "ended_ms": 1783785723456,
  "started_at": "2026-07-12T00:00:00.123Z",
  "ended_at": "2026-07-12T00:02:03.456Z",
  "elapsed_seconds": 123.333
}
```

Epoch fields and ISO strings represent the same UTC instants. `elapsed_seconds` uses a monotonic clock so wall-clock adjustments cannot produce a negative or inaccurate duration.

Bucket status is derived as follows:

- `success`: aggregation completed and no final recoverable request errors were recorded;
- `partial_failed`: aggregation completed with at least one final recoverable request error;
- `failed`: a hard bucket failure prevented a usable bucket result.

Existing application and run rollups continue to treat mixed child results as `partial_failed`.

## Manifest error schema

The existing bucket `error` and `partial_errors` fields remain for backward compatibility. Add an `errors` array for all bucket states. The same per-bucket collector feeds all three representations:

- `error`: concise aggregate string for existing simple readers, including partial failures;
- `partial_errors`: existing bounded counters and samples retained for shared-branch readers;
- `errors`: complete detailed list of final request failures required for diagnosis.

For no errors:

```json
{
  "error": null,
  "errors": []
}
```

For one or more errors, `error` is a concise aggregate suitable for existing readers:

```text
3 request failures; metadata=2, objectkeys=1; first: metadata object_key=root.txt status=404 reason=not found
```

Each `errors` item contains:

```json
{
  "endpoint": "objectkeys",
  "scope": "prefix",
  "scope_value": "photos/2025/",
  "url": "https://example.invalid/rest/boto3/s3/list/bucket/objectkeys?...",
  "status_code": 503,
  "reason": "Service Unavailable: busy",
  "response_body": "{\"success\":false,\"msg\":\"busy\"}",
  "response_body_truncated": false,
  "response_body_original_chars": 30,
  "exception_type": "OBSRequestError",
  "attempts": 4
}
```

For `filelist` and `objectkeys`, `scope_value` identifies the directory or prefix, while the unredacted URL retains the pagination cursor. For `metadata`, `scope_value` identifies the object key. For `bucket_endpoint`, it identifies the bucket.

A hard `bucket_endpoint` failure uses the same `errors` schema and produces a detailed `error` summary. A `listbuckets` failure remains an application-level error because no bucket records can be constructed reliably.

## Data flow

1. Prepare a request and retain its full encoded URL.
2. Execute the request under the global semaphore.
3. On failure, capture body/exception details, log the attempt, and classify retryability.
4. Return parsed JSON on success or raise one final structured `OBSRequestError`.
5. At the scanner call site, either:
   - propagate a hard failure to the application or bucket boundary; or
   - add contextual error details, mark the bucket partial, and continue the next work item.
6. Complete aggregation for recoverable failures.
7. Persist bucket status, errors, progress-derived results, and timing in `manifest.json`.

## Testing strategy

Implementation will use test-driven development.

### Request client tests

- Successful requests do not emit `httpx` URL logs.
- Every failed attempt logs the full URL, response body, attempt number, and failure type.
- Response bodies are retained at 2048 characters with correct truncation metadata.
- Timeout and connection failures record `<no response>`.
- 408, 429, 5xx, `success=false`, and invalid JSON retry three times after the initial request.
- Other 4xx responses do not retry.
- The final exception contains complete structured details.

### Scanner fallback tests

- `listbuckets` failure stops one application without stopping others.
- `bucket_endpoint` failure stops one bucket without stopping others.
- A failed root filelist request produces a header-only or partial CSV with bucket `partial_failed` instead of hard-failing the bucket.
- A failed child filelist directory does not stop other discovered directories.
- A failed metadata object does not stop other objects.
- A failed objectkeys prefix does not stop other prefixes and retains earlier successful pages.
- Worker gathers complete all work items despite recoverable failures.
- Unexpected non-request failures still hard-fail the bucket.

### Manifest and CSV tests

- Partial failures still generate a CSV containing all successfully collected rows.
- No valid rows produce a header-only partial CSV.
- `error` remains a string or `null`, and `errors` captures multiple detailed failures.
- All bucket states contain consistent start, end, and elapsed fields.
- A hard bucket failure has no CSV path.

### Progress tests

- tqdm total equals the number of final prefixes.
- Successful and failed prefixes both advance `completed`.
- `succeeded + failed == completed` throughout the phase.
- Page and object totals include only successful pages and valid rows.
- CLI mode displays tqdm; API mode only logs progress.
- Concurrent completion order does not alter final counters.

### End-to-end test

Simulate multiple recoverable interface failures in one bucket and verify:

- the scan reaches other work items and other buckets;
- the bucket is `partial_failed`;
- the final CSV contains successful partial data;
- manifest error details identify every final request failure;
- objectkeys progress reaches its total;
- bucket timing fields are present and ordered.

## Compatibility

- Existing OBS calls and final CSV columns remain unchanged.
- Existing manifest fields, including `partial_errors`, remain; `errors` and bucket timing fields are additive.
- Existing `error` readers continue to receive a summary string.
- Existing concurrency settings remain valid.
- `max_retries` changes its default from 5 to 3 while retaining the existing configuration field.
- Existing API routes remain unchanged.

## Acceptance criteria

- A normal CLI scan no longer prints a URL for every successful HTTP request.
- Every failed request attempt logs its unredacted URL and up to 2048 characters of response text.
- The five OBS endpoints follow the approved hard-failure and fallback table.
- Recoverable request failures produce a partial CSV and bucket `partial_failed` status.
- Bucket manifest entries contain a detailed summary, structured error list, start/end timestamps, and elapsed duration.
- Objectkeys progress is visible in CLI tqdm output and recorded in `scan.log` with accurate success/failure/page/object counters.
- All targeted and full test suites pass.
