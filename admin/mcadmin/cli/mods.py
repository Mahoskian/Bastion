"""Mod audit commands."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from ..core.manifest import Manifest
from ..core.models import Paths
from ..core.modrinth import ModrinthApi, ModrinthError
from ..core.mods import (
    ModFetcher,
    ModInstaller,
    ModScanner,
    StagedMod,
    StageManifest,
)
from ..core.units import human_bytes
from ..ui import mods as view
from ..ui.console import console, fail
from .server import controller

app = typer.Typer(no_args_is_help=True, help="Audit the installed mods against Modrinth.")

SIDES = ("server", "client", "both")


def _sources(paths: Paths, side: str) -> list[Path]:
    """Which mod directories a command should act on.

    Modrinth cannot tell us where a jar belongs -- most projects declare
    themselves required on both sides -- so the directory a mod already lives
    in is what decides where its update goes.
    """
    if side not in SIDES:
        fail(f"--side must be one of {', '.join(SIDES)}")
    chosen: list[Path] = []
    if side in ("server", "both"):
        chosen.append(paths.mods_dir)
    if side in ("client", "both"):
        chosen.append(paths.client_mods_dir)
    return chosen


@app.command()
def check(
    target: str = typer.Option(
        None, "--target", help="Check whether every mod has a build for this Minecraft version."
    ),
    client: bool = typer.Option(False, "--client", help="Audit client-install/mods instead."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="List up-to-date mods too."),
    urls: bool = typer.Option(False, "--urls", help="Print download URLs instead of a report."),
) -> None:
    """Find outdated mods, and mods that would block a Minecraft upgrade."""
    paths = Paths.from_env()
    directory = paths.client_mods_dir if client else paths.mods_dir
    if not directory.is_dir():
        fail(f"no mods directory at {directory}")

    scanner = ModScanner(paths)
    if not scanner.jars(directory):
        fail(f"no jars in {directory}")

    if target:
        try:
            known = ModrinthApi().game_versions()
        except ModrinthError as exc:
            fail(str(exc))
        if known and target not in known:
            # Without this, a typo reports every mod as a blocker.
            console.print(
                f"[yellow]Modrinth has no Minecraft {target}.[/] Newest release is "
                f"[bold]{known[0]}[/]; nothing will have a build for {target}."
            )

    try:
        scan = view.scan_with_progress(scanner, directory, target=target or "")
    except ModrinthError as exc:
        fail(str(exc))
    if urls:
        view.show_urls(scan, use_target=bool(target))
        return
    view.show_scan(scan, verbose=verbose)


@app.command()
def manifest(
    side: str = typer.Option(
        "both", "--side", help="Which manifest to write: server, client, or both."
    ),
    check: bool = typer.Option(
        False, "--check", help="Report drift and exit non-zero. Writes nothing."
    ),
) -> None:
    """Regenerate the tracked mod manifests from what is actually installed.

    The jars themselves cannot be committed, so these READMEs are what a clone
    has to rebuild them from. Run this after any mod change; `--check` is the
    same comparison without the write, for confirming the tree is honest.
    """
    paths = Paths.from_env()
    _, loader = paths.versions()
    scanner = ModScanner(paths)

    results: list[tuple[Path, bool, int]] = []
    for directory in _sources(paths, side):
        if not directory.is_dir():
            fail(f"no mods directory at {directory}")
        if not scanner.jars(directory):
            fail(f"no jars in {directory}")
        try:
            scan = view.scan_with_progress(scanner, directory)
        except ModrinthError as exc:
            fail(str(exc))

        build = Manifest.for_client if directory == paths.client_mods_dir else Manifest.for_server
        rendered = build(scan, loader)
        target = paths.mods_manifest(directory)
        differs = not rendered.matches(target) if check else rendered.write(target)
        results.append((target, differs, len(scan.reports)))

    view.show_manifests(results, checking=check)
    if check and any(differs for _, differs, _ in results):
        raise typer.Exit(1)


@app.command()
def fetch(
    target: str = typer.Option(None, "--target", help="Fetch builds for this Minecraft version."),
    side: str = typer.Option(
        "both", "--side", help="Which mod set to update: server, client, or both."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Download updated jars into fetch-mods/. Never installs them."""
    paths = Paths.from_env()
    sources = _sources(paths, side)
    scanner = ModScanner(paths)

    entries: list[StagedMod] = []
    downloads = []
    minecraft = ""
    for directory in sources:
        if not directory.is_dir():
            continue
        try:
            scan = view.scan_with_progress(scanner, directory, target=target or "")
        except ModrinthError as exc:
            fail(str(exc))
        minecraft = scan.minecraft
        for report in scan.reports:
            download = report.download_for(bool(target))
            if download is None:
                continue
            downloads.append(download)
            entries.append(
                StagedMod(
                    filename=download.filename,
                    destination=directory,
                    replaces=directory / report.mod.filename,
                    label=report.mod.label,
                    from_version=report.mod.version_number,
                    to_version=report.target_version if target else report.latest_version,
                    sha1=download.sha1,
                    sha512=download.sha512,
                )
            )

    if not downloads:
        console.print("Nothing to download -- everything is already on the right build.")
        return

    staging = paths.fetch_dir
    total = sum(d.size for d in downloads)
    console.print(
        f"[bold]{len(downloads)}[/] jar(s), {human_bytes(total)} -> [bold]{staging}[/]"
    )
    if not yes and console.input("Download? [y/N] ").strip().lower() not in ("y", "yes"):
        console.print("Aborted.")
        raise typer.Exit(1)

    with console.status("Downloading..."):
        results = ModFetcher(staging).fetch(downloads)
    ok = {r.download.filename for r in results if r.ok}
    StageManifest(
        created=datetime.now(),
        minecraft=minecraft,
        target=target or "",
        entries=[e for e in entries if e.filename in ok],
    ).save(staging)
    view.show_fetch(results, staging)


@app.command()
def install(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Install the staged jars, archiving whatever they replace."""
    paths = Paths.from_env()
    manifest = StageManifest.load(paths.fetch_dir)
    if manifest is None or not manifest.entries:
        fail(f"nothing staged in {paths.fetch_dir} -- run 'mc mods fetch' first")

    if controller().state().is_live:
        fail(
            "the server is running -- stop it first.\n"
            "        Swapping mods under a live server will not take effect and can corrupt saves."
        )

    view.show_plan(manifest)
    console.print(
        f"\n[dim]Replaced jars are kept in {paths.replaced_dir}, so this is undoable.[/]"
    )
    console.print("[yellow]Take a snapshot first if you have not: mc snapshot now[/]")
    if not yes and console.input("\nType [bold]install[/] to proceed: ").strip() != "install":
        console.print("Aborted.")
        raise typer.Exit(1)

    results = ModInstaller(paths.fetch_dir, paths.replaced_dir).install(manifest.entries)
    view.show_install(results, paths.replaced_dir)

    remaining = [e for e, r in zip(manifest.entries, results, strict=False) if not r.ok]
    manifest.model_copy(update={"entries": remaining}).save(paths.fetch_dir)
