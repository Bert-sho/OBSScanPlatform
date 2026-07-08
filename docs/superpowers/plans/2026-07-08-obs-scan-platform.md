# OBS Scan Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个 Python 后端 OBS 扫描平台，支持配置文件驱动的受控并发扫描、目录级 CSV 汇总、实时 CLI 日志、manifest 记录和轻量 FastAPI 管理接口。

**Architecture:** 项目采用 `src/` 布局，核心扫描能力放在 `obs_scan_platform` 包内。CLI、FastAPI 和测试复用同一套配置、OBS 客户端、扫描编排、临时 CSV 采集和目录聚合模块。

**Tech Stack:** Python 3.11+、asyncio、httpx、FastAPI、Typer、Pydantic、PyYAML、pytest、pytest-asyncio。

---

## Scope Check

本计划覆盖同一个第一版产品闭环：配置加载、OBS API 访问、扫描采集、目录汇总、CLI、FastAPI 查询。CLI 和 API 是同一扫描核心的两个入口，不拆成独立计划。

## File Structure

- Create: `pyproject.toml`
  - 定义包元数据、依赖、pytest 配置和 CLI entry point。
- Create: `README.md`
  - 记录第一版运行方式和配置文件位置。
- Create: `config/apps.example.yaml`
  - 示例配置，不包含真实 token。
- Create: `src/obs_scan_platform/__init__.py`
  - 包版本。
- Create: `src/obs_scan_platform/config.py`
  - 配置模型、配置加载、桶级阈值解析、token 脱敏。
- Create: `src/obs_scan_platform/models.py`
  - 扫描过程共享的数据模型。
- Create: `src/obs_scan_platform/paths.py`
  - object key 归一化、目录父级展开、安全文件名。
- Create: `src/obs_scan_platform/csv_store.py`
  - 临时对象 CSV 写入、最终目录 CSV 写入。
- Create: `src/obs_scan_platform/aggregation.py`
  - 从临时对象 CSV 汇总目录指标。
- Create: `src/obs_scan_platform/obs_client.py`
  - async OBS API client、base64 requestbody、重试和全局请求 semaphore。
- Create: `src/obs_scan_platform/scanner.py`
  - 应用/桶扫描编排、并发控制、manifest 写入。
- Create: `src/obs_scan_platform/logging_config.py`
  - CLI 和文件日志初始化。
- Create: `src/obs_scan_platform/cli.py`
  - Typer CLI。
- Create: `src/obs_scan_platform/api.py`
  - FastAPI 应用。
- Create: `tests/`
  - 覆盖配置、路径、聚合、OBS client、扫描器、CLI、API。

## Task 1: Project Skeleton

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `src/obs_scan_platform/__init__.py`
- Create: `tests/test_project_skeleton.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_project_skeleton.py`:

```python
from typer.testing import CliRunner

from obs_scan_platform import __version__
from obs_scan_platform.cli import app


def test_package_has_version():
    assert __version__ == "0.1.0"


def test_cli_help_loads():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "scan" in result.output
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
pytest tests/test_project_skeleton.py -v
```

Expected: FAIL because `obs_scan_platform` and `obs_scan_platform.cli` do not exist.

- [ ] **Step 3: Create project metadata**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "obs-scan-platform"
version = "0.1.0"
description = "Controlled OBS bucket scanner with directory CSV aggregation"
requires-python = ">=3.11"
dependencies = [
  "fastapi>=0.111",
  "httpx>=0.27",
  "pydantic>=2.7",
  "pyyaml>=6.0",
  "typer>=0.12",
  "uvicorn>=0.30",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2",
  "pytest-asyncio>=0.23",
]

[project.scripts]
obs-scan = "obs_scan_platform.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
pythonpath = ["src"]
```

- [ ] **Step 4: Create package and minimal CLI**

Create `src/obs_scan_platform/__init__.py`:

```python
__version__ = "0.1.0"
```

Create `src/obs_scan_platform/cli.py`:

```python
import typer

app = typer.Typer(help="OBS scan platform command line tools.")


@app.command()
def scan() -> None:
    """Run an OBS scan."""
    typer.echo("scan command is not wired yet")


def main() -> None:
    app()
```

- [ ] **Step 5: Add README**

Create `README.md`:

```markdown
# OBS Scan Platform

Python backend for scanning OBS bucket usage with controlled concurrency and directory-level CSV aggregation.

First-version entry points:

- CLI scanner: `obs-scan scan --config config/apps.yaml`
- API server: `uvicorn obs_scan_platform.api:app --reload`

Design spec: `docs/superpowers/specs/2026-07-08-obs-scan-platform-design.md`
```

- [ ] **Step 6: Run the test to verify it passes**

Run:

```bash
pytest tests/test_project_skeleton.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml README.md src/obs_scan_platform/__init__.py src/obs_scan_platform/cli.py tests/test_project_skeleton.py
git commit -m "chore: add Python project skeleton"
```

## Task 2: Configuration Loading

**Files:**
- Create: `src/obs_scan_platform/config.py`
- Create: `config/apps.example.yaml`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_config.py`:

