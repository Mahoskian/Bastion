"""Charts, rendered as PNG bytes for a Discord embed to carry.

Still `ui`: a pure function of a model that decides nothing. What it returns is
image bytes rather than terminal output, and `None` when there is nothing worth
plotting -- the caller then falls back to the text embed rather than posting an
empty pair of axes.

Three constraints shape everything here:

  * **Threads.** These render from `asyncio.to_thread`, and pyplot's global
    current-figure state is not thread-safe. Only the object-oriented API is
    used -- `Figure` with an Agg canvas attached by hand -- so two concurrent
    commands cannot draw into each other.
  * **Discord's surface.** The chart is a card on a channel that is dark for
    most readers and light for some, so it carries its own validated dark
    surface rather than a transparent background that would look broken on
    whichever theme was not tested.
  * **No hover.** A PNG has no tooltip layer to fall back on, so every value a
    reader needs is either direct-labelled or on an axis. That raises the bar
    on labelling rather than lowering it.

The palette is the validated categorical set stepped for a dark surface. The
slot order is the colourblind-safety mechanism, not decoration: it was checked
against this surface with the palette validator, so hues are taken in order and
never cycled.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from ..core.deaths import CLUSTER_RADIUS, DeathMap, Hotspot, dimension_label
from ..core.stats import DAMAGE_PER_HEART, Board, PlayerStats, Roster, Unit
from ..core.units import human_distance, human_seconds

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

# Surfaces and ink, stepped for the dark surface this renders on.
SURFACE = "#1a1a19"
INK = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"
# Categorical slots, in the fixed order that passes the CVD gates. Never cycled:
# past the eighth the tail folds into "Other" instead of inventing a ninth hue.
SERIES = (
    "#3987e5",  # blue
    "#d95926",  # orange
    "#199e70",  # aqua
    "#c98500",  # yellow
    "#d55181",  # magenta
    "#008300",  # green
    "#9085e9",  # violet
    "#e66767",  # red
)
# Status, reserved -- never reused as "series 9".
CRITICAL = "#d03b3b"
# The meter track: a darker step of the fill's own ramp, so state reads across
# the whole bar rather than only where it is filled.
TRACK = "#184f95"

DPI = 160
# Discord shows an embed image about 550px wide, so this renders at roughly
# twice that and lets the client scale it down.
FONT = ["DejaVu Sans"]


def available() -> bool:
    """Whether matplotlib is installed. Charts are an optional extra."""
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return False
    return True


# ------------------------------------------------------------------ scaffolding


def _figure(width: float, height: float) -> Figure:
    """A figure with an Agg canvas attached by hand, never through pyplot."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(width, height), dpi=DPI)
    FigureCanvasAgg(figure)
    figure.patch.set_facecolor(SURFACE)
    return figure


def _render(figure: Figure) -> bytes:
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=SURFACE, bbox_inches="tight", pad_inches=0.3)
    return buffer.getvalue()


def _dress(axes: Axes, *, grid: str = "") -> None:
    """The recessive chrome every plot shares: hairlines, no box, muted ink."""
    axes.set_facecolor(SURFACE)
    for side, spine in axes.spines.items():
        spine.set_visible(side == "bottom")
        spine.set_color(AXIS)
        spine.set_linewidth(1.0)
    axes.tick_params(colors=INK_MUTED, labelsize=8, length=0)
    for label in axes.get_xticklabels() + axes.get_yticklabels():
        label.set_color(INK_MUTED)
    if grid:
        # Solid hairlines, one step off the surface. Never dashed: dashing reads
        # as "threshold" when it is only a grid.
        axes.grid(True, axis=grid, color=GRID, linewidth=1.0, linestyle="-")
        axes.set_axisbelow(True)


