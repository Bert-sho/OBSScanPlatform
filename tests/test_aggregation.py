import csv
import logging
import re
from pathlib import Path

import pytest

from obs_scan_platform.aggregation import FINAL_FIELDS, aggregate_bucket
from obs_scan_platform.config import Thresholds
from obs_scan_platform.csv_store import append_object_rows, iter_object_csv, iter_object_rows
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


def test_iter_object_csv_roundtrip(tmp_path: Path):
    csv_path = tmp_path / "objects.csv"
    rows = [ObjectRow("a.txt", 1, 1000), ObjectRow("nested/b.txt", 2, 2000)]
    append_object_rows(csv_path, rows)

    assert list(iter_object_csv(csv_path)) == rows


def test_iter_object_csv_preserves_missing_last_modified(tmp_path: Path):
    csv_path = tmp_path / "objects.csv"
    append_object_rows(csv_path, [ObjectRow("a.txt", 1, None)])

    assert list(iter_object_csv(csv_path)) == [ObjectRow("a.txt", 1, None)]


def test_iter_object_csv_rejects_unexpected_header(tmp_path: Path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("name,size\nfile.txt,1\n", encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(f"unexpected object CSV header in {csv_path}")):
        list(iter_object_csv(csv_path))


def test_iter_object_rows_rejects_unexpected_header(tmp_path: Path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("name,size\nfile.txt,1\n", encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(f"unexpected object CSV header in {csv_path}")):
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
        max_directories_in_memory=100,
        keep_temp_files=False,
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
        max_directories_in_memory=100,
        keep_temp_files=False,
    )

    with output.open(newline="", encoding="utf-8") as file:
        assert next(csv.reader(file)) == FINAL_FIELDS


@pytest.mark.parametrize("create_temp_dir", [False, True])
def test_aggregate_bucket_writes_header_only_when_temp_dir_is_missing_or_empty(
    tmp_path: Path,
    create_temp_dir: bool,
):
    output = tmp_path / "bucket.csv"
    temp_dir = tmp_path / "temp"
    if create_temp_dir:
        temp_dir.mkdir()

    row_count = aggregate_bucket(
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
        max_directories_in_memory=100,
        keep_temp_files=False,
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
        max_directories_in_memory=100,
        keep_temp_files=False,
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
        max_directories_in_memory=100,
        keep_temp_files=False,
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
        max_directories_in_memory=100,
        keep_temp_files=False,
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
        max_directories_in_memory=100,
        keep_temp_files=False,
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


def test_aggregate_bucket_merges_multiple_sources_without_dedup_and_logs_progress(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(
        temp_dir / "prefix.csv",
        [ObjectRow("shared/prefix.bin", 7, 100), ObjectRow("duplicate.bin", 3, 200)],
    )
    append_object_rows(
        temp_dir / "metadata_files.csv",
        [ObjectRow("shared/metadata.bin", 11, 300), ObjectRow("duplicate.bin", 3, 200)],
    )
    output = tmp_path / "bucket.csv"

    with caplog.at_level(logging.INFO, logger="obs_scan_platform.aggregation"):
        count = aggregate_bucket(
            run_id="run-1",
            appid="app.one",
            bucket_name="bucket-a",
            bucket_id="bucket-id",
            temp_dir=temp_dir,
            output_path=output,
            thresholds=Thresholds(
                large_directory_bytes=20,
                large_file_bytes=10,
                inactive_directory_days=1,
            ),
            scan_started_ms=400,
            max_directories_in_memory=2,
            keep_temp_files=False,
            merge_fan_in=2,
        )

    with output.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        assert reader.fieldnames == FINAL_FIELDS
    assert count == 2
    assert [row["directory_path"] for row in rows] == ["/", "/shared/"]
    assert rows[0]["object_count"] == "4"
    assert rows[0]["total_size_bytes"] == "24"
    assert rows[1]["object_count"] == "2"
    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("aggregation start appid=app.one bucket=bucket-a sources=2 directory_limit=2") for message in messages)
    assert sum(message.startswith("aggregation source progress appid=app.one bucket=bucket-a") for message in messages) == 2
    assert any(message.startswith("aggregation merge appid=app.one bucket=bucket-a inputs=2 fan_in=2") for message in messages)
    assert any(message.startswith("aggregation finish appid=app.one bucket=bucket-a directories=2") for message in messages)
    assert all("duplicate.bin" not in message and str(temp_dir) not in message for message in messages)


def test_aggregate_bucket_retains_chunks_source_summaries_and_multi_round_runs(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    for index in range(5):
        append_object_rows(
            temp_dir / f"source-{index}.csv",
            [ObjectRow(f"dir-{index}/file.bin", index + 1, index)],
        )

    aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=tmp_path / "bucket.csv",
        thresholds=Thresholds(
            large_directory_bytes=100,
            large_file_bytes=100,
            inactive_directory_days=1,
        ),
        scan_started_ms=1000,
        max_directories_in_memory=1,
        keep_temp_files=True,
        merge_fan_in=2,
    )

    aggregation_dir = temp_dir / "_aggregation"
    assert len(list((aggregation_dir / "chunks").glob("*/*.csv"))) >= 5
    assert len(list((aggregation_dir / "prefixes").glob("*.csv"))) == 5
    assert len(list((aggregation_dir / "bucket-runs").glob("*.csv"))) == 5


def test_aggregate_bucket_removes_consumed_runs_but_keeps_detail_sources(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    detail_sources = []
    for index in range(5):
        source = temp_dir / f"source-{index}.csv"
        append_object_rows(source, [ObjectRow(f"dir-{index}/file.bin", 1, index)])
        detail_sources.append(source)

    aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=tmp_path / "bucket.csv",
        thresholds=Thresholds(
            large_directory_bytes=100,
            large_file_bytes=100,
            inactive_directory_days=1,
        ),
        scan_started_ms=1000,
        max_directories_in_memory=1,
        keep_temp_files=False,
        merge_fan_in=2,
    )

    assert all(source.exists() for source in detail_sources)
    assert len(list((temp_dir / "_aggregation" / "prefixes").glob("*.csv"))) == 5
    assert not list((temp_dir / "_aggregation" / "bucket-runs").glob("*.csv"))


def test_aggregate_bucket_failure_preserves_old_output_and_removes_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(temp_dir / "objects.csv", [ObjectRow("file.bin", 1, 100)])
    output = tmp_path / "bucket.csv"
    output.write_text("old,data\nstale,row\n", encoding="utf-8")

    def fail_while_merging(*args, **kwargs):
        raise RuntimeError("merge failed")
        yield

    monkeypatch.setattr("obs_scan_platform.aggregation.iter_merged_summary_rows", fail_while_merging)

    with pytest.raises(RuntimeError, match="merge failed"):
        aggregate_bucket(
            run_id="run-1",
            appid="app.one",
            bucket_name="bucket-a",
            bucket_id="bucket-id",
            temp_dir=temp_dir,
            output_path=output,
            thresholds=Thresholds(
                large_directory_bytes=100,
                large_file_bytes=100,
                inactive_directory_days=1,
            ),
            scan_started_ms=1000,
            max_directories_in_memory=1,
            keep_temp_files=False,
        )

    assert output.read_text(encoding="utf-8") == "old,data\nstale,row\n"
    assert not output.with_suffix(output.suffix + ".tmp").exists()