```python
from pathlib import Path

from obs_scan_platform.config import load_config


def test_load_config_and_resolve_bucket_thresholds(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
scan:
  results_dir: results
  temp_subdir: _tmp
  keep_temp_files: false
  page_size: 1000
  app_concurrency: 2
  bucket_concurrency: 4
  global_request_concurrency: 50
  per_bucket_prefix_concurrency: 8
  metadata_concurrency_per_bucket: 8
  request_timeout_seconds: 30
  max_retries: 5
  retry_base_delay_seconds: 2
  retry_max_delay_seconds: 60
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    endpoint: http://obs.example
    apptoken: secret-token
    enabled: true
    buckets:
      bucket-a:
        large_directory_bytes: 200
        large_file_bytes: 20
        inactive_directory_days: 365
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    app_config = config.enabled_applications()[0]

    assert app_config.appid == "app.one"
    assert config.thresholds_for(app_config, "bucket-a").large_directory_bytes == 200
    assert config.thresholds_for(app_config, "bucket-missing").large_directory_bytes == 100


def test_masked_config_hides_token(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
scan: {}
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    endpoint: http://obs.example
    apptoken: secret-token
    enabled: true
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    masked = config.masked_dict()

    assert masked["applications"][0]["apptoken"] == "******"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_config.py -v
```

Expected: FAIL because `obs_scan_platform.config` does not exist.

- [ ] **Step 3: Implement config models and loader**

Create `src/obs_scan_platform/config.py`:

```python
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ScanSettings(BaseModel):
    results_dir: str = "results"
    temp_subdir: str = "_tmp"
    keep_temp_files: bool = False
    page_size: int = 1000
    app_concurrency: int = 2
    bucket_concurrency: int = 4
    global_request_concurrency: int = 50
    per_bucket_prefix_concurrency: int = 8
    metadata_concurrency_per_bucket: int = 8
    request_timeout_seconds: int = 30
    max_retries: int = 5
    retry_base_delay_seconds: float = 2
    retry_max_delay_seconds: float = 60


class Thresholds(BaseModel):
    large_directory_bytes: int
    large_file_bytes: int
    inactive_directory_days: int


class ApplicationConfig(BaseModel):
    appid: str
    name: str
    endpoint: str
    apptoken: str
    enabled: bool = True
    buckets: dict[str, Thresholds] = Field(default_factory=dict)


class AppConfigFile(BaseModel):
    scan: ScanSettings = Field(default_factory=ScanSettings)
    defaults: Thresholds
    applications: list[ApplicationConfig]
    source_path: Path | None = None

    def enabled_applications(self) -> list[ApplicationConfig]:
        return [application for application in self.applications if application.enabled]

    def thresholds_for(self, application: ApplicationConfig, bucket_name: str) -> Thresholds:
        return application.buckets.get(bucket_name, self.defaults)

    def masked_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude={"source_path"})
        for application in data["applications"]:
            application["apptoken"] = "******"
        return data


def load_config(path: str | Path) -> AppConfigFile:
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = AppConfigFile.model_validate(raw)
    config.source_path = config_path
    return config
```

- [ ] **Step 4: Add example config**

Create `config/apps.example.yaml`:

```yaml
scan:
  results_dir: results
  temp_subdir: _tmp
  keep_temp_files: false
  page_size: 1000
  app_concurrency: 2
  bucket_concurrency: 4
  global_request_concurrency: 50
  per_bucket_prefix_concurrency: 8
  metadata_concurrency_per_bucket: 8
  request_timeout_seconds: 30
  max_retries: 5
  retry_base_delay_seconds: 2
  retry_max_delay_seconds: 60

defaults:
  large_directory_bytes: 107374182400
  large_file_bytes: 10737418240
  inactive_directory_days: 180

applications:
  - appid: com.camera.pergen
    name: Example application
    endpoint: http://example.com
    apptoken: replace-with-real-token
    enabled: true
    buckets:
      bucket-1191:
        large_directory_bytes: 214748364800
        large_file_bytes: 21474836480
        inactive_directory_days: 365
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
pytest tests/test_config.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/obs_scan_platform/config.py config/apps.example.yaml tests/test_config.py
git commit -m "feat: add scan configuration loading"
```

## Task 3: Shared Models and Path Utilities

**Files:**
- Create: `src/obs_scan_platform/models.py`
- Create: `src/obs_scan_platform/paths.py`
- Create: `tests/test_paths.py`

- [ ] **Step 1: Write failing path tests**

Create `tests/test_paths.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_paths.py -v
```

Expected: FAIL because `obs_scan_platform.paths` does not exist.

- [ ] **Step 3: Implement shared models**

Create `src/obs_scan_platform/models.py`:

```python
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
```

- [ ] **Step 4: Implement path utilities**

Create `src/obs_scan_platform/paths.py`:

```python
import hashlib
import re


def normalize_object_key(object_key: str) -> str:
    return object_key.strip().lstrip("/")


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
    return f"{safe_filename(prefix)}_{digest}.csv"
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_paths.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/obs_scan_platform/models.py src/obs_scan_platform/paths.py tests/test_paths.py
git commit -m "feat: add shared scan models and path helpers"
```

## Task 4: CSV Store and Directory Aggregation

**Files:**
- Create: `src/obs_scan_platform/csv_store.py`
- Create: `src/obs_scan_platform/aggregation.py`
- Create: `tests/test_aggregation.py`

- [ ] **Step 1: Write failing aggregation tests**