def _title(figure: Figure, text: str, subtitle: str = "") -> None:
    """Title and subtitle, spaced in inches rather than figure fractions.

    A fraction that clears the title on a tall figure overlaps it on a short
    one, which is exactly the collision this had on the first render.
    """
    height = figure.get_size_inches()[1]
    figure.text(
        0.02, 1 - 0.30 / height, text, color=INK, fontsize=15, fontweight="bold", ha="left"
    )
    if subtitle:
        figure.text(
            0.02, 1 - 0.56 / height, subtitle, color=INK_MUTED, fontsize=9.5, ha="left"
        )


def _bar(axes: Axes, y: float, width: float, colour: str, thickness: float = 9.0) -> None:
    """One horizontal bar: square at the baseline, rounded at the data end.

    Drawn as a line rather than a rectangle because a rectangle's corner radius
    would have to be given in data units, and the two axes here are in
    different units -- one radius cannot be right for both. A line's width is
    in points, so the bar is a fixed physical thickness whatever the values are.

    The two ends differ on purpose, so the cap is a separate round marker at
    the data end rather than a round capstyle on the whole line: a bar rounded
    at the baseline too would put ink to the left of zero.
    """
    if width <= 0:
        return
    axes.plot(
        [0, width],
        [y, y],
        color=colour,
        linewidth=thickness,
        solid_capstyle="butt",
        zorder=2,
        clip_on=False,
    )
    axes.plot(
        [width],
        [y],
        marker="o",
        markersize=thickness,
        markerfacecolor=colour,
        markeredgecolor="none",
        zorder=2,
        clip_on=False,
    )


def _format(value: float, unit: Unit) -> str:
    if unit is Unit.DURATION:
        return human_seconds(value)
    if unit is Unit.DISTANCE:
        return human_distance(value)
    if unit is Unit.HEALTH:
        return f"{value / DAMAGE_PER_HEART:,.0f}"
    return f"{value:,.0f}"


# ------------------------------------------------------------------ deaths


def death_map(found: DeathMap, spots: list[Hotspot]) -> bytes | None:
    """Where players die, one panel per dimension.

    Faceted rather than coloured by dimension because the coordinates are not
    comparable: nether distances are 1:8 to overworld ones, so plotting them on
    shared axes would put two different scales on one picture. Each panel is a
    single series, which is also why none of them carries a legend.
    """
    from matplotlib.lines import Line2D
    from matplotlib.patches import Circle
    from matplotlib.ticker import MaxNLocator

    plotted = found.located
    if not plotted:
        return None
    dimensions = found.dimensions[:3]
    if not dimensions:
        return None

    figure = _figure(4.6 * len(dimensions), 5.0)
    axes_list = figure.subplots(1, len(dimensions), squeeze=False)[0]
    by_dimension = {name: found.in_dimension(name) for name in dimensions}
    spots_by_dimension: dict[str, list[Hotspot]] = {}
    for spot in spots:
        spots_by_dimension.setdefault(spot.dimension, []).append(spot)

    for axes, name in zip(axes_list, dimensions, strict=False):
        deaths = by_dimension[name]
        here = spots_by_dimension.get(name, [])
        clustered = {id(death) for spot in here for death in spot.deaths}
        _dress(axes, grid="both")

        loose = [death for death in deaths if id(death) not in clustered]
        inside = [death for death in deaths if id(death) in clustered]
        # Two marks, not one: a death inside a hot spot is the thing the chart
        # is about, so it carries the status colour and the legend names both.
        for group, colour in ((loose, SERIES[0]), (inside, CRITICAL)):
            if group:
                axes.scatter(
                    [death.x for death in group],
                    [death.z for death in group],
                    s=70,
                    c=colour,
                    # The 2px surface ring, so overlapping deaths stay countable.
                    edgecolors=SURFACE,
                    linewidths=2,
                    zorder=4,
                )
        for spot in here:
            # Drawn at the radius the clustering actually used, so the circle
            # encloses its members instead of floating over empty ground.
            axes.add_patch(
                Circle(
                    (spot.x, spot.z),
                    CLUSTER_RADIUS,
                    facecolor=CRITICAL,
                    alpha=0.07,
                    edgecolor=CRITICAL,
                    linewidth=1.0,
                    zorder=2,
                )
            )
            axes.annotate(
                f"{spot.count}",
                (spot.x, spot.z - CLUSTER_RADIUS),
                textcoords="offset points",
                xytext=(0, 8),
                ha="center",
                color=INK,
                fontsize=10,
                fontweight="bold",
                zorder=5,
            )
        axes.set_title(
            f"{dimension_label(name)}   {len(deaths)}",
            color=INK_SECONDARY,
            fontsize=11,
            loc="left",
            pad=10,
        )
        # Block coordinates are whole numbers; a tick at 672.5 is a place that
        # cannot exist.
        axes.xaxis.set_major_locator(MaxNLocator(nbins=6, integer=True))
        axes.yaxis.set_major_locator(MaxNLocator(nbins=6, integer=True))
        # Equal aspect and inverted z: north is up, and a squashed axis would
        # be a plot that lies about where things are.
        axes.set_aspect("equal", adjustable="datalim")
        axes.invert_yaxis()

    if any(spots_by_dimension.values()):
        handles = [
            Line2D([], [], marker="o", linestyle="none", markersize=8,
                   markerfacecolor=SERIES[0], markeredgecolor=SURFACE, label="death"),
            Line2D([], [], marker="o", linestyle="none", markersize=8,
                   markerfacecolor=CRITICAL, markeredgecolor=SURFACE, label="in a hot spot"),
        ]
        legend = figure.legend(
            handles=handles, loc="upper right", frameon=False, fontsize=9,
            bbox_to_anchor=(0.99, 1 - 0.22 / 5.0), ncols=2, handletextpad=0.4,
        )
        for text in legend.get_texts():
            text.set_color(INK_SECONDARY)

    located, total = len(plotted), len(found.deaths)
    _title(
        figure,
        "Deaths",
        f"{total} deaths, {located} located · circles are hot spots · north is up",
    )
    figure.tight_layout(rect=(0, 0, 1, 0.86))
    return _render(figure)


