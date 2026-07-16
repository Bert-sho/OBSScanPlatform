import csv
import heapq
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from obs_scan_platform.config import Thresholds
from obs_scan_platform.csv_store import iter_object_csv
from obs_scan_platform.models import DirectoryStats
from obs_scan_platform.paths import directory_chain_for_object


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
            raise ValueError("directory paths must match")
        timestamps = [
            timestamp
            for timestamp in (self.latest_modified_ms, other.latest_modified_ms)
            if timestamp is not None
        ]
        return DirectorySummary(
            directory_path=self.directory_path,
            object_count=self.object_count + other.object_count,
            total_size_bytes=self.total_size_bytes + other.total_size_bytes,
            max_file_size_bytes=max(self.max_file_size_bytes, other.max_file_size_bytes),
            empty_file_count=self.empty_file_count + other.empty_file_count,
            large_file_count=self.large_file_count + other.large_file_count,
            latest_modified_ms=max(timestamps) if timestamps else None,
        )


def iter_summary_rows(path: Path) -> Iterator[DirectorySummary]:
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        header = next(reader, None)
        if header != SUMMARY_FIELDS:
            raise ValueError(f"unexpected summary CSV header in {path}")
        for values in reader:
            if len(values) != len(SUMMARY_FIELDS):
                raise ValueError(f"unexpected summary CSV row in {path}")
            yield DirectorySummary(
                directory_path=values[0],
                object_count=int(values[1]),
                total_size_bytes=int(values[2]),
                max_file_size_bytes=int(values[3]),
                empty_file_count=int(values[4]),
                large_file_count=int(values[5]),
                latest_modified_ms=None if values[6] == "" else int(values[6]),
            )


def write_summary_rows_atomic(path: Path, rows: Iterable[DirectorySummary]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(SUMMARY_FIELDS)
            for row in rows:
                writer.writerow(
                    [
                        row.directory_path,
                        row.object_count,
                        row.total_size_bytes,
                        row.max_file_size_bytes,
                        row.empty_file_count,
                        row.large_file_count,
                        "" if row.latest_modified_ms is None else row.latest_modified_ms,
                    ]
                )
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def iter_merged_summary_rows(
    inputs: Iterable[Path],
    fan_in: int = MERGE_FAN_IN,
) -> Iterator[DirectorySummary]:
    input_paths = list(inputs)
    if fan_in < 2:
        raise ValueError("fan_in must be at least 2")
    if len(input_paths) > fan_in:
        raise ValueError("input count exceeds fan_in")

    iterators = [iter(iter_summary_rows(path)) for path in input_paths]
    try:
        heap: list[tuple[str, int, DirectorySummary]] = []
        for index, rows in enumerate(iterators):
            row = next(rows, None)
            if row is not None:
                heapq.heappush(heap, (row.directory_path, index, row))

        while heap:
            directory_path, index, combined = heapq.heappop(heap)
            next_row = next(iterators[index], None)
            if next_row is not None:
                heapq.heappush(heap, (next_row.directory_path, index, next_row))

            while heap and heap[0][0] == directory_path:
                _, index, row = heapq.heappop(heap)
                combined = combined.combine(row)
                next_row = next(iterators[index], None)
                if next_row is not None:
                    heapq.heappush(heap, (next_row.directory_path, index, next_row))
            yield combined
    finally:
        for rows in iterators:
            close = getattr(rows, "close", None)
            if close is not None:
                close()


def merge_sorted_summaries(
    inputs: Iterable[Path],
    output: Path,
    fan_in: int = MERGE_FAN_IN,
) -> None:
    write_summary_rows_atomic(output, iter_merged_summary_rows(inputs, fan_in=fan_in))


def reduce_summary_runs(
    inputs: Iterable[Path],
    work_dir: Path,
    keep_intermediates: bool,
    fan_in: int = MERGE_FAN_IN,
) -> list[Path]:
    if fan_in < 2:
        raise ValueError("fan_in must be at least 2")

    current = list(inputs)
    generated: set[Path] = set()
    resolved_work_dir = work_dir.resolve()
    round_number = 1
    while len(current) > fan_in:
        next_round: list[Path] = []
        for run_index, start in enumerate(range(0, len(current), fan_in), start=1):
            group = current[start : start + fan_in]
            output = work_dir / f"round-{round_number}-run-{run_index}.csv"
            merge_sorted_summaries(group, output, fan_in=fan_in)
            if not keep_intermediates:
                for consumed in group:
                    if consumed in generated and consumed.resolve().is_relative_to(resolved_work_dir):
                        consumed.unlink()
            generated.add(output)
            next_round.append(output)
        current = next_round
        round_number += 1
    return current


def summarize_object_csv(
    source_path: Path,
    output_path: Path,
    *,
    chunk_dir: Path,
    max_directories_in_memory: int,
    keep_intermediates: bool,
    thresholds: Thresholds,
    fan_in: int = MERGE_FAN_IN,
) -> int:
    if max_directories_in_memory < 1:
        raise ValueError("max_directories_in_memory must be at least 1")

    chunks: list[Path] = []
    stats_by_directory: dict[str, DirectoryStats] = {}

    def write_chunk() -> None:
        chunk_path = chunk_dir / f"chunk-{len(chunks) + 1:06d}.csv"
        write_summary_rows_atomic(
            chunk_path,
            (
                DirectorySummary(
                    directory_path=directory_path,
                    object_count=stats_by_directory[directory_path].object_count,
                    total_size_bytes=stats_by_directory[directory_path].total_size_bytes,
                    max_file_size_bytes=stats_by_directory[directory_path].max_file_size_bytes,
                    empty_file_count=stats_by_directory[directory_path].empty_file_count,
                    large_file_count=stats_by_directory[directory_path].large_file_count,
                    latest_modified_ms=stats_by_directory[directory_path].latest_modified_ms,
                )
                for directory_path in sorted(stats_by_directory)
            ),
        )
        chunks.append(chunk_path)
        stats_by_directory.clear()

    for row in iter_object_csv(source_path):
        for directory_path in directory_chain_for_object(row.object_key):
            stats = stats_by_directory.setdefault(directory_path, DirectoryStats())
            stats.add_object(row, thresholds)
        if len(stats_by_directory) >= max_directories_in_memory:
            write_chunk()

    if stats_by_directory:
        write_chunk()

    reduced = reduce_summary_runs(
        chunks,
        chunk_dir,
        keep_intermediates=keep_intermediates,
        fan_in=fan_in,
    )
    merge_sorted_summaries(reduced, output_path, fan_in=fan_in)

    if not keep_intermediates:
        resolved_chunk_dir = chunk_dir.resolve()
        for generated in {*chunks, *reduced}:
            if generated.resolve().is_relative_to(resolved_chunk_dir):
                generated.unlink(missing_ok=True)

    return len(chunks)
