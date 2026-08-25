"""`mc why-slow` -- one view over the log, the GC log and who was online."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import typer

from ..core import gclog as gl
from ..core import logs as lg
from ..core import slow as sl
from ..core.metrics import MetricsStore
from ..core.models import Paths
from ..core.units import parse_duration, parse_moment
from ..ui import slow as view
from ..ui.console import console, fail

app = typer.Typer()


@app.command("why-slow")
def why_slow(
    at: str = typer.Option(
        None, "--at", help="Explain one moment: 18:00, 6pm, 'yesterday 18:00'."
    ),
    since: str = typer.Option(
        "24h", "--since", help="How far back to look, e.g. 6h, 3d."
    ),
    bucket: str = typer.Option("10m", "--bucket", help="Bucket size, e.g. 5m, 1h."),
    worst: int = typer.Option(5, "--worst", "-n", help="How many bad windows to list."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Correlate tick lag, GC pauses, players and pregen to say what went wrong."""
    paths = Paths.from_env()
    try:
        window = parse_duration(since)
        size = parse_duration(bucket)
    except ValueError as exc:
        fail(str(exc))
    if size <= timedelta(0):
        fail("--bucket must be a positive duration")

    moment: datetime | None = None
    if at is not None:
        try:
            moment = parse_moment(at)
        except ValueError as exc:
            fail(str(exc))
        # A named moment sets its own window, centred on what was asked about.
        start, end = moment - window / 2, moment + window / 2
    else:
        start, end = datetime.now() - window, None

    files = lg.log_files(paths.logs_dir)
    if not files:
        fail(f"no logs in {paths.logs_dir}")
    records = list(lg.parse(files))

    gc_files = gl.log_files(paths.logs_dir)
    runs = gl.parse(gc_files) if gc_files else []
    samples = MetricsStore(paths.metrics_db).samples(since=start)

    timeline = sl.build(
        records, runs=runs, samples=samples, since=start, until=end, size=size
    )
    focus = timeline.at(moment) if moment is not None else None
    if moment is not None and focus is None:
        fail(f"no log records cover {moment:%Y-%m-%d %H:%M}")

    if as_json:
        console.print_json(json.dumps(sl.as_dict(timeline, focus)))
        return
    view.show_timeline(timeline, focus, worst=worst)
