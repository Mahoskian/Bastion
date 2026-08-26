"""Rendering the Discord listener's state."""

from __future__ import annotations

from collections.abc import Sequence

from ..core.listener import ListenerState
from ..core.tmux import TmuxSession
from .console import console, rows
from .format import human_age


def show_status(
    state: ListenerState | None, session: TmuxSession, commands: Sequence[str]
) -> None:
    if state is None:
        console.print("The listener is [bold red]not running[/].")
        if session.exists():
            console.print(
                f"[yellow]A tmux session named {session.name!r} is open but holds no "
                "listener.[/]"
            )
            console.print("[dim]Clear it with: mc listen stop --force[/]")
        else:
            console.print("[dim]Start it with: mc listen start[/]")
        return

    # Started and connected are different states: the process can be up while
    # the gateway handshake has not finished, or has dropped and is resuming.
    if state.connected:
        console.print(f"Listening as [bold green]{state.bot}[/].")
    else:
        console.print("The listener is [bold yellow]running but not connected yet[/].")

    facts = {
        "pid": str(state.pid),
        "session": session.name,
        "since": f"{state.started_at:%Y-%m-%d %H:%M} ({human_age(state.started_at)})",
    }
    if state.guilds:
        facts["servers"] = ", ".join(state.guilds)
    console.print()
    rows(facts)
    console.print()
    rows({"commands": " ".join(f"/{name}" for name in commands)})
