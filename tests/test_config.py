from pathlib import Path

import pytest
from pydantic import ValidationError

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
endpoint: http://obs.example
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: secret-token
    enabled: true
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    masked = config.masked_dict()

    assert masked["endpoint"] == "http://obs.example"
    assert masked["applications"][0]["apptoken"] == "******"


def test_load_config_uses_top_level_endpoint_and_new_defaults(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  filelist_task_limit_per_bucket: 77
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
  filelist_depth: 5
applications:
  - appid: app.one
    name: App One
    apptoken: secret-token
    enabled: true
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    application = config.enabled_applications()[0]
    settings = config.thresholds_for(application, "missing-bucket")

    assert config.endpoint_for(application) == "http://obs.global"
    assert config.scan.filelist_task_limit_per_bucket == 77
    assert application.scan_shared_buckets is False
    assert settings.filelist_depth == 5


def test_load_config_falls_back_to_application_endpoint(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    endpoint: http://obs.app
    apptoken: secret-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    application = config.enabled_applications()[0]

    assert config.endpoint_for(application) == "http://obs.app"
    assert config.thresholds_for(application, "missing-bucket").filelist_depth == 5


def test_load_config_requires_some_endpoint(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: secret-token
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="endpoint"):
        load_config(config_file)


def test_bucket_override_can_set_only_filelist_depth(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
  filelist_depth: 5
applications:
  - appid: app.one
    name: App One
    apptoken: secret-token
    scan_shared_buckets: true
    buckets:
      bucket-a:
        filelist_depth: 8
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    application = config.enabled_applications()[0]
    settings = config.thresholds_for(application, "bucket-a")

    assert application.scan_shared_buckets is True
    assert settings.large_directory_bytes == 100
    assert settings.large_file_bytes == 10
    assert settings.inactive_directory_days == 180
    assert settings.filelist_depth == 8
