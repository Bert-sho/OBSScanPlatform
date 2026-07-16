# Bounded CSV Aggregation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace bucket-wide in-memory deduplication, directory aggregation, and sorting with a no-deduplication pure-CSV external merge pipeline whose directory dictionary and open-file counts are bounded.

**Architecture:** Each top-level object-detail CSV is summarized in bounded directory chunks and reduced to one sorted source-summary CSV. Source summaries are then reduced with a 32-way external merge, and the final merge streams the existing bucket CSV schema without retaining all object keys or directories.

**Tech Stack:** Python 3.12, standard-library `csv`, `heapq`, `pathlib`, Pydantic, pytest

## Global Constraints

- Add `scan.aggregation_max_directories_in_memory` with default `100000`; reject values below `1`.
- Do not deduplicate object keys. Every detail-row occurrence contributes to statistics.
- Use pure CSV files; do not add SQLite or third-party dependencies.
- Never merge more than `32` sorted summary inputs at once.
- Preserve final CSV fields, lexical `directory_path` order, manifest schema, timing/status behavior, and bucket concurrency.
- Treat both prefix detail CSVs and `metadata_files.csv` as source inputs.
- `keep_temp_files=true` retains detail, chunk, source-summary, and bucket-run artifacts; `false` allows consumed runs to be removed and retains the existing bucket-finalization cleanup.
- Write chunks, summaries, runs, and the final bucket CSV atomically through sibling `.tmp` files.

---

### Task 1: Aggregation Memory Configuration

**Files:**
- Modify: `src/obs_scan_platform/config.py`
- Modify: `config/apps.example.yaml`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: YAML key `scan.aggregation_max_directories_in_memory`
- Produces: `ScanSettings.aggregation_max_directories_in_memory: int` with default `100000` and `ge=1` validation

- [ ] **Step 1: Write failing default, explicit-load, and validation tests**

Add to `tests/test_config.py`:

```python
def test_scan_settings_defaults_aggregation_directory_limit(tmp_path: Path):
    config_file = write_minimal_config(tmp_path)

    config = load_config(config_file)

    assert config.scan.aggregation_max_directories_in_memory == 100000


def test_load_config_sets_aggregation_directory_limit(tmp_path: Path):
    config_file = write_minimal_config(
        tmp_path,
        scan_lines="  aggregation_max_directories_in_memory: 4321\n",
    )

    config = load_config(config_file)

    assert config.scan.aggregation_max_directories_in_memory == 4321


def test_load_config_rejects_non_positive_aggregation_directory_limit(tmp_path: Path):
    config_file = write_minimal_config(
        tmp_path,
        scan_lines="  aggregation_max_directories_in_memory: 0\n",
    )

    with pytest.raises(ValidationError):
        load_config(config_file)
```

If the existing tests do not have `write_minimal_config`, add a focused helper that writes the existing minimal endpoint/defaults/application fixture; do not refactor unrelated tests.

- [ ] **Step 2: Run the tests to verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py -k aggregation_directory_limit -q
```

Expected: default/explicit assertions fail because the field is absent, and the invalid value is accepted.

- [ ] **Step 3: Add the validated field and example configuration**

Add to `ScanSettings`:

```python
aggregation_max_directories_in_memory: int = Field(default=100000, ge=1)
```

Add beside the other scan limits in `config/apps.example.yaml`:

```yaml
aggregation_max_directories_in_memory: 100000
```

- [ ] **Step 4: Run configuration tests to verify GREEN**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py -q
```

Expected: all configuration tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/obs_scan_platform/config.py config/apps.example.yaml tests/test_config.py
git commit -m "feat: configure aggregation memory bound"
```

### Task 2: Sorted Summary Rows and Limited-Fan-In Merge

**Files:**
- Create: `src/obs_scan_platform/external_aggregation.py`
- Create: `tests/test_external_aggregation.py`

**Interfaces:**
- Produces: `SUMMARY_FIELDS`, `MERGE_FAN_IN`, `DirectorySummary`, `iter_summary_rows()`, `write_summary_rows_atomic()`, `merge_sorted_summaries()`, and `reduce_summary_runs()`
- Consumes later: sorted summary CSV paths and `_aggregation` work directories

- [ ] **Step 1: Write failing summary round-trip and merge-formula tests**

Create tests that use this public shape:

```python
from obs_scan_platform.external_aggregation import (
    DirectorySummary,
    iter_summary_rows,
    merge_sorted_summaries,
    write_summary_rows_atomic,
)