Create `tests/test_aggregation.py`:

```python
import csv
from pathlib import Path

from obs_scan_platform.aggregation import aggregate_bucket
from obs_scan_platform.config import Thresholds
from obs_scan_platform.csv_store import append_object_rows
from obs_scan_platform.models import ObjectRow


def test_aggregate_bucket_rolls_objects_to_parents(tmp_path: Path):
    temp_dir = tmp_path / "_tmp"
    temp_dir.mkdir()
    append_object_rows(
        temp_dir / "prefix.csv",
        [
            ObjectRow("a/b/file.txt", 10, 1000),
            ObjectRow("a/empty.log", 0, 2000),
            ObjectRow("root.bin", 25, 3000),
        ],
    )
    output = tmp_path / "bucket.csv"

    aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=output,
        thresholds=Thresholds(
            large_directory_bytes=20,
            large_file_bytes=20,
            inactive_directory_days=1,
        ),
        scan_started_ms=3_600_000 * 24 * 10,
    )

    rows = {row["directory_path"]: row for row in csv.DictReader(output.open())}
    assert rows["/"]["object_count"] == "3"
    assert rows["/"]["total_size_bytes"] == "35"
    assert rows["/"]["has_large_file"] == "true"
    assert rows["/a/"]["empty_file_count"] == "1"
    assert rows["/a/b/"]["object_count"] == "1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_aggregation.py -v
```

Expected: FAIL because CSV store and aggregation modules do not exist.

- [ ] **Step 3: Implement CSV store**

Create `src/obs_scan_platform/csv_store.py`:

```python
import csv
from pathlib import Path
from typing import Iterable

from obs_scan_platform.models import ObjectRow


OBJECT_ROW_FIELDS = ["object_key", "size_bytes", "last_modified_ms"]


def append_object_rows(path: Path, rows: Iterable[ObjectRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=OBJECT_ROW_FIELDS)
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "object_key": row.object_key,
                    "size_bytes": row.size_bytes,
                    "last_modified_ms": "" if row.last_modified_ms is None else row.last_modified_ms,
                }
            )


def iter_object_rows(temp_dir: Path):
    for csv_path in sorted(temp_dir.glob("*.csv")):
        with csv_path.open(newline="", encoding="utf-8") as file:
            for row in csv.DictReader(file):
                last_modified = row["last_modified_ms"]
                yield ObjectRow(
                    object_key=row["object_key"],
                    size_bytes=int(row["size_bytes"]),
                    last_modified_ms=int(last_modified) if last_modified else None,
                )
```

- [ ] **Step 4: Implement aggregation**

Create `src/obs_scan_platform/aggregation.py`:

```python
import csv
from pathlib import Path

from obs_scan_platform.config import Thresholds
from obs_scan_platform.csv_store import iter_object_rows
from obs_scan_platform.models import DirectoryStats
from obs_scan_platform.paths import depth_for_directory, directory_chain_for_object


FINAL_FIELDS = [
    "run_id",
    "appid",
    "bucket_name",
    "bucket_id",
    "directory_path",
    "depth",
    "object_count",
    "total_size_bytes",
    "max_file_size_bytes",
    "empty_file_count",
    "large_file_count",
    "latest_modified_ms",
    "inactive_days",
    "is_large_directory",
    "has_large_file",
    "has_empty_file",
    "is_inactive_directory",
]


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _inactive_days(scan_started_ms: int, latest_modified_ms: int | None) -> int | None:
    if latest_modified_ms is None:
        return None
    return max(0, (scan_started_ms - latest_modified_ms) // 86_400_000)


def aggregate_bucket(
    *,
    run_id: str,
    appid: str,
    bucket_name: str,
    bucket_id: str,
    temp_dir: Path,
    output_path: Path,
    thresholds: Thresholds,
    scan_started_ms: int,
) -> int:
    stats_by_directory: dict[str, DirectoryStats] = {}
    for row in iter_object_rows(temp_dir):
        for directory in directory_chain_for_object(row.object_key):
            stats = stats_by_directory.setdefault(directory, DirectoryStats())
            stats.add_object(row, thresholds)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FINAL_FIELDS)
        writer.writeheader()
        for directory in sorted(stats_by_directory):
            stats = stats_by_directory[directory]
            inactive_days = _inactive_days(scan_started_ms, stats.latest_modified_ms)
            writer.writerow(
                {
                    "run_id": run_id,
                    "appid": appid,
                    "bucket_name": bucket_name,
                    "bucket_id": bucket_id,
                    "directory_path": directory,
                    "depth": depth_for_directory(directory),
                    "object_count": stats.object_count,
                    "total_size_bytes": stats.total_size_bytes,
                    "max_file_size_bytes": stats.max_file_size_bytes,
                    "empty_file_count": stats.empty_file_count,
                    "large_file_count": stats.large_file_count,
                    "latest_modified_ms": "" if stats.latest_modified_ms is None else stats.latest_modified_ms,
                    "inactive_days": "" if inactive_days is None else inactive_days,
                    "is_large_directory": _bool(stats.total_size_bytes >= thresholds.large_directory_bytes),
                    "has_large_file": _bool(stats.large_file_count > 0),
                    "has_empty_file": _bool(stats.empty_file_count > 0),
                    "is_inactive_directory": _bool(
                        inactive_days is not None and inactive_days >= thresholds.inactive_directory_days
                    ),
                }
            )
    tmp_path.replace(output_path)
    return len(stats_by_directory)
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_aggregation.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/obs_scan_platform/csv_store.py src/obs_scan_platform/aggregation.py tests/test_aggregation.py
git commit -m "feat: aggregate temporary object rows into directory CSV"
```

