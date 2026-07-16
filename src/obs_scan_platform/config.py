from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class ScanSettings(BaseModel):
    results_dir: str = "results"
    temp_subdir: str = "_tmp"
    keep_temp_files: bool = False
    page_size: int = 1000
    bucket_concurrency: int = 4
    global_request_concurrency: int = 150
    per_bucket_prefix_concurrency: int | None = None
    objectkeys_concurrency_per_bucket: int | None = None
    metadata_concurrency_per_bucket: int = 8
    request_timeout_seconds: int = 30
    max_retries: int = 3
    retry_base_delay_seconds: float = 2
    retry_max_delay_seconds: float = 60
    filelist_task_limit_per_bucket: int = 100
    metadata_task_limit_per_bucket: int = 10000

    def objectkeys_concurrency_limit(self) -> int:
        if self.objectkeys_concurrency_per_bucket is not None:
            return self.objectkeys_concurrency_per_bucket
        if self.per_bucket_prefix_concurrency is not None:
            return self.per_bucket_prefix_concurrency
        return 30


class Thresholds(BaseModel):
    large_directory_bytes: int
    large_file_bytes: int
    inactive_directory_days: int
    filelist_depth: int = 5


class BucketOverrides(BaseModel):
    large_directory_bytes: int | None = None
    large_file_bytes: int | None = None
    inactive_directory_days: int | None = None
    filelist_depth: int | None = None


class ApplicationConfig(BaseModel):
    appid: str
    name: str
    endpoint: str | None = None
    apptoken: str
    enabled: bool = True
    scan_shared_buckets: bool = False
    buckets: dict[str, BucketOverrides] = Field(default_factory=dict)


class AppConfigFile(BaseModel):
    endpoint: str | None = None
    scan: ScanSettings = Field(default_factory=ScanSettings)
    defaults: Thresholds
    applications: list[ApplicationConfig]
    source_path: Path | None = None

    @model_validator(mode="after")
    def validate_endpoint_config(self) -> "AppConfigFile":
        if self.endpoint is not None:
            return self
        missing = [application.appid for application in self.applications if application.endpoint is None]
        if missing:
            raise ValueError("endpoint must be configured globally or for each application")
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
        values.update(override.model_dump(exclude_none=True))
        return Thresholds.model_validate(values)

    def endpoint_for(self, application: ApplicationConfig) -> str:
        endpoint = self.endpoint or application.endpoint
        if endpoint is None:
            raise ValueError("endpoint must be configured globally or for the application")
        return endpoint

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
