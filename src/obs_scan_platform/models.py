import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from obs_scan_platform.config import Thresholds
from obs_scan_platform.obs_client import OBSRequestError

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(
    r"(?i)\b((?:csb-)?token|access_token|api_key|apikey|secret|password|passwd|authorization)\s*=\s*([^&\s'\"<>]+)"
)


def _sanitize_reason(reason: str) -> str:
    sanitized = _URL_RE.sub("<redacted-url>", reason)
    sanitized = _SECRET_QUERY_RE.sub("<redacted-query>", sanitized)
    return sanitized


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


@dataclass(frozen=True, init=False)
class RootDiscovery:
    prefixes: list[str]
    metadata_files: list[str]

    def __init__(
        self,
        prefixes: list[str],
        metadata_files: list[str] | None = None,
        root_files: list[str] | None = None,
    ) -> None:
        if metadata_files is None:
            metadata_files = root_files or []
        object.__setattr__(self, "prefixes", prefixes)
        object.__setattr__(self, "metadata_files", metadata_files)

    @property
    def root_files(self) -> list[str]:
        return self.metadata_files


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
class PartialErrorSample:
    endpoint: str
    target: str
    status: int | None
    reason: str

    def to_manifest(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "target": self.target,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RequestFailureDetail:
    endpoint: str
    scope: str
    scope_value: str
    url: str
    status_code: int | None
    reason: str
    response_body: str
    response_body_truncated: bool
    response_body_original_chars: int
    exception_type: str
    attempts: int

    @classmethod
    def from_error(
        cls,
        error: OBSRequestError,
        *,
        scope: str,
        scope_value: str,
    ) -> "RequestFailureDetail":
        return cls(
            endpoint=error.endpoint,
            scope=scope,
            scope_value=scope_value,
            url=error.url,
            status_code=error.status_code,
            reason=error.reason,
            response_body=error.response_body,
            response_body_truncated=error.response_body_truncated,
            response_body_original_chars=error.response_body_original_chars,
            exception_type=error.exception_type,
            attempts=error.attempts,
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "scope": self.scope,
            "scope_value": self.scope_value,
            "url": self.url,
            "status_code": self.status_code,
            "reason": self.reason,
            "response_body": self.response_body,
            "response_body_truncated": self.response_body_truncated,
            "response_body_original_chars": self.response_body_original_chars,
            "exception_type": self.exception_type,
            "attempts": self.attempts,
        }


@dataclass
class ObjectkeysProgress:
    total: int
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    pages: int = 0
    objects: int = 0

    def __post_init__(self) -> None:
        if min(self.total, self.completed, self.succeeded, self.failed, self.pages, self.objects) < 0:
            raise ValueError("objectkeys progress values must be non-negative")
        if self.succeeded + self.failed != self.completed or self.completed > self.total:
            raise ValueError("objectkeys progress must satisfy succeeded + failed == completed <= total")

    def record_page(self, object_count: int) -> None:
        if object_count < 0:
            raise ValueError("object_count must be non-negative")
        self.pages += 1
        self.objects += object_count

    def record_success(self) -> None:
        if self.completed >= self.total:
            raise ValueError("objectkeys progress cannot exceed total")
        self.succeeded += 1
        self.completed += 1

    def record_failure(self) -> None:
        if self.completed >= self.total:
            raise ValueError("objectkeys progress cannot exceed total")
        self.failed += 1
        self.completed += 1


@dataclass
class PartialErrorSummary:
    sample_limit: int = 10
    filelist_failed_dirs: int = 0
    metadata_failed_files: int = 0
    objectkeys_failed_prefixes: int = 0
    samples: list[PartialErrorSample] = field(default_factory=list)
    errors: list[RequestFailureDetail] = field(default_factory=list)

    def record(
        self,
        endpoint: str,
        target: str,
        error: BaseException | str,
        *,
        scope: str = "target",
    ) -> None:
        if endpoint == "filelist":
            self.filelist_failed_dirs += 1
        elif endpoint == "metadata":
            self.metadata_failed_files += 1
        elif endpoint == "objectkeys":
            self.objectkeys_failed_prefixes += 1
        status = error.status_code if isinstance(error, OBSRequestError) else None
        reason = error.reason if isinstance(error, OBSRequestError) else str(error)
        if len(self.samples) < self.sample_limit:
            self.samples.append(
                PartialErrorSample(
                    endpoint=endpoint,
                    target=target,
                    status=status,
                    reason=_sanitize_reason(reason)[:200],
                )
            )
        if isinstance(error, OBSRequestError):
            self.errors.append(RequestFailureDetail.from_error(error, scope=scope, scope_value=target))

    def summary_text(self) -> str | None:
        if not self.errors:
            return None
        counts = Counter(detail.endpoint for detail in self.errors)
        count_text = ", ".join(f"{endpoint}={counts[endpoint]}" for endpoint in sorted(counts))
        first = self.errors[0]
        status = "unknown" if first.status_code is None else str(first.status_code)
        return (
            f"{len(self.errors)} request failures; {count_text}; first: {first.endpoint} "
            f"{first.scope}={first.scope_value} status={status} reason={first.reason}"
        )

    def has_errors(self) -> bool:
        return (
            self.filelist_failed_dirs > 0
            or self.metadata_failed_files > 0
            or self.objectkeys_failed_prefixes > 0
        )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "filelist_failed_dirs": self.filelist_failed_dirs,
            "metadata_failed_files": self.metadata_failed_files,
            "objectkeys_failed_prefixes": self.objectkeys_failed_prefixes,
            "samples": [sample.to_manifest() for sample in self.samples],
        }


@dataclass(frozen=True)
class BucketScanResult:
    appid: str
    bucket_name: str
    bucket_id: str
    status: ScanStatus
    csv_path: Path | None
    thresholds: Thresholds
    error: str | None = None
    partial_errors: PartialErrorSummary | None = None
    errors: list[RequestFailureDetail] = field(default_factory=list)
    started_ms: int = 0
    ended_ms: int = 0
    started_at: str = ""
    ended_at: str = ""
    elapsed_seconds: float = 0.0
