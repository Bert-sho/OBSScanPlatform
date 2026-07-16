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


def test_scan_settings_new_concurrency_defaults(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan: {}
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.global_request_concurrency == 150
    assert config.scan.objectkeys_concurrency_limit() == 30
    assert config.scan.max_retries == 3
    assert config.scan.metadata_task_limit_per_bucket == 10000


def test_load_config_sets_metadata_task_limit_per_bucket(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  metadata_task_limit_per_bucket: 4321
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.metadata_task_limit_per_bucket == 4321


def test_legacy_app_concurrency_is_ignored_and_omitted_from_modeled_config(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  app_concurrency: 1
  bucket_concurrency: 3
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.bucket_concurrency == 3
    assert not hasattr(config.scan, "app_concurrency")
    assert "app_concurrency" not in config.masked_dict()["scan"]


def test_legacy_per_bucket_prefix_concurrency_still_sets_objectkeys_limit(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  per_bucket_prefix_concurrency: 7
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.objectkeys_concurrency_limit() == 7


def test_new_objectkeys_concurrency_field_wins_over_legacy_field(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  per_bucket_prefix_concurrency: 7
  objectkeys_concurrency_per_bucket: 13
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.objectkeys_concurrency_limit() == 13


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
    assert masked["defaults"]["filelist_depth"] == 5
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


def test_aggregation_directory_limit_defaults_to_100000(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan: {}
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.aggregation_max_directories_in_memory == 100000


def test_aggregation_directory_limit_loads_explicit_value(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  aggregation_max_directories_in_memory: 4321
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.aggregation_max_directories_in_memory == 4321


def test_aggregation_directory_limit_rejects_zero(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
scan:
  aggregation_max_directories_in_memory: 0
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 180
applications:
  - appid: app.one
    name: App One
    apptoken: replace-with-test-token
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_file)