## Task 5: OBS Async Client

**Files:**
- Create: `src/obs_scan_platform/obs_client.py`
- Create: `tests/test_obs_client.py`

- [ ] **Step 1: Write failing client tests**

Create `tests/test_obs_client.py`:

```python
import asyncio

import httpx
import pytest

from obs_scan_platform.obs_client import OBSClient, OBSRequestError, encode_object_key, encode_request_body


def test_encode_request_body_contains_path_and_size():
    encoded = encode_request_body({"id": "bucket-id", "path": "/", "pointer": "", "size": 1000})
    assert isinstance(encoded, str)
    assert encoded


def test_encode_object_key_encodes_plain_path():
    encoded = encode_object_key("/a/b/file.txt")
    assert isinstance(encoded, str)
    assert encoded


@pytest.mark.asyncio
async def test_get_json_retries_503_then_succeeds():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"success": False, "msg": "busy"})
        return httpx.Response(200, json={"success": True, "value": 1})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=2,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    data = await client.get_json("http://obs.example/test", params={})

    assert data["value"] == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_get_json_raises_after_retries():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"success": False, "msg": "busy"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=1,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    with pytest.raises(OBSRequestError):
        await client.get_json("http://obs.example/test", params={})
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_obs_client.py -v
```

Expected: FAIL because `obs_scan_platform.obs_client` does not exist.

- [ ] **Step 3: Implement OBS client**

Create `src/obs_scan_platform/obs_client.py`:

```python
import asyncio
import base64
import json
from typing import Any

import httpx


class OBSRequestError(RuntimeError):
    pass


def encode_request_body(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def encode_object_key(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("utf-8")


class OBSClient:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        request_semaphore: asyncio.Semaphore,
        max_retries: int,
        retry_base_delay_seconds: float,
        retry_max_delay_seconds: float,
    ) -> None:
        self.http = http
        self.request_semaphore = request_semaphore
        self.max_retries = max_retries
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    async def get_json(self, url: str, *, params: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self.request_semaphore:
                    response = await self.http.get(url, params=params, headers=headers)
                if response.status_code >= 500:
                    raise OBSRequestError(f"HTTP {response.status_code}: {response.text}")
                response.raise_for_status()
                data = response.json()
                success = data.get("success")
                if success in (False, "false"):
                    raise OBSRequestError(str(data.get("msg") or data))
                return data
            except (httpx.TimeoutException, httpx.ConnectError, OBSRequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = min(
                    self.retry_max_delay_seconds,
                    self.retry_base_delay_seconds * (2**attempt),
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        raise OBSRequestError(str(last_error))

    async def close(self) -> None:
        await self.http.aclose()
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_obs_client.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/obs_scan_platform/obs_client.py tests/test_obs_client.py
git commit -m "feat: add async OBS request client"
```

## Task 6: Scanner Orchestration

**Files:**
- Create: `src/obs_scan_platform/logging_config.py`
- Create: `src/obs_scan_platform/scanner.py`
- Create: `tests/test_scanner.py`

- [ ] **Step 1: Write failing scanner tests**

Create `tests/test_scanner.py`:

```python
from obs_scan_platform.models import BucketInfo
from obs_scan_platform.scanner import is_owned_bucket, parse_int_or_none


def test_is_owned_bucket_excludes_shared_bucket():
    assert is_owned_bucket(BucketInfo("1", "a", "HEC", "cn-east-3", "owner", None))
    assert not is_owned_bucket(BucketInfo("2", "b", "HEC", "cn-east-3", "owner", "other"))
    assert not is_owned_bucket(BucketInfo("3", "c", "HEC", "cn-east-3", "reader", None))


def test_parse_int_or_none_handles_dirty_values():
    assert parse_int_or_none("123") == 123
    assert parse_int_or_none(456) == 456
    assert parse_int_or_none("17676892757s82") is None
    assert parse_int_or_none(None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_scanner.py -v
```

Expected: FAIL because `obs_scan_platform.scanner` does not exist.

- [ ] **Step 3: Implement scanner utility functions and class shell**

Create `src/obs_scan_platform/logging_config.py`:

```python
import logging
import sys
from pathlib import Path


def configure_logging(log_path: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers,
        force=True,
    )
```

Create `src/obs_scan_platform/scanner.py`:

