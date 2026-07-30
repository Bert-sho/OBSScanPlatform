import csv
import heapq
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Iterable, Iterator
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from obs_scan_platform.csv_store import iter_object_csv
from obs_scan_platform.external_aggregation import MERGE_FAN_IN, iter_top_level_csv_files
from obs_scan_platform.models import ObjectRow
from obs_scan_platform.paths import normalize_object_key


INTERNAL_SUMMARY_FIELDS = [
    "path",
    "object_count",
    "total_size",
    "max_file_size",
    "latest_modified_ms",
    "max_file_depth",
    "file_types",
]
PARQUET_SCHEMA = pa.schema(
    [
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
    ]
)
MAX_PARQUET_ROWS_PER_FILE = 50_000


@dataclass(frozen=True)
class ParquetDirectorySummary:
    path: str
    object_count: int
    total_size: int
    max_file_size: int
    latest_modified_ms: int | None
    max_file_depth: int
    file_types: frozenset[str]

    def combine(self, other: "ParquetDirectorySummary") -> "ParquetDirectorySummary":
        if self.path != other.path:
            raise ValueError("paths must match")
        timestamps = [
            value
            for value in (self.latest_modified_ms, other.latest_modified_ms)
            if value is not None
        ]
        return ParquetDirectorySummary(
            path=self.path,
            object_count=self.object_count + other.object_count,
            total_size=self.total_size + other.total_size,
            max_file_size=max(self.max_file_size, other.max_file_size),
            latest_modified_ms=max(timestamps) if timestamps else None,
            max_file_depth=max(self.max_file_depth, other.max_file_depth),
            file_types=self.file_types | other.file_types,
        )


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


def file_type_for_object(object_key: str, file_type_map: dict[str, str]) -> str:
    filename = normalize_object_key(object_key).rsplit("/", 1)[-1]
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return file_type_map.get(extension, "其他")


def summary_for_object(
    row: ObjectRow,
    *,
    aggregation_depth: int,
    file_type_map: dict[str, str],
) -> ParquetDirectorySummary:
    latest_modified_ms = row.last_modified_ms
    if latest_modified_ms is not None:
        try:
            _date_from_ms(latest_modified_ms)
        except (OverflowError, OSError, ValueError):
            latest_modified_ms = None
    return ParquetDirectorySummary(
        path=attributed_directory_path(row.object_key, aggregation_depth),
        object_count=1,
        total_size=row.size_bytes,
        max_file_size=row.size_bytes,
        latest_modified_ms=latest_modified_ms,
        max_file_depth=file_directory_depth(row.object_key),
        file_types=frozenset({file_type_for_object(row.object_key, file_type_map)}),
    )


def _date_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()


def _file_types_json(file_types: frozenset[str]) -> str:
    return json.dumps(sorted(file_types), ensure_ascii=False, separators=(",", ":"))


