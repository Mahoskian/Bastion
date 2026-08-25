"""The death map: where players die, and which spot keeps doing it."""

from __future__ import annotations

import json
from datetime import datetime

import typer

from ..core import deaths as dt
from ..core import logs as lg
from ..core.models import Paths
from ..core.units import parse_duration
from ..ui import deaths as view
from ..ui.console import console, fail

app = typer.Typer()


@app.command()
def deaths(
    since: str = typer.Option(
        None, "--since", help="Only this far back, e.g. 6h, 3d. Default: every log."
    ),
    player: str = typer.Option(None, "--player", "-p", help="Only this player's deaths."),
    dimension: str = typer.Option(
        None, "--dimension", "-d", help="Only this dimension, e.g. the_nether."
    ),
    width: int = typer.Option(view.WIDTH, "--width", "-w", help="Map width in characters."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Plot player deaths from the logs and find the spots that keep killing."""
    paths = Paths.from_env()
    files = lg.log_files(paths.logs_dir)
    if not files:
        fail(f"no logs in {paths.logs_dir}")

    records = list(lg.parse(files))
    if since is not None:
        try:
            window = parse_duration(since)
        except ValueError as exc:
            fail(str(exc))
        cutoff = datetime.now() - window
        records = [record for record in records if record.at >= cutoff]

    found = dt.collect(records)
    if player or dimension:
        wanted = [
            death
            for death in found.deaths
            if (not player or death.player.lower() == player.lower())
            and (not dimension or death.dimension == dimension)
        ]
        found = found.model_copy(update={"deaths": wanted})

    if as_json:
        console.print_json(json.dumps(dt.as_dict(found)))
        return
    view.show_map(found, width=width)
