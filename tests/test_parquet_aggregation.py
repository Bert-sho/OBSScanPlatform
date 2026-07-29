from pathlib import Path

import pyarrow.parquet as pq
import pytest

from obs_scan_platform.config import DEFAULT_FILE_TYPE_MAP
from obs_scan_platform.csv_store import append_object_rows
from obs_scan_platform.models import ObjectRow


@pytest.mark.parametrize(
    ("object_key", "max_depth", "expected"),
    [
        ("root.txt", 4, "/"),
        ("a/direct.txt", 4, "/a/"),
        ("a/b/c/d/file.txt", 4, "/a/b/c/d/"),
        ("a/b/c/d/e/deep.jpg", 4, "/a/b/c/d/"),
        ("a/b.txt", 0, "/"),
        ("/a/b/c.txt", 2, "/a/b/"),
    ],
)
def test_attributed_directory_path_maps_each_object_once(
    object_key: str,
    max_depth: int,
    expected: str,
):
    from obs_scan_platform.parquet_aggregation import attributed_directory_path

    assert attributed_directory_path(object_key, max_depth) == expected


@pytest.mark.parametrize(
    ("object_key", "expected"),
    [
        ("PHOTO.JPG", "图片"),
        ("archive.unknown", "其他"),
        ("README", "其他"),
        ("nested/archive.tar", "压缩包"),
    ],
)
def test_file_type_for_object_is_case_insensitive_and_has_other_fallback(
    object_key: str,
    expected: str,
):
    from obs_scan_platform.parquet_aggregation import file_type_for_object

    assert file_type_for_object(object_key, DEFAULT_FILE_TYPE_MAP) == expected


def test_parquet_directory_summary_combines_numeric_date_and_type_metrics():
    from obs_scan_platform.parquet_aggregation import summary_for_object

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


def test_parquet_directory_summary_rejects_combining_different_paths():
    from obs_scan_platform.parquet_aggregation import summary_for_object

    left = summary_for_object(
        ObjectRow("a/file.jpg", 1, None),
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
    )
    right = summary_for_object(
        ObjectRow("b/file.jpg", 1, None),
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
    )

    with pytest.raises(ValueError, match="paths must match"):
        left.combine(right)


def test_summary_for_object_ignores_timestamp_outside_supported_utc_range():
    from obs_scan_platform.parquet_aggregation import summary_for_object

    summary = summary_for_object(
        ObjectRow("a/file.jpg", 1, 10**30),
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
    )

    assert summary.latest_modified_ms is None


def _aggregate(
    temp_dir: Path,
    output_dir: Path,
    *,
    scan_started_ms: int = 1_700_000_000_000,
    max_directories_in_memory: int = 100_000,
):
    from obs_scan_platform.parquet_aggregation import aggregate_bucket_parquet

    return aggregate_bucket_parquet(
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_dir=output_dir,
        scan_started_ms=scan_started_ms,
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
        max_directories_in_memory=max_directories_in_memory,
        keep_temp_files=False,
        merge_fan_in=2,
    )


