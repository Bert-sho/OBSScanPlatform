# Handoff

## Timestamp

`2026-07-30 16:35:04 +08:00` (Asia/Shanghai)

## Machine/environment

- Workspace: `D:\code\OBSScanPlatform`
- OS/shell: Windows PowerShell
- Branch: `codex/parquet-overview`
- Python validation environment: Git-ignored
  `.superpowers\sdd\.venv\Scripts\python.exe`

## Current branch

`codex/parquet-overview`

## Latest commit before this session

`576e9df` — `docs: record parquet depth semantics push`

## Latest commits after this session

- `d85559f` — `docs: design comprehensive project guide`
- `ba8c226` — `docs: plan comprehensive project guide`
- `6631bc3` — `docs: add comprehensive project guide`
- `7ec1705` — `docs: clarify project guide concurrency limits`
- `334ae93` — `docs: document project security boundaries`
- `464c5b8` — `docs: finalize comprehensive project guide handoff`
- The final push-result commit is the commit containing the latest version of
  this file; resolve its exact hash with `git log -1 --oneline` after fetching.

## Summary of what changed

- Created `docs/project-guide.md`, a Chinese, fully self-contained guide for
  operators and maintainers that is independent of both README files.
- Covered positioning, supported features, declared dependency floors,
  installation, complete YAML, CLI, API, concurrency, architecture, every
  source module, CSV/Parquet contracts, result layout, Manifest/log behavior,
  failure semantics, security, troubleshooting, development, testing,
  extension points, limitations, and operations.
- Included an executable safe YAML block containing every current scan model
  field and exact defaults, the 26-entry built-in type map, global thresholds,
  all application fields, and complete bucket overrides.
- Included two Mermaid diagrams: the end-to-end scan sequence and component
  architecture/data flow.
- Added the approved design and execution plan. The plan was corrected after
  source inspection because `python -m obs_scan_platform.cli` does not invoke
  `main`; the installed `obs-scan` console script is the real entry point.
- Did not modify README files, configuration examples, runtime code, or tests.

## Important decisions and rationale

- A layered single-document structure serves both operator and developer
  reading paths without duplicating facts across separate guides.
- Examples are fully copyable but use only localhost and `.invalid` hosts plus
  an explicit fake token placeholder.
- Model defaults, source constants, and routes were introspected during
  validation instead of manually assuming the documentation stayed aligned.
- `aggregation_depth` is the only active name in the new YAML; legacy
  `max_depth` appears only in the compatibility explanation, while output
  `max_depth` is documented separately as deepest original file-directory
  level.
- The guide explicitly differentiates legacy CSV ancestor rollup from Parquet
  single-directory attribution and cutoff accumulation.
- Current security gaps are documented rather than hidden: detailed request
  failures may expose query/body data, API has no auth, CSV lacks Manifest
  membership checks, and run detail does not independently reject a symlinked
  Manifest.
- Repository task status remains `wip` because a full suite with any failures
  cannot be marked completed, even though all guide-specific checks passed.

## Failed attempts or rejected approaches

- A planned CLI probe using `python -m obs_scan_platform.cli` produced no CLI
  because the module has no `if __name__ == "__main__"` invocation. The plan
  and guide were corrected to use `obs-scan`.
- An early validation assertion expected 28 built-in file types; introspection
  showed the exact source map has 26 entries. The validator was corrected; the
  guide YAML already matched the source exactly.
- The broader planned CLI/config/API check still included
  `test_scan_success_path` and therefore returned one known Windows slash
  failure (`64 passed, 1 failed, 1 skipped, 4 deselected`). A second fresh run
  excluding the exact five established portability cases passed all remaining
  `278` tests.
- A dedicated Mermaid renderer was not added because it would expand the
  documentation-only task and the repository has no such dependency. Fence
  count, block count, content, and surrounding Markdown were validated.
- Splitting operator and developer content into two files was rejected by the
  user in favor of the approved layered single-guide approach.

## Review status

- `superpowers:requesting-code-review` was invoked. Collaboration rules did
  not authorize a reviewer subagent, so review was performed locally over
  `576e9df..334ae93` using the skill's review checklist.
- Review checked plan/spec alignment, both audiences, self-containment, source
  facts, configuration completeness, CLI/API behavior, output fields,
  concurrency, security boundaries, relative links, placeholders, README
  isolation, and unsupported-feature claims.
- Review findings about unknown YAML keys, internal `source_path`, CSV time
  wording, HTTPX pool limits, request error fields, CSV whitelist behavior, and
  Manifest symlink behavior were corrected and revalidated.
- No Critical or Important issue remains.

## Current test/build status

Fresh task verification:

```powershell
& '.superpowers\sdd\.venv\Scripts\obs-scan.exe' --help
& '.superpowers\sdd\.venv\Scripts\obs-scan.exe' scan --help
# both exit 0; command and scan options match the guide

& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_cli.py tests/test_api.py -k "health or config_apps or post_runs or parquet" -q
# 14 passed, 1 skipped, 16 deselected, 1 warning in 1.18s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q -k "not test_runs_list_ignores_symlinked_external_run and not test_runs_list_ignores_symlinked_external_manifest and not test_bucket_csv_downloads_file and not test_run_detail_rejects_backslash_segment and not test_scan_success_path"
# 278 passed, 1 skipped, 5 deselected, 1 warning in 5.78s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
# 278 passed, 5 failed, 1 skipped, 1 warning in 7.76s

& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
# exit 0

git diff --check
# exit 0
```

The five full-suite failures exactly match the baseline: two symlink creation
privilege failures, CSV response CRLF normalization, Windows backslash path
semantics, and CLI path-separator rendering. No documentation-specific or
non-baseline test fails.

The standalone guide validator reported:

```text
PASS headings=15 mermaid=2 yaml_blocks=2 links=21 scan_fields=20
file_types=26 csv_fields=17 parquet_fields=10 routes=8
```

## Push status

`git push -u origin HEAD` succeeded for `codex/parquet-overview` through
`464c5b8`:

```text
576e9df..464c5b8  HEAD -> codex/parquet-overview
```

The final documentation commit containing this push record is pushed
immediately after creation, followed by an explicit local/remote HEAD equality
check.

## Uncommitted changes, if any

At this push-result snapshot only `docs/current-task.md` and `docs/handoff.md`
are uncommitted. They are committed and pushed as the final follow-up action.
Expected final state: clean working tree, local HEAD equal to
`origin/codex/parquet-overview`.

## Exact resume instructions for the next Codex session

```powershell
cd D:\code\OBSScanPlatform
git fetch origin
git switch codex/parquet-overview
git pull --ff-only
git status --short --branch
git log -8 --oneline
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q -k "not test_runs_list_ignores_symlinked_external_run and not test_runs_list_ignores_symlinked_external_manifest and not test_bucket_csv_downloads_file and not test_run_detail_rejects_backslash_segment and not test_scan_success_path"
```

Confirm the working tree is clean and local HEAD equals
`origin/codex/parquet-overview`. Use `docs/project-guide.md` for project
onboarding and operation. For live validation, run a small representative OBS
scan and compare Manifest, CSV, and Parquet semantics with sections 11–13.
Treat the five Windows portability failures as a separate task.
