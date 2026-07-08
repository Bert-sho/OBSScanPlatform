from typer.testing import CliRunner

from obs_scan_platform.cli import app


def test_scan_requires_config():
    result = CliRunner().invoke(app, ["scan"])
    assert result.exit_code != 0
    assert "Missing option" in result.output
