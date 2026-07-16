# Bounded CSV Aggregation Design

## Goal

Eliminate bucket-processing `MemoryError` failures by replacing the unbounded
in-memory object-key set, directory-statistics dictionary, and final directory
sort with a bounded-memory, pure-CSV external aggregation pipeline.

## Confirmed decisions

- Use the no-deduplication option. If the same object key occurs more than once
  in temporary detail CSVs, every occurrence is counted.
- Trust the effective filelist frontier to keep normal prefix scan ranges
  non-overlapping. The aggregation layer no longer defends against duplicate
  pages or anomalous duplicate API results.
- Use pure CSV chunking and limited-fan-in merge, not SQLite.
- Add `scan.aggregation_max_directories_in_memory` with default `100000`.
- Reject configuration values below `1`.
- Use an internal merge fan-in of `32`; it is not configurable.
- Preserve the final bucket CSV schema, directory ordering, manifest schema,
  timing fields, bucket status behavior, and current bucket concurrency.

## Root cause

`aggregate_bucket()` currently streams input CSV rows, but retains every unique
object key in `seen_object_keys` for the lifetime of the bucket. Production
failed at `seen_object_keys.add(...)`, proving the set exhausted available
memory. Removing only that set is insufficient: `stats_by_directory` still
grows with every distinct directory, and `sorted(stats_by_directory)` creates
another bucket-wide allocation before output.

## Architecture

Aggregation remains a synchronous processing phase after a bucket finishes all
request work. It holds the existing global bucket permit, so this change does
not introduce aggregation threads or new processing concurrency.

The pipeline has two stages:

1. Each object-detail source CSV becomes one sorted source-summary CSV.
2. All source summaries are externally merged into the final bucket CSV.

An object-detail source is either a prefix objectkeys CSV or
`metadata_files.csv`. Source discovery reads only the existing top-level detail
CSVs; aggregation artifacts live below `_aggregation/` and cannot be confused
with object-detail inputs.

```text
_tmp/<appid>/<bucket>/
├── <prefix-detail>.csv
├── metadata_files.csv
└── _aggregation/
    ├── chunks/<source-id>/
    ├── prefixes/<source-id>.csv
    └── bucket-runs/
```

## Summary data model

Intermediate summary CSVs contain only associative merge fields:

```text
directory_path
object_count
total_size_bytes
max_file_size_bytes
empty_file_count
large_file_count
latest_modified_ms
```

Merge operations combine them as follows:

- `object_count`, `total_size_bytes`, `empty_file_count`, and
  `large_file_count`: sum.
- `max_file_size_bytes`: maximum.
- `latest_modified_ms`: maximum of present values; empty when every input is
  empty.

Run, application, bucket, depth, threshold flags, and inactive-day fields are
derived only when writing the final bucket CSV.

## Stage 1: detail source to source summary

Process one source CSV at a time.

1. Read object rows lazily.
2. For every object occurrence, update its containing directory, every parent
   directory, and `/` in the current `stats_by_directory` chunk.
3. Do not track object keys and do not remove duplicates.
4. After the update that makes the number of directory entries reach or exceed
   `aggregation_max_directories_in_memory`, sort that bounded dictionary by
   `directory_path`, atomically write a chunk summary CSV, then clear it.
5. Flush any remaining entries at end of input.
6. Merge the source's sorted chunks with the limited-fan-in algorithm into one
   sorted source-summary CSV. An empty input produces a header-only summary.

One object can add multiple missing ancestor directories, so a chunk can exceed
the configured count by at most the directory depth of the object that triggers
the flush. No earlier object rows remain in memory.

## Limited-fan-in merge

Never open more than `32` sorted summary inputs in one merge operation.

- A merge maintains one current row per input in a heap keyed by
  `directory_path`.
- Equal directory paths are combined and written immediately.
- If an operation has more than 32 inputs, merge groups of at most 32 into
  sorted run files, then repeat until one run remains.
