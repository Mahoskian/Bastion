"""Rendering the snapshot repository."""

from __future__ import annotations

from rich.table import Table

from ..core.repository import RepoStats, Snapshot, SnapshotSummary
from .console import console, rows
from .format import human_age, human_size


def show_snapshots(snapshots: list[Snapshot], stats: RepoStats | None = None) -> None:
    if not snapshots:
        console.print("No snapshots yet. Run [bold]mc snapshot now[/].")
        return
    table = Table(header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("id", style="dim")
    table.add_column("when")
    table.add_column("age", style="dim")
    table.add_column("tags", style="dim")
    for index, snapshot in enumerate(snapshots, 1):
        table.add_row(
            str(index),
            snapshot.short_id,
            f"{snapshot.time:%Y-%m-%d %H:%M:%S}",
            human_age(snapshot.time.replace(tzinfo=None)),
            ",".join(snapshot.tags),
        )
    console.print(table)
    if stats is not None:
        console.print(
            f"[dim]{len(snapshots)} snapshot(s), {human_size(stats.total_size)} on disk "
            f"after dedup[/]"
        )


def show_summary(summary: SnapshotSummary, stats: RepoStats | None = None) -> None:
    console.print(f"[bold green]Snapshot[/] {summary.snapshot_id[:8]}")
    block = {
        "read": human_size(summary.total_bytes_processed),
        "new data": f"[bold]{human_size(summary.data_added)}[/] [dim]before compression[/]",
        "files": (
            f"{summary.total_files_processed} "
            f"[dim]({summary.files_new} new, {summary.files_changed} changed)[/]"
        ),
        "took": f"{summary.total_duration:.1f}s",
    }
    if summary.data_added:
        block["dedup"] = f"[green]{summary.dedup_ratio:.0f}x[/] [dim]read per byte stored[/]"
    if stats is not None:
        block["repo"] = (
            f"{human_size(stats.total_size)} on disk across {stats.snapshots} snapshot(s)"
        )
    rows(block)


def show_stats(stats: RepoStats, location) -> None:
    block = {
        "repository": str(location),
        "snapshots": str(stats.snapshots),
        "size on disk": f"[bold]{human_size(stats.total_size)}[/]",
        "if fully restored": (
            f"{human_size(stats.restore_size)} across {stats.total_file_count} files"
        ),
    }
    if stats.dedup_ratio:
        block["dedup"] = f"[green]{stats.dedup_ratio:.1f}x[/]"
    rows(block)