def test_merge_sorted_summaries_combines_sum_max_and_missing_timestamp(tmp_path: Path):
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    output = tmp_path / "merged.csv"
    write_summary_rows_atomic(
        first,
        [
            DirectorySummary("/", 2, 10, 7, 1, 0, None),
            DirectorySummary("/a/", 1, 7, 7, 0, 0, 1000),
        ],
    )
    write_summary_rows_atomic(
        second,
        [
            DirectorySummary("/", 3, 20, 12, 0, 1, 2000),
            DirectorySummary("/b/", 1, 12, 12, 0, 1, 2000),
        ],
    )

    merge_sorted_summaries([first, second], output)

    assert list(iter_summary_rows(output)) == [
        DirectorySummary("/", 5, 30, 12, 1, 1, 2000),
        DirectorySummary("/a/", 1, 7, 7, 0, 0, 1000),
        DirectorySummary("/b/", 1, 12, 12, 0, 1, 2000),
    ]
```

Also assert exact header validation, lexical output order, and header-only output for zero inputs.

- [ ] **Step 2: Run to verify RED**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_external_aggregation.py -q
```

Expected: collection fails because `external_aggregation` does not exist.

- [ ] **Step 3: Implement the intermediate summary type and atomic CSV I/O**

Create `external_aggregation.py` with:

```python
from __future__ import annotations

import csv
import heapq
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

SUMMARY_FIELDS = [
    "directory_path",
    "object_count",
    "total_size_bytes",
    "max_file_size_bytes",
    "empty_file_count",
    "large_file_count",
    "latest_modified_ms",
]
MERGE_FAN_IN = 32


@dataclass(frozen=True)
class DirectorySummary:
    directory_path: str
    object_count: int
    total_size_bytes: int
    max_file_size_bytes: int
    empty_file_count: int
    large_file_count: int
    latest_modified_ms: int | None

    def combine(self, other: "DirectorySummary") -> "DirectorySummary":
        if self.directory_path != other.directory_path:
            raise ValueError("cannot combine different directory paths")
        timestamps = [value for value in (self.latest_modified_ms, other.latest_modified_ms) if value is not None]
        return DirectorySummary(
            directory_path=self.directory_path,
            object_count=self.object_count + other.object_count,
            total_size_bytes=self.total_size_bytes + other.total_size_bytes,
            max_file_size_bytes=max(self.max_file_size_bytes, other.max_file_size_bytes),
            empty_file_count=self.empty_file_count + other.empty_file_count,
            large_file_count=self.large_file_count + other.large_file_count,
            latest_modified_ms=max(timestamps) if timestamps else None,
        )
```

Implement strict `iter_summary_rows(path)` parsing and `write_summary_rows_atomic(path, rows)` using `path.with_suffix(path.suffix + ".tmp")` followed by `replace()`.

- [ ] **Step 4: Implement a heap-based merge for at most one fan-in group**

`merge_sorted_summaries(inputs, output)` must:

```python
if len(inputs) > MERGE_FAN_IN:
    raise ValueError(f"cannot merge more than {MERGE_FAN_IN} summary files")
```

Open each iterator, keep one current row per input in a heap keyed by `(directory_path, input_index)`, combine equal paths, and stream rows to the atomic writer. Close all input files through generator/context cleanup even when parsing fails.

- [ ] **Step 5: Add RED tests for multi-round reduction and retention**

Use an explicit `fan_in=2` test seam to create five one-row inputs. Assert:

- at least two merge rounds are required;
- the returned paths count is `<= 2`;
- `keep_intermediates=False` removes consumed run inputs created inside the work directory;
- `keep_intermediates=True` retains them;
- source-summary inputs outside the work directory are never deleted by the reducer.

- [ ] **Step 6: Implement `reduce_summary_runs()`**

Use this signature:

```python
def reduce_summary_runs(
    inputs: Sequence[Path],
    *,
    work_dir: Path,
    keep_intermediates: bool,
    fan_in: int = MERGE_FAN_IN,
) -> list[Path]:
    """Return at most fan_in sorted inputs, creating bounded merge rounds as needed."""
```

Validate `fan_in >= 2`. Merge groups into deterministic `round-<n>-run-<m>.csv` names. Only delete consumed files whose parent is inside `work_dir`, and only after their replacement run succeeds.

- [ ] **Step 7: Run merge tests to verify GREEN**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_external_aggregation.py -q
```

Expected: all external merge tests pass.

- [ ] **Step 8: Commit**

```powershell
git add src/obs_scan_platform/external_aggregation.py tests/test_external_aggregation.py
git commit -m "feat: add bounded csv summary merge"
```

### Task 3: Bounded Detail-Source Summarization

**Files:**
- Modify: `src/obs_scan_platform/csv_store.py`
- Modify: `src/obs_scan_platform/external_aggregation.py`
- Modify: `tests/test_aggregation.py`
- Modify: `tests/test_external_aggregation.py`

**Interfaces:**
- Produces: `iter_object_csv(path: Path) -> Iterator[ObjectRow]`
- Produces: `summarize_object_csv(source_path, output_path, chunk_dir, max_directories_in_memory, keep_intermediates, fan_in=32) -> int`
- Returns: number of sorted chunk files initially written, for progress logging and test evidence

- [ ] **Step 1: Refactor single-file parsing test-first**

Add tests proving `iter_object_csv(path)` round-trips missing timestamps and rejects a wrong header with the exact offending path. Run them before implementation:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_aggregation.py -k iter_object_csv -q
```

