"""Rendering the death map: a scatter plot per dimension, then the hot spots."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from ..core.deaths import Death, DeathMap, Hotspot, hotspots, normalise_cause
from .console import console
from .format import human_age

WIDTH = 60
MIN_HEIGHT, MAX_HEIGHT = 3, 18
# Terminal cells are about twice as tall as they are wide, so a square patch of
# world has to be squashed vertically or every map comes out stretched.
CELL_ASPECT = 2.0
# A single death would otherwise define a zero-wide window and divide by zero.
MIN_SPAN = 32.0
LABELS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
RECENT = 10


def _bounds(deaths: list[Death]) -> tuple[float, float, float, float]:
    xs = [death.x for death in deaths]
    zs = [death.z for death in deaths]
    low_x, high_x = min(xs), max(xs)
    low_z, high_z = min(zs), max(zs)
    # Pad a degenerate axis outwards rather than clamping points onto an edge.
    if high_x - low_x < MIN_SPAN:
        middle = (low_x + high_x) / 2
        low_x, high_x = middle - MIN_SPAN / 2, middle + MIN_SPAN / 2
    if high_z - low_z < MIN_SPAN:
        middle = (low_z + high_z) / 2
        low_z, high_z = middle - MIN_SPAN / 2, middle + MIN_SPAN / 2
    return low_x, high_x, low_z, high_z


def scatter(
    deaths: list[Death], letters: dict[Death, str], width: int = WIDTH
) -> tuple[list[str], tuple[float, float, float, float]]:
    """A character grid of the deaths, and the world bounds it covers.

    Cells hold the hot spot's letter when one of its deaths lands there, and
    otherwise how many deaths fell in that cell.
    """
    low_x, high_x, low_z, high_z = _bounds(deaths)
    span_x, span_z = high_x - low_x, high_z - low_z
    # A tall region shrinks the map's width rather than being squashed into the
    # height cap -- a plot whose axes are on different scales is a lie about
    # where things are.
    height = round(width * (span_z / span_x) / CELL_ASPECT)
    if height > MAX_HEIGHT:
        width = max(MIN_HEIGHT, round(width * MAX_HEIGHT / height))
        height = MAX_HEIGHT
    height = max(MIN_HEIGHT, height)

    counts: dict[tuple[int, int], int] = {}
    marks: dict[tuple[int, int], str] = {}
    for death in deaths:
        column = round((death.x - low_x) / span_x * (width - 1))
        row = round((death.z - low_z) / span_z * (height - 1))
        cell = (row, column)
        counts[cell] = counts.get(cell, 0) + 1
        letter = letters.get(death)
        if letter:
            marks[cell] = letter

    grid: list[str] = []
    for row in range(height):
        line = []
        for column in range(width):
            cell = (row, column)
            if cell in marks:
                line.append(f"[bold red]{marks[cell]}[/]")
            elif cell in counts:
                total = counts[cell]
                line.append(f"[yellow]{total if total < 10 else '+'}[/]")
            else:
                line.append("[dim]·[/]")
        grid.append("".join(line))
    return grid, (low_x, high_x, low_z, high_z)


def _plot(name: str, deaths: list[Death], letters: dict[Death, str], width: int) -> None:
    grid, (low_x, high_x, low_z, high_z) = scatter(deaths, letters, width)
    console.print(
        f"\n[bold]{name}[/]  [dim]{len(deaths)} death(s), "
        f"x {low_x:.0f}..{high_x:.0f}, z {low_z:.0f}..{high_z:.0f}, north is up[/]"
    )
    for line in grid:
        console.print(f"  {line}")


def _letter(index: int) -> str:
    return LABELS[index] if index < len(LABELS) else "*"


def _hotspot_table(spots: list[Hotspot]) -> None:
    console.print("\n[bold]Hot spots[/] [dim](deaths within 48 blocks of each other)[/]")
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("", width=1)
    table.add_column("where")
    table.add_column("n", justify="right")
    table.add_column("who")
    table.add_column("mostly", overflow="fold")
    for index, spot in enumerate(spots):
        letter = _letter(index)
        spread = f" [dim]±{spot.span:.0f}[/]" if spot.span >= 8 else ""
        table.add_row(
            f"[bold red]{letter}[/]",
            f"{spot.x}, {spot.y}, {spot.z}  [dim]{spot.dimension}[/]{spread}",
            str(spot.count),
            ", ".join(spot.players),
            normalise_cause(spot.headline) if spot.headline else "[dim]unrecorded[/]",
        )
    console.print(table)


def _tallies(death_map: DeathMap) -> None:
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("player")
    table.add_column("deaths", justify="right")
    table.add_column("most often", overflow="fold")
    for player, count in death_map.by_player.items():
        causes = [
            normalise_cause(death.cause)
            for death in death_map.deaths
            if death.player == player and death.cause
        ]
        common = max(set(causes), key=causes.count) if causes else ""
        table.add_row(player, str(count), common or "[dim]unrecorded[/]")
    console.print("\n[bold]Who is dying[/]")
    console.print(table)

    if death_map.by_cause:
        console.print("\n[bold]How[/]")
        for cause, count in list(death_map.by_cause.items())[:8]:
            console.print(f"  {count:3d}  [dim]{cause}[/]")


def show_map(death_map: DeathMap, width: int = WIDTH, recent: int = RECENT) -> None:
    if not death_map.deaths:
        console.print(
            "No deaths in that window. "
            "[dim]Locations come from gravestone placements in the log; without a "
            "gravestone mod only the cause is recorded.[/]"
        )
        return

    console.print(
        Panel(
            f"[bold]{len(death_map.deaths)} death(s)[/] "
            f"[dim]{death_map.start:%Y-%m-%d %H:%M} -> {death_map.end:%Y-%m-%d %H:%M}"
            f"  {len(death_map.located)} with a location[/]",
            expand=False,
        )
    )

    spots = hotspots(death_map.located)
    # Letters are assigned once across every dimension, so the legend below
    # means the same thing on each map.
    letters = {
        death: _letter(index)
        for index, spot in enumerate(spots)
        for death in spot.deaths
    }
    for name in death_map.dimensions:
        _plot(name, death_map.in_dimension(name), letters, width)

    if spots:
        _hotspot_table(spots)
    _tallies(death_map)

    unrecovered = death_map.unrecovered
    if unrecovered:
        console.print(
            f"\n[bold]Graves never collected[/] [dim]({len(unrecovered)})[/]"
        )
        for death in unrecovered[-recent:]:
            console.print(
                f"  [dim]{death.at:%m-%d %H:%M}[/]  {death.player} "
                f"[dim]{death.position} in {death.dimension}[/]"
            )

    if death_map.unlocated:
        console.print(
            f"\n[dim]{len(death_map.unlocated)} death(s) had no gravestone placement, "
            "so they are counted but not plotted.[/]"
        )

    console.print(f"\n[dim]newest death {human_age(death_map.deaths[-1].at)}[/]")
