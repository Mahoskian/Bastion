"""Metrics: sample the server on a schedule, then look at the trend."""

from __future__ import annotations

from datetime import datetime

import typer

from ..core import gclog as gl
from ..core.metrics import GcRecord, MetricsStore, Sampler
from ..core.units import parse_duration
from ..ui import metrics as view
from ..ui.console import console, fail

app = typer.Typer(no_args_is_help=True, help="Time series of server load and GC behaviour.")

DEFAULT_RETENTION = "90d"


def _window(text: str):
    try:
        return parse_duration(text)
    except ValueError as exc:
        fail(str(exc))


@app.command()
def sample(
    world_size: bool = typer.Option(
        True, "--world/--no-world", help="Measure the world directory too."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Say nothing on success."),
    retain: str = typer.Option(
        DEFAULT_RETENTION, "--retain", help="Drop samples older than this."
    ),
) -> None:
    """Take one sample. Cheap enough to run every minute from cron."""
    store = MetricsStore()
    taken = Sampler().take(world_size=world_size)
    store.add(taken)
    if retain:
        store.prune(_window(retain))
    if not quiet:
        view.show_sample(taken)


@app.command()
def show(
    since: str = typer.Option("24h", "--since", help="How far back, e.g. 6h, 90m, 3d."),
) -> None:
    """Show the trend over a window."""
    store = MetricsStore()
    cutoff = datetime.now() - _window(since)
    view.show_metrics(store.samples(since=cutoff), store)


@app.command()
def gc(
    limit: int = typer.Option(25, "--limit", "-n", help="How many runs to show."),
) -> None:
    """Show recorded GC analyses, including runs whose logs have rotated away."""
    view.show_gc_history(MetricsStore().gc_runs(limit=limit))


@app.command()
def record(
    all_runs: bool = typer.Option(False, "--all", help="Record every run still in the logs."),
) -> None:
    """Copy GC analyses out of the logs before they rotate."""
    files = gl.log_files()
    if not files:
        fail("no gc.log* files to record")
    runs = gl.parse(files)
    if not runs:
        fail("GC logs contain no parseable records.")

    store = MetricsStore()
    chosen = runs if all_runs else runs[-1:]
    for run in chosen:
        store.record_gc(GcRecord.from_analysis(gl.analyze(run)))
    console.print(f"[green]Recorded[/] {len(chosen)} GC run(s).")


@app.command()
def prune(
    older_than: str = typer.Option(DEFAULT_RETENTION, "--older-than", help="e.g. 30d."),
) -> None:
    """Drop old samples."""
    removed = MetricsStore().prune(_window(older_than))
    console.print(f"Removed {removed} sample(s).")
