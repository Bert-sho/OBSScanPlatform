import csv
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from obs_scan_platform.external_aggregation import (
    MERGE_FAN_IN,
    SUMMARY_FIELDS,
    DirectorySummary,
    iter_summary_rows,
    merge_sorted_summaries,
    reduce_summary_runs,
    write_summary_rows_atomic,
)


def _summary(
    directory_path: str,
    *,
    object_count: int = 1,
    total_size_bytes: int = 10,
    max_file_size_bytes: int = 10,
    empty_file_count: int = 0,
    large_file_count: int = 0,
    latest_modified_ms: int | None = None,
) -> DirectorySummary:
    return DirectorySummary(
        directory_path=directory_path,
        object_count=object_count,
        total_size_bytes=total_size_bytes,
        max_file_size_bytes=max_file_size_bytes,
        empty_file_count=empty_file_count,
        large_file_count=large_file_count,
        latest_modified_ms=latest_modified_ms,
    )


def test_summary_rows_round_trip_and_write_exact_header(tmp_path: Path):
    path = tmp_path / "summaries.csv"
    rows = [_summary("/a/", latest_modified_ms=None), _summary("/b/", latest_modified_ms=1234)]

    write_summary_rows_atomic(path, rows)

    with path.open(newline="", encoding="utf-8") as file:
        assert next(csv.reader(file)) == SUMMARY_FIELDS
    assert list(iter_summary_rows(path)) == rows
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_iter_summary_rows_rejects_an_inexact_header(tmp_path: Path):
    path = tmp_path / "bad.csv"
    path.write_text("directory_path,object_count\n/a/,1\n", encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(f"unexpected summary CSV header in {path}")):
        list(iter_summary_rows(path))


def test_directory_summary_is_frozen_and_combines_sum_max_and_nullable_timestamp():
    first = _summary(
        "/a/",
        object_count=2,
        total_size_bytes=20,
        max_file_size_bytes=12,
        empty_file_count=1,
        large_file_count=0,
        latest_modified_ms=None,
    )
    second = _summary(
        "/a/",
        object_count=3,
        total_size_bytes=40,
        max_file_size_bytes=30,
        empty_file_count=2,
        large_file_count=1,
        latest_modified_ms=900,
    )

    assert first.combine(second) == _summary(
        "/a/",
        object_count=5,
        total_size_bytes=60,
        max_file_size_bytes=30,
        empty_file_count=3,
        large_file_count=1,
        latest_modified_ms=900,
    )
    assert second.combine(_summary("/a/", latest_modified_ms=1200)).latest_modified_ms == 1200
    with pytest.raises(ValueError, match="directory paths must match"):
        first.combine(_summary("/b/"))
    with pytest.raises(FrozenInstanceError):
        first.object_count = 99


def test_merge_sorted_summaries_writes_lexical_order_and_combines_equal_paths(tmp_path: Path):
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    output = tmp_path / "merged.csv"
    write_summary_rows_atomic(first, [_summary("/a/", total_size_bytes=3), _summary("/c/")])
    write_summary_rows_atomic(
        second,
        [_summary("/a/", object_count=2, total_size_bytes=7, latest_modified_ms=100), _summary("/b/")],
    )

    merge_sorted_summaries([first, second], output)

    rows = list(iter_summary_rows(output))
    assert [row.directory_path for row in rows] == ["/a/", "/b/", "/c/"]
    assert rows[0].object_count == 3
    assert rows[0].total_size_bytes == 10
    assert rows[0].latest_modified_ms == 100


def test_merge_sorted_summaries_writes_header_only_for_no_inputs(tmp_path: Path):
    output = tmp_path / "empty.csv"

    merge_sorted_summaries([], output)

    with output.open(newline="", encoding="utf-8") as file:
        assert list(csv.reader(file)) == [SUMMARY_FIELDS]


def test_merge_sorted_summaries_enforces_fan_in_bounds(tmp_path: Path):
    inputs = [tmp_path / f"{index}.csv" for index in range(3)]
    for path in inputs:
        write_summary_rows_atomic(path, [])

    assert MERGE_FAN_IN == 32
    with pytest.raises(ValueError, match="fan_in must be at least 2"):
        merge_sorted_summaries([], tmp_path / "low.csv", fan_in=1)
    with pytest.raises(ValueError, match="input count exceeds fan_in"):
        merge_sorted_summaries(inputs, tmp_path / "many.csv", fan_in=2)


def test_reduce_summary_runs_handles_multiple_rounds_and_deletes_only_consumed_generated_runs(
    tmp_path: Path,
):
    source_dir = tmp_path / "source"
    work_dir = tmp_path / "work"
    inputs = []
    for index in range(5):
        path = source_dir / f"source-{index}.csv"
        write_summary_rows_atomic(
            path,
            [
                _summary("/shared/", object_count=1, total_size_bytes=index + 1),
                _summary(f"/unique-{index}/"),
            ],
        )
        inputs.append(path)

    reduced = reduce_summary_runs(inputs, work_dir, keep_intermediates=False, fan_in=2)

    assert reduced == [work_dir / "round-2-run-1.csv", work_dir / "round-2-run-2.csv"]
    assert all(path.exists() for path in inputs)
    assert not any(work_dir.glob("round-1-run-*.csv"))
    assert all(path.exists() for path in reduced)

    output = tmp_path / "final.csv"
    merge_sorted_summaries(reduced, output, fan_in=2)
    rows = list(iter_summary_rows(output))
    assert [row.directory_path for row in rows] == [
        "/shared/",
        "/unique-0/",
        "/unique-1/",
        "/unique-2/",
        "/unique-3/",
        "/unique-4/",
    ]
    assert rows[0].object_count == 5
    assert rows[0].total_size_bytes == 15


def test_reduce_summary_runs_retains_all_generated_runs_when_requested(tmp_path: Path):
    inputs = []
    for index in range(5):
        path = tmp_path / "source" / f"source-{index}.csv"
        write_summary_rows_atomic(path, [_summary(f"/{index}/")])
        inputs.append(path)
    work_dir = tmp_path / "work"

    reduced = reduce_summary_runs(inputs, work_dir, keep_intermediates=True, fan_in=2)

    assert reduced == [work_dir / "round-2-run-1.csv", work_dir / "round-2-run-2.csv"]
    assert sorted(path.name for path in work_dir.glob("*.csv")) == [
        "round-1-run-1.csv",
        "round-1-run-2.csv",
        "round-1-run-3.csv",
        "round-2-run-1.csv",
        "round-2-run-2.csv",
    ]
    assert all(path.exists() for path in inputs)
