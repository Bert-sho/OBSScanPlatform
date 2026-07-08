import typer

app = typer.Typer(help="OBS scan platform command line tools.")


@app.command()
def scan() -> None:
    """Run an OBS scan."""
    typer.echo("scan command is not wired yet")


def main() -> None:
    app()
