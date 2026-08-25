"""Rendering the composite slowness view."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from ..core.gclog import Level
from ..core.slow import Bucket, Timeline, explain
from ..core.units import human_bytes, human_seconds
from .console import console

LEVEL_STYLE = {Level.INFO: "", Level.WARN: "yellow", Level.ERROR: "red"}
BLOCKS = "▁▂▃▄▅▆▇█"
QUIET = "·"
STRIP_WIDTH = 72


def _lost(bucket: Bucket) -> str:
    if not bucket.lost_ms:
        return "[dim]-[/]"
    text = f"{bucket.lost_ms / 1000:.1f}s"
    if bucket.lost_ms >= 10_000:
        return f"[red]{text}[/]"
    return f"[yellow]{text}[/]" if bucket.interesting else f"[dim]{text}[/]"


def strip(buckets: list[Bucket], width: int = STRIP_WIDTH) -> tuple[str, int]:
    """One glyph per bucket, folded to fit, and how many columns it came to.

    Columns take the *worst* bucket they cover rather than the mean: this line
    exists to find outages, and averaging one away is exactly the failure it
    has to avoid. Scaled to the peak, so a quiet day looks quiet.
    """
    if not buckets:
        return "", 0
    columns: list[float] = []
    if len(buckets) > width:
        size = len(buckets) / width
        for index in range(width):
            group = buckets[int(index * size) : max(int((index + 1) * size), int(index * size) + 1)]
            columns.append(max((bucket.lost_ms for bucket in group), default=0.0))
    else:
        columns = [bucket.lost_ms for bucket in buckets]

    peak = max(columns, default=0.0)
    if not peak:
        return f"[green]{QUIET * len(columns)}[/]", len(columns)
    top = len(BLOCKS) - 1
    marks = []
    for value in columns:
        if not value:
            marks.append(f"[dim]{QUIET}[/]")
            continue
        glyph = BLOCKS[round(value / peak * top)]
        marks.append(f"[{'red' if value >= 10_000 else 'yellow'}]{glyph}[/]")
    return "".join(marks), len(columns)


def _table(buckets: list[Bucket], mark: Bucket | None = None) -> Table:
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("")
    table.add_column("when")
    for name in ("lost", "warns", "GC", "GC ms", "players", "pregen", "errors"):
        table.add_column(name, justify="right")
    for bucket in buckets:
        table.add_row(
            "[bold]>[/]" if bucket is mark else " ",
            bucket.label,
            _lost(bucket),
            str(bucket.lag_events or ""),
            str(bucket.gc_pauses) if bucket.gc_known else "[dim]?[/]",
            f"{bucket.gc_ms:.0f}" if bucket.gc_known else "[dim]?[/]",
            str(bucket.players_peak or ""),
            f"{bucket.pregen_chunks:,}" if bucket.pregen_chunks else "",
            f"[red]{bucket.errors}[/]" if bucket.errors else "",
        )
    return table


def _findings(bucket: Bucket) -> None:
    for finding in explain(bucket):
        style = LEVEL_STYLE.get(finding.level, "")
        bullet = f"[{style}]*[/]" if style else "*"
        console.print(f"  {bullet} {finding.message}")


def show_timeline(timeline: Timeline, focus: Bucket | None, worst: int = 5) -> None:
    if not timeline.buckets:
        console.print("No log records in that window.")
        return

    console.print(
        Panel(
            f"[bold]{timeline.start:%Y-%m-%d %H:%M}[/] -> "
            f"[bold]{timeline.end:%Y-%m-%d %H:%M}[/]"
            f"  [dim]{len(timeline.buckets)} x "
            f"{human_seconds(timeline.size.total_seconds())} buckets, "
            f"{human_seconds(timeline.lost_ms / 1000)} of tick time lost[/]",
            expand=False,
        )
    )
    marks, columns = strip(timeline.buckets)
    left, right = f"{timeline.start:%m-%d %H:%M}", f"{timeline.end:%m-%d %H:%M}"
    gap = max(1, columns - len(left) - len(right))
    console.print(f"  {marks}")
    console.print(f"  [dim]{left}{' ' * gap}{right}[/]")

    if not timeline.gc_covered:
        console.print(
            "\n[yellow]No GC data covers this window[/] [dim]-- the GC logs have "
            "rotated past it, so the heap cannot be ruled in or out. "
            "`mc metrics gc` keeps the summary of runs that have rolled away.[/]"
        )

    if focus is not None:
        console.print(f"\n[bold]{focus.start:%Y-%m-%d %H:%M}[/] and either side")
        console.print(_table(timeline.around(focus), mark=focus))
        console.print(f"\n[bold]Why[/] [dim]{focus.label}[/]")
        _findings(focus)
        _detail(focus)
        return

    bad = timeline.bad[:worst]
    if not bad:
        console.print(
            "\n[bold green]Nothing lost more than 2s of tick time.[/] "
            "[dim]No window in this range is worth explaining.[/]"
        )
        # The strip still has shape to it, so show what that shape was rather
        # than leaving the reader to wonder what the marks meant.
        busiest = sorted(timeline.buckets, key=lambda b: -b.lost_ms)[:3]
        if busiest and busiest[0].lost_ms:
            console.print(_table(busiest))
        return

    console.print(f"\n[bold]Worst windows[/] [dim](top {len(bad)})[/]")
    console.print(_table(bad))
    console.print(f"\n[bold]Why {bad[0].label} was the worst[/]")
    _findings(bad[0])
    _detail(bad[0])


def _detail(bucket: Bucket) -> None:
    extra: list[str] = []
    if bucket.worst_gc_ms:
        extra.append(f"longest GC pause {bucket.worst_gc_ms:.0f}ms")
    if bucket.non_gc_stw_ms:
        extra.append(f"non-GC safepoints {bucket.non_gc_stw_ms:.0f}ms")
    if bucket.heap_used:
        extra.append(f"heap ~{human_bytes(bucket.heap_used)}")
    if bucket.pregen_rate:
        extra.append(f"pregen peak {bucket.pregen_rate:.0f} chunks/s")
    if extra:
        console.print(f"\n[dim]{'  '.join(extra)}[/]")