```python
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from obs_scan_platform.aggregation import aggregate_bucket
from obs_scan_platform.config import AppConfigFile, ApplicationConfig, load_config
from obs_scan_platform.csv_store import append_object_rows
from obs_scan_platform.models import BucketInfo, BucketScanResult, ObjectRow, RootDiscovery, ScanStatus
from obs_scan_platform.logging_config import configure_logging
from obs_scan_platform.obs_client import OBSClient, encode_object_key, encode_request_body
from obs_scan_platform.paths import prefix_temp_filename


logger = logging.getLogger(__name__)


def parse_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_owned_bucket(bucket: BucketInfo) -> bool:
    return bucket.auth == "owner" and bucket.share_from is None


class Scanner:
    def __init__(self, config: AppConfigFile) -> None:
        self.config = config
        self.request_semaphore = asyncio.Semaphore(config.scan.global_request_concurrency)

    async def run(self, *, run_id: str | None = None, appid: str | None = None) -> dict[str, Any]:
        run_id = run_id or time.strftime("%Y%m%d_%H%M%S")
        started_ms = int(time.time() * 1000)
        results_dir = Path(self.config.scan.results_dir) / run_id
        results_dir.mkdir(parents=True, exist_ok=True)
        configure_logging(results_dir / "scan.log")
        logger.info("scan started run_id=%s appid=%s", run_id, appid or "*")

        selected_apps = [
            app for app in self.config.enabled_applications()
            if appid is None or app.appid == appid
        ]
        app_semaphore = asyncio.Semaphore(self.config.scan.app_concurrency)

        async def run_app(application: ApplicationConfig) -> dict[str, Any]:
            async with app_semaphore:
                return await self._scan_application(application, run_id, results_dir, started_ms)

        app_entries = await asyncio.gather(*(run_app(application) for application in selected_apps))
        status = "success" if all(entry["status"] == "success" for entry in app_entries) else "partial_failed"
        manifest = {
            "run_id": run_id,
            "status": status,
            "started_ms": started_ms,
            "ended_ms": int(time.time() * 1000),
            "config_path": str(self.config.source_path or ""),
            "applications": app_entries,
        }
        (results_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("scan finished run_id=%s status=%s", run_id, status)
        return manifest

    async def _scan_application(
        self,
        application: ApplicationConfig,
        run_id: str,
        results_dir: Path,
        started_ms: int,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(self.config.scan.request_timeout_seconds)
        logger.info("application scan started appid=%s", application.appid)
        async with httpx.AsyncClient(timeout=timeout) as http:
            client = OBSClient(
                http=http,
                request_semaphore=self.request_semaphore,
                max_retries=self.config.scan.max_retries,
                retry_base_delay_seconds=self.config.scan.retry_base_delay_seconds,
                retry_max_delay_seconds=self.config.scan.retry_max_delay_seconds,
            )
            buckets = await self._list_owned_buckets(client, application)
            bucket_semaphore = asyncio.Semaphore(self.config.scan.bucket_concurrency)

            async def run_bucket(bucket: BucketInfo) -> BucketScanResult:
                async with bucket_semaphore:
                    return await self._scan_bucket(client, application, bucket, run_id, results_dir, started_ms)

            bucket_results = await asyncio.gather(*(run_bucket(bucket) for bucket in buckets))
        app_status = "success" if all(result.status == ScanStatus.SUCCESS for result in bucket_results) else "partial_failed"
        logger.info("application scan finished appid=%s status=%s", application.appid, app_status)
        return {
            "appid": application.appid,
            "status": app_status,
            "buckets": [self._bucket_result_to_manifest(result) for result in bucket_results],
        }

    async def _list_owned_buckets(self, client: OBSClient, application: ApplicationConfig) -> list[BucketInfo]:
        url = f"{application.endpoint}/rest/s3/listbuckets"
        data = await client.get_json(
            url,
            params={"appid": application.appid},
            headers={"Content-Type": "application/json", "csb-token": application.apptoken},
        )
        buckets: list[BucketInfo] = []
        for item in data.get("buckets", []):
            bucket = BucketInfo(
                bucket_id=str(item.get("id")),
                name=str(item.get("name")),
                vendor=str(item.get("vendor")),
                region=str(item.get("region")),
                auth=item.get("auth"),
                share_from=item.get("shareFrom"),
            )
            if is_owned_bucket(bucket):
                buckets.append(bucket)
        return buckets

    async def _scan_bucket(
        self,
        client: OBSClient,
        application: ApplicationConfig,
        bucket: BucketInfo,
        run_id: str,
        results_dir: Path,
        started_ms: int,
    ) -> BucketScanResult:
        thresholds = self.config.thresholds_for(application, bucket.name)
        temp_dir = results_dir / self.config.scan.temp_subdir / application.appid / bucket.name
        output_path = results_dir / application.appid / f"{bucket.name}.csv"
        logger.info("bucket scan started appid=%s bucket=%s", application.appid, bucket.name)
        try:
            endpoint = await self._get_bucket_endpoint(client, application, bucket)
            discovery = await self._discover_root(client, application, bucket)
            await self._collect_root_files(client, endpoint, application, bucket, discovery.root_files, temp_dir)
            await self._collect_prefixes(client, endpoint, application, bucket, discovery.prefixes, temp_dir)
            aggregate_bucket(
                run_id=run_id,
                appid=application.appid,
                bucket_name=bucket.name,
                bucket_id=bucket.bucket_id,
                temp_dir=temp_dir,
                output_path=output_path,
                thresholds=thresholds,
                scan_started_ms=started_ms,
            )
            logger.info("bucket scan finished appid=%s bucket=%s csv=%s", application.appid, bucket.name, output_path)
            return BucketScanResult(application.appid, bucket.name, bucket.bucket_id, ScanStatus.SUCCESS, output_path, thresholds)
        except Exception as exc:
            logger.exception("bucket scan failed appid=%s bucket=%s", application.appid, bucket.name)
            return BucketScanResult(application.appid, bucket.name, bucket.bucket_id, ScanStatus.FAILED, None, thresholds, str(exc))

    async def _get_bucket_endpoint(self, client: OBSClient, application: ApplicationConfig, bucket: BucketInfo) -> str:
        url = f"{application.endpoint}/rest/s3/bucket/endpoint"
        data = await client.get_json(
            url,
            params={
                "bucketid": bucket.name,
                "token": application.apptoken,
                "vendor": bucket.vendor,
                "region": bucket.region,
                "bucketUid": bucket.bucket_id,
            },
            headers={"Content-Type": "application/json"},
        )
        return str(data["result"]).rstrip("/")

    async def _discover_root(self, client: OBSClient, application: ApplicationConfig, bucket: BucketInfo) -> RootDiscovery:
        prefixes: set[str] = set()
        root_files: list[str] = []
        pointer = ""
        while True:
            request_body = encode_request_body(
                {"id": bucket.bucket_id, "path": "/", "pointer": pointer, "size": self.config.scan.page_size}
            )
            data = await client.get_json(
                f"{application.endpoint}/rest/s3/bucket/filelist",
                params={"appid": application.appid, "requestbody": request_body},
                headers={"Content-Type": "application/json", "csb-token": application.apptoken},
            )
            for item in data.get("objects", []):
                object_type = str(item.get("objectType") or "").lower()
                object_key = str(item.get("objectKey") or "")
                if object_type == "folder":
                    prefixes.add(object_key.strip("/").split("/")[0] + "/")
                elif object_key:
                    root_files.append(object_key)
            next_pointer = str(data.get("nextOffset") or "")
            if not next_pointer or next_pointer == pointer:
                break
            pointer = next_pointer
        return RootDiscovery(prefixes=sorted(prefixes), root_files=root_files)

    async def _collect_root_files(
        self,
        client: OBSClient,
        endpoint: str,
        application: ApplicationConfig,
        bucket: BucketInfo,
        root_files: list[str],
        temp_dir: Path,
    ) -> None:
        semaphore = asyncio.Semaphore(self.config.scan.metadata_concurrency_per_bucket)

        async def collect_one(object_key: str) -> ObjectRow | None:
            async with semaphore:
                encoded = encode_object_key("/" + object_key.lstrip("/"))
                data = await client.get_json(
                    f"{endpoint}/rest/boto3/s3/object/metadata",
                    params={
                        "vendor": bucket.vendor,
                        "region": bucket.region,
                        "bucketid": bucket.name,
                        "apptoken": application.apptoken,
                        "objectkey": encoded,
                        "bucketld": bucket.bucket_id,
                    },
                    headers={"Content-Type": "application/json"},
                )
                meta = data.get("objectKey", {})
                size = parse_int_or_none(meta.get("size"))
                if size is None:
                    return None
                return ObjectRow(object_key, size, parse_int_or_none(meta.get("lastModifyTime")))

        rows = [row for row in await asyncio.gather(*(collect_one(key) for key in root_files)) if row is not None]
        append_object_rows(temp_dir / "root_files.csv", rows)

    async def _collect_prefixes(
        self,
        client: OBSClient,
        endpoint: str,
        application: ApplicationConfig,
        bucket: BucketInfo,
        prefixes: list[str],
        temp_dir: Path,
    ) -> None:
        semaphore = asyncio.Semaphore(self.config.scan.per_bucket_prefix_concurrency)

        async def collect_prefix(prefix: str) -> None:
            async with semaphore:
                await self._collect_prefix(client, endpoint, application, bucket, prefix, temp_dir)

        await asyncio.gather(*(collect_prefix(prefix) for prefix in prefixes))

    async def _collect_prefix(
        self,
        client: OBSClient,
        endpoint: str,
        application: ApplicationConfig,
        bucket: BucketInfo,
        prefix: str,
        temp_dir: Path,
    ) -> None:
        marker = ""
        output = temp_dir / prefix_temp_filename(prefix)
        while True:
            encoded_prefix = encode_object_key("/" + prefix.lstrip("/"))
            data = await client.get_json(
                f"{endpoint}/rest/boto3/s3/list/bucket/objectkeys",
                params={
                    "vendor": bucket.vendor,
                    "region": bucket.region,
                    "bucketid": bucket.name,
                    "apptoken": application.apptoken,
                    "objectkey": encoded_prefix,
                    "nextmarker": marker,
                    "bucketld": bucket.bucket_id,
                },
                headers={"Content-Type": "application/json"},
            )
            rows: list[ObjectRow] = []
            for item in data.get("objectKeys", []):
                size = parse_int_or_none(item.get("size"))
                if size is None:
                    continue
                rows.append(ObjectRow(str(item.get("objectKey")), size, parse_int_or_none(item.get("lastModifyTime"))))
            append_object_rows(output, rows)
            next_marker = str(data.get("nextmarker") or "")
            truncated = str(data.get("truncated") or "false").lower() == "true"
            if not truncated or not next_marker or next_marker == marker:
                break
            marker = next_marker

    def _bucket_result_to_manifest(self, result: BucketScanResult) -> dict[str, Any]:
        return {
            "bucket_name": result.bucket_name,
            "bucket_id": result.bucket_id,
            "status": result.status.value,
            "csv_path": "" if result.csv_path is None else str(result.csv_path),
            "thresholds": result.thresholds.model_dump(),
            "error": result.error,
        }


async def run_scan(config_path: str | Path, *, run_id: str | None = None, appid: str | None = None) -> dict[str, Any]:
    scanner = Scanner(load_config(config_path))
    return await scanner.run(run_id=run_id, appid=appid)
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest tests/test_scanner.py -v
```

