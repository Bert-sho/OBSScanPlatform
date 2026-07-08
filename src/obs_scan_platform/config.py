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
