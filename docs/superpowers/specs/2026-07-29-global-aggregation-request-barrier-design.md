# Global Aggregation Request Barrier Design

## Background

Each scan run starts application and bucket coroutines concurrently, but
`Scanner._scan_bucket` calls the selected synchronous CSV
`aggregate_bucket` or Parquet `aggregate_bucket_parquet` function directly
from the asyncio event-loop thread. While aggregation reads, reduces, and
writes overview data, the event loop cannot schedule other bucket coroutines.
A request belonging to another bucket may therefore be waiting to send, read a
response, parse it, or write the returned rows when aggregation blocks the
loop.

The required behavior is deliberate stop-the-world coordination for OBS
traffic: before any bucket aggregates, stop admitting new HTTP attempts, let
currently admitted attempts finish reading and parsing their response, let the
caller synchronously consume that returned page, then run aggregation. Only
one bucket may aggregate globally within a scan run. If several buckets are
ready, their aggregations run consecutively; requests resume only after the
aggregation queue is empty.

## Goals

- Allow at most one bucket aggregation across all applications and buckets in
  one `Scanner.run` invocation.
- Give waiting aggregation work priority over new HTTP attempts and retries.
- Drain already admitted HTTP attempts through response-body reading, status
  and business validation, and JSON parsing before aggregation starts.
- Allow the caller's current synchronous response processing to finish before
  the event loop can switch to aggregation.
- Pause, rather than cancel, queued metadata, objectkeys, filelist, endpoint,
  and listbuckets work; resume it after all queued aggregations finish.
- Preserve existing request concurrency, retry, timeout, keep-alive, partial
  failure, and bucket concurrency behavior.
- Release all coordination state safely on request errors, aggregation errors,
  and task cancellation.

## Non-goals

- Running aggregation in a worker thread or process.
- Allowing requests to overlap aggregation.
- Cancelling queued request tasks or discarding their work.
- Coordinating separate OS processes or independent scan runs. "Global" means
  all applications and buckets in one scan run.
- Adding a YAML setting for aggregation concurrency; the required value is
  fixed at one.
- Changing aggregation algorithms, outputs, request payloads, pagination,
  retry limits, or HTTPX limits.

## Approaches Considered

### Selected: writer-preferred condition coordinator

Use one `asyncio.Condition`-based coordinator for the scan run. HTTP attempts
enter as readers and aggregation enters as a writer. Once any writer waits,
new readers stop entering. Existing readers drain, then queued writers run one
at a time before readers resume.

This directly models the required lifecycle, permits cancellation-safe state
updates, and does not overload request concurrency with phase coordination.

### Rejected: aggregation acquires every request semaphore permit

An aggregator could try to acquire all
`scan.global_request_concurrency` permits. Acquiring them incrementally races
with new request tasks because `asyncio.Semaphore` does not provide the needed
writer-priority contract. It also couples correctness to a configurable
concurrency count and makes cancellation cleanup error-prone.

### Rejected: threaded aggregation plus a one-slot aggregation semaphore

`asyncio.to_thread` would keep requests moving and a semaphore would serialize
aggregations. That avoids event-loop blocking but contradicts the approved
requirement that requests drain and pause while aggregation executes.

## Architecture

Add a focused `ScanPhaseCoordinator` in a new module. One instance is created
by `Scanner` for each scan run and shared by every per-application `OBSClient`
and every bucket aggregation.

The coordinator owns:

- the existing global request-concurrency semaphore;
- an `asyncio.Condition` protecting phase state;
- an active-request count;
- a waiting-aggregation count;
- an aggregation-active flag.

It exposes two cancellation-safe async context managers:

```python
async with coordinator.request_attempt():
    # send, read, validate, and parse one HTTP attempt

async with coordinator.aggregation():
    # synchronously run the selected CSV or Parquet aggregation
```

`request_attempt()` first obtains a global request-concurrency permit, then
waits until no aggregation is active or waiting. Only after admission does it
increment the active-request count. This ordering prevents a request waiting
for ordinary concurrency capacity from delaying a ready aggregation.

`aggregation()` registers itself as waiting before checking active readers.
That registration closes admission for subsequent requests. It waits for the
active-request count to reach zero and for any current aggregation to finish,
then marks itself active. Multiple aggregation waiters are serialized by the
condition. Because the waiting count remains nonzero, readers stay blocked
between consecutive aggregations and resume only after the queue empties. The
writer scope surrounds the existing format dispatch so CSV and Parquet buckets
share the same global aggregation slot.

## Request Data Flow

Each retry attempt in `OBSClient.get_json` gets its own request scope:

1. Wait for a request-concurrency permit.
2. Wait for the aggregation gate to admit readers.
3. Build/send the HTTP request and fully read the response using HTTPX's normal
   non-streaming behavior.
