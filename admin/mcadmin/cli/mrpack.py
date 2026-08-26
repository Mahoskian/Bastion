"""Client modpack build command."""

from __future__ import annotations

from pathlib import Path

import typer

from ..core.changelog import Changelog, ChangelogError, Release
from ..core.models import Paths
from ..core.mrpack import PackBuilder, PackResult, PackSpec
from ..core.notify import Attachment, DiscordBot, DiscordConfig, NotifyError
from ..ui import discord as embeds
from ..ui import mrpack as view
from ..ui.console import console, fail

app = typer.Typer()


@app.command()
def mrpack(
    version: str = typer.Option("1.0.0", "--version", help="Pack version; bump when mods change."),
    name: str = typer.Option(None, "--name", help="Pack display name."),
    out: Path = typer.Option(None, "--out", help="Output path."),
    mc: str = typer.Option(None, "--mc", help="Minecraft version the pack targets."),
    loader: str = typer.Option(None, "--loader", help="Fabric loader version."),
    index: bool = typer.Option(
        True, "--index/--no-index", help="Also refresh the tracked modrinth.index.json."
    ),
    force: bool = typer.Option(
        False, "--force", help="Build even when no mod has changed since the last release."
    ),
) -> None:
    """Build a Modrinth .mrpack, when the mods have changed since the last one.

    A build that would produce the same set of mods as the last release is not
    a release: nothing is built, nothing is written, and nothing is announced.
    `--force` builds anyway, which is what to reach for when the pack file
    itself is missing rather than out of date.
    """
    fields = {"version": version}
    if name:
        fields["name"] = name
    if mc:
        fields["mc_version"] = mc
    if loader:
        fields["loader"] = loader

    paths = Paths.from_env()
    builder = PackBuilder(paths=paths, spec=PackSpec.from_paths(paths, **fields))
    jars = builder.jars()
    if not jars:
        fail(f"no jars in {builder.paths.client_mods_dir}")

    # The mods folder is the pack, so what a build *would* contain is knowable
    # before spending a minute resolving and zipping it. Deciding here is what
    # lets an unchanged rebuild cost nothing at all.
    try:
        history = Changelog.load(paths.pack_history)
    except ChangelogError as exc:
        fail(str(exc))
    updated, release = history.record(sorted(path.name for path in jars), version)
    if not release.changed and not force:
        view.show_unchanged(history, paths.pack_changelog)
        return

    linked, bundled = view.resolve_with_progress(builder, jars)
    result = builder.write(out or builder.default_output(), linked, bundled)
    if index:
        # The pack cannot be committed; its manifest can, and is what keeps the
        # client set reproducible from a clone.
        result = result.model_copy(update={"index_path": builder.export_index(linked)})
    view.show_result(result, builder.spec)

    # Written only now: a build that raised leaves the history describing the
    # pack that still exists, rather than one that was never produced.
    recorded = updated.save(paths.pack_history)
    updated.export(paths.pack_changelog)
    view.show_changes(release, paths.pack_changelog, recorded)

    if release.changed:
        _announce(paths, result, release, builder.spec)


def _bot(paths: Paths) -> DiscordBot | None:
    """The bot to announce with, or None when nobody asked for announcements.

    The same rule `notifier_for` follows: an unconfigured server announces
    nothing and says nothing about it, while a config that exists and does not
    parse warns rather than taking the build down with it.
    """
    try:
        config = DiscordConfig.load(paths.notify_config)
    except NotifyError as exc:
        console.print(f"[yellow]Not announced[/] -- {exc}")
        return None
    if config is None or not config.enabled:
        return None
    return DiscordBot(config)


def _announce(paths: Paths, result: PackResult, release: Release, spec: PackSpec) -> None:
    """Post the release, carrying the pack and the manifest that describes it.

    The pack is what a player imports; the index is 80K of text naming the
    exact build of every mod in it, and is worth having in the channel when
    somebody asks six months from now what a release actually contained. It
    goes only when this build wrote it -- an index left over from an earlier
    build would describe a different pack than the one attached beside it.

    A failure here is reported and not fatal. The pack was built and the
    history was written; exiting non-zero would say otherwise, and the fix for
    a Discord outage is to post again, not to build again.
    """
    bot = _bot(paths)
    if bot is None:
        return

    pack = Attachment.read(result.path)
    files = [pack] if pack.fits else []
    if result.index_path is not None:
        files.append(Attachment.read(result.index_path))
    embed = embeds.pack_release(release, result, spec, attached=pack.fits)
    try:
        with console.status(f"Posting {result.path.name} to Discord..."):
            bot.post(embed, files)
    except NotifyError as exc:
        console.print(f"[bold red]Not announced[/] -- {exc}")
        return
    if pack.fits:
        console.print("[bold green]Announced[/] the release in Discord, with the pack attached.")
    else:
        console.print(
            "[yellow]Announced[/] the release in Discord without the pack -- it is over "
            "Discord's upload limit. Share the file another way."
        )
