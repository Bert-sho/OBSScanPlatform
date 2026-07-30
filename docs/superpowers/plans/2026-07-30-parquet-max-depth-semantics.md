# Parquet Maximum File Depth Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Parquet `max_depth` report the deepest original containing-directory level represented by each row, and rename the canonical global cutoff setting to `aggregation_depth` with safe legacy input compatibility.

**Architecture:** Migrate raw `scan.max_depth` input into the canonical `ScanSettings.aggregation_depth` field before Pydantic field validation, rejecting ambiguous dual configuration. Carry a new `max_file_depth` value through the existing `ParquetDirectorySummary` and bounded internal CSV merge pipeline, then write that value to the unchanged Parquet `max_depth` column.

**Tech Stack:** Python 3.11+, Pydantic 2, PyArrow/Apache Parquet, pytest, FastAPI TestClient, YAML, Git.

## Global Constraints

- `aggregation_depth` is global, non-negative, and defaults to `4`; `/` is depth `0`.
- Legacy YAML `max_depth` is accepted only when `aggregation_depth` is absent.
- Supplying both YAML names is a validation error even when their values match.
- Model serialization and `GET /config/apps` expose only `aggregation_depth`.
- File depth counts the containing-directory components and excludes the filename.
- Parquet schema field name and type remain `max_depth int32 non-null`.
- CSV aggregation, path attribution, all non-depth Parquet metrics, Snappy compression, the 50,000-row part limit, atomic output publication, and request/aggregation coordination remain unchanged.
- Use the Git-ignored `.superpowers\sdd\.venv` Python environment for validation.
- Update `docs/current-task.md` and `docs/handoff.md` before final push.

---

## File Structure

- Modify `src/obs_scan_platform/config.py`: canonical setting and raw legacy-name migration.
- Modify `src/obs_scan_platform/parquet_aggregation.py`: original file-depth calculation and bounded summary propagation.
- Modify `src/obs_scan_platform/scanner.py`: pass the canonical cutoff argument without changing coordination.
- Modify `tests/test_config.py`: configuration default, migration, conflict, serialization, and validation tests.
- Modify `tests/test_parquet_aggregation.py`: direct, cutoff, root, and merge-round depth semantics.
- Modify `tests/test_scanner.py`: scanner-to-aggregator canonical argument contract.
- Modify `tests/test_api.py`: `/config/apps` canonical serialization contract.
- Modify `config/apps.example.yaml`, `README.md`, `README.zh-CN.md`, `docs/scan-start-guide.md`, and `CLAUDE.md`: operator-facing naming and semantics.
- Modify `docs/current-task.md` and `docs/handoff.md`: final evidence and cross-machine resume state.

### Task 1: Canonical Configuration and Legacy Migration

**Files:**
- Modify: `tests/test_config.py:512-578`
- Modify: `tests/test_api.py:90-120`
- Modify: `src/obs_scan_platform/config.py:38-79`

**Interfaces:**
- Consumes: raw `scan` YAML/dict values accepted by `ScanSettings.model_validate`.
- Produces: `ScanSettings.aggregation_depth: int`, default `4`, `ge=0`.
- Produces: `ScanSettings.migrate_legacy_max_depth(value: object) -> object` before-validator behavior.
- Produces: canonical `model_dump()` and `AppConfigFile.masked_dict()` output without `max_depth`.

- [ ] **Step 1: Write failing configuration tests**

Replace the current depth assertions and add exact compatibility tests:

```python
def test_overview_settings_default_to_parquet_depth_four_and_default_type_map():
    config = AppConfigFile()
    assert config.scan.overview_format == "parquet"
    assert config.scan.aggregation_depth == 4
    assert "max_depth" not in config.scan.model_dump()


def test_aggregation_depth_loads_canonical_yaml_override(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text("scan:\n  aggregation_depth: 2\n", encoding="utf-8")
    scan = load_config(config_file).scan
    assert scan.aggregation_depth == 2
    assert scan.model_dump()["aggregation_depth"] == 2
    assert "max_depth" not in scan.model_dump()


def test_legacy_max_depth_loads_as_canonical_aggregation_depth(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text("scan:\n  max_depth: 3\n", encoding="utf-8")
    scan = load_config(config_file).scan
    assert scan.aggregation_depth == 3
    assert "max_depth" not in scan.model_dump()


@pytest.mark.parametrize(
    "scan",
    [
        {"aggregation_depth": 4, "max_depth": 4},
        {"aggregation_depth": 2, "max_depth": 4},
    ],
)
def test_aggregation_depth_rejects_canonical_and_legacy_names_together(scan):
    with pytest.raises(ValidationError, match="aggregation_depth.*max_depth"):
        AppConfigFile(scan=scan)


@pytest.mark.parametrize("name", ["aggregation_depth", "max_depth"])
def test_aggregation_depth_rejects_negative_value_through_either_name(name: str):
    with pytest.raises(ValidationError):
        AppConfigFile(scan={name: -1})
```

