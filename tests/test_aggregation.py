import csv
from pathlib import Path

from obs_scan_platform.aggregation import aggregate_bucket
from obs_scan_platform.config import Thresholds
from obs_scan_platform.csv_store import append_object_rows
from obs_scan_platform.models import ObjectRow


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