- When temporary retention is disabled, successfully consumed aggregation run
  files may be deleted immediately. Original object-detail CSVs remain under
  the existing bucket lifecycle and are removed when the bucket finalizes.
- When temporary retention is enabled, detail CSVs, chunks, source summaries,
  and bucket merge runs are retained.

The final bucket merge uses the same algorithm. Its last pass writes the current
`FINAL_FIELDS` directly, calculates derived values, counts output directories,
and atomically replaces the final bucket CSV.

## Memory and resource bounds

Chunking bounds the largest directory dictionary and its sort to roughly:

```text
aggregation_max_directories_in_memory + one object's directory depth
```

Merging bounds live rows and file descriptors to the internal fan-in of 32.
The pipeline never retains all object keys, all bucket directories, all source
summaries, or an unbounded number of open files in memory.

Disk use can exceed the original detail CSV size because every object
contributes to all ancestor directories before chunk rows are merged. Multi-pass
runs are deleted as soon as safe when `keep_temp_files=false`; choosing
`keep_temp_files=true` intentionally retains all aggregation artifacts and can
require substantial disk capacity.

## File lifecycle and atomicity

- Every chunk, summary, merge run, and final bucket CSV is written to a sibling
  `.tmp` path and then atomically replaced after close.
- A failed write never replaces a previously completed final bucket CSV.
- `keep_temp_files=false` preserves the current rule: after aggregation or a
  final bucket failure is determined, the bucket's temporary directory is
  deleted by the existing finalizer.
- `keep_temp_files=true` preserves all files below the bucket temporary
  directory for diagnosis.
- Empty or missing detail input still produces the existing header-only final
  bucket CSV and returns zero directories.

## Error handling and status

Malformed object-detail or intermediate summary headers raise `ValueError` with
the offending path. I/O, disk-full, merge, and CSV conversion errors propagate
through the existing processing-stage exception path. The bucket becomes
`failed`, keeps the measured request and processing durations, and does not
abandon sibling buckets. No retry is added for deterministic local processing
errors.

The scanner should emit concise aggregation start, source progress, merge-round,
and finish INFO records so long local processing is distinguishable from a
stalled request. Logging must use counts and paths scoped to the bucket without
including object keys.

## Compatibility

- Final bucket CSV columns and lexical `directory_path` ordering are unchanged.
- Directory aggregation formulas and threshold flags are unchanged.
- Manifest fields and CSV paths are unchanged.
- Processing remains inside the bucket's existing lifecycle and timing phase.
- The intentional behavior change is that duplicate object-key rows are counted
  repeatedly instead of being suppressed.

## Testing

Tests must cover:

1. Default and explicit configuration values, plus rejection of values below 1.
2. A tiny directory limit that forces multiple chunk flushes while producing
   the same final statistics as a single chunk for non-duplicate input.
3. A single very large prefix source that is processed through multiple chunks.
4. Source-summary merge formulas, missing timestamps, and lexical ordering.
5. More inputs than the fan-in, forcing at least two merge rounds without
   opening all files simultaneously.
6. Multiple prefix summaries sharing `/` and other ancestors.
7. Duplicate object keys counted once per occurrence, documenting the selected
   no-deduplication semantics.
8. `metadata_files.csv` participating as an ordinary detail source.
9. Header-only output for missing or empty temporary input.
10. Atomic overwrite behavior and cleanup/retention of aggregation artifacts.
11. Processing failures preserving existing bucket failure and timing behavior.
12. Existing scanner and end-to-end output-schema regressions.

A synthetic stress regression should use a deliberately small configured limit
and many unique directory paths, then assert successful output and multiple
chunk/merge artifacts rather than relying on platform-specific process-memory
measurements.

## Out of scope

- Exact or approximate object-key deduplication.
- SQLite or other database dependencies.
- Changing request concurrency or moving aggregation to worker threads.
- Adding a configurable merge fan-in or byte-accurate memory budget.
- Changing final CSV or manifest schemas.
