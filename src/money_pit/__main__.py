"""Command-line interface."""

import typer


app: typer.Typer = typer.Typer()


@app.command(name="money-pit")
def main() -> None:
    """Money Pit."""


if __name__ == "__main__":
    app()  # pragma: no cover
