"""Rendering the mod audit."""

from __future__ import annotations

from pathlib import Path

from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table

from ..core.mods import (
    FetchResult,
    InstallResult,
    ModReport,
    ModScan,
    ModScanner,
    ModStatus,
    StageManifest,
    TargetAction,
)
from ..core.units import human_bytes
from .console import console

STATUS_STYLE = {
    ModStatus.CURRENT: "green",
    ModStatus.OUTDATED: "yellow",
    ModStatus.BEHIND: "red",
    ModStatus.NO_BUILD: "red",
    ModStatus.UNKNOWN: "dim",
}

STATUS_NOTE = {
    ModStatus.CURRENT: "newest build for this Minecraft version",
    ModStatus.OUTDATED: "a newer build exists",
    ModStatus.BEHIND: "installed build targets an older Minecraft",
    ModStatus.NO_BUILD: "Modrinth knows the mod, but has no build for this Minecraft",
    ModStatus.UNKNOWN: "not on Modrinth -- bundled or hand-installed",
}


def scan_with_progress(scanner: ModScanner, directory=None, target: str = "") -> ModScan:
    jars = scanner.jars(directory)
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("hashing", total=len(jars))

        def advance(_name: str) -> None:
            progress.advance(task)

        progress.update(task, description="hashing jars")
        scan = scanner.scan(directory, target=target, on_progress=advance)
        progress.update(task, description="asking Modrinth", completed=len(jars))
    return scan


TARGET_STYLE = {
    TargetAction.COMPATIBLE: "green",
    TargetAction.UPGRADE: "yellow",
    TargetAction.DOWNGRADE: "cyan",
    TargetAction.MISSING: "red",
}

TARGET_NOTE = {
    TargetAction.COMPATIBLE: "the installed build already runs on it",
    TargetAction.UPGRADE: "needs a newer build",
    TargetAction.DOWNGRADE: "needs an older build",
    TargetAction.MISSING: "no build exists -- would have to be dropped",
}


def _table(reports: list[ModReport], show_target: bool) -> Table:
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("mod", overflow="fold")
    table.add_column("installed")
    table.add_column("note", overflow="fold")
    for report in reports:
        table.add_row(report.mod.label, report.mod.version_number or "?", report.detail)
    return table


def _target_table(reports: list[ModReport]) -> Table:
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("mod", overflow="fold")
    table.add_column("change", overflow="fold")
    for report in reports:
        table.add_row(report.mod.label, report.target_detail)
    return table


def show_scan(scan: ModScan, verbose: bool) -> None:
    title = f"[bold]{len(scan.reports)} mods[/] against Minecraft [bold]{scan.minecraft}[/]"
    if scan.target:
        title += f", checking upgrade to [bold]{scan.target}[/]"
    console.print(Panel(title, expand=False))

    counts = scan.counts()
    summary = Table(show_header=False, box=None, pad_edge=False)
    summary.add_column(style="dim", width=10)
    summary.add_column(justify="right", width=4)
    summary.add_column(overflow="fold")
    for status in ModStatus:
        count = counts.get(status, 0)
        if not count:
            continue
        style = STATUS_STYLE[status]
        summary.add_row(f"[{style}]{status.value}[/]", str(count), f"[dim]{STATUS_NOTE[status]}[/]")
    console.print(summary)

    if scan.target:
        _target(scan)

    for status in (ModStatus.BEHIND, ModStatus.NO_BUILD, ModStatus.OUTDATED):
        reports = scan.of(status)
        if not reports:
            continue
        console.print(f"\n[bold {STATUS_STYLE[status]}]{status.value}[/] [dim]({len(reports)})[/]")
        console.print(_table(reports, show_target=False))

    unknown = scan.of(ModStatus.UNKNOWN)
    if unknown:
        console.print(f"\n[bold]not on Modrinth[/] [dim]({len(unknown)})[/]")
        for report in unknown if verbose else unknown[:8]:
            console.print(f"  [dim]{report.mod.filename}[/]")
        if not verbose and len(unknown) > 8:
            console.print(f"  [dim]... {len(unknown) - 8} more (--verbose to list)[/]")

    if verbose:
        current = scan.of(ModStatus.CURRENT)
        if current:
            console.print(f"\n[bold green]current[/] [dim]({len(current)})[/]")
            console.print(_table(current, show_target=False))

    _verdict(scan)


def _target(scan: ModScan) -> None:
    """What moving to the target Minecraft version would take."""
    console.print(f"\n[bold]Moving to Minecraft {scan.target}[/]")

    summary = Table(show_header=False, box=None, pad_edge=False)
    summary.add_column(style="dim", width=11)
    summary.add_column(justify="right", width=4)
    summary.add_column(overflow="fold")
    for action in TargetAction:
        reports = scan.for_target(action)
        if not reports:
            continue
        style = TARGET_STYLE[action]
        summary.add_row(
            f"[{style}]{action.value}[/]", str(len(reports)), f"[dim]{TARGET_NOTE[action]}[/]"
        )
    console.print(summary)

    for action in (TargetAction.MISSING, TargetAction.DOWNGRADE, TargetAction.UPGRADE):
        reports = scan.for_target(action)
        if not reports:
            continue
        console.print(
            f"\n  [bold {TARGET_STYLE[action]}]{action.value}[/] [dim]({len(reports)})[/]"
        )
        console.print(_target_table(reports))


