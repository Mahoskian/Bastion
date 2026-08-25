"""The shared consoles, and the one way this CLI reports a failure."""

from __future__ import annotations

from typing import NoReturn

import typer
from rich.console import Console

console = Console()
err = Console(stderr=True)


def fail(message: str) -> NoReturn:
    """Print an error and exit non-zero. Never returns."""
    err.print(f"[bold red]error[/] {message}")
    raise typer.Exit(1)


def rows(pairs: dict[str, str], *, key_style: str = "dim") -> None:
    """A borderless two-column block -- the house style for key/value output."""
    from rich.table import Table

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style=key_style)
    table.add_column()
    for key, value in pairs.items():
        table.add_row(key, value)
    console.print(table)