Update the file-type override fixture to use `aggregation_depth: 0` and assert
`scan.aggregation_depth == 0`.

- [ ] **Step 2: Write the failing `/config/apps` serialization test**

Extend the existing config endpoint test with a legacy-input fixture and assert
that only the canonical name is returned:

```python
def test_config_apps_normalizes_legacy_max_depth_to_aggregation_depth(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text("scan:\n  max_depth: 3\n", encoding="utf-8")
    client = TestClient(api.create_app(config_path=config_file, results_dir=tmp_path / "results"))

    response = client.get("/config/apps")

    assert response.status_code == 200
    assert response.json()["scan"]["aggregation_depth"] == 3
    assert "max_depth" not in response.json()["scan"]
```

- [ ] **Step 3: Run the tests to verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_api.py -k "aggregation_depth or legacy_max_depth or overview_settings_default" -q
```

Expected: failures because `aggregation_depth` does not exist, dual names are
not rejected, and serialized configuration still contains `max_depth`.

- [ ] **Step 4: Implement minimal canonical migration**

In `ScanSettings`, replace the field and add a model before-validator ahead of
the existing file-type validator:

```python
class ScanSettings(BaseModel):
    overview_format: Literal["csv", "parquet"] = "parquet"
    aggregation_depth: int = Field(default=4, ge=0)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_max_depth(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        has_canonical = "aggregation_depth" in value
        has_legacy = "max_depth" in value
        if has_canonical and has_legacy:
            raise ValueError("aggregation_depth and max_depth cannot both be configured")
        if not has_legacy:
            return value
        migrated = dict(value)
        migrated["aggregation_depth"] = migrated.pop("max_depth")
        return migrated
```

Do not define a `max_depth` model field or serialization alias.

- [ ] **Step 5: Run focused and full configuration/API tests**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_api.py -k "config_apps" -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit configuration migration**

```powershell
git add src/obs_scan_platform/config.py tests/test_config.py tests/test_api.py
git commit -m "feat: rename parquet aggregation depth setting"
```

### Task 2: Preserve Actual Maximum File Depth Through Bounded Aggregation

**Files:**
- Modify: `tests/test_parquet_aggregation.py:11-285`
- Modify: `src/obs_scan_platform/parquet_aggregation.py:18-457`

**Interfaces:**
- Consumes: `ObjectRow.object_key` and canonical `aggregation_depth: int`.
- Produces: `file_directory_depth(object_key: str) -> int`.
- Produces: `attributed_directory_path(object_key: str, aggregation_depth: int) -> str`.
- Produces: `ParquetDirectorySummary.max_file_depth: int` combined with `max()`.
- Produces: `summary_for_object(row: ObjectRow, *, aggregation_depth: int, file_type_map: dict[str, str]) -> ParquetDirectorySummary`.
- Produces: `aggregate_bucket_parquet(..., aggregation_depth: int, ...) -> tuple[Path, ...]`.

- [ ] **Step 1: Write failing file-depth and summary tests**

Rename cutoff parameters in the test helper and add:

```python
@pytest.mark.parametrize(
    ("object_key", "expected"),
    [
        ("file.txt", 0),
        ("/a/b/file.txt", 2),
        ("/a/b/c/d/e/file.txt", 5),
    ],
)
def test_file_directory_depth_excludes_filename(object_key: str, expected: int):
    from obs_scan_platform.parquet_aggregation import file_directory_depth
    assert file_directory_depth(object_key) == expected
```

Update the summary-combine test to combine a direct `/a/` file with a deeper
file attributed to the same cutoff path, then assert:

```python
assert combined.max_file_depth == 3
```

- [ ] **Step 2: Write failing end-to-end merge-depth test**

Use separate input files and `max_directories_in_memory=1` so the same cutoff
path is combined through internal CSV merge runs:

```python
def test_aggregate_bucket_parquet_preserves_deepest_original_depth_across_merge_runs(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(temp_dir / "source-a.csv", [ObjectRow("a/b/c/d/direct.jpg", 1, None)])
    append_object_rows(temp_dir / "source-b.csv", [ObjectRow("a/b/c/d/e/file.jpg", 2, None)])
    append_object_rows(temp_dir / "source-c.csv", [ObjectRow("a/b/c/d/e/f/g/deep.jpg", 3, None)])

    parts = _aggregate(temp_dir, tmp_path / "bucket-a", max_directories_in_memory=1)
    rows = pq.read_table(parts[0]).to_pylist()

    assert rows == [
        {
            "bucket_id": "bucket-id",
            "bucket_name": "bucket-a",
            "appid": "app.one",
            "path": "/a/b/c/d/",
            "object_count": 3,
            "total_size": 6,
            "max_file_size": 3,
            "last_modified": "2023-11-14",
            "max_depth": 7,
            "file_types": '["图片"]',
        }
    ]
```

Change existing exact expected rows so root remains `max_depth: 0` and the
cutoff `/a/b/c/d/` row containing depth-5 files becomes `max_depth: 5`.

- [ ] **Step 3: Run aggregation tests to verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_parquet_aggregation.py -q
```

Expected: failures because file depth and `max_file_depth` do not exist and
cutoff rows still write the output path depth.

- [ ] **Step 4: Implement original-depth calculation and summary state**

Remove the now-unused `depth_for_directory` import and add a shared helper:

```python
def _directory_parts_for_object(object_key: str) -> list[str]:
    normalized = normalize_object_key(object_key)
    return [part for part in normalized.split("/")[:-1] if part]


def file_directory_depth(object_key: str) -> int:
    return len(_directory_parts_for_object(object_key))


def attributed_directory_path(object_key: str, aggregation_depth: int) -> str:
    attributed_parts = _directory_parts_for_object(object_key)[:aggregation_depth]
    if not attributed_parts:
        return "/"
    return "/" + "/".join(attributed_parts) + "/"
```

Add `max_file_depth: int` to `ParquetDirectorySummary`, initialize it with
`file_directory_depth(row.object_key)`, and combine it with:

```python
max_file_depth=max(self.max_file_depth, other.max_file_depth)
```

- [ ] **Step 5: Persist the depth through internal CSV merge runs**

Add `"max_file_depth"` to `INTERNAL_SUMMARY_FIELDS` immediately before
`"file_types"`. Write `row.max_file_depth`, read it with `int(values[5])`, and
move the `file_types` JSON index from `5` to `6`.

Keep header and row-length validation exact so stale/malformed internal files
still fail explicitly.

- [ ] **Step 6: Write actual maximum depth to Parquet and rename cutoff arguments**

Change `_parquet_row` to:

```python
"max_depth": summary.max_file_depth,
```

Rename Parquet-specific `max_depth` parameters and keyword calls to
`aggregation_depth` in `attributed_directory_path`, `summary_for_object`,
`_summarize_objects`, `aggregate_bucket_parquet`, and the test helper. Do not
rename the Parquet schema column.

- [ ] **Step 7: Run aggregation and legacy CSV suites**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_parquet_aggregation.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_aggregation.py tests/test_external_aggregation.py -q
```

Expected: all tests pass and the exact Parquet schema remains unchanged.

- [ ] **Step 8: Commit depth semantics**

```powershell
git add src/obs_scan_platform/parquet_aggregation.py tests/test_parquet_aggregation.py
git commit -m "fix: report deepest file level in parquet"
```

### Task 3: Wire Canonical Depth Through Scanner

**Files:**
- Modify: `tests/test_scanner.py:2790-2835`
- Modify: `src/obs_scan_platform/scanner.py:427-457`

**Interfaces:**
- Consumes: `ScanSettings.aggregation_depth` from Task 1.
- Consumes: `aggregate_bucket_parquet(..., aggregation_depth: int, ...)` from Task 2.
- Produces: scanner dispatch using only the canonical name while preserving the run-local aggregation coordinator.

- [ ] **Step 1: Update scanner contract test before production code**

In the focused Parquet scanner test, set:

```python
scanner.config.scan.aggregation_depth = 3
```

Capture the patched aggregator keyword arguments and assert:

```python
assert captured["aggregation_depth"] == 3
assert "max_depth" not in captured
```

- [ ] **Step 2: Run the focused scanner test to verify RED**

Run the exact test identified by:

```powershell
rg -n "captures|aggregate_bucket_parquet|aggregation_depth" tests/test_scanner.py
```

Then run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -k "parquet and argument" -q
```

If the existing test name does not contain `argument`, run its exact node ID
from the `rg` result. Expected: failure from the obsolete `max_depth` keyword
or missing canonical field usage.

- [ ] **Step 3: Update scanner dispatch minimally**

Inside the existing `async with phase_coordinator.aggregation():` Parquet
branch, change only the cutoff keyword:

```python
aggregation_depth=self.config.scan.aggregation_depth,
```

Do not move or modify the aggregation coordinator, CSV branch, timing boundary,
cleanup, manifest, or status logic.

- [ ] **Step 4: Run scanner and end-to-end suites**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit scanner wiring**

```powershell
git add src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "fix: pass canonical parquet aggregation depth"
```

### Task 4: Update Operator Documentation

**Files:**
- Modify: `config/apps.example.yaml`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/scan-start-guide.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: canonical configuration and output semantics from Tasks 1-3.
- Produces: copy-paste-ready `aggregation_depth: 4` examples and explicit legacy compatibility guidance.

- [ ] **Step 1: Replace active configuration examples**

Change operational YAML from:

```yaml
max_depth: 4
```

to:

```yaml
aggregation_depth: 4
```

Do not rewrite historical approved spec/plan files from 2026-07-29; the new
2026-07-30 design and plan document the changed contract.

- [ ] **Step 2: Correct field semantics in English and Chinese docs**

Document all of the following in each operator-facing guide:

- `aggregation_depth` is the output path cutoff and defaults to 4;
- legacy `max_depth` YAML works only when the canonical name is absent;
- configuring both names is invalid;
- Parquet `max_depth` is the deepest original containing-directory level among
  the row's files, not the output path depth;
- `/a/b/file.txt` has depth 2 and `/a/b/c/d/e/file.txt` has depth 5.

- [ ] **Step 3: Verify example configuration and active references**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -c "from obs_scan_platform.config import load_config; c=load_config('config/apps.example.yaml'); assert c.scan.aggregation_depth == 4; assert 'max_depth' not in c.scan.model_dump(); print(c.scan.aggregation_depth)"
rg -n "aggregation_depth|max_depth" config/apps.example.yaml README.md README.zh-CN.md docs/scan-start-guide.md CLAUDE.md
```

Expected: the example prints `4`; active YAML uses only
`aggregation_depth`; `max_depth` references describe only the Parquet column or
legacy compatibility.

- [ ] **Step 4: Commit documentation**

```powershell
git add config/apps.example.yaml README.md README.zh-CN.md docs/scan-start-guide.md CLAUDE.md
git commit -m "docs: distinguish aggregation and file depth"
```

### Task 5: Review, Verify, Handoff, and Push

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Consumes: all implementation commits and validation output.
- Produces: reviewed, reproducible, pushed `codex/parquet-overview` branch.

- [ ] **Step 1: Run local whole-change review**

Invoke `superpowers:requesting-code-review`. Because subagent delegation is not
authorized in the current collaboration mode, perform the review locally
against base `fcb57b9` and the current HEAD. Check specifically:

- dual-name conflict detection occurs before field validation;
- legacy negative values still fail after migration;
- canonical serialization contains no `max_depth` config key;
- every internal summary write/read index includes `max_file_depth`;
- every combine path uses `max()`;
- the Parquet schema name/order/type did not change;
- scanner aggregation coordination is untouched;
- no unrelated refactor or secret is present.

Fix any Critical or Important findings with a new RED→GREEN test and a separate
commit.

- [ ] **Step 2: Run fresh completion verification**

Invoke `superpowers:verification-before-completion`, then run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_parquet_aggregation.py tests/test_aggregation.py tests/test_external_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_api.py -k "config_apps or parquet" -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
git diff --check
```

Read every exit code and record exact pass/fail/skip counts. Compare any full
suite failures with the known Windows baseline rather than hiding them.

- [ ] **Step 3: Update mandatory handoff files**

Set `docs/current-task.md` to this task, current branch, goal, completed work,
remaining work, files, exact commands/results, risks, and next action. Keep
status `wip` if the full suite is not green under repository policy.

Set `docs/handoff.md` with Asia/Shanghai timestamp, Windows environment, base
commit `fcb57b9`, all new commits, decisions, failed attempts, review findings,
test/build status, uncommitted state, push state, and exact resume commands.

- [ ] **Step 4: Review final diff and commit handoff**

```powershell
git status --short --branch
git diff --stat fcb57b9...HEAD
git diff fcb57b9...HEAD
git diff --check
git add .
git commit -m "docs: finalize parquet depth semantics handoff"
```

Inspect changed lines for secrets, generated dependencies, machine-specific
paths outside the documented workspace, and unrelated edits before committing.

- [ ] **Step 5: Push and record remote state**

```powershell
git push -u origin HEAD
```

If push succeeds, update the handoff with the pushed commit and output, commit
that documentation-only update, and run `git push` again. If it fails, follow
the repository push-failure procedure and do not retry blindly.

- [ ] **Step 6: Verify clean local/remote equality**

```powershell
git fetch origin
git status --short --branch
git rev-parse HEAD
git rev-parse origin/codex/parquet-overview
```

Expected: clean working tree and identical hashes.