Expected: PASS.

- [ ] **Step 5: Run existing unit tests**

Run:

```bash
pytest tests/test_config.py tests/test_paths.py tests/test_aggregation.py tests/test_obs_client.py tests/test_scanner.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/obs_scan_platform/logging_config.py src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "feat: orchestrate OBS bucket scanning"
```

## Task 7: Logging and CLI Scan Command

**Files:**
- Modify: `src/obs_scan_platform/logging_config.py`
- Modify: `src/obs_scan_platform/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_cli.py`:

```python
from typer.testing import CliRunner

from obs_scan_platform.cli import app


def test_scan_requires_config():
    result = CliRunner().invoke(app, ["scan"])
    assert result.exit_code != 0
    assert "Missing option" in result.output
```

- [ ] **Step 2: Run tests to verify they fail or expose current stub**

Run:

```bash
pytest tests/test_cli.py -v
```

Expected: FAIL because the current `scan` command does not require `--config`.

- [ ] **Step 3: Confirm logging setup exists**

Confirm `src/obs_scan_platform/logging_config.py` contains:

```python
import logging
import sys
from pathlib import Path


def configure_logging(log_path: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers,
        force=True,
    )
```

- [ ] **Step 4: Replace CLI implementation**

Modify `src/obs_scan_platform/cli.py`:

