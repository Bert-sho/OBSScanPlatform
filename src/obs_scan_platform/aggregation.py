import csv
from pathlib import Path

from obs_scan_platform.config import Thresholds
from obs_scan_platform.csv_store import iter_object_rows
from obs_scan_platform.models import DirectoryStats
from obs_scan_platform.paths import depth_for_directory, directory_chain_for_object


FINAL_FIELDS = [
    "run_id",
    "appid",
    "bucket_name",
    "bucket_id",
    "directory_path",
    "depth",
    "object_count",
    "total_size_bytes",
    "max_file_size_bytes",
    "empty_file_count",
    "large_file_count",
    "latest_modified_ms",
    "inactive_days",
    "is_large_directory",
    "has_large_file",
    "has_empty_file",
    "is_inactive_directory",
]


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _inactive_days(scan_started_ms: int, latest_modified_ms: int | None) -> int | None:
    if latest_modified_ms is None:
        return None
    return max(0, (scan_started_ms - latest_modified_ms) // 86_400_000)


def aggregate_bucket(
    *,
    run_id: str,
    appid: str,
    bucket_name: str,
    bucket_id: str,
    temp_dir: Path,
    output_path: Path,
    thresholds: Thresholds,
    scan_started_ms: int,
) -> int:
    stats_by_directory: dict[str, DirectoryStats] = {}
    seen_object_keys: set[str] = set()
    for row in iter_object_rows(temp_dir):
        if row.object_key in seen_object_keys:
            continue
        seen_object_keys.add(row.object_key)
        for directory in directory_chain_for_object(row.object_key):
            stats = stats_by_directory.setdefault(directory, DirectoryStats())
            stats.add_object(row, thresholds)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FINAL_FIELDS)
        writer.writeheader()
        for directory in sorted(stats_by_directory):
            stats = stats_by_directory[directory]
            inactive_days = _inactive_days(scan_started_ms, stats.latest_modified_ms)
            writer.writerow(
                {
                    "run_id": run_id,
                    "appid": appid,
                    "bucket_name": bucket_name,
                    "bucket_id": bucket_id,
                    "directory_path": directory,
                    "depth": depth_for_directory(directory),
                    "object_count": stats.object_count,
                    "total_size_bytes": stats.total_size_bytes,
                    "max_file_size_bytes": stats.max_file_size_bytes,
                    "empty_file_count": stats.empty_file_count,
                    "large_file_count": stats.large_file_count,
                    "latest_modified_ms": "" if stats.latest_modified_ms is None else stats.latest_modified_ms,
                    "inactive_days": "" if inactive_days is None else inactive_days,
                    "is_large_directory": _bool(stats.total_size_bytes >= thresholds.large_directory_bytes),
                    "has_large_file": _bool(stats.large_file_count > 0),
                    "has_empty_file": _bool(stats.empty_file_count > 0),
                    "is_inactive_directory": _bool(
                        inactive_days is not None and inactive_days >= thresholds.inactive_directory_days
                    ),
                }
            )
    tmp_path.replace(output_path)
    return len(stats_by_directory)
