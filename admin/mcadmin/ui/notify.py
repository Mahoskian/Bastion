"""Rendering the notification config."""

from __future__ import annotations

from ..core.notify import DiscordConfig
from .console import console, rows


def show_config(config: DiscordConfig | None, source: str) -> None:
    if config is None:
        console.print("Discord notifications are [bold yellow]not configured[/].")
        console.print("[dim]Set them up with: mc notify setup[/]")
        return

    state = "[bold green]on[/]" if config.enabled else "[bold yellow]off[/] (enabled: false)"
    console.print(f"Discord notifications are {state}.")
    console.print()
    rows(
        {
            "channel": config.channel_id,
            "token": config.redacted_token,
            "config": source,
        }
    )
