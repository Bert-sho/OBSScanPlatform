import hashlib
import re


def normalize_object_key(object_key: str) -> str:
    return object_key.lstrip("/")


def directory_chain_for_object(object_key: str) -> list[str]:
    normalized = normalize_object_key(object_key)
    if "/" not in normalized:
        return ["/"]
    parts = normalized.split("/")[:-1]
    directories: list[str] = []
    for index in range(len(parts), 0, -1):
        directories.append("/" + "/".join(parts[:index]) + "/")
    directories.append("/")
    return directories


def depth_for_directory(directory_path: str) -> int:
    if directory_path == "/":
        return 0
    return len([part for part in directory_path.strip("/").split("/") if part])


def safe_filename(value: str) -> str:
    cleaned = value.strip("/")
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", cleaned)
    return cleaned.strip("_") or "root"


def prefix_temp_filename(prefix: str) -> str:
    digest = hashlib.sha1(prefix.encode("utf-8")).hexdigest()[:12]
    safe_prefix = safe_filename(prefix)[:80]
    return f"{safe_prefix}_{digest}.csv"
