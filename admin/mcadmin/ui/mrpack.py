"""Rendering the modpack build."""

from __future__ import annotations

from pathlib import Path

from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from ..core.mrpack import ModFile, PackBuilder, PackResult, PackSpec
from .console import console, rows
from .format import human_size


def resolve_with_progress(
    builder: PackBuilder, jars: list[Path]
) -> tuple[list[ModFile], list[Path]]:
    """Resolve every jar against Modrinth, showing a live count."""
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("hashing jars", total=len(jars))

        def advance(index: int, _item: object) -> None:
            progress.update(task, completed=index)

        return builder.resolve(jars, on_progress=advance)


def show_result(result: PackResult, spec: PackSpec) -> None:
    console.print(f"\n[bold green]Built[/] {result.path}  [dim]{human_size(result.size)}[/]")
    if result.index_path:
        console.print(f"[bold green]Wrote[/] {result.index_path}  [dim]commit this[/]")
    rows(
        {
            "linked from Modrinth": str(len(result.linked)),
            "bundled in overrides": str(len(result.bundled)),
            "targets": f"minecraft {spec.mc_version} / fabric-loader {spec.loader}",
            "version": spec.version,
        }
    )
    if result.bundled:
        console.print("\n[dim]bundled (Modrinth does not host these):[/]")
        for path in result.bundled:
            console.print(f"  [dim]{path.name}[/]")