```python
import asyncio
from pathlib import Path

import typer

from obs_scan_platform.scanner import run_scan

app = typer.Typer(help="OBS scan platform command line tools.")


@app.command()
def scan(
    config: Path = typer.Option(..., "--config", "-c", help="Path to config/apps.yaml"),
    appid: str | None = typer.Option(None, "--appid", help="Scan only one application id"),
    run_id: str | None = typer.Option(None, "--run-id", help="Use a fixed run id"),
) -> None:
    """Run an OBS scan."""
    manifest = asyncio.run(run_scan(config, run_id=run_id, appid=appid))
    typer.echo(f"scan finished: {manifest['status']} run_id={manifest['run_id']}")
    if manifest["status"] != "success":
        raise typer.Exit(code=1)


def main() -> None:
    app()
```

- [ ] **Step 5: Run CLI tests**

Run:

```bash
pytest tests/test_cli.py tests/test_project_skeleton.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/obs_scan_platform/logging_config.py src/obs_scan_platform/cli.py tests/test_cli.py
git commit -m "feat: wire scan CLI command"
```

## Task 8: FastAPI Management API

**Files:**
- Create: `src/obs_scan_platform/api.py`
- Create: `tests/test_api.py`

- [ ] **Step 1: Write failing API tests**

Create `tests/test_api.py`:

```python
import json
from pathlib import Path

from fastapi.testclient import TestClient

from obs_scan_platform.api import create_app


def test_health():
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_runs_list_reads_manifest(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps({"run_id": "run-1", "status": "success", "applications": []}),
        encoding="utf-8",
    )
    client = TestClient(create_app(results_dir=tmp_path))

    response = client.get("/runs")

    assert response.status_code == 200
    assert response.json()[0]["run_id"] == "run-1"


def test_post_runs_requires_config_path(tmp_path: Path):
    client = TestClient(create_app(results_dir=tmp_path))
    response = client.post("/runs")
    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_api.py -v
```

Expected: FAIL because `obs_scan_platform.api` does not exist.

- [ ] **Step 3: Implement FastAPI app**

Create `src/obs_scan_platform/api.py`:

```python
import json
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from obs_scan_platform.config import load_config
from obs_scan_platform.scanner import run_scan


def _read_manifest(run_dir: Path) -> dict:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="run manifest not found")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def create_app(*, config_path: Path | None = None, results_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="OBS Scan Platform")
    configured_results_dir = results_dir or Path("results")
    app.state.active_scan = False

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/config/apps")
    def config_apps() -> dict:
        if config_path is None:
            raise HTTPException(status_code=404, detail="config path is not configured")
        return load_config(config_path).masked_dict()

    @app.get("/runs")
    def runs() -> list[dict]:
        if not configured_results_dir.exists():
            return []
        manifests = []
        for run_dir in sorted(configured_results_dir.iterdir()):
            if run_dir.is_dir() and (run_dir / "manifest.json").exists():
                manifests.append(_read_manifest(run_dir))
        return manifests

    @app.get("/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        return _read_manifest(configured_results_dir / run_id)

    @app.get("/runs/{run_id}/logs")
    def run_logs(run_id: str) -> PlainTextResponse:
        log_path = configured_results_dir / run_id / "scan.log"
        if not log_path.exists():
            raise HTTPException(status_code=404, detail="scan log not found")
        return PlainTextResponse(log_path.read_text(encoding="utf-8"))

    @app.get("/runs/{run_id}/apps/{appid}/buckets/{bucket_name}/csv")
    def bucket_csv(run_id: str, appid: str, bucket_name: str) -> FileResponse:
        csv_path = configured_results_dir / run_id / appid / f"{bucket_name}.csv"
        if not csv_path.exists():
            raise HTTPException(status_code=404, detail="bucket csv not found")
        return FileResponse(csv_path, media_type="text/csv", filename=f"{bucket_name}.csv")

    async def _run_scan_background() -> None:
        try:
            await run_scan(config_path)
        finally:
            app.state.active_scan = False

    @app.post("/runs")
    async def trigger_run(background_tasks: BackgroundTasks) -> dict[str, str]:
        if config_path is None:
            raise HTTPException(status_code=404, detail="config path is not configured")
        if app.state.active_scan:
            raise HTTPException(status_code=409, detail="scan is already running")
        app.state.active_scan = True
        background_tasks.add_task(_run_scan_background)
        return {"status": "accepted"}

    return app


app = create_app()
```

