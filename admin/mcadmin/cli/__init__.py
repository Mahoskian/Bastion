"""The `mc` command line.

Every module here is a thin wrapper: parse arguments, call into `core`, hand
the result to `ui`. Logic that is not about argument parsing belongs in `core`.
"""

from __future__ import annotations

from typing import Any

import typer
from typer.core import TyperGroup
from typer.main import get_command_name

from . import (
    chunks,
    deaths,
    dev,
    gc,
    listen,
    logs,
    metrics,
    mods,
    mrpack,
    notify,
    server,
    slow,
    snapshot,
    stats,
)

# How `mc --help` reads: which panel each command belongs to, and the order the
# panels and their contents appear in. One table, because arrangement is one
# decision -- and left to itself typer orders the root help by registration and
# always puts every sub-command group after every flat command, whatever the
# two are about, which is how `snapshot` ended up below `test`.
#
# The grouping lives here rather than in the command names: `mc start` is typed
# far too often to become `mc server start`, so the panels do the sorting a
# name hierarchy would otherwise have to.
LAYOUT: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Server", ("status", "start", "stop", "restart", "console", "rcon", "notify", "listen")),
    ("Snapshots", ("snapshot",)),
    ("Performance and health", ("logs", "metrics", "gc", "why-slow", "chunks")),
    ("For the players", ("wrapped", "deaths")),
    ("Mods and client packs", ("mods", "mrpack")),
    ("Development", ("test",)),
)

PANELS = {name: panel for panel, names in LAYOUT for name in names}
ORDER = [name for _, names in LAYOUT for name in names]


class Layout(TyperGroup):
    """Lists the root's commands in LAYOUT's order, not in registration order."""

    # `ctx` is a click Context, but typer vendors its own copy of click under a
    # private name, so there is nothing public to annotate it with.
    def list_commands(self, ctx: Any) -> list[str]:
        placed = [name for name in ORDER if name in self.commands]
        return placed + [name for name in self.commands if name not in PANELS]


app = typer.Typer(
    cls=Layout,
    no_args_is_help=True,
    add_completion=False,
    help="Admin utilities for the Minecraft server.",
)

app.add_typer(snapshot.app, name="snapshot")
app.add_typer(logs.app, name="logs")
app.add_typer(metrics.app, name="metrics")
app.add_typer(mods.app, name="mods")
app.add_typer(notify.app, name="notify")
app.add_typer(listen.app, name="listen")
for module in (server, gc, mrpack, stats, deaths, chunks, slow, dev):
    app.registered_commands.extend(module.app.registered_commands)


def apply_layout(target: typer.Typer) -> None:
    """Put every command in the panel LAYOUT names for it.

    A command LAYOUT does not mention keeps typer's default panel, which prints
    *above* the named ones -- so a new command nobody placed is the first thing
    in the help rather than quietly buried at the bottom of it.
    """
    for command in target.registered_commands:
        name = command.name or get_command_name(command.callback.__name__)
        command.rich_help_panel = PANELS.get(name)
    for group in target.registered_groups:
        group.rich_help_panel = PANELS.get(group.name or "")


apply_layout(app)

__all__ = ["app"]
