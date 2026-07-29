# Parquet Bucket Overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add globally configurable CSV or bounded, split Snappy Parquet bucket overviews, with Parquet as the default and secure per-part downloads.

**Architecture:** Preserve object discovery, temporary object CSVs, and the existing CSV aggregator. Add a separate bounded external Parquet aggregation module that attributes every object to one truncated directory, merges sorted intermediate summaries, and atomically publishes 50,000-row Parquet parts; wire its paths into scanner manifests and a manifest-authorized download endpoint.

**Tech Stack:** Python 3.11+, Pydantic 2, PyYAML, PyArrow 16+, FastAPI, pytest, existing CSV external merge utilities.

## Global Constraints

- `scan.overview_format` accepts `csv` or `parquet` and defaults to `parquet`.
- `scan.max_depth` is global, non-negative, and defaults to `4`; `/` is depth 0.
- YAML `scan.file_type_map` merges over the exact built-in mapping approved in the design.
- CSV file names, schema, aggregation semantics, and download endpoint remain unchanged.
- Parquet uses an exact non-nullable 10-column schema and Snappy compression.
- Each Parquet part contains at most 50,000 rows and uses `part-00001.parquet` numbering.
- Parquet object attribution is single-path: direct directory below the cutoff, truncated directory at the cutoff.
- `last_modified` is the latest valid UTC date; only an all-missing group falls back to the UTC scan-start date.
- `max_depth` in each Parquet row is the depth of that row's `path`.
- No object deduplication is introduced.
- Preserve bounded memory through `aggregation_max_directories_in_memory` and the existing merge fan-in of 32.
- Do not change OBS request, retry, concurrency, discovery, or temporary object-row behavior.

---

### Task 1: Configuration Contract and PyArrow Dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/obs_scan_platform/config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `DEFAULT_FILE_TYPE_MAP: dict[str, str]`.
- Produces: `ScanSettings.overview_format: Literal["csv", "parquet"]`.
- Produces: `ScanSettings.max_depth: int` with `ge=0`.
- Produces: `ScanSettings.file_type_map: dict[str, str]`, normalized and merged over defaults.
- Consumes: Pydantic before-validation for YAML normalization.

- [ ] **Step 1: Write failing configuration tests**

Add imports for `DEFAULT_FILE_TYPE_MAP` and tests with these exact assertions:

```python
def test_overview_settings_default_to_parquet_depth_four_and_default_type_map():
    config = AppConfigFile()
    assert config.scan.overview_format == "parquet"
    assert config.scan.max_depth == 4
    assert config.scan.file_type_map == DEFAULT_FILE_TYPE_MAP


def test_file_type_map_merges_normalized_yaml_overrides(tmp_path: Path):
    path = tmp_path / "apps.yaml"
    path.write_text(
        "scan:\n  overview_format: csv\n  max_depth: 0\n"
        "  file_type_map:\n    .JPG: 自定义图片\n    TXT: 文档\n",
        encoding="utf-8",
    )
    scan = load_config(path).scan
    assert scan.overview_format == "csv"
    assert scan.max_depth == 0
    assert scan.file_type_map["jpg"] == "自定义图片"
    assert scan.file_type_map["txt"] == "文档"
    assert scan.file_type_map["png"] == "图片"


@pytest.mark.parametrize("value", ["avro", "PARQUET", ""])
def test_overview_format_rejects_unsupported_values(value: str):
    with pytest.raises(ValidationError):
        AppConfigFile(scan={"overview_format": value})


def test_max_depth_rejects_negative_value():
    with pytest.raises(ValidationError):
        AppConfigFile(scan={"max_depth": -1})


@pytest.mark.parametrize("mapping", [{"": "图片"}, {"jpg": ""}, {".": "图片"}])
def test_file_type_map_rejects_empty_normalized_keys_or_values(mapping: dict[str, str]):
    with pytest.raises(ValidationError):
        AppConfigFile(scan={"file_type_map": mapping})
```

