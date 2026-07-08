from obs_scan_platform.config import Thresholds
from obs_scan_platform.models import DirectoryStats, ObjectRow


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
