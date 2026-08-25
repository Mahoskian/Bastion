"""Rendering leaderboards and a single player's wrapped card."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from ..core.stats import (
    DAMAGE_PER_HEART,
    Board,
    PlayerStats,
    Roster,
    Unit,
    short,
    titles,
)
from ..core.units import human_distance, human_seconds
from .console import console
from .format import human_age

MEDALS = ("[yellow]1[/]", "[white]2[/]", "[#b87333]3[/]")
TRAVEL_PREVIEW = 5


def _value(value: float, unit: Unit) -> str:
    if unit is Unit.DURATION:
        return human_seconds(value)
    if unit is Unit.DISTANCE:
        return human_distance(value)
    if unit is Unit.HEALTH:
        return f"{value / DAMAGE_PER_HEART:,.0f} hearts"
    return f"{value:,.0f}"


def _rank(rank: int) -> str:
    return MEDALS[rank - 1] if rank <= len(MEDALS) else f"[dim]{rank}[/]"


def _freshness(roster: Roster) -> str:
    """Stats are only as current as the last time a player was saved off."""
    updated = roster.updated
    if updated is None:
        return ""
    return f"  [dim]as of {updated:%Y-%m-%d %H:%M} ({human_age(updated)})[/]"


def _empty(roster: Roster) -> bool:
    if roster.players:
        return False
    console.print(
        f"No player stats in [bold]{roster.directory}[/].\n"
        "[dim]The server writes these when it saves a player off, so they appear "
        "once someone has played and been saved.[/]"
    )
    return True


def show_boards(roster: Roster, tables: list[Board], top: int) -> None:
    if _empty(roster):
        return
    console.print(
        Panel(
            f"[bold]{len(roster.players)} player(s)[/]{_freshness(roster)}",
            expand=False,
        )
    )
    for board in tables:
        if not board.standings:
            continue
        heading = f"\n[bold]{board.title}[/]"
        if board.note:
            heading += f" [dim]({board.note})[/]"
        console.print(heading)
        table = Table(box=None, pad_edge=False, show_header=False)
        table.add_column(width=2, justify="right")
        table.add_column(min_width=14)
        table.add_column(justify="right")
        table.add_column(style="dim")
        for standing in board.standings[:top]:
            table.add_row(
                _rank(standing.rank),
                standing.player,
                _value(standing.value, board.unit),
                standing.detail,
            )
        console.print(table)


def _pairs(caption: str, entries: list[tuple[str, int]]) -> None:
    if not entries:
        return
    console.print(f"\n[bold]{caption}[/]")
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(min_width=22)
    table.add_column(justify="right")
    for key, count in entries:
        table.add_row(short(key), f"{count:,}")
    console.print(table)


def show_wrapped(roster: Roster, player: PlayerStats) -> None:
    """One player's year in review -- or week, on a server this young."""
    won = titles(roster, player)
    console.print(
        Panel(
            f"[bold]{player.name}[/]{_freshness(roster)}",
            expand=False,
        )
    )

    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(style="dim", min_width=18)
    table.add_column()
    survived = human_seconds(player.since_death_seconds)
    table.add_row("playtime", human_seconds(player.play_seconds))
    table.add_row("blocks mined", f"{player.blocks_mined:,}")
    table.add_row("items crafted", f"{player.items_crafted:,}")
    table.add_row("mobs killed", f"{player.mob_kills:,}")
    table.add_row(
        "deaths",
        f"{player.deaths:,}  [dim]{player.deaths_per_hour:.1f}/hour"
        f", {survived} since the last one[/]",
    )
    table.add_row("distance", human_distance(player.distance_cm))
    table.add_row("damage taken", _value(player.damage_taken, Unit.HEALTH))
    table.add_row("advancements", f"{player.advancements:,}  [dim]vanilla only[/]")
    if player.player_kills:
        table.add_row("player kills", f"{player.player_kills:,}")
    console.print(table)

    if won:
        console.print(f"\n[bold yellow]Tops the server in[/] {', '.join(won).lower()}")

    nemesis = player.nemesis
    if nemesis is not None:
        console.print(
            f"\n[bold]Nemesis[/]  {short(nemesis[0])} [dim]killed them "
            f"{nemesis[1]} time(s)[/]"
        )

    travel = player.travel()
    if travel:
        console.print("\n[bold]How they got around[/]")
        table = Table(box=None, pad_edge=False, show_header=False)
        table.add_column(min_width=22)
        table.add_column(justify="right")
        table.add_column(justify="right", style="dim")
        total = sum(travel.values()) or 1
        for mode, centimetres in list(travel.items())[:TRAVEL_PREVIEW]:
            table.add_row(
                mode.replace("_", " "),
                human_distance(centimetres),
                f"{100 * centimetres / total:.0f}%",
            )
        console.print(table)

    _pairs("Most mined", player.top("minecraft:mined"))
    _pairs("Most killed", player.top("minecraft:killed"))
    _pairs("Most crafted", player.top("minecraft:crafted"))
