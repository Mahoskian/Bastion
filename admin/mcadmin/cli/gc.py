"""GC log analysis command."""

from __future__ import annotations

from pathlib import Path

import typer

from ..core import gclog as gl
from ..core.models import Paths
from ..ui import gc as view
from ..ui.console import console, fail

app = typer.Typer()


@app.command()
def gc(
    run: int = typer.Option(1, "--run", "-r", help="Which JVM run; 1 is the most recent."),
    all_runs: bool = typer.Option(False, "--all", help="Summarise every run in the logs."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    directory: Path = typer.Option(None, "--dir", help="Read GC logs from here instead."),
    record: bool = typer.Option(
        False, "--record", help="Also save this analysis, so it outlives the log."
    ),
) -> None:
    """Size the heap from the GC logs instead of guessing."""
    files = gl.log_files(directory)
    if not files:
        fail(f"no gc.log* files in {directory or Paths.from_env().logs_dir}")
    runs = gl.parse(files)
    if not runs:
        fail("GC logs contain no parseable records.")

    if all_runs:
        view.show_runs(runs)
        return
    if not 1 <= run <= len(runs):
        fail(f"no run {run} -- the logs hold {len(runs)} (1 is the most recent).")

    analysis = gl.analyze(runs[-run])
    if record:
        from ..core.metrics import GcRecord, MetricsStore

        MetricsStore().record_gc(GcRecord.from_analysis(analysis))
        console.print("[dim]recorded to the metrics store[/]")
    if as_json:
        console.print_json(gl.dumps(analysis))
        return
    view.show_report(analysis, run, len(runs))