4. Inspect HTTP status, obtain bounded error text when needed, parse JSON, and
   apply OBS business-success validation while still inside the request scope.
5. On success, leave the scope and return parsed data.
6. On failure, leave the scope before retry backoff. A subsequent retry is a
   new reader and therefore pauses when aggregation is waiting or active.

All current scanner call sites consume a successful `get_json` result with
synchronous code before their next `await`. Consequently, after the request
scope exits, the event loop cannot switch to the waiting aggregation until the
current call site has completed its immediate work:

- objectkeys converts the current page and appends its rows to CSV;
- metadata converts and appends the row to its in-memory buffer;
- filelist updates discovery state;
- listbuckets and bucket-endpoint calls normalize their returned payload.

The same bucket cannot aggregate until its complete collection methods return,
so any remaining per-bucket buffered rows are also finalized before that
bucket reaches the aggregation gate.

This synchronous-after-return invariant is part of the design. A future call
site that awaits between `get_json` and consuming its result must introduce an
explicit processing scope or otherwise preserve the invariant.

## Aggregation Data Flow

After filelist discovery, metadata, and objectkeys collection finish for a
bucket:

1. The bucket registers as an aggregation waiter.
2. New HTTP attempts and retries stop entering.
3. Already active attempts finish response reading and JSON validation.
4. Their callers finish the immediate synchronous processing described above.
5. When no request reader remains, the first bucket enters the aggregation
   writer scope and calls the selected `aggregate_bucket` or
   `aggregate_bucket_parquet` function synchronously.
6. If more buckets are queued, they aggregate one at a time without reopening
   request admission.
7. When the last aggregation exits, all paused request tasks are notified and
   compete normally under the existing request-concurrency limit.

The existing bucket semaphore continues to cover the bucket's full lifecycle,
including aggregation and cleanup. This change does not increase the number of
active buckets or alter bucket result ordering.

## Error and Cancellation Handling

- A request scope decrements the active count and releases its concurrency
  permit in `finally`, including HTTP errors and cancellation.
- A cancelled aggregation waiter decrements the waiting count and notifies the
  condition so requests cannot remain paused by a ghost writer.
- An aggregation scope clears the active flag and notifies all waiters in
  `finally`, including aggregation exceptions and cancellation.
- Existing `OBSClient` retry classification and delay calculation remain
  unchanged. Delays occur outside the request scope.
- Existing bucket-level error handling records an aggregation exception as a
  bucket failure. Other buckets' requests resume after the writer scope exits.
- No lock or condition is held while running user-visible logging callbacks or
  while waiting for request-concurrency capacity.

## Testing

Tests will be written before production changes.

### Coordinator unit tests

- an aggregation waiter does not enter until an admitted request exits;
- a request arriving after an aggregation waiter remains paused;
- two aggregation waiters never overlap and both run before paused requests
  resume;
- request cancellation releases active state and capacity;
- aggregation-waiter cancellation reopens request admission;
- aggregation failure releases the writer and resumes requests.

### OBSClient tests

- response reading and JSON validation occur inside the admitted request
  attempt;
- a failed attempt releases its reader before backoff;
- a retry cannot start while aggregation is waiting or active;
- existing retry counts, errors, and concurrency behavior remain green.

### Scanner integration tests

- objectkeys current-page conversion and CSV append complete before a waiting
  aggregation begins;
- requests from another bucket pause while aggregation executes and resume
  afterward;
- aggregations ready from different applications are globally serialized and
  run consecutively before requests resume;
- CSV and Parquet aggregations use the same global writer slot;
- an aggregation exception does not leave the scan request gate closed;
- existing scanner, aggregation, end-to-end, configuration, and HTTP client
  tests retain their behavior.

## Documentation and Handoff

- Document the stop-the-world aggregation/request behavior in `README.md`,
  `README.zh-CN.md`, and `docs/scan-start-guide.md`.
- State that the single aggregation limit is fixed per scan run and is not a
  new YAML option.
- Update `docs/current-task.md` and `docs/handoff.md` with exact validation,
  review, commit, and push state.

## Success Criteria

- At most one CSV or Parquet aggregation call is active per scan run.
- Once aggregation waits, no new request attempt or retry starts.
- Active responses complete reading and JSON parsing before aggregation.
- Current synchronous page processing completes before aggregation takes the
  event loop.
- All queued bucket aggregations run serially before request admission reopens.
- Request and aggregation errors or cancellation cannot leave the gate closed.
- Task-relevant tests pass; unrelated existing platform failures are reported
  without being hidden or modified.
- The current feature branch is reviewed, committed, and pushed with a clean
  working tree and mandatory handoff records.
