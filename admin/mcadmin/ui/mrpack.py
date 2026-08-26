"""Rendering the modpack build."""

from __future__ import annotations

from pathlib import Path

from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

from ..core.changelog import Changelog, Release
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


def show_changes(release: Release, changelog: Path, recorded: bool) -> None:
    """What this build changed, and where that was written down.

    `recorded` is false when nothing changed: the history is deliberately not
    appended to, and saying so beats printing an empty release.
    """
    if not recorded:
        console.print("\n[dim]No mod changes since the last release -- changelog untouched.[/]")
        return

    console.print(f"\n[bold green]Recorded[/] {changelog}  [dim]{release.summary}[/]")
    if release.initial:
        console.print("[dim]First build on record -- the changelog starts here.[/]")
        return
    for label, colour, names in (
        ("added", "green", release.added),
        ("removed", "red", release.removed),
    ):
        for name in names:
            console.print(f"  [{colour}]{label:>7}[/] {name}")
    for change in release.updated:
        console.print(
            f"  [yellow]updated[/] {change.mod}  [dim]{change.before} -> {change.after}[/]"
        )


def show_unchanged(history: Changelog, changelog: Path) -> None:
    """Why there is no pack, when the command was asked for one.

    Silence would read as a failure, so this names the release the mods folder
    already matches and the flag that overrides the decision.
    """
    latest = history.latest
    built = f" as {latest.version} on {latest.built_at:%Y-%m-%d}" if latest else ""
    count = len(history.mods)
    console.print(
        f"[bold]Nothing to build.[/] The {count} mod{'' if count == 1 else 's'} in the "
        f"client folder are the ones released{built}."
    )
    console.print(f"[dim]History: {changelog}[/]")
    console.print("[dim]Build it anyway with: mc mrpack --force[/]")
