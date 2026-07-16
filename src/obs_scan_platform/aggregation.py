import csv
import hashlib
import logging
from pathlib import Path

from obs_scan_platform.config import Thresholds
from obs_scan_platform.external_aggregation import (
    MERGE_FAN_IN,
    DirectorySummary,
    iter_merged_summary_rows,
    reduce_summary_runs,
    summarize_object_csv,
)
from obs_scan_platform.paths import depth_for_directory, safe_filename


LOGGER = logging.getLogger(__name__)


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


def _source_id(source_path: Path) -> str:
    digest = hashlib.sha1(source_path.name.encode("utf-8")).hexdigest()[:12]
    return f"{safe_filename(source_path.stem)[:80]}_{digest}"


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
    max_directories_in_memory: int,
    keep_temp_files: bool,
    merge_fan_in: int = MERGE_FAN_IN,
) -> int:
    if max_directories_in_memory < 1:
        raise ValueError("max_directories_in_memory must be at least 1")

    sources = sorted(temp_dir.glob("*.csv"))
    aggregation_dir = temp_dir / "_aggregation"
    chunks_dir = aggregation_dir / "chunks"
    prefixes_dir = aggregation_dir / "prefixes"
    bucket_runs_dir = aggregation_dir / "bucket-runs"
    source_summaries: list[Path] = []

    LOGGER.info(
        "aggregation start appid=%s bucket=%s sources=%s directory_limit=%s",
        appid,
        bucket_name,
        len(sources),
        max_directories_in_memory,
    )
    for completed, source in enumerate(sources, start=1):
        source_id = _source_id(source)
        summary_path = prefixes_dir / f"{source_id}.csv"
        chunk_count = summarize_object_csv(
            source,
            summary_path,
            chunk_dir=chunks_dir / source_id,
            max_directories_in_memory=max_directories_in_memory,
            keep_intermediates=keep_temp_files,
            thresholds=thresholds,
            fan_in=merge_fan_in,
        )
        source_summaries.append(summary_path)
        LOGGER.info(
            "aggregation source progress appid=%s bucket=%s completed=%s total=%s chunks=%s",
            appid,
            bucket_name,
            completed,
            len(sources),
            chunk_count,
        )

    LOGGER.info(
        "aggregation merge appid=%s bucket=%s inputs=%s fan_in=%s",
        appid,
        bucket_name,
        len(source_summaries),
        merge_fan_in,
    )
    reduced = reduce_summary_runs(
        source_summaries,
        bucket_runs_dir,
        keep_intermediates=keep_temp_files,
        fan_in=merge_fan_in,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    directory_count = 0
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=FINAL_FIELDS)
            writer.writeheader()
            for summary in iter_merged_summary_rows(reduced, fan_in=merge_fan_in):
                inactive_days = _inactive_days(scan_started_ms, summary.latest_modified_ms)
                writer.writerow(
                    {
                        "run_id": run_id,
                        "appid": appid,
                        "bucket_name": bucket_name,
                        "bucket_id": bucket_id,
                        "directory_path": summary.directory_path,
                        "depth": depth_for_directory(summary.directory_path),
                        "object_count": summary.object_count,
                        "total_size_bytes": summary.total_size_bytes,
                        "max_file_size_bytes": summary.max_file_size_bytes,
                        "empty_file_count": summary.empty_file_count,
                        "large_file_count": summary.large_file_count,
                        "latest_modified_ms": (
                            "" if summary.latest_modified_ms is None else summary.latest_modified_ms
                        ),
                        "inactive_days": "" if inactive_days is None else inactive_days,
                        "is_large_directory": _bool(
                            summary.total_size_bytes >= thresholds.large_directory_bytes
                        ),
                        "has_large_file": _bool(summary.large_file_count > 0),
                        "has_empty_file": _bool(summary.empty_file_count > 0),
                        "is_inactive_directory": _bool(
                            inactive_days is not None
                            and inactive_days >= thresholds.inactive_directory_days
                        ),
                    }
                )
                directory_count += 1
        tmp_path.replace(output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    if not keep_temp_files:
        resolved_bucket_runs_dir = bucket_runs_dir.resolve()
        for run in reduced:
            if run.resolve().is_relative_to(resolved_bucket_runs_dir):
                run.unlink(missing_ok=True)

    LOGGER.info(
        "aggregation finish appid=%s bucket=%s directories=%s",
        appid,
        bucket_name,
        directory_count,
    )
    return directory_count
