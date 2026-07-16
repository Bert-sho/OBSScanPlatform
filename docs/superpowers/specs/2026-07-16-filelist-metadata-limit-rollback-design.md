# Filelist Metadata Limit Rollback Design

## Goal

Prevent deep `filelist` traversal from producing an excessive number of
per-object metadata requests. Discovery must stop at the deepest complete BFS
level whose cumulative metadata task count does not exceed a configurable
per-bucket limit.

## Configuration

Add this global scan setting:

```yaml
scan:
  metadata_task_limit_per_bucket: 10000
```

- The default is `10000`.
- The limit is evaluated independently for each bucket.
- Exactly `10000` metadata tasks are allowed; rollback occurs only when the
  candidate count is greater than the configured limit.
- The setting belongs to `ScanSettings`, not bucket threshold overrides.

## Selected Approach

Use a scheduler checkpoint for each active BFS level. Only the current level's
checkpoint must remain in memory because the threshold is evaluated
immediately after every complete level.

Before a level starts, the scheduler records:

- the prefix frontier that would be used if that level were not expanded; and
- the metadata candidates accumulated before that level.

The scanner then processes every task in the level using the existing
concurrent whole-level behavior. After all tasks finish, it calculates the
actual cumulative metadata task count that would be returned by discovery.

## Normal Level Commit

If the cumulative metadata task count is less than or equal to the limit:

- keep the level's discoveries;
- schedule the next complete BFS level subject to the existing depth and
  filelist-task limits; and
- replace the checkpoint when that next level starts.

No request is cancelled merely because another task in the same level crosses
the threshold. This keeps results deterministic regardless of concurrent task
completion order.

## Rollback

If the cumulative metadata task count is greater than the limit:

- restore the frontier saved at the start of the level;
- restore the metadata candidate list saved at the start of the level;
- discard folders, metadata candidates, and queued deeper tasks produced by
  the rejected level;
- stop filelist traversal for the bucket; and
- use the restored frontier as the final `objectkeys` prefix list.

Rollback applies to the entire bucket level, not only the branch that produced
the most metadata candidates.

Example:

```text
root filelist -> a/
a/ filelist -> a/b/ plus enough direct files to exceed the limit
```

The result after rollback uses `a/` as the objectkeys prefix and discards the
metadata candidates and `a/b/` frontier produced while expanding `a/`.

## Root Fallback

The root level has no parent directory checkpoint. Its rollback frontier is the
single prefix `/`.

If root filelist alone produces more metadata candidates than the configured
limit:

- clear all metadata candidates;
- clear discovered child prefixes;
- stop traversal; and
- run one objectkeys task with prefix `/`, which scans the whole bucket.

The existing prefix temp-file naming already maps `/` to a safe root filename.

## Empty and Failed Directories

- A directory confirmed empty during the active level is removed from both the
  live frontier and the rollback frontier, so rollback does not create an
  unnecessary empty-prefix request.
- A directory whose filelist request fails remains in the rollback frontier.
- Existing failure details remain in `PartialErrorSummary`; metadata-limit
  rollback does not convert, remove, or hide request failures.
- Descendants and direct files discovered before a later-page failure continue
  to follow the existing failed-directory pruning behavior.

## Counting Semantics

The threshold uses the count of metadata tasks that discovery would actually
return at the end of the current level, after excluding objects covered by a
retained objectkeys prefix. It is not merely the raw number of file rows seen,
and it is not limited to files added by the newest level.

The count is cumulative from root through the current accepted level. Since
each accepted checkpoint is at or below the limit, restoring the checkpoint
guarantees the final metadata task count is also at or below the limit.

## Logging and Progress

On rollback, emit one INFO log containing:

- application ID;
- bucket name;
- rejected filelist depth;
- observed cumulative metadata task count;
- configured limit; and
- restored prefix task count.

Filelist progress continues to report requests that actually completed and is
not decremented after rollback. Objectkeys progress uses the restored final
prefix count. No manifest or final CSV schema fields are added.

## Testing

Tests must cover:

- configuration default and YAML loading;
- cumulative count equal to the limit continuing normally;
- cumulative count one above the limit rolling back;
- a deeper level restoring the previous frontier and previous metadata list;
- rollback applying to the entire level across multiple branches;
- root overflow producing only `/` and no metadata tasks;
- empty directories being absent from a restored frontier;
- failed directories remaining prefixes with failure details preserved;
- rollback logging and objectkeys prefix totals; and
- an end-to-end bucket scan that never submits more metadata tasks than the
  configured limit.

## Non-Goals

- Do not stop or cancel part of an in-progress BFS level.
- Do not introduce branch-local metadata limits.
- Do not change metadata worker concurrency.
- Do not change object aggregation, manifest fields, or output CSV schemas.
