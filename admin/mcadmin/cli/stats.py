"""Leaderboards and per-player wrapped cards, from the world's stats files."""

from __future__ import annotations

import json

import typer

from ..core import stats as st
from ..ui import stats as view
from ..ui.console import console, fail

app = typer.Typer()


@app.command()
def wrapped(
    player: str = typer.Argument(None, help="One player's card. Omit for leaderboards."),
    top: int = typer.Option(5, "--top", "-n", help="How many places per board."),
    board: str = typer.Option(None, "--board", help="Only this board, e.g. deaths."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Leaderboards from world stats -- playtime, mining, kills, deaths, distance."""
    roster = st.load()
    if as_json:
        console.print_json(json.dumps(st.as_dict(roster)))
        return

    if player:
        found = roster.find(player)
        if found is None:
            known = ", ".join(p.name for p in roster.players) or "nobody yet"
            fail(f"no stats for {player!r} -- players with stats: {known}")
        view.show_wrapped(roster, found)
        return

    tables = st.boards(roster)
    if board:
        tables = [table for table in tables if table.key == board]
        if not tables:
            keys = ", ".join(definition[0] for definition in st.DEFINITIONS)
            fail(f"no board called {board!r} -- try one of: {keys}")
    view.show_boards(roster, tables, top=top)
