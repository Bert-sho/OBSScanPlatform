from dataclasses import dataclass

from obs_scan_platform.models import ObjectRow
from obs_scan_platform.paths import normalize_object_key


@dataclass(frozen=True)
class ParquetDirectorySummary:
    path: str
    object_count: int
    total_size: int
    max_file_size: int
    latest_modified_ms: int | None
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
            file_types=self.file_types | other.file_types,
        )


def attributed_directory_path(object_key: str, max_depth: int) -> str:
    normalized = normalize_object_key(object_key)
    directory_parts = [part for part in normalized.split("/")[:-1] if part]
    attributed_parts = directory_parts[:max_depth]
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
    max_depth: int,
    file_type_map: dict[str, str],
) -> ParquetDirectorySummary:
    return ParquetDirectorySummary(
        path=attributed_directory_path(row.object_key, max_depth),
        object_count=1,
        total_size=row.size_bytes,
        max_file_size=row.size_bytes,
        latest_modified_ms=row.last_modified_ms,
        file_types=frozenset({file_type_for_object(row.object_key, file_type_map)}),
    )
