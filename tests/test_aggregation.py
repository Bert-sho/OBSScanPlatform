import csv
from pathlib import Path

import pytest

from obs_scan_platform.aggregation import FINAL_FIELDS, aggregate_bucket
from obs_scan_platform.config import Thresholds
from obs_scan_platform.csv_store import append_object_rows, iter_object_rows
from obs_scan_platform.models import ObjectRow


def test_append_object_rows_writes_header_for_empty_existing_csv(tmp_path: Path):
    csv_path = tmp_path / "empty.csv"
    csv_path.touch()

    append_object_rows(csv_path, [ObjectRow("file.txt", 7, 1000)])

    assert list(iter_object_rows(tmp_path)) == [ObjectRow("file.txt", 7, 1000)]


def test_object_rows_roundtrip_preserves_missing_last_modified(tmp_path: Path):
    append_object_rows(
        tmp_path / "objects.csv",
        [
            ObjectRow("a.txt", 1, None),
            ObjectRow("b.txt", 2, 3000),
        ],
    )

    assert list(iter_object_rows(tmp_path)) == [
        ObjectRow("a.txt", 1, None),
        ObjectRow("b.txt", 2, 3000),
    ]


def test_iter_object_rows_rejects_unexpected_header(tmp_path: Path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("name,size\nfile.txt,1\n", encoding="utf-8")

    with pytest.raises(ValueError, match=f"unexpected object CSV header in {csv_path}"):
        list(iter_object_rows(tmp_path))


def test_aggregate_bucket_rolls_objects_to_parents(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    temp_dir.mkdir()
    append_object_rows(
        temp_dir / "prefix.csv",
        [
            ObjectRow("a/b/file.txt", 10, 1000),
            ObjectRow("a/empty.log", 0, 2000),
            ObjectRow("root.bin", 25, 3000),
        ],
    )
    output = tmp_path / "bucket.csv"

    aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=output,
        thresholds=Thresholds(
            large_directory_bytes=20,
            large_file_bytes=20,
            inactive_directory_days=1,
        ),
        scan_started_ms=3_600_000 * 24 * 10,
    )

    rows = {row["directory_path"]: row for row in csv.DictReader(output.open())}
    assert rows["/"]["object_count"] == "3"
    assert rows["/"]["total_size_bytes"] == "35"
    assert rows["/"]["has_large_file"] == "true"
    assert rows["/a/"]["empty_file_count"] == "1"
    assert rows["/a/b/"]["object_count"] == "1"


def test_aggregate_bucket_writes_exact_final_header(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(temp_dir / "objects.csv", [ObjectRow("file.txt", 1, 1000)])
    output = tmp_path / "bucket.csv"

    aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=output,
        thresholds=Thresholds(
            large_directory_bytes=20,
            large_file_bytes=20,
            inactive_directory_days=1,
        ),
        scan_started_ms=1000,
    )

    with output.open(newline="", encoding="utf-8") as file:
        assert next(csv.reader(file)) == FINAL_FIELDS


def test_aggregate_bucket_writes_header_only_when_temp_dir_is_missing(tmp_path: Path):
    output = tmp_path / "bucket.csv"

    row_count = aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=tmp_path / "missing-temp",
        output_path=output,
        thresholds=Thresholds(
            large_directory_bytes=20,
            large_file_bytes=20,
            inactive_directory_days=1,
        ),
        scan_started_ms=1000,
    )

    assert row_count == 0
    with output.open(newline="", encoding="utf-8") as file:
        assert list(csv.reader(file)) == [FINAL_FIELDS]


def test_aggregate_bucket_writes_lowercase_bool_values(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(temp_dir / "objects.csv", [ObjectRow("file.txt", 25, 1000)])
    output = tmp_path / "bucket.csv"

    aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=output,
        thresholds=Thresholds(
            large_directory_bytes=20,
            large_file_bytes=20,
            inactive_directory_days=1,
        ),
        scan_started_ms=1000,
    )

    row = next(csv.DictReader(output.open(newline="", encoding="utf-8")))
    assert row["is_large_directory"] == "true"
    assert row["has_large_file"] == "true"
    assert row["has_empty_file"] == "false"
    assert row["is_inactive_directory"] == "false"


def test_aggregate_bucket_leaves_inactive_fields_empty_without_last_modified(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(temp_dir / "objects.csv", [ObjectRow("file.txt", 1, None)])
    output = tmp_path / "bucket.csv"

    aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=output,
        thresholds=Thresholds(
            large_directory_bytes=20,
            large_file_bytes=20,
            inactive_directory_days=1,
        ),
        scan_started_ms=86_400_000,
    )

    row = next(csv.DictReader(output.open(newline="", encoding="utf-8")))
    assert row["latest_modified_ms"] == ""
    assert row["inactive_days"] == ""
    assert row["is_inactive_directory"] == "false"


def test_aggregate_bucket_marks_old_directory_inactive(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(temp_dir / "objects.csv", [ObjectRow("file.txt", 1, 0)])
    output = tmp_path / "bucket.csv"

    aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=output,
        thresholds=Thresholds(
            large_directory_bytes=20,
            large_file_bytes=20,
            inactive_directory_days=1,
        ),
        scan_started_ms=86_400_000,
    )

    row = next(csv.DictReader(output.open(newline="", encoding="utf-8")))
    assert row["inactive_days"] == "1"
    assert row["is_inactive_directory"] == "true"


def test_aggregate_bucket_overwrites_existing_output_and_removes_tmp(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(temp_dir / "objects.csv", [ObjectRow("file.txt", 1, 1000)])
    output = tmp_path / "bucket.csv"
    output.write_text("old,data\nstale,row\n", encoding="utf-8")

    aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=output,
        thresholds=Thresholds(
            large_directory_bytes=20,
            large_file_bytes=20,
            inactive_directory_days=1,
        ),
        scan_started_ms=1000,
    )

    rows = list(csv.DictReader(output.open(newline="", encoding="utf-8")))
    assert rows == [
        {
            "run_id": "run-1",
            "appid": "app.one",
            "bucket_name": "bucket-a",
            "bucket_id": "bucket-id",
            "directory_path": "/",
            "depth": "0",
            "object_count": "1",
            "total_size_bytes": "1",
            "max_file_size_bytes": "1",
            "empty_file_count": "0",
            "large_file_count": "0",
            "latest_modified_ms": "1000",
            "inactive_days": "0",
            "is_large_directory": "false",
            "has_large_file": "false",
            "has_empty_file": "false",
            "is_inactive_directory": "false",
        }
    ]
    assert not output.with_suffix(output.suffix + ".tmp").exists()
