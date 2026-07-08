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
