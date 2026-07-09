# Current Task

## Current task title

Design: OBS scan behavior corrections, phase ordering, and concurrency defaults

## Current branch

`codex/obs-scan-platform`

## Task status

`completed`

## User goal

Capture the approved design for fixing scanner behavior before implementation:

- Default CLI and `scan.log` output must not print request links, query strings, bodies, or tokens.
- Failed requests such as `404` and `503` must still log safe status/reason diagnostics.
- `scan_shared_buckets: true` must scan scan-capable shared bucket entries.
- Recursive `filelist` scheduling must process whole levels; the task limit is a deeper-recursion threshold, not a hard cap for the current level.
- A bucket must finish all `filelist` discovery and metadata collection before any `objectkeys` collection starts.
- Default global request concurrency should be `150`.
- Default per-bucket `objectkeys` concurrency should be `30`.

## Completed work

- Used the Superpowers brainstorming workflow and kept this turn in design/spec mode.
- Added `docs/superpowers/specs/2026-07-09-obs-scan-behavior-corrections-design.md`.
- Recorded that erroneous empty-bucket OBS interface failures are out of scope for this correction.
- Documented sanitized request failure logging requirements.
- Documented shared-bucket selection behavior for `scan_shared_buckets`.
- Documented level-based `filelist` scheduling semantics and examples.
- Documented the required per-bucket phase order: bucket endpoint, full filelist discovery, metadata, objectkeys, aggregation.
- Documented `objectkeys_concurrency_per_bucket` with compatibility for the old `per_bucket_prefix_concurrency` field.

## Remaining work

- Wait for explicit user approval of the design document.
- After approval, use the Superpowers writing-plans workflow before editing implementation code.
- Implement tests and code changes in a follow-up task.

## Key files changed

- `docs/superpowers/specs/2026-07-09-obs-scan-behavior-corrections-design.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

- `rg -n 'T''BD|TO''DO|place''holder|\\?\\?|pend''ing|may''be|should ''choose' docs/superpowers/specs/2026-07-09-obs-scan-behavior-corrections-design.md docs/current-task.md docs/handoff.md`
- `/opt/homebrew/bin/git diff --check`

## Validation result

- No unresolved draft markers were found after the design wording was tightened.
- `git diff --check` passed.

## Known risks

- No production code has been changed yet; the listed behavior gaps remain until the next implementation task.
- The design intentionally leaves bad empty-bucket OBS interface responses as real failures because the user deferred that issue.

## Next recommended action

Ask the user to review and approve `docs/superpowers/specs/2026-07-09-obs-scan-behavior-corrections-design.md`. If approved, proceed to a writing-plans step, then implement tests and code.
