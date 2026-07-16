import csv
from pathlib import Path
from typing import Iterable

from obs_scan_platform.models import ObjectRow


OBJECT_ROW_FIELDS = ["object_key", "size_bytes", "last_modified_ms"]


def append_object_rows(path: Path, rows: Iterable[ObjectRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    has_header = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=OBJECT_ROW_FIELDS)
        if not has_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "object_key": row.object_key,
                    "size_bytes": row.size_bytes,
                    "last_modified_ms": "" if row.last_modified_ms is None else row.last_modified_ms,
                }
            )


def iter_object_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != OBJECT_ROW_FIELDS:
            raise ValueError(f"unexpected object CSV header in {path}")
        for row in reader:
            last_modified = row["last_modified_ms"]
            yield ObjectRow(
                object_key=row["object_key"],
                size_bytes=int(row["size_bytes"]),
                last_modified_ms=int(last_modified) if last_modified else None,
            )


def iter_object_rows(temp_dir: Path):
    for csv_path in sorted(temp_dir.glob("*.csv")):
        yield from iter_object_csv(csv_path)
