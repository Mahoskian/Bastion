"""Rendering the notification config."""

from __future__ import annotations

from ..core.board import PinState
from ..core.notify import DiscordConfig
from .console import console, rows


def show_config(config: DiscordConfig | None, source: str, pin: PinState | None = None) -> None:
    if config is None:
        console.print("Discord notifications are [bold yellow]not configured[/].")
        console.print("[dim]Set them up with: mc notify setup[/]")
        return

    state = "[bold green]on[/]" if config.enabled else "[bold yellow]off[/] (enabled: false)"
    console.print(f"Discord notifications are {state}.")
    console.print()
    # A pin in a channel the config has since moved off is stale rather than
    # wrong -- the next update posts a fresh message there -- but it is worth
    # seeing, because until then the old channel still shows a live-looking board.
    board = "not posted yet"
    if pin is not None:
        board = pin.message_id
        if pin.channel_id != config.channel_id:
            board += f" [yellow](in channel {pin.channel_id})[/]"
    rows(
        {
            "channel": config.channel_id,
            "token": config.redacted_token,
            "status board": board,
            "config": source,
        }
    )