Expected: FAIL because the function is absent.

Implement:

```python
def iter_object_csv(csv_path: Path):
    with csv_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != OBJECT_ROW_FIELDS:
            raise ValueError(f"unexpected object CSV header in {csv_path}")
        for row in reader:
            last_modified = row["last_modified_ms"]
            yield ObjectRow(
                object_key=row["object_key"],
                size_bytes=int(row["size_bytes"]),
                last_modified_ms=int(last_modified) if last_modified else None,
            )
```

Make existing `iter_object_rows(temp_dir)` delegate to `iter_object_csv()` so behavior stays compatible.

- [ ] **Step 2: Write failing chunk-bound and no-dedup tests**

Create a detail CSV whose objects create more than three distinct directories and call:

```python
chunk_count = summarize_object_csv(
    source_path,
    summary_path,
    chunk_dir=tmp_path / "chunks",
    max_directories_in_memory=3,
    keep_intermediates=True,
    fan_in=2,
)
```

Assert `chunk_count >= 2`, output is lexically sorted, root/parent values equal the original aggregation formulas, and all chunk CSVs remain. Add the same object key twice and assert the summary counts both occurrences.

- [ ] **Step 3: Run to verify RED**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_external_aggregation.py -k 'summarize or duplicate' -q
```

Expected: FAIL because `summarize_object_csv` is absent.

- [ ] **Step 4: Implement bounded source summarization**

Use a `dict[str, DirectoryStats]` only for the current chunk. After each object contributes through `directory_chain_for_object()`, flush when `len(stats_by_directory) >= max_directories_in_memory`.

Convert each entry to `DirectorySummary`, sort only the current dictionary, write `chunk-<n>.csv` atomically, and clear the dictionary. Reduce chunks until at most the fan-in, then merge them into `output_path`. For an empty source, write a header-only summary.

After the final source-summary replacement succeeds, delete consumed chunk/run
inputs when `keep_intermediates=False`; retain them all when it is `True`.

Do not create or consult a `set` of object keys.

- [ ] **Step 5: Add a synthetic stress regression**

Generate at least 500 objects with unique nested directories, set the directory limit to `10`, and assert successful summary output, more than one chunk, correct root count, and no in-memory-dedup behavior. Do not assert process RSS.

- [ ] **Step 6: Run source and existing aggregation tests**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_external_aggregation.py tests/test_aggregation.py -q
```

Expected: all tests pass except the known Windows regex-path baseline if the existing test is selected; if present, record it separately and run the new focused tests to prove GREEN.

- [ ] **Step 7: Commit**

```powershell
git add src/obs_scan_platform/csv_store.py src/obs_scan_platform/external_aggregation.py tests/test_aggregation.py tests/test_external_aggregation.py
git commit -m "feat: summarize object csvs with bounded memory"
```

### Task 4: Bucket Aggregation Orchestration and Scanner Integration

**Files:**
- Modify: `src/obs_scan_platform/aggregation.py`
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `tests/test_aggregation.py`
- Modify: `tests/test_scanner.py`
- Modify: `tests/test_scan_end_to_end.py`

**Interfaces:**
- Changes: `aggregate_bucket(..., max_directories_in_memory: int, keep_temp_files: bool) -> int`
- Consumes: `summarize_object_csv()`, `reduce_summary_runs()`, and merged summary iterators
- Scanner passes: `self.config.scan.aggregation_max_directories_in_memory` and `self.config.scan.keep_temp_files`

- [ ] **Step 1: Write failing bounded bucket-pipeline tests**

Adapt existing `aggregate_bucket` calls to pass the two new required arguments. Add tests that create multiple top-level detail CSVs, including `metadata_files.csv`, and assert:

- shared ancestor directory statistics merge correctly;
- final rows stay lexically ordered and preserve `FINAL_FIELDS` exactly;
- duplicate object keys in different detail files count twice;
- a small limit and `fan_in=2` test seam force multiple source chunks and bucket rounds;
- `keep_temp_files=True` retains `_aggregation/chunks`, `prefixes`, and bucket runs;
- missing/empty temp input still writes a header-only final CSV;
- replacing an existing final CSV remains atomic.

