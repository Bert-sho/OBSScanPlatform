from obs_scan_platform.paths import (
    depth_for_directory,
    directory_chain_for_object,
    normalize_object_key,
    prefix_temp_filename,
    safe_filename,
)


def test_normalize_object_key_removes_leading_slash():
    assert normalize_object_key("/a/b/file.txt") == "a/b/file.txt"
    assert normalize_object_key("a/b/file.txt") == "a/b/file.txt"


def test_normalize_object_key_preserves_spaces():
    assert normalize_object_key(" a/file.txt ") == " a/file.txt "


def test_directory_chain_for_nested_object():
    assert directory_chain_for_object("a/b/file.txt") == ["/a/b/", "/a/", "/"]


def test_directory_chain_for_root_file():
    assert directory_chain_for_object("file.txt") == ["/"]


def test_safe_filename_replaces_path_separators():
    assert safe_filename("a/b/") == "a_b"


def test_prefix_temp_filename_is_bounded_and_keeps_hash_suffix():
    name = prefix_temp_filename("a" * 300 + "/")
    assert name.endswith(".csv")
    assert len(name) <= 100


def test_depth_for_directory_counts_segments():
    assert depth_for_directory("/") == 0
    assert depth_for_directory("/a/b/") == 2
