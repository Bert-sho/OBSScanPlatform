from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


DEFAULT_FILE_TYPE_MAP = {
    "jpg": "图片",
    "jpeg": "图片",
    "png": "图片",
    "gif": "图片",
    "cr3": "RAW",
    "nef": "RAW",
    "braw": "RAW",
    "mp4": "视频",
    "mov": "视频",
    "avi": "视频",
    "py": "脚本",
    "sh": "脚本",
    "js": "脚本",
    "ts": "脚本",
    "onnx": "模型",
    "ckpt": "模型",
    "safetensors": "模型",
    "pt": "模型",
    "parquet": "Parquet",
    "json": "配置文件",
    "yaml": "配置文件",
    "yml": "配置文件",
    "md": "文档",
    "pdf": "文档",
    "zip": "压缩包",
    "tar": "压缩包",
}


class ScanSettings(BaseModel):
    results_dir: str = "results"
    temp_subdir: str = "_tmp"
    keep_temp_files: bool = False
    overview_format: Literal["csv", "parquet"] = "parquet"
    aggregation_depth: int = Field(default=4, ge=0)
    file_type_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_FILE_TYPE_MAP))
    page_size: int = 1000
    bucket_concurrency: int = 4
    global_request_concurrency: int = 150
    per_bucket_prefix_concurrency: int | None = None
    objectkeys_concurrency_per_bucket: int | None = None
    metadata_concurrency_per_bucket: int = 8
    request_timeout_seconds: int = 30
    keepalive_expiry_seconds: float = Field(default=5.0, gt=0)
    max_retries: int = 3
    retry_base_delay_seconds: float = 2
    retry_max_delay_seconds: float = 60
    filelist_task_limit_per_bucket: int = 100
    metadata_task_limit_per_bucket: int = 10000
    aggregation_max_directories_in_memory: int = Field(default=100000, ge=1)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_max_depth(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        has_canonical = "aggregation_depth" in value
        has_legacy = "max_depth" in value
        if has_canonical and has_legacy:
            raise ValueError("aggregation_depth and max_depth cannot both be configured")
        if not has_legacy:
            return value
        migrated = dict(value)
        migrated["aggregation_depth"] = migrated.pop("max_depth")
        return migrated

    @field_validator("file_type_map", mode="before")
    @classmethod
    def merge_file_type_map(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        merged = dict(DEFAULT_FILE_TYPE_MAP)
        for raw_extension, raw_category in value.items():
            extension = str(raw_extension).strip().lower().removeprefix(".")
            category = raw_category.strip() if isinstance(raw_category, str) else ""
            if not extension or not category:
                raise ValueError("file_type_map keys and values must be non-empty")
            merged[extension] = category
        return merged

    def objectkeys_concurrency_limit(self) -> int:
        if self.objectkeys_concurrency_per_bucket is not None:
            return self.objectkeys_concurrency_per_bucket
        if self.per_bucket_prefix_concurrency is not None:
            return self.per_bucket_prefix_concurrency
        return 30


class Thresholds(BaseModel):
    large_directory_bytes: int = 107374182400
    large_file_bytes: int = 10737418240
    inactive_directory_days: int = 180
    filelist_depth: int = 5


class BucketOverrides(BaseModel):
    enable: bool = True
    large_directory_bytes: int | None = None
    large_file_bytes: int | None = None
    inactive_directory_days: int | None = None
    filelist_depth: int | None = None


class ApplicationConfig(BaseModel):
    appid: str = ""
    name: str = ""
    endpoint: str | None = ""
    apptoken: str = ""
    enabled: bool = True
    scan_shared_buckets: bool = False
    buckets: dict[str, BucketOverrides] = Field(default_factory=dict)


class AppConfigFile(BaseModel):
    endpoint: str | None = ""
    scan: ScanSettings = Field(default_factory=ScanSettings)
    defaults: Thresholds = Field(default_factory=Thresholds)
    applications: list[ApplicationConfig] = Field(default_factory=list)
    source_path: Path | None = None

    @model_validator(mode="after")
    def inherit_global_endpoint(self) -> "AppConfigFile":
        if (self.endpoint or "").strip():
            for application in self.applications:
                if not (application.endpoint or "").strip():
                    application.endpoint = self.endpoint
        return self

    def enabled_applications(self) -> list[ApplicationConfig]:
        return [application for application in self.applications if application.enabled]

    def thresholds_for(self, application: ApplicationConfig, bucket_name: str) -> Thresholds:
        override = application.buckets.get(bucket_name)
        if override is None:
            return self.defaults
        values = {
            "large_directory_bytes": self.defaults.large_directory_bytes,
            "large_file_bytes": self.defaults.large_file_bytes,
            "inactive_directory_days": self.defaults.inactive_directory_days,
            "filelist_depth": self.defaults.filelist_depth,
        }
        values.update(override.model_dump(exclude_none=True, exclude={"enable"}))
        return Thresholds.model_validate(values)

    def endpoint_for(self, application: ApplicationConfig) -> str:
        return (application.endpoint or "").strip() or (self.endpoint or "").strip()

    def missing_scan_fields(self, application: ApplicationConfig) -> list[str]:
        values = {
            "endpoint": self.endpoint_for(application),
            "appid": application.appid,
            "apptoken": application.apptoken,
        }
        return [name for name, value in values.items() if not value.strip()]

    def bucket_enabled(self, application: ApplicationConfig, bucket_name: str) -> bool:
        override = application.buckets.get(bucket_name)
        return override is None or override.enable

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
