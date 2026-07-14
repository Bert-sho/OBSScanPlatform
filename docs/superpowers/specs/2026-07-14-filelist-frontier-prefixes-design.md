# Filelist Frontier Prefixes Design

## Goal

Make each bucket's `objectkeys` tasks scan only the effective frontier of the
`filelist` traversal. A directory successfully expanded by `filelist` must not
also be scanned as an `objectkeys` prefix when its children form the next
frontier.

## Selected Approach

The discovery scheduler maintains a set of candidate final prefixes. Every
folder discovered by `filelist` enters the candidate set. When that folder is
later processed successfully as a `filelist` task, its own prefix leaves the
candidate set because its direct files are handled by metadata and its child
folders become the new candidates.

This stateful frontier is preferred over post-processing all discovered paths:
simply selecting the deepest paths is incorrect for uneven directory trees and
for branches where a `filelist` request fails.

## Traversal Rules

- A newly discovered child directory is a candidate `objectkeys` prefix.
- If depth and task-limit rules allow it, the child is also queued for a later
  `filelist` level.
- After the child's complete paginated `filelist` request succeeds, remove the
  child's own prefix from the candidate set.
- Direct files returned from that expanded directory remain metadata tasks.
- Child directories returned from that expanded directory remain candidates
  until they are themselves expanded successfully.
- A directory not queued because of depth or task limits remains a final
  `objectkeys` prefix.
- A directory whose `filelist` request fails remains a final prefix so
  `objectkeys` can still attempt to cover its subtree.
- When a directory fails after earlier pages discovered descendants, remove
  those descendant candidates, queued tasks, and direct-file metadata
  candidates. The failed directory becomes the boundary for that whole branch.
- A confirmed empty directory is removed from the candidate set.
- The root `/` is never an `objectkeys` prefix.

## Example

For `/ -> alpha/ -> alpha/beta/`, when `alpha/` is successfully expanded but
`alpha/beta/` reaches the traversal boundary, the final prefix list is:

```text
alpha/beta/
```

It does not contain `alpha/`.

## Data and Failure Behavior

The existing per-prefix temporary CSV naming remains unchanged. Because final
prefixes are non-overlapping, parent/child temporary CSV duplication and the
associated duplicate request work are removed during normal successful
traversal. Aggregation's exact-object-key deduplication remains as defensive
protection for API anomalies.

If a `filelist` task fails after returning earlier pages, its prefix remains in
the frontier and its descendants are removed from pending discovery state, so
the failed prefix is the only `objectkeys` boundary for that branch.

## Validation

Regression tests must prove:

- a successfully expanded parent is excluded while its unexpanded child is
  retained;
- an unexpanded directory at the depth/task boundary is retained;
- a failed expanded-directory request remains a final prefix;
- descendants found before a later-page failure are not scanned separately;
- an empty expanded directory is excluded;
- objectkeys progress totals and temporary CSV files match only final frontier
  prefixes;
- existing aggregation deduplication remains intact.