- [ ] **Step 2: Run the configuration tests to verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py -q
```

Expected: new tests fail because the fields and mapping constant do not exist.

- [ ] **Step 3: Add the dependency and minimal configuration implementation**

Add `"pyarrow>=16.0"` to runtime dependencies. In `config.py`, define the approved mapping and implement:

```python
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_FILE_TYPE_MAP = {
    "jpg": "图片", "jpeg": "图片", "png": "图片", "gif": "图片",
    "cr3": "RAW", "nef": "RAW", "braw": "RAW",
    "mp4": "视频", "mov": "视频", "avi": "视频",
    "py": "脚本", "sh": "脚本", "js": "脚本", "ts": "脚本",
    "onnx": "模型", "ckpt": "模型", "safetensors": "模型", "pt": "模型",
    "parquet": "Parquet", "json": "配置文件", "yaml": "配置文件",
    "yml": "配置文件", "md": "文档", "pdf": "文档",
    "zip": "压缩包", "tar": "压缩包",
}


class ScanSettings(BaseModel):
    overview_format: Literal["csv", "parquet"] = "parquet"
    max_depth: int = Field(default=4, ge=0)
    file_type_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_FILE_TYPE_MAP))

    @field_validator("file_type_map", mode="before")
    @classmethod
    def merge_file_type_map(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        merged = dict(DEFAULT_FILE_TYPE_MAP)
        for raw_extension, raw_category in value.items():
            extension = str(raw_extension).strip().lower().removeprefix(".")
            category = raw_category.strip() if isinstance(raw_category, str) else ""
            if not extension or not category:
                raise ValueError("file_type_map keys and values must be non-empty")
            merged[extension] = category
        return merged
```

Place the three fields with the other global scan/output settings; do not disturb existing defaults.

- [ ] **Step 4: Run configuration tests to verify GREEN**

Run the command from Step 2. Expected: all `tests/test_config.py` tests pass.

- [ ] **Step 5: Install the new runtime dependency in the task venv**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pip install 'pyarrow>=16.0'
```

Expected: installation succeeds and `python -c "import pyarrow"` exits 0.

- [ ] **Step 6: Commit Task 1**

```powershell
git add pyproject.toml src/obs_scan_platform/config.py tests/test_config.py
git commit -m "feat: configure parquet bucket overviews"
```

---

### Task 2: Single-Path Summary Semantics

**Files:**
- Create: `src/obs_scan_platform/parquet_aggregation.py`
- Create: `tests/test_parquet_aggregation.py`

**Interfaces:**
- Consumes: `ObjectRow`, `normalize_object_key`, `depth_for_directory`.
- Produces: `ParquetDirectorySummary` with `combine(other)`.
- Produces: `attributed_directory_path(object_key: str, max_depth: int) -> str`.
- Produces: `file_type_for_object(object_key: str, file_type_map: dict[str, str]) -> str`.
- Produces: `summary_for_object(row: ObjectRow, *, max_depth: int, file_type_map: dict[str, str]) -> ParquetDirectorySummary`.

- [ ] **Step 1: Write failing pure-semantic tests**

Create tests covering the approved examples and aggregation values:

```python
@pytest.mark.parametrize(
    ("object_key", "max_depth", "expected"),
    [
        ("root.txt", 4, "/"),
        ("a/direct.txt", 4, "/a/"),
        ("a/b/c/d/file.txt", 4, "/a/b/c/d/"),
        ("a/b/c/d/e/deep.jpg", 4, "/a/b/c/d/"),
        ("a/b.txt", 0, "/"),
    ],
)
def test_attributed_directory_path_maps_each_object_once(object_key, max_depth, expected):
    assert attributed_directory_path(object_key, max_depth) == expected


@pytest.mark.parametrize(
    ("key", "expected"),
    [("PHOTO.JPG", "图片"), ("archive.unknown", "其他"), ("README", "其他")],
)
def test_file_type_for_object_is_case_insensitive_and_has_other_fallback(key, expected):
    assert file_type_for_object(key, DEFAULT_FILE_TYPE_MAP) == expected


def test_parquet_directory_summary_combines_numeric_date_and_type_metrics():
    left = summary_for_object(
        ObjectRow("a/direct.JPG", 7, 1_700_000_000_000),
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
    )
    right = summary_for_object(
        ObjectRow("a/other.bin", 11, None),
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
    )
    combined = left.combine(right)
    assert combined.path == "/a/"
    assert combined.object_count == 2
    assert combined.total_size == 18
    assert combined.max_file_size == 11
    assert combined.latest_modified_ms == 1_700_000_000_000
    assert combined.file_types == frozenset({"图片", "其他"})
```

- [ ] **Step 2: Run tests to verify RED**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_parquet_aggregation.py -q
```

Expected: import fails because `parquet_aggregation.py` does not exist.

- [ ] **Step 3: Implement the minimal pure aggregation model**

Use a frozen dataclass and keep timestamps as milliseconds until final output:

```python
@dataclass(frozen=True)
class ParquetDirectorySummary:
    path: str
    object_count: int
    total_size: int
    max_file_size: int
    latest_modified_ms: int | None
    file_types: frozenset[str]

    def combine(self, other: "ParquetDirectorySummary") -> "ParquetDirectorySummary":
        if self.path != other.path:
            raise ValueError("paths must match")
        timestamps = [value for value in (self.latest_modified_ms, other.latest_modified_ms) if value is not None]
        return ParquetDirectorySummary(
            path=self.path,
            object_count=self.object_count + other.object_count,
            total_size=self.total_size + other.total_size,
            max_file_size=max(self.max_file_size, other.max_file_size),
            latest_modified_ms=max(timestamps) if timestamps else None,
            file_types=self.file_types | other.file_types,
        )
```

Implement `attributed_directory_path` by normalizing `/`, taking all slash-separated components except the filename, slicing to `max_depth`, and formatting `/` or `/<parts>/`. Implement extension classification from the final filename segment and construct one summary per object.

- [ ] **Step 4: Run pure-semantic tests to verify GREEN**

Run the command from Step 2. Expected: all new tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/obs_scan_platform/parquet_aggregation.py tests/test_parquet_aggregation.py
git commit -m "feat: define parquet overview aggregation semantics"
```

---

### Task 3: Bounded External Merge and Atomic Parquet Parts

**Files:**
- Modify: `src/obs_scan_platform/parquet_aggregation.py`
- Modify: `tests/test_parquet_aggregation.py`

**Interfaces:**
- Consumes: top-level object CSV files through `iter_top_level_csv_files` and `iter_object_csv`.
- Produces: `PARQUET_SCHEMA: pyarrow.Schema` with ten ordered non-nullable fields.
- Produces: `aggregate_bucket_parquet(*, appid: str, bucket_name: str, bucket_id: str, temp_dir: Path, output_dir: Path, scan_started_ms: int, max_depth: int, file_type_map: dict[str, str], max_directories_in_memory: int, keep_temp_files: bool, merge_fan_in: int = MERGE_FAN_IN) -> tuple[Path, ...]`.

- [ ] **Step 1: Add RED tests for exact aggregation and Parquet metadata**

Write object rows with `append_object_rows`, call `aggregate_bucket_parquet`, read parts with `pyarrow.parquet`, and assert:

```python
assert [field.name for field in parquet_file.schema_arrow] == [
    "bucket_id", "bucket_name", "appid", "path", "object_count",
    "total_size", "max_file_size", "last_modified", "max_depth", "file_types",
]
assert all(not field.nullable for field in parquet_file.schema_arrow)
assert parquet_file.metadata.row_group(0).column(0).compression == "SNAPPY"
assert table.to_pylist() == [
    {
        "bucket_id": "bucket-id",
        "bucket_name": "bucket-a",
        "appid": "app.one",
        "path": "/",
        "object_count": 1,
        "total_size": 3,
        "max_file_size": 3,
        "last_modified": "2023-11-14",
        "max_depth": 0,
        "file_types": '["其他"]',
    },
    {
        "bucket_id": "bucket-id",
        "bucket_name": "bucket-a",
        "appid": "app.one",
        "path": "/a/b/c/d/",
        "object_count": 2,
        "total_size": 18,
        "max_file_size": 11,
        "last_modified": "2023-11-14",
        "max_depth": 4,
        "file_types": '["其他","图片"]',
    },
]
```

Choose input timestamps so `1_700_000_000_000` converts to `2023-11-14` UTC. Include duplicate occurrences and two source CSVs to prove cross-source merging without deduplication.

- [ ] **Step 2: Add RED tests for missing dates, empty buckets, row splitting, and atomic failure**

Add focused tests asserting:

```python
# all timestamps missing -> scan start date
assert row["last_modified"] == "2026-07-29"

# empty input -> one typed zero-row part
assert parts == (output_dir / "part-00001.parquet",)
assert pq.read_table(parts[0]).num_rows == 0

# 50,001 distinct paths -> exact split
assert [pq.read_table(path).num_rows for path in parts] == [50_000, 1]

# injected pq.write_table failure preserves output_dir / "old.txt"
assert (output_dir / "old.txt").read_text(encoding="utf-8") == "old"
assert not list(output_dir.parent.glob(f".{output_dir.name}.staging-*"))
```

Use `monkeypatch` to fail the second Parquet write. Also test `max_directories_in_memory=0` raises `ValueError` before producing output.

- [ ] **Step 3: Run new tests to verify RED**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_parquet_aggregation.py -q
```

Expected: failures report missing schema/writer/external merge behavior.

- [ ] **Step 4: Implement internal summary serialization and bounded merge**

Use internal fields:

```python
INTERNAL_SUMMARY_FIELDS = [
    "path", "object_count", "total_size", "max_file_size",
    "latest_modified_ms", "file_types",
]
```

Serialize `file_types` with `json.dumps(sorted(types), ensure_ascii=False, separators=(",", ":"))`. Implement `write_summary_rows_atomic`, `iter_summary_rows`, heap-based `iter_merged_summary_rows`, `merge_sorted_summaries`, and a Parquet-specific online reducer with fan-in validation. Stream all top-level object CSVs, flush sorted chunks when the directory dictionary reaches the configured limit, and merge path-equal rows with `combine`.

- [ ] **Step 5: Implement exact schema, date conversion, splitting, and atomic publish**

Define:

```python
PARQUET_SCHEMA = pa.schema([
    pa.field("bucket_id", pa.string(), nullable=False),
    pa.field("bucket_name", pa.string(), nullable=False),
    pa.field("appid", pa.string(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("object_count", pa.int64(), nullable=False),
    pa.field("total_size", pa.int64(), nullable=False),
    pa.field("max_file_size", pa.int64(), nullable=False),
    pa.field("last_modified", pa.string(), nullable=False),
    pa.field("max_depth", pa.int32(), nullable=False),
    pa.field("file_types", pa.string(), nullable=False),
])
MAX_PARQUET_ROWS_PER_FILE = 50_000
```

Convert milliseconds with `datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()`. Build fixed-size Python row batches, use `pa.Table.from_pylist(batch, schema=PARQUET_SCHEMA)` and `pq.write_table(..., compression="snappy")`, and always write one empty typed part when no summaries exist.

Create unique staging and backup siblings with UUID suffixes. Publish only after every part succeeds; restore a moved prior output if the final rename fails; remove staging on every exception. Return final part paths after the official directory is in place.

- [ ] **Step 6: Run Parquet tests to verify GREEN**

Run the command from Step 3. Expected: all Parquet aggregation tests pass.

- [ ] **Step 7: Run legacy aggregation regressions**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_aggregation.py tests/test_external_aggregation.py -q
```

Expected: all existing CSV aggregation tests pass unchanged.

- [ ] **Step 8: Commit Task 3**

```powershell
git add src/obs_scan_platform/parquet_aggregation.py tests/test_parquet_aggregation.py
git commit -m "feat: write bounded snappy parquet overviews"
```

---

### Task 4: Scanner Dispatch and Manifest Paths

**Files:**
- Modify: `src/obs_scan_platform/models.py`
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_scan_end_to_end.py`

**Interfaces:**
- Consumes: `aggregate_bucket_parquet(...) -> tuple[Path, ...]` and scan settings from Task 1.
- Produces: `BucketScanResult.overview_format: str`, `overview_path: Path | None`, and `overview_files: tuple[Path, ...]` with backward-compatible defaults.
- Produces manifest keys `overview_format`, `overview_path`, `overview_files`; preserves `csv_path`.

- [ ] **Step 1: Preserve legacy tests with explicit CSV configuration**

Set `overview_format="csv"` in `make_scanner()` and end-to-end configurations whose assertions explicitly require `.csv` output. Do not change their expected legacy CSV values.

- [ ] **Step 2: Write RED scanner tests for default Parquet and manifest fields**

Add one focused bucket test that patches `aggregate_bucket_parquet`, captures all arguments, returns two part paths, and asserts:

```python
assert captured["max_depth"] == 4
assert captured["file_type_map"]["jpg"] == "图片"
assert result.csv_path is None
assert result.overview_format == "parquet"
assert result.overview_path == results_dir / application.appid / bucket.name
assert result.overview_files == (
    result.overview_path / "part-00001.parquet",
    result.overview_path / "part-00002.parquet",
)
manifest = scanner._bucket_result_to_manifest(result)
assert manifest["overview_format"] == "parquet"
assert manifest["overview_path"] == str(result.overview_path)
assert manifest["overview_files"] == [str(path) for path in result.overview_files]
assert manifest["csv_path"] is None
```

Add an explicit CSV-mode assertion that `overview_path` and `overview_files` contain the existing CSV and that `csv_path` remains unchanged.

- [ ] **Step 3: Run focused scanner tests to verify RED**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -q
```

Expected: new attributes and dispatch do not exist.

- [ ] **Step 4: Extend result model and dispatch by format**

After `thresholds` in `BucketScanResult`, add defaults:

```python
overview_format: str = "csv"
overview_path: Path | None = None
overview_files: tuple[Path, ...] = ()
```

In `_scan_bucket`, choose the output target from `self.config.scan.overview_format`. Call only the existing CSV aggregator in CSV mode. In Parquet mode, call `aggregate_bucket_parquet` with global depth/map, capture returned parts, and set `csv_path=None`. Populate all three new manifest fields on successful or partial output; error returns use the selected format with no claimed output.

- [ ] **Step 5: Run scanner and end-to-end tests to verify GREEN**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: all tests pass; existing CSV assertions run under explicit CSV mode.

- [ ] **Step 6: Commit Task 4**

```powershell
git add src/obs_scan_platform/models.py src/obs_scan_platform/scanner.py tests/test_scanner.py tests/test_scan_end_to_end.py
git commit -m "feat: generate configured bucket overview format"
```

---

### Task 5: Manifest-Authorized Parquet Part Download

**Files:**
- Modify: `src/obs_scan_platform/api.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: bucket manifest `overview_format` and `overview_files` from Task 4.
- Produces: `GET /runs/{run_id}/apps/{appid}/buckets/{bucket_name}/parquet/{part_name}`.

- [ ] **Step 1: Write RED API tests**

Create a run manifest with one listed part and assert:

```python
response = client.get("/runs/run-1/apps/app-1/buckets/bucket-a/parquet/part-00001.parquet")
assert response.status_code == 200
assert response.content == b"parquet-data"
assert response.headers["content-type"] == "application/vnd.apache.parquet"
```

Add parametrized 404 cases for `part-1.parquet`, `part-00001.csv`, an unlisted `part-00002.parquet`, a bucket with `overview_format: csv`, a missing file, and an external symlink. Skip only the symlink case when Windows denies symlink creation.

- [ ] **Step 2: Run API tests to verify RED**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_api.py -q
```

Expected: the valid request returns 404 because the route does not exist.

- [ ] **Step 3: Implement secure manifest lookup and download**

Add a compiled full-match pattern `r"part-\d{5}\.parquet"`. Locate the exact application and bucket in `_read_manifest(run_dir)`, require Parquet format, build the expected path with `_safe_child`, compare its resolved path against resolved `overview_files` entries, reject symlinks/non-files, and return:

```python
return FileResponse(
    parquet_path,
    media_type="application/vnd.apache.parquet",
    filename=part_name,
)
```

Map every invalid or missing condition to the same 404 detail so no external path is disclosed.

- [ ] **Step 4: Run API tests to verify GREEN**

Run the command from Step 2. Expected: all API tests pass or only pre-existing Windows symlink privilege cases fail.

- [ ] **Step 5: Commit Task 5**

```powershell
git add src/obs_scan_platform/api.py tests/test_api.py
git commit -m "feat: download parquet overview parts"
```

---

### Task 6: Operator Documentation and Example Configuration

**Files:**
- Modify: `config/apps.example.yaml`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/scan-start-guide.md`

**Interfaces:**
- Documents: exact YAML keys/defaults, path attribution examples, schema, split naming, manifest fields, CSV compatibility, and API route.

- [ ] **Step 1: Update the example YAML**

Add `overview_format: parquet`, `max_depth: 4`, and the complete approved `file_type_map` under `scan` without changing unrelated values.

- [ ] **Step 2: Update English and Chinese operator docs**

Document both output layouts, state that Parquet is the default, provide the depth-4 direct/truncated example, list the ten fields and UTC missing-date fallback, explain 50,000-row Snappy parts, and show the Parquet part download URL. Retain and qualify existing CSV documentation as `overview_format: csv` behavior.

- [ ] **Step 3: Verify documented keys and routes mechanically**

```powershell
rg -n "overview_format|max_depth|file_type_map|part-00001.parquet|parquet/\{part_name\}" config/apps.example.yaml README.md README.zh-CN.md docs/scan-start-guide.md
```

Expected: every document contains the relevant configuration/output description; the guide contains the API route.

- [ ] **Step 4: Commit Task 6**

```powershell
git add config/apps.example.yaml README.md README.zh-CN.md docs/scan-start-guide.md
git commit -m "docs: document parquet bucket overviews"
```

---

### Task 7: Review, Full Validation, Handoff, and Push

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`
- Review: all files changed since `7efb393`

**Interfaces:**
- Produces: a self-contained cross-machine handoff, final validation evidence, final commit, and pushed branch.

- [ ] **Step 1: Run focused and affected validation**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_parquet_aggregation.py tests/test_aggregation.py tests/test_external_aggregation.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py tests/test_api.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
```

Expected: all affected tests pass; any Windows symlink privilege failure is identified precisely.

- [ ] **Step 2: Invoke `superpowers:requesting-code-review` and address findings**

Review the complete `7efb393..HEAD` diff against the approved design. Fix every Critical or Important issue through a RED/GREEN test when behavior changes, and rerun its focused suite.

- [ ] **Step 3: Invoke `superpowers:verification-before-completion` and run fresh full checks**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
git status --short --branch
git diff --stat 7efb393..HEAD
git diff 7efb393..HEAD
```

Record exact results. Do not mark the task completed if task-caused validation fails.

- [ ] **Step 4: Update mandatory task and handoff documents**

Write every field required by `AGENTS.md`, including task title/status, user goal, completed/remaining work, changed files, exact commands/results, risks, branch, before/after commits, rejected approaches, uncommitted state, and exact resume instructions. Record the existing five-failure Windows baseline separately from new validation.

- [ ] **Step 5: Review status, diff, and secret safety before the final commit**

```powershell
git status
git diff --stat
git diff
git diff --check
git diff | Select-String -Pattern '(?i)(api[_-]?key|secret|password|passwd|authorization|apptoken)\s*[:=]'
```

Expected: only intended handoff changes remain and no real credential value is present.

- [ ] **Step 6: Commit final handoff**

```powershell
git add docs/current-task.md docs/handoff.md
git commit -m "docs: finalize parquet overview handoff"
```

- [ ] **Step 7: Push and verify the remote branch**

```powershell
git push -u origin HEAD
git fetch origin
git rev-parse HEAD
git rev-parse origin/codex/parquet-overview
git status --short --branch
```

Expected: push succeeds, local and remote hashes match, and the working tree is clean. If push fails, update `docs/handoff.md` with the exact command/error, commit that record if useful, and provide the manual command without blind retries.