# ------------------------------------------------------------------ stats


def leaderboards(roster: Roster, boards: list[Board], top: int = 3) -> bytes | None:
    """Every board as its own panel of bars.

    One hue per panel, taken in slot order -- the colour identifies the board,
    not the rank, so a bar never changes colour because someone overtook
    someone else. Names ride the bars and values sit at the tip, which is what
    replaces the tooltip a PNG cannot have.
    """
    filled = [board for board in boards if board.standings]
    if not roster.players or not filled:
        return None
    filled = filled[: len(SERIES)]

    columns = 2
    rows = (len(filled) + columns - 1) // columns
    figure = _figure(12.0, 1.15 * rows + 1.1)
    grid = figure.subplots(rows, columns, squeeze=False)

    for index, board in enumerate(filled):
        axes = grid[index // columns][index % columns]
        _dress(axes)
        standings = board.standings[:top]
        widest = max(standing.value for standing in standings) or 1
        for position, standing in enumerate(standings):
            y = len(standings) - position
            _bar(axes, y, standing.value, SERIES[index % len(SERIES)], thickness=8.0)
            # Names sit outside the plot rather than above each bar: stacked
            # over the bars they read as belonging to the row below.
            axes.annotate(
                standing.player, (0, y), textcoords="offset points", xytext=(-10, 0),
                ha="right", va="center", color=INK_SECONDARY, fontsize=9,
                annotation_clip=False,
            )
            axes.annotate(
                _format(standing.value, board.unit), (standing.value, y),
                textcoords="offset points", xytext=(12, 0), ha="left", va="center",
                color=INK, fontsize=9, fontweight="bold", annotation_clip=False,
            )
        axes.set_title(board.title, color=INK_MUTED, fontsize=9.5, loc="left", pad=8)
        # Left edge is exactly zero so the baseline cap is clipped square.
        axes.set_xlim(0, widest * 1.30)
        axes.set_ylim(0.4, len(standings) + 0.6)
        axes.set_xticks([])
        axes.set_yticks([])
        for spine in axes.spines.values():
            spine.set_visible(False)

    for empty in range(len(filled), rows * columns):
        grid[empty // columns][empty % columns].set_visible(False)

    updated = roster.updated
    _title(
        figure,
        "Leaderboards",
        f"{len(roster.players)} players · as of {updated:%Y-%m-%d %H:%M}"
        if updated
        else f"{len(roster.players)} players",
    )
    figure.tight_layout(rect=(0, 0, 1, 1 - 0.85 / figure.get_size_inches()[1]))
    figure.subplots_adjust(wspace=0.5, hspace=0.75)
    return _render(figure)


def player_card(player: PlayerStats, roster: Roster, won: list[str]) -> bytes | None:
    """One player against the best anyone has managed.

    A bare number says nothing on its own -- 6,099 blocks is either a lot or a
    little depending on the server. The track behind each bar is the server
    best, so every row reads as a share of what is achievable here.
    """
    if not roster.players:
        return None
    metrics: list[tuple[str, float, float, Unit]] = []
    for label, attribute, unit in (
        ("Playtime", "play_seconds", Unit.DURATION),
        ("Blocks mined", "blocks_mined", Unit.COUNT),
        ("Mobs killed", "mob_kills", Unit.COUNT),
        ("Distance", "distance_cm", Unit.DISTANCE),
        ("Items crafted", "items_crafted", Unit.COUNT),
        ("Damage taken", "damage_taken", Unit.HEALTH),
        ("Deaths", "deaths", Unit.COUNT),
        ("Advancements", "advancements", Unit.COUNT),
    ):
        best = max((getattr(other, attribute) for other in roster.players), default=0)
        metrics.append((label, float(getattr(player, attribute)), float(best), unit))

    figure = _figure(9.5, 0.46 * len(metrics) + 1.3)
    axes = figure.subplots()
    _dress(axes)
    for spine in axes.spines.values():
        spine.set_visible(False)

    for index, (label, value, best, unit) in enumerate(metrics):
        y = len(metrics) - index
        span = best or 1
        _bar(axes, y, 1.0, TRACK, thickness=11.0)
        _bar(axes, y, value / span, SERIES[0], thickness=11.0)
        # Offsets in points, not data units: these labels sit outside the axes,
        # so anything measured in x would re-crowd the moment the axes width
        # changed -- which is exactly how they collided the first time.
        axes.annotate(
            label, (0, y), textcoords="offset points", xytext=(-12, 0),
            ha="right", va="center", color=INK_SECONDARY, fontsize=10, annotation_clip=False,
        )
        axes.annotate(
            _format(value, unit), (1.0, y), textcoords="offset points", xytext=(14, 0),
            ha="left", va="center", color=INK, fontsize=10, fontweight="bold",
            annotation_clip=False,
        )
        if value >= best > 0:
            # Its own column, so the flags form a line instead of ragging along
            # behind values of different widths.
            axes.annotate(
                "best", (1.0, y), textcoords="offset points", xytext=(96, 0),
                ha="left", va="center", color=INK_MUTED, fontsize=9, annotation_clip=False,
            )

    axes.set_xlim(0, 1.0)
    axes.set_ylim(0.4, len(metrics) + 0.6)
    axes.set_xticks([])
    axes.set_yticks([])
    subtitle = "bar is this player, track is the server best"
    if won:
        subtitle = "tops " + ", ".join(won).lower() + " · " + subtitle
    _title(figure, player.name, subtitle)
    # Fixed margins rather than tight_layout: the labels are deliberately
    # outside the axes, and tight_layout cannot see them to make room.
    figure.subplots_adjust(
        left=0.20, right=0.72, top=1 - 0.95 / figure.get_size_inches()[1], bottom=0.04
    )
    return _render(figure)
