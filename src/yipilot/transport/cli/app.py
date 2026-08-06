import json

import typer

from yipilot import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False, invoke_without_command=True)


@app.callback()
def main(
    version: bool = typer.Option(False, "--version"),
    output: str = typer.Option("human", "--output"),
) -> None:
    if not version:
        return

    if output == "json":
        typer.echo(json.dumps({"name": "yipilot", "version": __version__}, separators=(",", ":")))
    elif output == "human":
        typer.echo(f"yipilot {__version__}")
    else:
        raise typer.BadParameter("must be either 'human' or 'json'", param_hint="--output")

    raise typer.Exit()
