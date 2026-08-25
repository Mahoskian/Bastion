"""Rendering server state."""

from __future__ import annotations

from rich.panel import Panel

from ..core.controller import Status
from ..core.models import ServerState
from ..core.properties import ServerProperties
from .console import console, rows

STATE_STYLE = {
    ServerState.RUNNING: "bold green",
    ServerState.BOOTING: "bold yellow",
    ServerState.ORPHANED: "bold yellow",
    ServerState.STOPPED: "bold red",
}


def show_status(status: Status, props: ServerProperties) -> None:
    style = STATE_STYLE[status.state]
    line = f"Server is [{style}]{status.state.description}[/]"
    if status.supervised:
        line += "  [dim](supervised)[/]"
    console.print(Panel(line, expand=False))

    facts: dict[str, str] = {}
    if status.pid:
        facts["pid"] = str(status.pid)
    if status.runtime is not None:
        runtime = status.runtime
        facts["session"] = runtime.session
        facts["heap"] = runtime.heap
        facts["since"] = f"{runtime.started_at:%Y-%m-%d %H:%M:%S}"
        if runtime.restarts:
            facts["restarts"] = str(runtime.restarts)
    elif status.session_exists:
        facts["session"] = "open, but no supervisor in it"
    if facts:
        rows(facts)
        console.print()

    rows(props.notable())
    if status.players:
        console.print()
        rows({"players": status.players})
