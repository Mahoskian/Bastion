"""Tests for the client modpack builder. No network: the client is faked."""

import hashlib
import json
import zipfile

import pytest

from mcadmin.core.models import Paths
from mcadmin.core.mrpack import ModFile, PackBuilder, PackSpec


class FakeModrinth:
    """Resolves only the hashes it was told about, in one bulk call."""

    def __init__(self, known: dict[str, str] | None = None) -> None:
        self.known = known or {}
        self.calls = 0

    def download_urls(self, hashes) -> dict[str, str]:
        self.calls += 1
        return {sha1: self.known[sha1] for sha1 in hashes if sha1 in self.known}


@pytest.fixture
def builder(tmp_path):
    root = tmp_path / "server"
    mods = root / "client-install" / "mods"
    mods.mkdir(parents=True)
    (mods / "known.jar").write_bytes(b"known-mod")
    (mods / "unknown.jar").write_bytes(b"unknown-mod")
    shaders = root / "client-install" / "shaderpacks"
    shaders.mkdir(parents=True)
    (shaders / "pretty.zip").write_bytes(b"shader")

    known_sha = hashlib.sha1(b"known-mod").hexdigest()
    api = FakeModrinth({known_sha: "https://cdn.modrinth.com/known.jar"})
    spec = PackSpec(version="2.0.0")
    return PackBuilder(paths=Paths(server_dir=root), spec=spec, api=api)


def test_jars_are_discovered_and_sorted(builder):
    assert [p.name for p in builder.jars()] == ["known.jar", "unknown.jar"]


def test_resolve_splits_linked_from_bundled(builder):
    linked, bundled = builder.resolve(builder.jars())
    assert [m.name for m in linked] == ["known.jar"]
    assert [p.name for p in bundled] == ["unknown.jar"]
    assert linked[0].url == "https://cdn.modrinth.com/known.jar"
    assert linked[0].size == len(b"known-mod")


def test_resolve_reports_progress_for_every_jar(builder):
    seen: list[int] = []
    builder.resolve(builder.jars(), on_progress=lambda i, item: seen.append(i))
    assert seen == [1, 2]


def test_resolve_asks_modrinth_once_for_the_whole_set(builder):
    """One bulk call, not one per jar -- 162 jars would otherwise be 162
    requests and a politeness delay between each."""
    builder.resolve(builder.jars())
    assert builder.api.calls == 1


def test_spec_takes_versions_from_the_server_jar(tmp_path):
    (tmp_path / "fabric-server-mc.26.2-loader.0.19.3-launcher.1.1.2.jar").write_text("")
    spec = PackSpec.from_paths(Paths(server_dir=tmp_path), version="9.9.9")
    assert (spec.mc_version, spec.loader) == ("26.2", "0.19.3")
    assert spec.version == "9.9.9"


def test_an_explicit_version_overrides_the_one_in_the_jars_name(tmp_path):
    """`--mc` and `--loader` describe a pack for something this server is not
    running, so they have to win over what the launcher jar says -- passing
    both used to collide with the defaults and raise a TypeError."""
    (tmp_path / "fabric-server-mc.26.2-loader.0.19.3-launcher.1.1.2.jar").write_text("")
    spec = PackSpec.from_paths(Paths(server_dir=tmp_path), mc_version="26.1", loader="0.19.2")
    assert (spec.mc_version, spec.loader) == ("26.1", "0.19.2")


def test_manifest_entry_shape_is_what_modrinth_expects():
    mod = ModFile(name="a.jar", sha1="s1", sha512="s512", size=7, url="https://x/a.jar")
    entry = mod.manifest_entry()
    assert entry["path"] == "mods/a.jar"
    assert entry["downloads"] == ["https://x/a.jar"]
    assert entry["fileSize"] == 7
    assert entry["env"] == {"client": "required", "server": "unsupported"}


def test_index_records_the_dependencies(builder):
    index = builder.index([])
    assert index["dependencies"] == {"minecraft": "26.2", "fabric-loader": "0.19.3"}
    assert index["versionId"] == "2.0.0"
    assert index["formatVersion"] == 1


def test_written_pack_links_known_mods_and_bundles_the_rest(builder, tmp_path):
    linked, bundled = builder.resolve(builder.jars())
    result = builder.write(tmp_path / "out.mrpack", linked, bundled)

    with zipfile.ZipFile(result.path) as archive:
        names = set(archive.namelist())
        index = json.loads(archive.read("modrinth.index.json"))

    assert "overrides/mods/unknown.jar" in names, "unresolved mods must ship in the pack"
    assert "overrides/mods/known.jar" not in names, "resolved mods are CDN links, not payload"
    assert "overrides/shaderpacks/pretty.zip" in names
    assert [f["path"] for f in index["files"]] == ["mods/known.jar"]


def test_result_counts_everything(builder, tmp_path):
    linked, bundled = builder.resolve(builder.jars())
    result = builder.write(tmp_path / "out.mrpack", linked, bundled)
    assert result.total == 2
    assert result.size > 0


def test_default_output_is_dated(builder):
    assert builder.default_output().name.startswith("HammysServer-")
    assert builder.default_output().suffix == ".mrpack"


def test_no_jars_yields_nothing_to_resolve(tmp_path):
    root = tmp_path / "empty"
    (root / "client-install" / "mods").mkdir(parents=True)
    assert PackBuilder(paths=Paths(server_dir=root)).jars() == []


def test_exported_index_matches_the_one_inside_the_pack(builder, tmp_path):
    """The tracked copy is the pack's own manifest, not a second rendering.

    A file that only claimed to describe the pack would be the same drift the
    hand-written mod list already produced once.
    """
    linked, bundled = builder.resolve(builder.jars())
    result = builder.write(tmp_path / "out.mrpack", linked, bundled)
    exported = builder.export_index(linked, tmp_path / "modrinth.index.json")

    with zipfile.ZipFile(result.path) as archive:
        packed = archive.read("modrinth.index.json").decode()
    assert exported.read_text() == packed


def test_export_index_defaults_into_the_tracked_client_folder(builder):
    linked, _ = builder.resolve(builder.jars())
    written = builder.export_index(linked)
    assert written == builder.paths.client_index
    assert json.loads(written.read_text())["files"][0]["path"] == "mods/known.jar"


def test_exported_index_only_claims_mods_it_can_link(builder, tmp_path):
    """Bundled mods have no url to record; client-install/mods/README.md is
    what names them, and the index must not imply the set is complete."""
    linked, _ = builder.resolve(builder.jars())
    index = json.loads(builder.export_index(linked, tmp_path / "i.json").read_text())
    assert [f["path"] for f in index["files"]] == ["mods/known.jar"]
