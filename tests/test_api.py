import json
import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import obs_scan_platform.api as api


def _write_config(path: Path) -> None:
    path.write_text(
        """
defaults:
  large_directory_bytes: 100
  large_file_bytes: 10
  inactive_directory_days: 30
applications:
  - appid: app-1
    name: Demo App
    endpoint: https://example.com
    apptoken: secret-token
""",
        encoding="utf-8",
    )


def test_health():
    client = TestClient(api.create_app())
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
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get("/runs")

    assert response.status_code == 200
    assert response.json()[0]["run_id"] == "run-1"


def test_runs_list_ignores_symlinked_external_run(tmp_path: Path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    outside_dir = tmp_path / "outside-run"
    outside_dir.mkdir()
    (outside_dir / "manifest.json").write_text(
        json.dumps({"run_id": "outside-run", "status": "success", "applications": []}),
        encoding="utf-8",
    )
    (results_dir / "linked-run").symlink_to(outside_dir, target_is_directory=True)
    client = TestClient(api.create_app(results_dir=results_dir))

    response = client.get("/runs")

    assert response.status_code == 200
    assert response.json() == []
    assert "outside-run" not in response.text


def test_runs_list_ignores_symlinked_external_manifest(tmp_path: Path):
    results_dir = tmp_path / "results"
    run_dir = results_dir / "run-1"
    run_dir.mkdir(parents=True)
    outside_manifest = tmp_path / "outside_manifest.json"
    outside_manifest.write_text(
        json.dumps({"run_id": "outside-run", "secret": "external-json"}),
        encoding="utf-8",
    )
    (run_dir / "manifest.json").symlink_to(outside_manifest)
    client = TestClient(api.create_app(results_dir=results_dir))

    response = client.get("/runs")

    assert response.status_code == 200
    assert response.json() == []
    assert "outside-run" not in response.text
    assert "external-json" not in response.text


def test_post_runs_requires_config_path(tmp_path: Path):
    client = TestClient(api.create_app(results_dir=tmp_path))
    response = client.post("/runs")
    assert response.status_code == 404


def test_config_apps_masks_tokens(tmp_path: Path):
    config_path = tmp_path / "apps.yaml"
    _write_config(config_path)
    client = TestClient(api.create_app(config_path, tmp_path))

    response = client.get("/config/apps")

    assert response.status_code == 200
    assert response.json()["applications"][0]["apptoken"] == "******"
    assert "secret-token" not in response.text


def test_config_apps_normalizes_legacy_max_depth_to_aggregation_depth(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text("scan:\n  max_depth: 3\n", encoding="utf-8")
    client = TestClient(api.create_app(config_path=config_file, results_dir=tmp_path / "results"))

    response = client.get("/config/apps")

    assert response.status_code == 200
    assert response.json()["scan"]["aggregation_depth"] == 3
    assert "max_depth" not in response.json()["scan"]


def test_module_app_reads_config_from_environment(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "apps.yaml"
    _write_config(config_path)
    monkeypatch.setenv("OBS_SCAN_CONFIG", str(config_path))

    reloaded_api = importlib.reload(api)
    try:
        response = TestClient(reloaded_api.app).get("/config/apps")
        assert response.status_code == 200
        assert response.json()["applications"][0]["appid"] == "app-1"
    finally:
        monkeypatch.delenv("OBS_SCAN_CONFIG", raising=False)
        importlib.reload(api)


def test_run_detail_reads_manifest(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    manifest = {"run_id": "run-1", "status": "success", "applications": []}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get("/runs/run-1")

    assert response.status_code == 200
    assert response.json() == manifest


def test_run_logs_reads_scan_log(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "scan.log").write_text("scan output\n", encoding="utf-8")
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get("/runs/run-1/logs")

    assert response.status_code == 200
    assert response.text == "scan output\n"


def test_bucket_csv_downloads_file(tmp_path: Path):
    csv_dir = tmp_path / "run-1" / "app-1"
    csv_dir.mkdir(parents=True)
    (csv_dir / "bucket-1.csv").write_text("name,size\nfile.txt,1\n", encoding="utf-8")
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get("/runs/run-1/apps/app-1/buckets/bucket-1/csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text == "name,size\nfile.txt,1\n"


def _write_parquet_manifest(
    results_dir: Path,
    *,
    overview_format: str = "parquet",
    listed_parts: list[Path] | None = None,
) -> Path:
    run_dir = results_dir / "run-1"
    bucket_dir = run_dir / "app-1" / "bucket-a"
    bucket_dir.mkdir(parents=True, exist_ok=True)
    if listed_parts is None:
        listed_parts = [bucket_dir / "part-00001.parquet"]
    manifest = {
        "run_id": "run-1",
        "applications": [
            {
                "appid": "app-1",
                "buckets": [
                    {
                        "bucket_name": "bucket-a",
                        "overview_format": overview_format,
                        "overview_path": str(bucket_dir),
                        "overview_files": [str(path) for path in listed_parts],
                    }
                ],
            }
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bucket_dir


def test_bucket_parquet_downloads_manifest_listed_part(tmp_path: Path):
    bucket_dir = _write_parquet_manifest(tmp_path)
    (bucket_dir / "part-00001.parquet").write_bytes(b"parquet-data")
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get(
        "/runs/run-1/apps/app-1/buckets/bucket-a/parquet/part-00001.parquet"
    )

    assert response.status_code == 200
    assert response.content == b"parquet-data"
    assert response.headers["content-type"] == "application/vnd.apache.parquet"


@pytest.mark.parametrize("part_name", ["part-1.parquet", "part-00001.csv", "part-00002.parquet"])
def test_bucket_parquet_rejects_invalid_or_unlisted_part(tmp_path: Path, part_name: str):
    bucket_dir = _write_parquet_manifest(tmp_path)
    (bucket_dir / "part-00001.parquet").write_bytes(b"listed")
    (bucket_dir / "part-00002.parquet").write_bytes(b"unlisted")
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get(
        f"/runs/run-1/apps/app-1/buckets/bucket-a/parquet/{part_name}"
    )

    assert response.status_code == 404
    assert b"unlisted" not in response.content


def test_bucket_parquet_rejects_csv_overview_manifest(tmp_path: Path):
    bucket_dir = _write_parquet_manifest(tmp_path, overview_format="csv")
    (bucket_dir / "part-00001.parquet").write_bytes(b"parquet-data")
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get(
        "/runs/run-1/apps/app-1/buckets/bucket-a/parquet/part-00001.parquet"
    )

    assert response.status_code == 404


def test_bucket_parquet_rejects_missing_listed_file(tmp_path: Path):
    _write_parquet_manifest(tmp_path)
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get(
        "/runs/run-1/apps/app-1/buckets/bucket-a/parquet/part-00001.parquet"
    )

    assert response.status_code == 404


def test_bucket_parquet_rejects_path_traversal(tmp_path: Path):
    results_dir = tmp_path / "results"
    bucket_dir = _write_parquet_manifest(results_dir)
    (bucket_dir / "part-00001.parquet").write_bytes(b"listed")
    (results_dir / "run-1" / "app-1" / "external.parquet").write_bytes(b"external")
    client = TestClient(api.create_app(results_dir=results_dir))

    response = client.get(
        "/runs/run-1/apps/app-1/buckets/bucket-a/parquet/%2E%2E%2Fexternal.parquet"
    )

    assert response.status_code in {400, 404}
    assert b"external" not in response.content


def test_bucket_parquet_rejects_symlinked_part(tmp_path: Path):
    bucket_dir = _write_parquet_manifest(tmp_path)
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"external")
    try:
        (bucket_dir / "part-00001.parquet").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get(
        "/runs/run-1/apps/app-1/buckets/bucket-a/parquet/part-00001.parquet"
    )

    assert response.status_code == 404
    assert b"external" not in response.content


def test_run_detail_rejects_path_traversal(tmp_path: Path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "manifest.json").write_text('{"secret": true}', encoding="utf-8")
    (tmp_path / "manifest.json").write_text('{"secret": "parent"}', encoding="utf-8")
    client = TestClient(api.create_app(results_dir=results_dir))

    response = client.get("/runs/%2E%2E%2Foutside")
    dotdot_response = client.get("/runs/%2E%2E")

    assert response.status_code in {400, 404}
    assert "secret" not in response.text
    assert dotdot_response.status_code in {400, 404}
    assert "parent" not in dotdot_response.text


def test_run_detail_rejects_backslash_segment(tmp_path: Path):
    unsafe_dir = tmp_path / "bad\\run"
    unsafe_dir.mkdir()
    (unsafe_dir / "manifest.json").write_text('{"secret": "backslash"}', encoding="utf-8")
    client = TestClient(api.create_app(results_dir=tmp_path))

    response = client.get("/runs/bad%5Crun")

    assert response.status_code in {400, 404}
    assert "backslash" not in response.text


def test_run_logs_rejects_path_traversal(tmp_path: Path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "scan.log").write_text("external log", encoding="utf-8")
    (tmp_path / "scan.log").write_text("parent log", encoding="utf-8")
    client = TestClient(api.create_app(results_dir=results_dir))

    response = client.get("/runs/%2E%2E%2Foutside/logs")
    dotdot_response = client.get("/runs/%2E%2E/logs")

    assert response.status_code in {400, 404}
    assert "external log" not in response.text
    assert dotdot_response.status_code in {400, 404}
    assert "parent log" not in dotdot_response.text


def test_bucket_csv_rejects_path_traversal(tmp_path: Path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "bucket-1.csv").write_text("external csv", encoding="utf-8")
    client = TestClient(api.create_app(results_dir=results_dir))

    response = client.get("/runs/%2E%2E/apps/outside/buckets/bucket-1/csv")

    assert response.status_code in {400, 404}
    assert "external csv" not in response.text


def test_bucket_csv_rejects_appid_path_traversal(tmp_path: Path):
    results_dir = tmp_path / "results"
    (results_dir / "run-1").mkdir(parents=True)
    (results_dir / "bucket-1.csv").write_text("external app csv", encoding="utf-8")
    client = TestClient(api.create_app(results_dir=results_dir))

    response = client.get("/runs/run-1/apps/%2E%2E/buckets/bucket-1/csv")

    assert response.status_code in {400, 404}
    assert "external app csv" not in response.text


def test_bucket_csv_rejects_bucket_name_path_traversal(tmp_path: Path):
    results_dir = tmp_path / "results"
    app_dir = results_dir / "run-1" / "app-1"
    app_dir.mkdir(parents=True)
    (app_dir / "...csv").write_text("dotdot bucket csv", encoding="utf-8")
    (results_dir / "run-1" / "external.csv").write_text(
        "external bucket csv",
        encoding="utf-8",
    )
    client = TestClient(api.create_app(results_dir=results_dir))

    dotdot_response = client.get("/runs/run-1/apps/app-1/buckets/%2E%2E/csv")
    slash_response = client.get("/runs/run-1/apps/app-1/buckets/%2E%2E%2Fexternal/csv")

    assert dotdot_response.status_code in {400, 404}
    assert "dotdot bucket csv" not in dotdot_response.text
    assert slash_response.status_code in {400, 404}
    assert "external bucket csv" not in slash_response.text


def test_post_runs_accepts_scan_and_resets_active_scan(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "apps.yaml"
    _write_config(config_path)
    called_with = []

    async def fake_run_scan(path, *, show_progress=False):
        called_with.append((path, show_progress))

    monkeypatch.setattr(api, "run_scan", fake_run_scan)
    client = TestClient(api.create_app(config_path=config_path, results_dir=tmp_path))

    response = client.post("/runs")

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert called_with == [(config_path, False)]
    assert client.app.state.active_scan is False


def test_post_runs_returns_conflict_when_scan_active(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "apps.yaml"
    _write_config(config_path)

    async def fake_run_scan(path, *, show_progress=False):
        raise AssertionError("run_scan should not be called")

    monkeypatch.setattr(api, "run_scan", fake_run_scan)
    client = TestClient(api.create_app(config_path=config_path, results_dir=tmp_path))
    client.app.state.active_scan = True

    response = client.post("/runs")

    assert response.status_code == 409


def test_post_runs_resets_active_scan_after_failure(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "apps.yaml"
    _write_config(config_path)

    async def fake_run_scan(path, *, show_progress=False):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(api, "run_scan", fake_run_scan)
    client = TestClient(
        api.create_app(config_path=config_path, results_dir=tmp_path),
        raise_server_exceptions=False,
    )

    response = client.post("/runs")

    assert response.status_code == 202
    assert client.app.state.active_scan is False
