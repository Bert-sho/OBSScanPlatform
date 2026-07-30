# Current Task

## Current task title

Create a fully self-contained OBS Scan Platform project guide

## Current branch

`codex/parquet-overview`

## Task status

`wip`

The requested standalone guide, review, and documentation-specific validation
are complete. Repository policy keeps the task at `wip` because the full
Windows test suite still contains the same five pre-existing portability
failures.

## User goal

- Create a Markdown project guide independent of `README.md`.
- Serve both operators/users and developers/maintainers.
- Make the guide fully self-contained, in Chinese with English technical names.
- Cover architecture, technology stack, supported features, complete usage,
  configuration, CLI/API, output formats, aggregation semantics, operations,
  troubleshooting, security, development, testing, extension points, and
  current limitations.
- Continue autonomously through validation, commit, and push.

## Completed work

- Added `docs/project-guide.md` with 15 layered sections and separate reading
  paths for operators and developers.
- Added complete Windows and Linux/macOS setup, CLI, API startup, request, and
  download examples without depending on either README.
- Included a model-validated YAML example with all 20 `ScanSettings` fields,
  all threshold/application/bucket fields, the exact 26-entry built-in file
  type map, default Parquet output, and canonical `aggregation_depth: 4`.
- Documented legacy `max_depth` input compatibility and conflict behavior,
  while distinguishing it from Parquet output `max_depth`.
- Documented all 8 application routes, CLI options/exit behavior, output tree,
  Manifest fields, logs, temporary files, status rollup, retry behavior, and
  current process-local constraints.
- Added one Mermaid scan sequence and one Mermaid component architecture
  diagram.
- Listed all 15 modules under `src/obs_scan_platform` with working relative
  source links and responsibilities.
- Listed the exact 17-column CSV output and exact 10-column non-null Parquet
  schema, including Snappy, 50,000-row splitting, empty part, duplicates,
  cutoff attribution, deepest original directory depth, file type JSON, UTC
  fallback date, bounded merge, and atomic publication.
- Documented security boundaries without overstating them: masked config
  tokens, sensitive failure logs/Manifest details, unauthenticated API,
  path/name risks, strict Parquet whitelist behavior, non-whitelisted CSV
  download, and the run-detail Manifest symlink limitation.
- Added the approved design and implementation plan; corrected the plan's CLI
  probe to use the real installed `obs-scan` entry point.
- Kept `README.md`, `README.zh-CN.md`, runtime source, tests, and example YAML
  unchanged.
- Completed a local whole-task review from `576e9df` through `334ae93`; no
  Critical or Important finding remains.
- Pushed the complete guide task and mandatory handoff through `464c5b8` to
  `origin/codex/parquet-overview`; the final push-result record is committed
  and pushed as the last delivery action.

## Remaining work

- No requested guide content or review work remains.
- Run a representative live OBS scan when an environment is available.
- Address the five unrelated Windows portability failures in a separate task.

## Key files changed

- `docs/project-guide.md`
- `docs/superpowers/specs/2026-07-30-project-guide-design.md`
- `docs/superpowers/plans/2026-07-30-project-guide.md`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

```powershell
& '.superpowers\sdd\.venv\Scripts\obs-scan.exe' --help
& '.superpowers\sdd\.venv\Scripts\obs-scan.exe' scan --help
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_cli.py tests/test_api.py -k "health or config_apps or post_runs or parquet" -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_cli.py tests/test_config.py tests/test_api.py -k "not runs_list_ignores_symlinked_external and not bucket_csv_downloads_file and not run_detail_rejects_backslash_segment" -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q -k "not test_runs_list_ignores_symlinked_external_run and not test_runs_list_ignores_symlinked_external_manifest and not test_bucket_csv_downloads_file and not test_run_detail_rejects_backslash_segment and not test_scan_success_path"
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
```

A Python guide validator also checked numbered headings, Mermaid/code-fence
balance, both YAML blocks, exact model field coverage/defaults, the built-in
file type map, repository example loading, CSV and Parquet fields, routes,
local links, placeholder URLs, and secret patterns.

## Validation result

- CLI root and scan help: exit `0`; documented command/options match output.
- Focused CLI/API documentation tests: `14 passed, 1 skipped, 16 deselected,
  1 warning in 1.18s`.
- The broader planned CLI/config/API command: `64 passed, 1 failed, 1 skipped,
  4 deselected, 1 warning in 1.55s`; the sole failure is the known Windows
  `test_scan_success_path` slash expectation.
- Full suite excluding the exact five known portability cases: `278 passed,
  1 skipped, 5 deselected, 1 warning in 5.78s`.
- Fresh full suite: `278 passed, 5 failed, 1 skipped, 1 warning in 7.76s`.
- The five failures exactly match the pre-task baseline: two Windows symlink
  privilege cases, CSV CRLF response normalization, backslash path semantics,
  and CLI path-separator rendering.
- Compile validation and `git diff --check`: exit `0`.
- Guide contract validator: `15` headings, `2` Mermaid blocks, `2` YAML blocks,
  `21` local links, `20` scan fields, `26` file types, `17` CSV fields, `10`
  Parquet fields, and `8` routes; all assertions passed.

## Known risks

- The guide documents current behavior, including security and portability
  limitations; future runtime changes can make it stale unless documentation
  is updated with the same change.
- Mermaid syntax was structurally checked but not rendered through a dedicated
  Mermaid CLI because the repository does not include that tool.
- No live OBS service was available for a production-scale example scan.
- The result directory contains potentially sensitive detailed request errors
  and must be protected; the built-in API remains unauthenticated.
- The five existing Windows test failures remain outside this documentation
  task.

## Next recommended action

Publish the branch, then use `docs/project-guide.md` as the primary standalone
operator/developer guide. Validate it alongside a representative real OBS scan
and handle Windows portability cleanup as a separate task.
