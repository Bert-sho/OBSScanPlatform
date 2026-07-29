from pathlib import Path

import pytest
from pydantic import ValidationError

from obs_scan_platform.config import AppConfigFile, load_config


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


def test_empty_config_loads_all_global_defaults(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text("", encoding="utf-8")

    config = load_config(config_file)

    assert config.endpoint == ""
    assert config.applications == []
    assert config.scan.results_dir == "results"
    assert config.scan.objectkeys_concurrency_limit() == 30
    assert config.defaults.large_directory_bytes == 107374182400
    assert config.defaults.large_file_bytes == 10737418240
    assert config.defaults.inactive_directory_days == 180
    assert config.defaults.filelist_depth == 5


def test_partial_application_and_bucket_load_missing_defaults(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
applications:
  - buckets:
      bucket-a: {}
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    application = config.applications[0]

    assert application.appid == ""
    assert application.name == ""
    assert application.endpoint == "http://obs.global"
    assert application.apptoken == ""
    assert application.enabled is True
    assert application.scan_shared_buckets is False
    assert config.bucket_enabled(application, "bucket-a") is True
    assert config.thresholds_for(application, "bucket-a") == config.defaults


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
    assert config.scan.keepalive_expiry_seconds == 5.0
    assert config.scan.metadata_task_limit_per_bucket == 10000


def test_load_config_sets_keepalive_expiry_seconds(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
scan:
  keepalive_expiry_seconds: 2.5
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.keepalive_expiry_seconds == 2.5


@pytest.mark.parametrize("value", [0, -1])
def test_load_config_rejects_non_positive_keepalive_expiry_seconds(tmp_path: Path, value: int):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        f"scan:\n  keepalive_expiry_seconds: {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_file)


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


def test_explicit_application_endpoint_wins_over_global(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
applications:
  - endpoint: http://obs.application
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.applications[0].endpoint == "http://obs.application"
    assert config.endpoint_for(config.applications[0]) == "http://obs.application"


def test_null_application_endpoint_inherits_global(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
applications:
  - endpoint: null
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.applications[0].endpoint == "http://obs.global"


def test_whitespace_application_endpoint_falls_back_to_empty_global_endpoint():
    config = AppConfigFile(endpoint="\t", applications=[{"endpoint": "  "}])

    assert config.endpoint_for(config.applications[0]) == ""


def test_whitespace_global_endpoint_falls_back_to_empty():
    config = AppConfigFile(endpoint="  ", applications=[{}])

    assert config.endpoint_for(config.applications[0]) == ""


def test_null_non_nullable_default_is_rejected(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
defaults:
  large_file_bytes: null
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_file)


def test_missing_scan_fields_reports_only_operational_identity():
    config = AppConfigFile(applications=[{}])
    application = config.applications[0]
    assert config.missing_scan_fields(application) == ["endpoint", "appid", "apptoken"]


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


def test_bucket_override_can_disable_a_bucket(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
applications:
  - buckets:
      bucket-disabled:
        enable: false
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    application = config.applications[0]

    assert config.bucket_enabled(application, "bucket-disabled") is False


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


def test_overview_settings_default_to_parquet_depth_four_and_default_type_map():
    config = AppConfigFile()

    assert config.scan.overview_format == "parquet"
    assert config.scan.max_depth == 4
    assert config.scan.file_type_map["jpg"] == "图片"
    assert config.scan.file_type_map["tar"] == "压缩包"


def test_file_type_map_merges_normalized_yaml_overrides(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        "scan:\n  overview_format: csv\n  max_depth: 0\n"
        "  file_type_map:\n    .JPG: 自定义图片\n    TXT: 文档\n",
        encoding="utf-8",
    )

    scan = load_config(config_file).scan

    assert scan.overview_format == "csv"
    assert scan.max_depth == 0
    assert scan.file_type_map["jpg"] == "自定义图片"
    assert scan.file_type_map["txt"] == "文档"
    assert scan.file_type_map["png"] == "图片"


@pytest.mark.parametrize("value", ["avro", "PARQUET", ""])
def test_overview_format_rejects_unsupported_values(value: str):
    with pytest.raises(ValidationError):
        AppConfigFile(scan={"overview_format": value})


def test_max_depth_rejects_negative_value():
    with pytest.raises(ValidationError):
        AppConfigFile(scan={"max_depth": -1})


@pytest.mark.parametrize("mapping", [{"": "图片"}, {"jpg": ""}, {".": "图片"}])
def test_file_type_map_rejects_empty_normalized_keys_or_values(mapping: dict[str, str]):
    with pytest.raises(ValidationError):
        AppConfigFile(scan={"file_type_map": mapping})
