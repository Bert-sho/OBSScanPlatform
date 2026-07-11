from obs_scan_platform.config import Thresholds
from obs_scan_platform.models import (
    DirectoryStats,
    ObjectkeysProgress,
    ObjectRow,
    PartialErrorSummary,
    RequestFailureDetail,
    RootDiscovery,
)
from obs_scan_platform.obs_client import OBSRequestError


def request_error(endpoint: str = "objectkeys") -> OBSRequestError:
    return OBSRequestError(
        endpoint=endpoint,
        status_code=503,
        reason="Service Unavailable: busy",
        url="https://obs.example/test?token=raw-token",
        response_body='{"success":false,"msg":"busy"}',
        response_body_truncated=False,
        response_body_original_chars=30,
        exception_type="OBSBusinessError",
        attempts=4,
    )


def test_root_discovery_accepts_legacy_root_files_constructor():
    discovery = RootDiscovery(prefixes=["alpha/"], root_files=["root.txt"])

    assert discovery.metadata_files == ["root.txt"]
    assert discovery.root_files == ["root.txt"]


def test_directory_stats_add_object_accumulates_counts_and_sizes():
    stats = DirectoryStats()
    thresholds = Thresholds(large_directory_bytes=100, large_file_bytes=10, inactive_directory_days=180)

    stats.add_object(ObjectRow("a/empty.txt", 0, None), thresholds)
    stats.add_object(ObjectRow("a/large.bin", 10, 1000), thresholds)
    stats.add_object(ObjectRow("a/newer.bin", 5, 2000), thresholds)

    assert stats.object_count == 3
    assert stats.total_size_bytes == 15
    assert stats.max_file_size_bytes == 10
    assert stats.empty_file_count == 1
    assert stats.large_file_count == 1
    assert stats.latest_modified_ms == 2000


def test_request_failure_detail_serializes_complete_request_context():
    detail = RequestFailureDetail.from_error(
        request_error(),
        scope="prefix",
        scope_value="photos/2025/",
    )

    assert detail.to_manifest() == {
        "endpoint": "objectkeys",
        "scope": "prefix",
        "scope_value": "photos/2025/",
        "url": "https://obs.example/test?token=raw-token",
        "status_code": 503,
        "reason": "Service Unavailable: busy",
        "response_body": '{"success":false,"msg":"busy"}',
        "response_body_truncated": False,
        "response_body_original_chars": 30,
        "exception_type": "OBSBusinessError",
        "attempts": 4,
    }


def test_partial_error_summary_keeps_compatible_samples_and_complete_errors():
    summary = PartialErrorSummary(sample_limit=1)
    summary.record("metadata", "root.txt", request_error("metadata"), scope="object_key")
    summary.record("objectkeys", "logs/", request_error(), scope="prefix")

    assert summary.metadata_failed_files == 1
    assert summary.objectkeys_failed_prefixes == 1
    assert len(summary.samples) == 1
    assert len(summary.errors) == 2
    assert summary.summary_text() == (
        "2 request failures; metadata=1, objectkeys=1; first: metadata "
        "object_key=root.txt status=503 reason=Service Unavailable: busy"
    )


def test_objectkeys_progress_tracks_pages_objects_and_prefix_outcomes():
    progress = ObjectkeysProgress(total=3)
    progress.record_page(4)
    progress.record_success()
    progress.record_page(2)
    progress.record_failure()

    assert progress.completed == 2
    assert progress.succeeded == 1
    assert progress.failed == 1
    assert progress.pages == 2
    assert progress.objects == 6
    assert progress.succeeded + progress.failed == progress.completed <= progress.total