def test_aggregate_bucket_parquet_writes_exact_sorted_snappy_schema(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(
        temp_dir / "source-a.csv",
        [
            ObjectRow("root.bin", 3, 1_700_000_000_000),
            ObjectRow("a/b/c/d/e/deep.JPG", 7, 1_700_000_000_000),
        ],
    )
    append_object_rows(
        temp_dir / "source-b.csv",
        [ObjectRow("a/b/c/d/f/other.bin", 11, None)],
    )
    output_dir = tmp_path / "bucket-a"

    parts = _aggregate(temp_dir, output_dir, max_directories_in_memory=1)

    assert parts == (output_dir / "part-00001.parquet",)
    parquet_file = pq.ParquetFile(parts[0])
    assert [field.name for field in parquet_file.schema_arrow] == [
        "bucket_id",
        "bucket_name",
        "appid",
        "path",
        "object_count",
        "total_size",
        "max_file_size",
        "last_modified",
        "max_depth",
        "file_types",
    ]
    assert all(not field.nullable for field in parquet_file.schema_arrow)
    assert parquet_file.metadata.row_group(0).column(0).compression == "SNAPPY"
    assert parquet_file.read().to_pylist() == [
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


def test_aggregate_bucket_parquet_counts_duplicate_object_rows(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    duplicate = ObjectRow("a/file.jpg", 5, 1_700_000_000_000)
    append_object_rows(temp_dir / "source-a.csv", [duplicate])
    append_object_rows(temp_dir / "source-b.csv", [duplicate])

    parts = _aggregate(temp_dir, tmp_path / "bucket-a")

    assert pq.read_table(parts[0]).to_pylist()[0]["object_count"] == 2


def test_aggregate_bucket_parquet_uses_scan_date_when_all_timestamps_are_missing(
    tmp_path: Path,
):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(temp_dir / "objects.csv", [ObjectRow("a/file.txt", 1, None)])

    parts = _aggregate(temp_dir, tmp_path / "bucket-a", scan_started_ms=1_785_283_200_000)

    assert pq.read_table(parts[0]).to_pylist()[0]["last_modified"] == "2026-07-29"


def test_aggregate_bucket_parquet_writes_one_typed_empty_part(tmp_path: Path):
    output_dir = tmp_path / "bucket-a"

    parts = _aggregate(tmp_path / "missing-temp", output_dir)

    assert parts == (output_dir / "part-00001.parquet",)
    table = pq.read_table(parts[0])
    assert table.num_rows == 0
    assert all(not field.nullable for field in table.schema)


def test_aggregate_bucket_parquet_splits_after_50000_rows(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    append_object_rows(
        temp_dir / "objects.csv",
        [ObjectRow(f"dir-{index:05d}/file.bin", 1, None) for index in range(50_001)],
    )

    parts = _aggregate(temp_dir, tmp_path / "bucket-a")

    assert [path.name for path in parts] == ["part-00001.parquet", "part-00002.parquet"]
    assert [pq.read_table(path).num_rows for path in parts] == [50_000, 1]


def test_aggregate_bucket_parquet_failure_preserves_existing_output_and_removes_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import obs_scan_platform.parquet_aggregation as parquet_aggregation

    temp_dir = tmp_path / "_tmp"
    append_object_rows(
        temp_dir / "objects.csv",
        [ObjectRow("a/file.jpg", 1, None), ObjectRow("b/file.jpg", 1, None)],
    )
    output_dir = tmp_path / "bucket-a"
    output_dir.mkdir()
    (output_dir / "old.txt").write_text("old", encoding="utf-8")
    real_write_table = pq.write_table
    call_count = 0

    def fail_second_write(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("parquet write failed")
        return real_write_table(*args, **kwargs)

    monkeypatch.setattr(parquet_aggregation, "MAX_PARQUET_ROWS_PER_FILE", 1)
    monkeypatch.setattr(parquet_aggregation.pq, "write_table", fail_second_write)

    with pytest.raises(RuntimeError, match="parquet write failed"):
        _aggregate(temp_dir, output_dir)

    assert (output_dir / "old.txt").read_text(encoding="utf-8") == "old"
    assert not list(output_dir.parent.glob(f".{output_dir.name}.staging-*"))
    assert not list(output_dir.parent.glob(f".{output_dir.name}.backup-*"))


def test_aggregate_bucket_parquet_rejects_zero_directory_limit(tmp_path: Path):
    from obs_scan_platform.parquet_aggregation import aggregate_bucket_parquet

    with pytest.raises(ValueError, match="max_directories_in_memory must be at least 1"):
        aggregate_bucket_parquet(
            appid="app.one",
            bucket_name="bucket-a",
            bucket_id="bucket-id",
            temp_dir=tmp_path / "_tmp",
            output_dir=tmp_path / "bucket-a",
            scan_started_ms=0,
            max_depth=4,
            file_type_map=DEFAULT_FILE_TYPE_MAP,
            max_directories_in_memory=0,
            keep_temp_files=False,
        )