If a fan-in seam is needed, keep it keyword-only and internal to `aggregate_bucket`, defaulting to `MERGE_FAN_IN`.

- [ ] **Step 2: Run to verify RED**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_aggregation.py -k 'bounded or duplicate or metadata or retains or header_only or exact_final' -q
```

Expected: new tests fail because `aggregate_bucket` still uses `seen_object_keys` and the bucket-wide dictionary.

- [ ] **Step 3: Replace bucket-wide aggregation with external orchestration**

`aggregate_bucket()` must:

```python
detail_paths = sorted(temp_dir.glob("*.csv")) if temp_dir.exists() else []
aggregation_dir = temp_dir / "_aggregation"
prefix_summary_dir = aggregation_dir / "prefixes"
```

Summarize one detail source at a time into a deterministic summary named from the unique source filename. Reduce all source summaries in `bucket-runs/` until at most fan-in inputs remain. Stream the final merged summaries to the existing `FINAL_FIELDS` writer, deriving depth, inactive days, and threshold flags exactly as today. Return the number of final directory rows.

Remove `seen_object_keys` and the bucket-wide `stats_by_directory` entirely.

- [ ] **Step 4: Add aggregation progress logging**

Use `obs_scan_platform.aggregation` INFO records without object keys:

```text
aggregation start appid=<...> bucket=<...> sources=<n> directory_limit=<n>
aggregation source progress appid=<...> bucket=<...> completed=<n> total=<n> chunks=<n>
aggregation merge appid=<...> bucket=<...> inputs=<n> fan_in=32
aggregation finish appid=<...> bucket=<...> directories=<n>
```

Tests should assert the start/source/finish records for a multi-source bucket, not exact temporary absolute paths.

- [ ] **Step 5: Pass configuration and retention from the scanner**

Change the scanner call to:

```python
aggregate_bucket(
    run_id=run_id,
    appid=application.appid,
    bucket_name=bucket.name,
    bucket_id=bucket.bucket_id,
    temp_dir=temp_dir,
    output_path=output_path,
    thresholds=thresholds,
    scan_started_ms=scan_started_ms,
    max_directories_in_memory=self.config.scan.aggregation_max_directories_in_memory,
    keep_temp_files=self.config.scan.keep_temp_files,
)
```

Keep the existing synchronous processing phase and bucket-finalization cleanup.

- [ ] **Step 6: Run aggregation, scanner, and end-to-end suites**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_aggregation.py tests/test_external_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: all task-relevant tests pass; document the known Windows regex baseline separately if it remains in `test_aggregation.py`.

- [ ] **Step 7: Commit**

```powershell
git add src/obs_scan_platform/aggregation.py src/obs_scan_platform/scanner.py tests/test_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py
git commit -m "fix: bound bucket csv aggregation memory"
```

### Task 5: Documentation, Review, Verification, Handoff, and Push

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Documents: configuration, no-dedup semantics, temporary artifacts, memory bounds, and operational disk trade-offs
- Produces: reviewed commits and a pushed `codex/obs-scan-platform` branch

- [ ] **Step 1: Update operator and architecture documentation**

README must document:

- `aggregation_max_directories_in_memory` default `100000`;
- duplicate object rows are counted repeatedly;
- per-source chunks and 32-way external merges bound memory;
- `keep_temp_files=true` retains `_aggregation` artifacts and may consume substantial disk;
- aggregation progress log fields.

CLAUDE must replace the bucket-wide in-memory aggregation description with the two-stage bounded external CSV architecture.

- [ ] **Step 2: Request independent code review**

Review specifically for:

- absence of bucket-wide object-key or directory collections;
- flush bound and one-object depth overshoot;
- correct sum/max/nullable merge algebra;
- fan-in/file-descriptor bound in every round;
- atomic write and safe deletion order;
- no-dedup behavior documented and tested;
- final CSV/schema/timing/status compatibility.

Fix every Critical and Important finding and re-review.

- [ ] **Step 3: Run fresh completion verification**

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_aggregation.py tests/test_external_aggregation.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
```

Record exact counts. Compare any full-suite failures with the six authorized Windows baseline failures; do not call the task completed if any test fails.

- [ ] **Step 4: Update mandatory handoff documents**

Record task title, branch, status, goal, completed/remaining work, changed files, exact validation output, known risks, before/after commits, design decisions, failed attempts, uncommitted state, and exact resume instructions.

- [ ] **Step 5: Inspect, commit, and push**

```powershell
git status
git diff --stat
git diff
git diff --check
git add .
git commit -m "docs: record bounded aggregation handoff"
git push -u origin HEAD
git status --short --branch
git rev-parse HEAD
git rev-parse origin/codex/obs-scan-platform
```

Verify a clean tracked worktree and exact local/remote hash parity.
