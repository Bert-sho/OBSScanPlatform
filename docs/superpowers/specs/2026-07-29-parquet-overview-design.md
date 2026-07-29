# Configurable CSV or Parquet Bucket Overviews

## Goal

Add a global YAML setting that selects one bucket-overview output format. The
default format is Parquet. CSV remains available and retains its current file,
schema, aggregation, and download behavior without modification.

Parquet output must use Apache Parquet with Snappy compression, contain no more
than 50,000 rows per file, assign every object to exactly one output path, and
truncate deeper objects into the configured maximum directory depth.

## Configuration

Add these fields under `scan`:

```yaml
scan:
  overview_format: parquet
  max_depth: 4
  file_type_map:
    jpg: 图片
    jpeg: 图片
    png: 图片
    gif: 图片
    cr3: RAW
    nef: RAW
    braw: RAW
    mp4: 视频
    mov: 视频
    avi: 视频
    py: 脚本
    sh: 脚本
    js: 脚本
    ts: 脚本
    onnx: 模型
    ckpt: 模型
    safetensors: 模型
    pt: 模型
    parquet: Parquet
    json: 配置文件
    yaml: 配置文件
    yml: 配置文件
    md: 文档
    pdf: 文档
    zip: 压缩包
    tar: 压缩包
```

- `overview_format` accepts only `csv` and `parquet`, and defaults to
  `parquet`.
- `max_depth` is a non-negative integer and defaults to `4`. `/` has depth 0,
  `/a/` depth 1, and `/a/b/` depth 2.
- `file_type_map` has the complete default shown above. User-supplied entries
  merge into the default map and replace matching extensions.
- Extension keys are normalized to lowercase and have one leading `.` removed
  before matching. Empty extension keys or empty category values are invalid.

These settings are global and apply to every scanned bucket. Existing
threshold settings and bucket-level overrides are unchanged.

## Format Selection and Compatibility

Object discovery and collection do not change. Both output modes consume the
same object-level temporary CSV files.

- `overview_format: csv` invokes the current `aggregate_bucket` implementation
  and writes `results/<run_id>/<appid>/<bucket>.csv`. Its schema and cumulative
  ancestor behavior remain exactly as they are now.
- `overview_format: parquet` invokes a new Parquet aggregation implementation
  and writes only Parquet files. It does not also write a CSV.

The default change to Parquet is intentional. Tests or installations that
require the legacy output must set `scan.overview_format: csv` explicitly.

## Parquet Path Attribution

Each object is attributed to exactly one path: its containing directory,
truncated to `scan.max_depth` when necessary.

For `max_depth: 4`:

- `root.txt` maps to `/`.
- `a/direct.txt` maps to `/a/`.
- `a/b/c/d/file.txt` maps to `/a/b/c/d/`.
- `a/b/c/d/e/deep.jpg` also maps to `/a/b/c/d/`.

Therefore paths below depth 4 contain only directly contained files. A path at
depth 4 contains its direct files and all files from deeper descendants. No
object is accumulated into any shallower ancestor. A path appears only when at
least one collected object maps to it.

The existing no-deduplication contract remains in force: if collection writes
the same object row more than once, every occurrence contributes once to the
Parquet aggregate.

## Parquet Schema

Every field is non-nullable and columns appear in this exact order:

| Field | Parquet type | Meaning |
| --- | --- | --- |
| `bucket_id` | string | OBS bucket ID |
| `bucket_name` | string | OBS bucket name |
| `appid` | string | Owning application ID |
| `path` | string | Attributed directory path |
| `object_count` | int64 | Number of attributed object-row occurrences |
| `total_size` | int64 | Sum of object sizes in bytes |
| `max_file_size` | int64 | Maximum object size in bytes |
| `last_modified` | string | Latest date as `YYYY-MM-DD` |
| `max_depth` | int32 | Depth of this row's `path`, not the configured cutoff |
| `file_types` | string | JSON array of distinct mapped file categories |

Rows are ordered by `path` ascending for deterministic output.

### `last_modified`

For each row, use the maximum valid `last_modified_ms` among the objects
attributed to that row and convert it to a UTC `YYYY-MM-DD` string.

- A path below the configured cutoff compares only its direct files.
- A path at the configured cutoff compares its direct and deeper descendant
  files because all those objects map to that path.
- Missing or invalid timestamps are ignored when at least one valid timestamp
  exists.
- If every attributed object lacks a valid timestamp, use the scan start date
  in UTC.
- An empty bucket has no data row and therefore needs no fallback date.

### `file_types`

Use the final suffix after the last `.` in an object's filename, matched
case-insensitively. A missing suffix or an extension absent from
`file_type_map` maps to `其他`. Categories are de-duplicated and sorted by
their string value, then serialized as a compact JSON array with Unicode
characters preserved, for example `["其他","图片","视频"]`.

## Bounded External Aggregation

Implement Parquet aggregation in a separate module so the current CSV path is
not altered.

1. Stream object rows from each top-level temporary CSV source.
2. Map each object to its single attributed path.
3. Aggregate path summaries in memory until
   `scan.aggregation_max_directories_in_memory` is reached.
