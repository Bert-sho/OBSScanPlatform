from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from obs_scan_platform.config import Thresholds


class ScanStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL_FAILED = "partial_failed"


@dataclass(frozen=True)
class BucketInfo:
    bucket_id: str
    name: str
    vendor: str
    region: str
    auth: str | None
    share_from: str | None


@dataclass(frozen=True)
class RootDiscovery:
    prefixes: list[str]
    root_files: list[str]


@dataclass(frozen=True)
class ObjectRow:
    object_key: str
    size_bytes: int
    last_modified_ms: int | None


@dataclass
class DirectoryStats:
    object_count: int = 0
    total_size_bytes: int = 0
    max_file_size_bytes: int = 0
    empty_file_count: int = 0
    large_file_count: int = 0
    latest_modified_ms: int | None = None

    def add_object(self, row: ObjectRow, thresholds: Thresholds) -> None:
        self.object_count += 1
        self.total_size_bytes += row.size_bytes
        self.max_file_size_bytes = max(self.max_file_size_bytes, row.size_bytes)
        if row.size_bytes == 0:
            self.empty_file_count += 1
        if row.size_bytes >= thresholds.large_file_bytes:
            self.large_file_count += 1
        if row.last_modified_ms is not None:
            if self.latest_modified_ms is None:
                self.latest_modified_ms = row.last_modified_ms
            else:
                self.latest_modified_ms = max(self.latest_modified_ms, row.last_modified_ms)


@dataclass(frozen=True)
class BucketScanResult:
    appid: str
    bucket_name: str
    bucket_id: str
    status: ScanStatus
    csv_path: Path | None
    thresholds: Thresholds
    error: str | None = None
