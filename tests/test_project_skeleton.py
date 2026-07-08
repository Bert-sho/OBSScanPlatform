from typer.testing import CliRunner

from obs_scan_platform import __version__
from obs_scan_platform.cli import app


def test_package_has_version():
    assert __version__ == "0.1.0"


def test_cli_help_loads():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "scan" in result.output