def _verdict(scan: ModScan) -> None:
    if scan.target:
        console.print()
        changes = scan.needs_change
        blockers = scan.blockers
        if not changes:
            console.print(f"[bold green]Every mod already runs on {scan.target}.[/]")
        elif blockers:
            console.print(
                f"[bold red]{len(blockers)} mod(s) have no {scan.target} build at all[/] "
                f"-- moving would mean dropping them. "
                f"{len(changes) - len(blockers)} other(s) would need swapping."
            )
        else:
            console.print(
                f"[yellow]{len(changes)} mod(s) would need swapping to run {scan.target}[/], "
                "but every one of them has a build."
            )
        return

    actionable = scan.actionable
    console.print()
    if actionable:
        console.print(f"[yellow]{len(actionable)} mod(s) worth attention.[/]")
        console.print(
            "[dim]Check an upgrade before attempting it: mc mods check --target 26.3[/]"
        )
    else:
        console.print("[bold green]Everything is on its newest build.[/]")


def show_urls(scan: ModScan, use_target: bool) -> None:
    """Plain, copy-pasteable output -- no table wrapping to mangle the URLs."""
    reports = [r for r in scan.reports if r.download_for(use_target)]
    if not reports:
        console.print("Nothing to download.")
        return
    console.print(f"[bold]{len(reports)} download(s)[/]\n")
    for report in reports:
        download = report.download_for(use_target)
        console.print(f"[bold]{report.mod.label}[/]  [dim]{report.mod.version_number} -> "
                      f"{report.target_version if use_target else report.latest_version}[/]")
        if report.mod.page:
            console.print(f"  [dim]{report.mod.page}[/]")
        console.print(f"  {download.url}")
        console.print()


def show_fetch(results: list[FetchResult], destination) -> None:
    fetched = [r for r in results if r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]
    failed = [r for r in results if not r.ok]

    for result in fetched:
        console.print(f"  [green]saved[/]   {result.download.filename} "
                      f"[dim]{human_bytes(result.download.size)}[/]")
    for result in skipped:
        console.print(f"  [dim]present {result.download.filename}[/]")
    for result in failed:
        console.print(f"  [red]failed[/]  {result.download.filename} [dim]{result.error}[/]")

    console.print()
    console.print(
        f"[bold green]{len(fetched)} downloaded[/], {len(skipped)} already present"
        + (f", [red]{len(failed)} failed[/]" if failed else "")
    )
    console.print(f"[dim]staged in {destination}[/]")
    console.print(
        "[dim]Nothing was installed yet. Review, then: mc mods install[/]"
    )


def show_plan(manifest: StageManifest) -> None:
    """What install is about to do, grouped by which mod set it touches."""
    console.print(f"[bold]{len(manifest.entries)} jar(s) staged[/]")
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("side", width=7)
    table.add_column("mod", overflow="fold")
    table.add_column("change", overflow="fold")
    for entry in manifest.entries:
        change = (
            f"{entry.from_version} -> {entry.to_version}"
            if entry.from_version and entry.to_version
            else entry.filename
        )
        table.add_row(entry.side, entry.label or entry.filename, change)
    console.print(table)


def show_install(results: list[InstallResult], archive) -> None:
    done = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    for result in done:
        console.print(f"  [green]installed[/] {result.staged.label or result.staged.filename}")
    for result in failed:
        console.print(
            f"  [red]failed[/]    {result.staged.label or result.staged.filename} "
            f"[dim]{result.error}[/]"
        )
    console.print()
    console.print(
        f"[bold green]{len(done)} installed[/]"
        + (f", [red]{len(failed)} failed[/]" if failed else "")
    )
    if any(r.archived for r in done):
        console.print(f"[dim]Replaced jars kept in {archive} -- delete when happy.[/]")


def show_manifests(results: list[tuple[Path, bool, int]], *, checking: bool) -> None:
    """Report what each manifest did. `results` is (path, differs, mods)."""
    for path, differs, count in results:
        if not differs:
            console.print(f"  [green]current[/]   {path} [dim]({count} mods)[/]")
        elif checking:
            console.print(f"  [red]stale[/]     {path} [dim]({count} mods installed)[/]")
        else:
            console.print(f"  [yellow]updated[/]   {path} [dim]({count} mods)[/]")

    stale = [path for path, differs, _ in results if differs]
    console.print()
    if not stale:
        console.print("[bold green]Manifests match what is installed.[/]")
    elif checking:
        console.print(
            f"[bold red]{len(stale)} manifest(s) out of date.[/] "
            "Run [bold]mc mods manifest[/] and commit the result."
        )
    else:
        console.print(f"[bold green]Wrote {len(stale)} manifest(s).[/] [dim]commit them[/]")