def _write_summary_rows_atomic(path: Path, rows: Iterable[ParquetDirectorySummary]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(INTERNAL_SUMMARY_FIELDS)
            for row in rows:
                writer.writerow(
                    [
                        row.path,
                        row.object_count,
                        row.total_size,
                        row.max_file_size,
                        "" if row.latest_modified_ms is None else row.latest_modified_ms,
                        row.max_file_depth,
                        _file_types_json(row.file_types),
                    ]
                )
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _iter_summary_rows(path: Path) -> Iterator[ParquetDirectorySummary]:
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        if next(reader, None) != INTERNAL_SUMMARY_FIELDS:
            raise ValueError(f"unexpected parquet summary CSV header in {path}")
        for values in reader:
            if len(values) != len(INTERNAL_SUMMARY_FIELDS):
                raise ValueError(f"unexpected parquet summary CSV row in {path}")
            file_types = json.loads(values[6])
            if not isinstance(file_types, list) or not all(
                isinstance(file_type, str) for file_type in file_types
            ):
                raise ValueError(f"unexpected file_types value in {path}")
            yield ParquetDirectorySummary(
                path=values[0],
                object_count=int(values[1]),
                total_size=int(values[2]),
                max_file_size=int(values[3]),
                latest_modified_ms=None if values[4] == "" else int(values[4]),
                max_file_depth=int(values[5]),
                file_types=frozenset(file_types),
            )


def _iter_merged_summary_rows(
    inputs: Iterable[Path],
    *,
    fan_in: int,
) -> Iterator[ParquetDirectorySummary]:
    if fan_in < 2:
        raise ValueError("fan_in must be at least 2")
    input_paths = list(islice(inputs, fan_in + 1))
    if len(input_paths) > fan_in:
        raise ValueError("input count exceeds fan_in")

    iterators = [iter(_iter_summary_rows(path)) for path in input_paths]
    try:
        heap: list[tuple[str, int, ParquetDirectorySummary]] = []
        for index, rows in enumerate(iterators):
            row = next(rows, None)
            if row is not None:
                heapq.heappush(heap, (row.path, index, row))

        while heap:
            path, index, combined = heapq.heappop(heap)
            next_row = next(iterators[index], None)
            if next_row is not None:
                heapq.heappush(heap, (next_row.path, index, next_row))

            while heap and heap[0][0] == path:
                _, index, row = heapq.heappop(heap)
                combined = combined.combine(row)
                next_row = next(iterators[index], None)
                if next_row is not None:
                    heapq.heappush(heap, (next_row.path, index, next_row))
            yield combined
    finally:
        for rows in iterators:
            close = getattr(rows, "close", None)
            if close is not None:
                close()


def _merge_sorted_summaries(inputs: Iterable[Path], output: Path, *, fan_in: int) -> None:
    _write_summary_rows_atomic(output, _iter_merged_summary_rows(inputs, fan_in=fan_in))


class _OnlineSummaryReducer:
    def __init__(
        self,
        work_dir: Path,
        *,
        delete_root: Path,
        keep_intermediates: bool,
        fan_in: int,
    ) -> None:
        self.work_dir = work_dir
        self.keep_intermediates = keep_intermediates
        self.fan_in = fan_in
        self._levels: list[list[Path]] = []
        self._run_counts: list[int] = []
        self._resolved_delete_root = delete_root.resolve()

    def add(self, path: Path) -> None:
        self._add_at_level(path, 0)

    def finish(self) -> list[Path]:
        current = [path for level in self._levels for path in level]
        finish_round = 1
        while len(current) > self.fan_in:
            next_round: list[Path] = []
            for start in range(0, len(current), self.fan_in):
                group = current[start : start + self.fan_in]
                if len(group) == 1:
                    next_round.append(group[0])
                    continue
                output = self.work_dir / f"finish-{finish_round}-run-{len(next_round) + 1}.csv"
                _merge_sorted_summaries(group, output, fan_in=self.fan_in)
                self._delete_consumed(group)
                next_round.append(output)
            current = next_round
            finish_round += 1
        return current

    def _add_at_level(self, path: Path, level_number: int) -> None:
        while len(self._levels) <= level_number:
            self._levels.append([])
            self._run_counts.append(0)
        level = self._levels[level_number]
        level.append(path)
        if len(level) < self.fan_in:
            return

        group = tuple(level)
        run_number = self._run_counts[level_number] + 1
        output = self.work_dir / f"round-{level_number + 1}-run-{run_number}.csv"
        _merge_sorted_summaries(group, output, fan_in=self.fan_in)
        level.clear()
        self._run_counts[level_number] = run_number
        self._delete_consumed(group)
        self._add_at_level(output, level_number + 1)

    def _delete_consumed(self, paths: Iterable[Path]) -> None:
        if self.keep_intermediates:
            return
        for path in paths:
            if path.resolve().is_relative_to(self._resolved_delete_root):
                path.unlink(missing_ok=True)


def _summarize_objects(
    temp_dir: Path,
    aggregation_dir: Path,
    *,
    aggregation_depth: int,
    file_type_map: dict[str, str],
    max_directories_in_memory: int,
    keep_intermediates: bool,
    fan_in: int,
) -> list[Path]:
    chunks_dir = aggregation_dir / "chunks"
    reducer = _OnlineSummaryReducer(
        aggregation_dir / "runs",
        delete_root=aggregation_dir,
        keep_intermediates=keep_intermediates,
        fan_in=fan_in,
    )
    summaries: dict[str, ParquetDirectorySummary] = {}
    chunk_count = 0

    def flush_chunk() -> Path:
        nonlocal chunk_count
        chunk_count += 1
        chunk_path = chunks_dir / f"chunk-{chunk_count:06d}.csv"
        _write_summary_rows_atomic(
            chunk_path,
            (summaries[path] for path in sorted(summaries)),
        )
        summaries.clear()
        return chunk_path

    for source in iter_top_level_csv_files(temp_dir):
        for row in iter_object_csv(source):
            summary = summary_for_object(
                row,
                aggregation_depth=aggregation_depth,
                file_type_map=file_type_map,
            )
            current = summaries.get(summary.path)
            summaries[summary.path] = summary if current is None else current.combine(summary)
            if len(summaries) >= max_directories_in_memory:
                reducer.add(flush_chunk())
    if summaries:
        reducer.add(flush_chunk())
    return reducer.finish()


def _parquet_row(
    summary: ParquetDirectorySummary,
    *,
    bucket_id: str,
    bucket_name: str,
    appid: str,
    fallback_date: str,
) -> dict[str, object]:
    return {
        "bucket_id": bucket_id,
        "bucket_name": bucket_name,
        "appid": appid,
        "path": summary.path,
        "object_count": summary.object_count,
        "total_size": summary.total_size,
        "max_file_size": summary.max_file_size,
        "last_modified": (
            fallback_date
            if summary.latest_modified_ms is None
            else _date_from_ms(summary.latest_modified_ms)
        ),
        "max_depth": summary.max_file_depth,
        "file_types": _file_types_json(summary.file_types),
    }


def _write_parquet_parts(
    staging_dir: Path,
    summaries: Iterable[ParquetDirectorySummary],
    *,
    bucket_id: str,
    bucket_name: str,
    appid: str,
    fallback_date: str,
) -> tuple[Path, ...]:
    staging_dir.mkdir(parents=True)
    parts: list[Path] = []
    batch: list[dict[str, object]] = []

    def write_batch() -> None:
        part_path = staging_dir / f"part-{len(parts) + 1:05d}.parquet"
        table = pa.Table.from_pylist(batch, schema=PARQUET_SCHEMA)
        pq.write_table(table, part_path, compression="snappy")
        parts.append(part_path)
        batch.clear()

    for summary in summaries:
        batch.append(
            _parquet_row(
                summary,
                bucket_id=bucket_id,
                bucket_name=bucket_name,
                appid=appid,
                fallback_date=fallback_date,
            )
        )
        if len(batch) == MAX_PARQUET_ROWS_PER_FILE:
            write_batch()
    if batch or not parts:
        write_batch()
    return tuple(parts)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _publish_staging_directory(staging_dir: Path, output_dir: Path) -> None:
    backup_dir = output_dir.parent / f".{output_dir.name}.backup-{uuid4().hex}"
    moved_existing = False
    try:
        if output_dir.exists() or output_dir.is_symlink():
            output_dir.replace(backup_dir)
            moved_existing = True
        try:
            staging_dir.replace(output_dir)
        except BaseException:
            if moved_existing:
                backup_dir.replace(output_dir)
            raise
        if moved_existing:
            _remove_path(backup_dir)
    except BaseException:
        if staging_dir.exists() or staging_dir.is_symlink():
            _remove_path(staging_dir)
        raise


def aggregate_bucket_parquet(
    *,
    appid: str,
    bucket_name: str,
    bucket_id: str,
    temp_dir: Path,
    output_dir: Path,
    scan_started_ms: int,
    aggregation_depth: int,
    file_type_map: dict[str, str],
    max_directories_in_memory: int,
    keep_temp_files: bool,
    merge_fan_in: int = MERGE_FAN_IN,
) -> tuple[Path, ...]:
    if max_directories_in_memory < 1:
        raise ValueError("max_directories_in_memory must be at least 1")
    if merge_fan_in < 2:
        raise ValueError("merge_fan_in must be at least 2")

    aggregation_dir = temp_dir / "_aggregation" / "parquet"
    reduced = _summarize_objects(
        temp_dir,
        aggregation_dir,
        aggregation_depth=aggregation_depth,
        file_type_map=file_type_map,
        max_directories_in_memory=max_directories_in_memory,
        keep_intermediates=keep_temp_files,
        fan_in=merge_fan_in,
    )
    summaries = _iter_merged_summary_rows(reduced, fan_in=merge_fan_in)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir.parent / f".{output_dir.name}.staging-{uuid4().hex}"
    try:
        staged_parts = _write_parquet_parts(
            staging_dir,
            summaries,
            bucket_id=bucket_id,
            bucket_name=bucket_name,
            appid=appid,
            fallback_date=_date_from_ms(scan_started_ms),
        )
        part_names = [path.name for path in staged_parts]
        _publish_staging_directory(staging_dir, output_dir)
    except BaseException:
        if staging_dir.exists() or staging_dir.is_symlink():
            _remove_path(staging_dir)
        raise
    finally:
        close = getattr(summaries, "close", None)
        if close is not None:
            close()

    return tuple(output_dir / name for name in part_names)