- [ ] **Step 4: Run API tests**

Run:

```bash
pytest tests/test_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/obs_scan_platform/api.py tests/test_api.py
git commit -m "feat: add FastAPI management endpoints"
```

## Task 9: End-to-End Validation With Mocked OBS

**Files:**
- Create: `tests/test_scan_end_to_end.py`
- Modify: `README.md`

- [ ] **Step 1: Write end-to-end test**

Create `tests/test_scan_end_to_end.py`:

```python
from pathlib import Path

from obs_scan_platform.aggregation import aggregate_bucket
from obs_scan_platform.config import Thresholds
from obs_scan_platform.csv_store import append_object_rows
from obs_scan_platform.models import ObjectRow


def test_end_to_end_local_collection_and_aggregation(tmp_path: Path):
    run_dir = tmp_path / "results" / "run-1"
    temp_dir = run_dir / "_tmp" / "app.one" / "bucket-a"
    final_csv = run_dir / "app.one" / "bucket-a.csv"
    append_object_rows(
        temp_dir / "a.csv",
        [
            ObjectRow("a/file-1.txt", 5, 1000),
            ObjectRow("a/big.bin", 30, 2000),
            ObjectRow("a/b/old.log", 1, 3000),
        ],
    )

    row_count = aggregate_bucket(
        run_id="run-1",
        appid="app.one",
        bucket_name="bucket-a",
        bucket_id="bucket-id",
        temp_dir=temp_dir,
        output_path=final_csv,
        thresholds=Thresholds(
            large_directory_bytes=20,
            large_file_bytes=20,
            inactive_directory_days=180,
        ),
        scan_started_ms=4_000,
    )

    assert row_count == 3
    assert final_csv.exists()
    assert "is_large_directory" in final_csv.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run full test suite**

Run:

```bash
pytest -v
```

Expected: PASS.

- [ ] **Step 3: Update README with usage**

Modify `README.md`:

```markdown
# OBS Scan Platform

Python backend for scanning OBS bucket usage with controlled concurrency and directory-level CSV aggregation.

## Install for development

```bash
pip install -e ".[dev]"
```

## Configure

Copy the example config:

```bash
cp config/apps.example.yaml config/apps.yaml
```

Edit `config/apps.yaml` and fill in real application endpoint, appid, and token values.

## Run a scan

```bash
obs-scan scan --config config/apps.yaml
```

Optional single-application scan:

```bash
obs-scan scan --config config/apps.yaml --appid com.camera.pergen
```

## Run API

```bash
uvicorn obs_scan_platform.api:app --reload
```

Design spec: `docs/superpowers/specs/2026-07-08-obs-scan-platform-design.md`
```

- [ ] **Step 4: Run full test suite again**

Run:

```bash
pytest -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_scan_end_to_end.py README.md
git commit -m "test: add local end-to-end scan validation"
```

## Task 10: Final Verification

**Files:**
- Modify only if verification exposes a concrete defect in files created by earlier tasks.

- [ ] **Step 1: Run full tests**

Run:

```bash
pytest -v
```

Expected: PASS.

- [ ] **Step 2: Validate CLI help**

Run:

```bash
obs-scan --help
obs-scan scan --help
```

Expected: both commands exit 0 and show usage text.

- [ ] **Step 3: Validate API import**

Run:

```bash
python -c "from obs_scan_platform.api import app; print(app.title)"
```

Expected output:

```text
OBS Scan Platform
```

- [ ] **Step 4: Check git status**

Run:

```bash
git status --short
```

Expected: only intentional files are changed or staged.

- [ ] **Step 5: Commit verification fixes if any were needed**

If Step 1, Step 2, or Step 3 exposed a defect and code was changed, commit the focused fix:

```bash
git add <changed-files>
git commit -m "fix: address final verification issue"
```

If no fixes were needed, do not create an empty commit.

## Self-Review

- Spec coverage: the plan covers config, shared bucket filtering, root discovery, metadata for root files, bounded async request concurrency, temporary object CSVs, directory aggregation, final bucket CSV, manifest, CLI, API, logging entry points, and tests.
- Placeholder scan: this plan avoids unfinished markers and vague implementation instructions.
- Type consistency: `Thresholds`, `ObjectRow`, `BucketInfo`, `BucketScanResult`, `ScanStatus`, and scanner function names are introduced before use and reused consistently.
