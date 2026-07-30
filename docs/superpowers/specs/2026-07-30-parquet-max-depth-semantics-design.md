# Parquet Maximum File Depth and Aggregation Depth Configuration Design

## Status

Approved for implementation on 2026-07-30.

## Problem

Parquet rows currently write `max_depth` from the output row's `path`. That is
correct only when a row contains files directly in that path. At the configured
cutoff, deeper files are truncated into the cutoff path, so the current value
loses the actual deepest contributing file location.

The global YAML setting is also named `max_depth`. This collides conceptually
with the Parquet field even though the two values have different meanings:

- the YAML setting is the directory level where deeper objects are grouped;
- the Parquet field is the deepest original file-directory level represented
  by that row.

## Goals

- Make Parquet `max_depth` equal the maximum original directory depth among all
  objects represented by that row.
- Rename the canonical global YAML setting to `aggregation_depth`.
- Keep `max_depth` as a backward-compatible YAML input alias when used alone.
- Reject configurations that specify both names.
- Serialize configuration, including `GET /config/apps`, with only the
  canonical `aggregation_depth` name.
- Preserve all other Parquet, CSV, scanner coordination, manifest, and API
  behavior.

## Non-goals

- Renaming the Parquet output field `max_depth`.
- Changing path attribution, object counts, byte metrics, modification-date
  aggregation, file-type aggregation, compression, schema nullability, or part
  size.
- Changing legacy CSV aggregation.
- Changing filelist discovery's unrelated internal `max_depth` terminology.
- Cleaning up unrelated Windows test portability failures.

## Definitions

File depth is the depth of the file's containing directory after the existing
leading-slash normalization. The filename is not counted:

- `/file.txt` has depth `0`;
- `/a/b/file.txt` has depth `2`;
- `/a/b/c/d/e/file.txt` has depth `5`.

`aggregation_depth` is the maximum output path depth. Objects below that level
are attributed to the cutoff path exactly as they are today. It remains a
non-negative global integer with default `4`; `/` is depth `0`.

## Selected approach

Carry the deepest original file depth through the existing bounded external
aggregation state.

For each input object:

1. Normalize the object key using the existing path rules.
2. Count its non-empty containing-directory components to obtain the original
   file depth.
3. Attribute the object to a single output path using `aggregation_depth`.
4. Create a summary containing both the attributed path and original depth.

`ParquetDirectorySummary` gains a maximum-file-depth value. Combining summaries
for the same path takes the maximum. The internal summary CSV gains a matching
column so the value survives chunk flushing and every merge round. Final
Parquet rows write this aggregate value into `max_depth` instead of recomputing
depth from the truncated output path.

This retains one pass over object detail rows and the current bounded merge
behavior. A second input scan was rejected because it adds I/O and duplicates
path matching. Encoding depth into grouping keys was rejected because it adds
an unnecessary regrouping stage.

## Examples

With `aggregation_depth: 4`:

| Input objects represented by the row | Output `path` | Parquet `max_depth` |
| --- | --- | ---: |
| `/root.txt` | `/` | 0 |
| `/a/b/file.txt` | `/a/b/` | 2 |
| `/a/b/c/d/direct.txt` | `/a/b/c/d/` | 4 |
| `/a/b/c/d/e/file.txt` | `/a/b/c/d/` | 5 |
| files at directory depths 4, 5, and 7 under the same cutoff path | `/a/b/c/d/` | 7 |

An empty bucket still contains zero rows, so it has no `max_depth` value to
populate.

## Configuration compatibility

`ScanSettings` exposes only:

```yaml
scan:
  aggregation_depth: 4
```

A before-validation migration handles raw YAML input:

- neither name: use `aggregation_depth: 4`;
- only `aggregation_depth`: validate and use it;
- only legacy `max_depth`: move its value to `aggregation_depth`, then validate;
- both names: raise a configuration validation error, even if values match.

Negative values remain invalid. The legacy name is an input compatibility path,
not a model field or serialization alias. Model dumps and `GET /config/apps`
therefore return only `aggregation_depth`.

All Parquet-specific function parameters that mean the cutoff are renamed to
`aggregation_depth`. The Parquet schema column remains `max_depth`. Unrelated
filelist-discovery variables keep their existing names.

## Error handling and atomicity

Configuration conflicts fail during normal Pydantic validation before a scan
starts. Invalid legacy values are migrated first and then receive the same
validation as canonical values.

The aggregation output staging, backup, atomic publication, cleanup, and error
propagation behavior remain unchanged. Adding the internal summary column does
not introduce a new output publication path.

## Testing

Configuration tests will prove:

- canonical default is `aggregation_depth == 4`;
- canonical YAML override works;
- legacy `max_depth` alone migrates correctly;
- model dump and `/config/apps` contain only `aggregation_depth`;
- both names fail, including equal values;
- negative values fail through either name.

Aggregation tests will prove:

- direct files report their containing-directory depth;
- a cutoff row reports a deeper original file depth;
- combining chunks and merge rounds preserves the maximum contributing depth;
- root files report zero;
- schema order and `int32` type remain unchanged;
- all existing metrics and the zero-row empty part remain unchanged.

Scanner tests will prove the canonical configured value is passed to the
Parquet aggregator. Existing CSV tests continue to verify that CSV behavior is
unchanged.

Focused Parquet/config/scanner/API tests, compilation, and the full repository
suite will be rerun. Pre-existing Windows portability failures will be reported
separately and will not be hidden.

## Documentation and handoff

Update example YAML, English and Chinese README content, the scan-start guide,
and architecture notes to use `aggregation_depth` and explain the revised
Parquet `max_depth` meaning. Update `docs/current-task.md` and `docs/handoff.md`
with validation evidence, commits, risks, and exact resume instructions before
pushing the feature branch.
