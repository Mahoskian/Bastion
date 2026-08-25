"""The `mc` command line.

Every module here is a thin wrapper: parse arguments, call into `core`, hand
the result to `ui`. Logic that is not about argument parsing belongs in `core`.
"""

from __future__ import annotations

import typer

from . import dev, gc, logs, metrics, mods, mrpack, server, snapshot

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Admin utilities for the Minecraft server.",
)

app.add_typer(snapshot.app, name="snapshot")
app.add_typer(logs.app, name="logs")
app.add_typer(metrics.app, name="metrics")
app.add_typer(mods.app, name="mods")
for module in (server, gc, mrpack, dev):
    app.registered_commands.extend(module.app.registered_commands)

__all__ = ["app"]
