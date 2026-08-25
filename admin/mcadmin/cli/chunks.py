"""Chunk forensics over the world's region files."""

from __future__ import annotations

import json

import typer

from ..core import regions as rg
from ..core.models import Paths
from ..ui import regions as view
from ..ui.console import console, fail

app = typer.Typer()


@app.command()
def chunks(
    dimension: str = typer.Option(
        None, "--dimension", "-d", help="Only this dimension, e.g. the_nether."
    ),
    kind: str = typer.Option(
        "region", "--kind", help=f"Which files: {', '.join(rg.KINDS)}."
    ),
    top: int = typer.Option(5, "--top", "-n", help="How many big chunks to name."),
    detail: bool = typer.Option(
        True, "--detail/--summary", help="Include distributions and big chunks."
    ),
    timeline: bool = typer.Option(
        False, "--timeline", help="Also show chunks last written per day."
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Count chunks, size their distribution, and find the bloated ones."""
    if kind not in rg.KINDS:
        fail(f"--kind must be one of {', '.join(rg.KINDS)}")
    paths = Paths.from_env()
    available = paths.region_dirs(kind)
    if not available:
        fail(f"no {kind} directories under {paths.world_dir}")
    if dimension and dimension not in available:
        fail(f"no dimension {dimension!r} -- found: {', '.join(sorted(available))}")

    scans = rg.scan_world(paths, kind=kind, dimension=dimension, keep=max(top, 10))
    if as_json:
        console.print_json(json.dumps(rg.as_dict(scans)))
        return

    view.show_summary(scans)
    if detail:
        for result in scans:
            view.show_detail(result, limit=top, timeline=timeline)
