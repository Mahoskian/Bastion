"""Building a Modrinth modpack (.mrpack) from the client mods folder.

Each jar is hashed and looked up on Modrinth by SHA1. Mods that resolve are
listed in the manifest as CDN downloads -- a small file and no redistribution.
Anything Modrinth does not know is bundled into overrides/mods/ instead, so the
pack still works.

The pack itself is usually not committable -- it embeds any shaderpacks whose
licences forbid reuploading -- so `export_index` writes the same manifest out
beside it. That file is small, carries no payload, and is what makes the client
set reproducible from a clone.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .models import Paths
from .modrinth import ModrinthApi


class PackSpec(BaseModel):
    """What the pack claims to be."""

    model_config = ConfigDict(frozen=True)

    name: str = "Hammys Custom Modded Minecraft Server"
    version: str = "1.0.0"
    mc_version: str = "26.2"
    loader: str = "0.19.3"

    @classmethod
    def from_paths(cls, paths: Paths, **overrides) -> PackSpec:
        """Take the versions from the server jar rather than hard-coding them.

        An override replaces what the jar said rather than colliding with it:
        `--mc` and `--loader` exist precisely to describe a pack for something
        other than what this server is running.
        """
        minecraft, loader = paths.versions()
        return cls(**{"mc_version": minecraft, "loader": loader, **overrides})

    @property
    def summary(self) -> str:
        return f"Client pack -- Fabric {self.loader} / Minecraft {self.mc_version}"


class ModFile(BaseModel):
    """A mod Modrinth hosts, referenced by URL rather than bundled."""

    model_config = ConfigDict(frozen=True)

    name: str
    sha1: str
    sha512: str
    size: int
    url: str

    def manifest_entry(self) -> dict:
        return {
            "path": f"mods/{self.name}",
            "hashes": {"sha1": self.sha1, "sha512": self.sha512},
            "env": {"client": "required", "server": "unsupported"},
            "downloads": [self.url],
            "fileSize": self.size,
        }


class PackResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    size: int
    linked: list[ModFile]
    bundled: list[Path]
    index_path: Path | None = None  # the tracked copy of the manifest, if written

    @property
    def total(self) -> int:
        return len(self.linked) + len(self.bundled)


class PackBuilder:
    """Resolves the client mods, then zips the pack."""

    def __init__(
        self,
        paths: Paths | None = None,
        spec: PackSpec | None = None,
        api: ModrinthApi | None = None,
    ) -> None:
        self.paths = paths or Paths.from_env()
        self.spec = spec or PackSpec()
        self.api = api or ModrinthApi()

    def jars(self) -> list[Path]:
        return sorted(self.paths.client_mods_dir.glob("*.jar"))

    def default_output(self) -> Path:
        return self.paths.mrpack_dir / f"HammysServer-{date.today():%Y-%m-%d}.mrpack"

    def resolve(
        self,
        jars: Iterable[Path],
        on_progress: Callable[[int, ModFile | Path], None] | None = None,
    ) -> tuple[list[ModFile], list[Path]]:
        """Hash every jar, then ask Modrinth about all of them at once."""
        digests: list[tuple[Path, bytes, str]] = []
        for index, path in enumerate(jars, 1):
            blob = path.read_bytes()
            digests.append((path, blob, hashlib.sha1(blob).hexdigest()))
            if on_progress:
                on_progress(index, path)

        urls = self.api.download_urls([sha1 for _, _, sha1 in digests])

        linked: list[ModFile] = []
        bundled: list[Path] = []
        for path, blob, sha1 in digests:
            url = urls.get(sha1)
            if url is None:
                bundled.append(path)
                continue
            linked.append(
                ModFile(
                    name=path.name,
                    sha1=sha1,
                    sha512=hashlib.sha512(blob).hexdigest(),
                    size=len(blob),
                    url=url,
                )
            )
        return linked, bundled

    def index(self, linked: list[ModFile]) -> dict:
        return {
            "formatVersion": 1,
            "game": "minecraft",
            "versionId": self.spec.version,
            "name": self.spec.name,
            "summary": self.spec.summary,
            "files": [mod.manifest_entry() for mod in linked],
            "dependencies": {
                "minecraft": self.spec.mc_version,
                "fabric-loader": self.spec.loader,
            },
        }

    def export_index(self, linked: list[ModFile], path: Path | None = None) -> Path:
        """Write the pack's manifest to a tracked path.

        Byte-for-byte the file that goes into the .mrpack, so the two can be
        diffed. It therefore describes only the mods Modrinth hosts: the
        bundled ones live in the pack's overrides and have no url to record.
        `mods/README.md` on the client side is what names those.
        """
        target = path or self.paths.client_index
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._manifest_json(linked))
        return target

    def _manifest_json(self, linked: list[ModFile]) -> str:
        return json.dumps(self.index(linked), indent=2) + "\n"

    def write(self, output: Path, linked: list[ModFile], bundled: list[Path]) -> PackResult:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp)
            (build / "modrinth.index.json").write_text(self._manifest_json(linked))

            if self.paths.shaderpacks_dir.is_dir():
                shaders = build / "overrides" / "shaderpacks"
                shaders.mkdir(parents=True, exist_ok=True)
                for pack in sorted(self.paths.shaderpacks_dir.glob("*.zip")):
                    shutil.copy(pack, shaders / pack.name)

            if bundled:  # mods Modrinth does not host
                mods = build / "overrides" / "mods"
                mods.mkdir(parents=True, exist_ok=True)
                for path in bundled:
                    shutil.copy(path, mods / path.name)

            output.unlink(missing_ok=True)
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(build.rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(build))

        return PackResult(
            path=output,
            size=output.stat().st_size,
            linked=linked,
            bundled=bundled,
        )
