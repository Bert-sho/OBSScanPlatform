from typer.testing import CliRunner

from obs_scan_platform.cli import app


def test_scan_requires_config():
    result = CliRunner().invoke(app, ["scan"])
    assert result.exit_code != 0
    assert "Missing option" in result.output


def test_scan_success_path(monkeypatch):
    calls = {}

    async def fake_run_scan(config, *, run_id=None, appid=None):
        calls["config"] = config
        calls["run_id"] = run_id
        calls["appid"] = appid
        return {"status": "success", "run_id": "run-1"}

    monkeypatch.setattr("obs_scan_platform.cli.run_scan", fake_run_scan)

    result = CliRunner().invoke(
        app,
        [
            "scan",
            "--config",
            "config/apps.yaml",
            "--appid",
            "app.one",
            "--run-id",
            "run-1",
        ],
    )

    assert result.exit_code == 0
    assert "scan finished: success run_id=run-1" in result.output
    assert str(calls["config"]).endswith("config/apps.yaml")
    assert calls["appid"] == "app.one"
    assert calls["run_id"] == "run-1"


def test_scan_non_success_path_exits_one(monkeypatch):
    async def fake_run_scan(config, *, run_id=None, appid=None):
        return {"status": "partial_failed", "run_id": "run-2"}

    monkeypatch.setattr("obs_scan_platform.cli.run_scan", fake_run_scan)

    result = CliRunner().invoke(
        app,
        ["scan", "--config", "config/apps.yaml", "--run-id", "run-2"],
    )

    assert result.exit_code == 1
    assert "scan finished: partial_failed run_id=run-2" in result.output
