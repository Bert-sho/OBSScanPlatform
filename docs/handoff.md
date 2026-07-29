# Handoff

## Timestamp

`2026-07-29 17:30:40 +08:00` (Asia/Shanghai)

## Machine/environment

- Workspace: `D:\code\OBSScanPlatform`
- OS/shell: Windows PowerShell
- Branch: `codex/parquet-overview`
- Python validation environment: Git-ignored `.superpowers\sdd\.venv`
- PyArrow under validation: `25.0.0`

## Current branch

`codex/parquet-overview`

## Latest commit before this session

`6add9fd39e2bd7c144e39969394d23f6edccb80a`
(`docs: finalize HTTPX keepalive handoff`)

## Latest commits after this session

- `7efb393` — `docs: design parquet bucket overviews`
- `b1f8bf0` — `docs: plan parquet bucket overviews`
- `5f6c74d` — `feat: configure parquet bucket overviews`
- `0bfc445` — `feat: define parquet overview aggregation semantics`
- `ffeb967` — `feat: write bounded snappy parquet overviews`
- `5fdc8ed` — `feat: generate configured bucket overview format`
- `f32c08b` — `feat: download parquet overview parts`
- `0a1107e` — `docs: document parquet bucket overviews`
- `f2f0c0a` — `test: finalize parquet overview verification`
- The final verification/handoff commits are the commits containing the last
  updates to this file; resolve their exact hashes with
  `git log -2 --oneline` after fetching the branch.

## Summary of what changed

- Added global YAML output selection with Parquet as the default and legacy CSV
  as the alternative.
- Added global Parquet cutoff depth (default 4) and merge-over-default extension
  category mapping.
- Added one-directory-only aggregation with cutoff accumulation, exact required
  schema, UTC date aggregation/fallback, Snappy compression, 50,000-row part
  bounds, and a typed empty part.
- Added atomic per-bucket Parquet directory publication using staging and
  backup paths.
- Added scanner dispatch, output metadata in manifests, and secure downloads
  for manifest-listed Parquet parts.
- Added PyArrow as a runtime dependency and documented configuration, output
  layout, field semantics, and API usage.

## Important decisions and rationale

- `/` is depth 0, and Parquet `max_depth` stores the current row path's depth.
- Objects in directories shallower than the configured cutoff contribute only
  to their direct directory. Objects below the cutoff are truncated into and
  accumulated at the cutoff path. No object contributes to ancestors.
- Extension matching uses the final suffix case-insensitively. Unknown and
  missing suffixes map to `其他`; category arrays are unique and sorted JSON.
- `last_modified` is the maximum valid contributing UTC date. If an aggregate
  has no valid timestamp, it uses the UTC date captured at scan start.
- Parquet output is split by aggregate rows, not input objects. Empty buckets
  still publish `part-00001.parquet` with the exact typed schema and zero rows.
- The existing CSV aggregator was left intact and selected only when YAML asks
  for CSV, limiting regression risk.
- Part downloads require an exact filename pattern and exact manifest
  membership, in addition to path and symlink checks.

## Failed attempts or rejected approaches

- The first 50,001-row split test accidentally used a one-directory memory
  limit, producing approximately 100,000 merge artifacts. The exact pytest
  processes were terminated; the test helper was corrected to use the normal
  limit, while a small exact-merge test retains limit-1 coverage.
- Loading Parquet support before adding PyArrow produced the expected TDD RED
  import failure; PyArrow was then added to project dependencies and the local
  validation environment.
- Upward accumulation for every ancestor was rejected for Parquet because it
  conflicts with the approved one-directory attribution rule. It remains only
  in legacy CSV mode.
- A single downloadable archive was rejected in favor of the approved
  manifest-listed per-part route.

## Review status

- Local whole-change review covered configuration, schema/types, aggregation
  and merge bounds, atomic publication, scanner dispatch, manifest content,
  API authorization, tests, and documentation.
- Documentation drift in `CLAUDE.md` and the package description was corrected.
- The exact default file-type mapping assertion was strengthened during review.
- No Critical or Important issue remains. Operators should treat a run ID as
  one scan identity.

## Current test/build status

Fresh completion verification:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_parquet_aggregation.py tests/test_aggregation.py tests/test_external_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
# 193 passed in 5.02s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_api.py -k parquet -q
# 7 passed, 1 skipped, 19 deselected in 1.03s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
# exit 0

& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
# 256 passed, 5 failed, 1 skipped, 1 warning in 6.46s
```

The full-suite failures exactly match the pre-session Windows baseline:

- two tests require Windows symlink privilege;
- one CSV response assertion differs only by CRLF normalization;
- one backslash-path test follows Windows path semantics;
- one CLI assertion expects a POSIX separator.

The requested feature suites are green. Project policy keeps
`docs/current-task.md` at `wip` because the repository-wide suite is not green.

## Push status

`git push -u origin HEAD` succeeded for `codex/parquet-overview` through
`f2f0c0a`:

```text
* [new branch] HEAD -> codex/parquet-overview
```

The final documentation commit containing this push record is pushed
immediately after creation, followed by an explicit local/remote HEAD equality
check.

## Uncommitted changes, if any

At the time this push-result update was prepared, only `docs/current-task.md`
and `docs/handoff.md` were uncommitted. They are committed and pushed as the
final follow-up action. Expected final working tree state: clean.

## Exact resume instructions for the next Codex session

```powershell
cd D:\code\OBSScanPlatform
git fetch origin
git switch codex/parquet-overview
git pull --ff-only
git status --short --branch
git log -10 --oneline
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_parquet_aggregation.py tests/test_aggregation.py tests/test_external_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_api.py -k parquet -q
```

Confirm the branch is clean and local HEAD equals
`origin/codex/parquet-overview`. For operational validation, run a
representative default Parquet scan, inspect each bucket's ordered
`overview_files`, and read every part with a Parquet-capable client. Handle the
five Windows baseline failures only in a separate task.
