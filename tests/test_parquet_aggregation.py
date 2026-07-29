import pytest

from obs_scan_platform.config import DEFAULT_FILE_TYPE_MAP
from obs_scan_platform.models import ObjectRow


@pytest.mark.parametrize(
    ("object_key", "max_depth", "expected"),
    [
        ("root.txt", 4, "/"),
        ("a/direct.txt", 4, "/a/"),
        ("a/b/c/d/file.txt", 4, "/a/b/c/d/"),
        ("a/b/c/d/e/deep.jpg", 4, "/a/b/c/d/"),
        ("a/b.txt", 0, "/"),
        ("/a/b/c.txt", 2, "/a/b/"),
    ],
)
def test_attributed_directory_path_maps_each_object_once(
    object_key: str,
    max_depth: int,
    expected: str,
):
    from obs_scan_platform.parquet_aggregation import attributed_directory_path

    assert attributed_directory_path(object_key, max_depth) == expected


@pytest.mark.parametrize(
    ("object_key", "expected"),
    [
        ("PHOTO.JPG", "图片"),
        ("archive.unknown", "其他"),
        ("README", "其他"),
        ("nested/archive.tar", "压缩包"),
    ],
)
def test_file_type_for_object_is_case_insensitive_and_has_other_fallback(
    object_key: str,
    expected: str,
):
    from obs_scan_platform.parquet_aggregation import file_type_for_object

    assert file_type_for_object(object_key, DEFAULT_FILE_TYPE_MAP) == expected


def test_parquet_directory_summary_combines_numeric_date_and_type_metrics():
    from obs_scan_platform.parquet_aggregation import summary_for_object

    left = summary_for_object(
        ObjectRow("a/direct.JPG", 7, 1_700_000_000_000),
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
    )
    right = summary_for_object(
        ObjectRow("a/other.bin", 11, None),
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
    )

    combined = left.combine(right)

    assert combined.path == "/a/"
    assert combined.object_count == 2
    assert combined.total_size == 18
    assert combined.max_file_size == 11
    assert combined.latest_modified_ms == 1_700_000_000_000
    assert combined.file_types == frozenset({"图片", "其他"})


def test_parquet_directory_summary_rejects_combining_different_paths():
    from obs_scan_platform.parquet_aggregation import summary_for_object

    left = summary_for_object(
        ObjectRow("a/file.jpg", 1, None),
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
    )
    right = summary_for_object(
        ObjectRow("b/file.jpg", 1, None),
        max_depth=4,
        file_type_map=DEFAULT_FILE_TYPE_MAP,
    )

    with pytest.raises(ValueError, match="paths must match"):
        left.combine(right)
