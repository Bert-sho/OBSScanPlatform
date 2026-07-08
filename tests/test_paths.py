from obs_scan_platform.paths import directory_chain_for_object, normalize_object_key, safe_filename


def test_normalize_object_key_removes_leading_slash():
    assert normalize_object_key("/a/b/file.txt") == "a/b/file.txt"
    assert normalize_object_key("a/b/file.txt") == "a/b/file.txt"


def test_directory_chain_for_nested_object():
    assert directory_chain_for_object("a/b/file.txt") == ["/a/b/", "/a/", "/"]


def test_directory_chain_for_root_file():
    assert directory_chain_for_object("file.txt") == ["/"]


def test_safe_filename_replaces_path_separators():
    assert safe_filename("a/b/") == "a_b"