4. Write each sorted chunk as an internal CSV summary containing the path,
   numeric metrics, latest millisecond timestamp, and JSON-encoded category
   set.
5. Merge sorted chunks and source summaries with the existing bounded online
   reduction pattern and fan-in limit. During merge, counts and total sizes are
   summed, maximum values are selected, and file-type sets are unioned.
6. Stream the final sorted summaries into Parquet output batches.

The directory-count setting remains the memory bound. File-type sets are also
bounded by the finite number of configured category values plus `其他`.
Intermediate artifacts live under the bucket's existing `_aggregation`
temporary tree and follow `scan.keep_temp_files` cleanup behavior.

## Parquet Output and Atomicity

Use PyArrow as a direct runtime dependency. Write Apache Parquet with Snappy
compression and the explicit non-nullable schema above.

Output layout:

```text
results/<run_id>/<appid>/<bucket>/part-00001.parquet
results/<run_id>/<appid>/<bucket>/part-00002.parquet
```

Each file contains at most 50,000 rows. A 50,001-row result produces two files
containing 50,000 and 1 row. An empty bucket produces one zero-row
`part-00001.parquet` with the complete schema.

Write all parts into a sibling staging directory. Only after every part closes
successfully may the staged directory replace the official bucket directory.
If replacement of a pre-existing result is necessary, preserve it until the
new directory is ready and restore it if cutover fails. Any aggregation,
serialization, compression, or cutover failure removes the staging output and
marks the bucket failed without exposing a partial new result.

## Scanner and Manifest Integration

Extend each bucket manifest entry with:

- `overview_format`: `csv` or `parquet`;
- `overview_path`: the CSV file path or Parquet bucket directory path;
- `overview_files`: an ordered list containing the CSV path or every Parquet
  part path.

Retain the existing `csv_path` field for backward compatibility. In CSV mode
it has its current value; in Parquet mode it is `null`. Failed buckets have no
official overview path and an empty overview-file list unless a previously
completed result from the same run ID was preserved during a failed retry; the
failed attempt does not claim that preserved result as its own output.

Successful Parquet generation follows the current status behavior:

- no recoverable request failures: `success`;
- recoverable request failures with an output: `partial_failed`;
- aggregation or output failure: `failed`.

Processing timing includes either CSV or Parquet aggregation as it does today.

## HTTP Download

Keep the existing CSV endpoint unchanged. Add:

```text
GET /runs/{run_id}/apps/{appid}/buckets/{bucket_name}/parquet/{part_name}
```

The endpoint may return a file only when all of these conditions hold:

- run, application, and bucket segments pass the existing safe-path checks;
- `part_name` matches `part-` followed by exactly five decimal digits and the
  `.parquet` suffix;
- the selected bucket manifest has `overview_format: parquet`;
- the resolved file path is present in that bucket's `overview_files` list;
- the path exists as a regular, non-symlink file below the expected bucket
  result directory.

Return the file with media type `application/vnd.apache.parquet`. Do not add a
ZIP or whole-directory download in this task.

## Error Handling and Cleanup

- Invalid YAML values fail configuration validation before scanning.
- Object-row parsing and intermediate-summary errors fail the current bucket
  through the scanner's existing processing-error path.
- Temporary Parquet directories and incomplete files are removed on failure.
- `keep_temp_files` affects collection and aggregation intermediates, not
  incomplete official Parquet staging output.
- Existing CSV error and atomic replacement behavior is unchanged.

## Tests and Validation

Use test-driven development and add focused coverage for:

- Parquet defaults, explicit CSV selection, invalid formats, non-negative
  `max_depth`, default mapping, extension-key normalization, and override merge;
- exact single-path attribution at root, shallower than the cutoff, at the
  cutoff, and deeper than the cutoff;
- numeric aggregation, duplicate input occurrences, file-type matching and
  ordering, timestamp selection, ignored missing timestamps, all-missing UTC
  scan-date fallback, and path-depth values;
- exact non-nullable PyArrow schema, column order, Snappy compression metadata,
  deterministic row ordering, an empty bucket, and 50,000/50,001 row output;
- staging cleanup and preservation of an existing official output on failure;
- explicit CSV mode regression against the unchanged legacy schema and
  ancestor accumulation;
- scanner result paths, manifest fields, `success`, `partial_failed`, and
  `failed` behavior;
- authorized Parquet downloads plus invalid part names, traversal attempts,
  unlisted files, symlinks, and non-Parquet bucket rejection.

Run the focused tests, all affected scanner/API/end-to-end tests, compilation,
and the full suite. The repository's existing Windows-only baseline failures
must be reported accurately and must not be attributed to this feature.

## Documentation and Handoff

Update the example YAML, English and Chinese READMEs, scan start guide,
`docs/current-task.md`, and `docs/handoff.md`. The final handoff must record the
branch, commits, validation commands and results, known Windows baseline, and
push status as required by `AGENTS.md`.

## Out of Scope

- Changing object discovery, metadata requests, or temporary object CSV schema.
- Changing legacy CSV columns or aggregation semantics.
- Per-application or per-bucket Parquet configuration overrides.
- Configurable Parquet codec or row limit.
- ZIP or whole-directory API downloads.
