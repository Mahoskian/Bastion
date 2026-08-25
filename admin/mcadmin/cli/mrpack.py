"""Client modpack build command."""

from __future__ import annotations

from pathlib import Path

import typer

from ..core.models import Paths
from ..core.mrpack import PackBuilder, PackSpec
from ..ui import mrpack as view
from ..ui.console import fail

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
) -> None:
    """Build a Modrinth .mrpack from the client mods folder."""
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

    linked, bundled = view.resolve_with_progress(builder, jars)
    result = builder.write(out or builder.default_output(), linked, bundled)
    if index:
        # The pack cannot be committed; its manifest can, and is what keeps the
        # client set reproducible from a clone.
        result = result.model_copy(update={"index_path": builder.export_index(linked)})
    view.show_result(result, builder.spec)
