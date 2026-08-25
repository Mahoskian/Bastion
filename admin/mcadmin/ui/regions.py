"""Rendering the region scan: what the world is made of, and what is bloated."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from ..core.regions import BIG_SECTORS, SECTOR, Scan
from ..core.units import human_bytes
from .console import console

BAR_WIDTH = 28
HISTOGRAM_ROWS = 8
DAYS = 14


def _bar(value: float, peak: float, width: int = BAR_WIDTH) -> str:
    filled = round(width * value / peak) if peak else 0
    return "█" * max(1 if value else 0, filled)


def show_summary(scans: list[Scan]) -> None:
    total_chunks = sum(result.chunks for result in scans)
    total_bytes = sum(result.file_bytes for result in scans)
    console.print(
        Panel(
            f"[bold]{total_chunks:,} chunks[/] in "
            f"{sum(result.regions for result in scans):,} region files"
            f"  [dim]{human_bytes(total_bytes)} on disk[/]",
            expand=False,
        )
    )

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("dimension")
    for name in ("regions", "empty", "chunks", "on disk", "mean", "p99", "big", "filled"):
        table.add_column(name, justify="right")
    for result in scans:
        table.add_row(
            result.dimension,
            f"{result.regions:,}",
            f"{result.empty_regions:,}" if result.empty_regions else "0",
            f"{result.chunks:,}",
            human_bytes(result.file_bytes),
            human_bytes(result.mean_bytes),
            human_bytes(result.percentile(0.99)),
            f"[yellow]{result.big_chunks:,}[/]" if result.big_chunks else "0",
            f"{result.fill * 100:.0f}%",
        )
    console.print(table)
    console.print(
        f"[dim]big = at least {human_bytes(BIG_SECTORS * SECTOR)}; "
        "empty = region files holding no generated chunk at all; "
        "filled = generated chunks as a share of the bounding box, so a low "
        "number means an uneven world rather than a missing one[/]"
    )


def _distribution(result: Scan) -> None:
    if not result.sectors:
        return
    console.print(f"\n[bold]{result.dimension}[/] [dim]chunk size distribution[/]")
    peak = max(result.sectors.values())
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(justify="right", min_width=7)
    table.add_column(justify="right", min_width=8)
    table.add_column()
    ordered = sorted(result.sectors.items())
    head = ordered[: HISTOGRAM_ROWS - 1]
    tail = ordered[HISTOGRAM_ROWS - 1 :]
    for size, count in head:
        table.add_row(human_bytes(size * SECTOR), f"{count:,}", f"[cyan]{_bar(count, peak)}[/]")
    if tail:
        # Everything above the histogram's last row is the interesting end, so
        # it is summed rather than dropped.
        total = sum(count for _, count in tail)
        table.add_row(
            f"{human_bytes(tail[0][0] * SECTOR)}+",
            f"{total:,}",
            f"[yellow]{_bar(total, peak)}[/]",
        )
    console.print(table)


def _biggest(result: Scan, limit: int) -> None:
    if not result.biggest:
        return
    console.print(f"\n[bold]{result.dimension}[/] [dim]largest chunks[/]")
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("size", justify="right")
    table.add_column("chunk")
    table.add_column("blocks")
    table.add_column("last written")
    for chunk in result.biggest[:limit]:
        note = " [red]external[/]" if chunk.external else ""
        table.add_row(
            f"{human_bytes(chunk.bytes)}{note}",
            f"{chunk.x}, {chunk.z}",
            chunk.position,
            f"{chunk.modified:%Y-%m-%d %H:%M}" if chunk.modified else "[dim]unknown[/]",
        )
    console.print(table)
    console.print(
        f"[dim]go and look: /execute in minecraft:{result.dimension} run tp @s "
        "<blocks>[/]"
    )


def _timeline(result: Scan, days: int = DAYS) -> None:
    if not result.written:
        return
    recent = list(result.written.items())[-days:]
    peak = max(count for _, count in recent)
    console.print(f"\n[bold]{result.dimension}[/] [dim]chunks last written, by day[/]")
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(min_width=10)
    table.add_column(justify="right", min_width=8)
    table.add_column()
    for day, count in recent:
        table.add_row(day.isoformat(), f"{count:,}", f"[green]{_bar(count, peak)}[/]")
    console.print(table)
    console.print(
        "[dim]a chunk counts on the day it was last saved, so this tracks a "
        "pregen run in progress and where players have been since[/]"
    )


def show_detail(result: Scan, limit: int, timeline: bool) -> None:
    _distribution(result)
    _biggest(result, limit)
    if timeline:
        _timeline(result)
    if result.slack_bytes:
        console.print(
            f"\n[dim]{human_bytes(result.slack_bytes)} of {result.dimension} is free "
            "space inside region files -- rewritten chunks leave their old sectors "
            "behind, and region files never shrink.[/]"
        )
    if result.unreadable:
        console.print(
            f"\n[yellow]{len(result.unreadable)} unreadable region file(s)[/]: "
            + ", ".join(result.unreadable[:5])
        )
