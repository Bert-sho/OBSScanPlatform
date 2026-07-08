import asyncio
from pathlib import Path

import typer

from obs_scan_platform.scanner import run_scan

app = typer.Typer(help="OBS scan platform command line tools.")


@app.callback()
def root() -> None:
    """OBS scan platform command line tools."""


@app.command()
def scan(
    config: Path = typer.Option(..., "--config", "-c", help="Path to config/apps.yaml"),
    appid: str | None = typer.Option(None, "--appid", help="Scan only one application id"),
    run_id: str | None = typer.Option(None, "--run-id", help="Use a fixed run id"),
) -> None:
    """Run an OBS scan."""
    manifest = asyncio.run(run_scan(config, run_id=run_id, appid=appid))
    typer.echo(f"scan finished: {manifest['status']} run_id={manifest['run_id']}")
    if manifest["status"] != "success":
        raise typer.Exit(code=1)


def main() -> None:
    app()
