"""Auditing the installed mods against Modrinth.

162 jars is not a set anyone hand-checks. Two questions matter: which mods have
a newer build, and -- before an upgrade -- which have no build for the version
you are moving to. The second is the one that decides whether an upgrade is
even possible.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .models import Paths
from .modrinth import ModrinthApi, ModrinthError

FABRIC = ("fabric",)


class ModStatus(StrEnum):
    CURRENT = "current"  # newest build for this Minecraft version
    OUTDATED = "outdated"  # a newer build exists
    BEHIND = "behind"  # installed build targets an older Minecraft
    NO_BUILD = "no-build"  # Modrinth knows it, but not for this Minecraft
    UNKNOWN = "unknown"  # Modrinth does not know this file at all

    @property
    def is_actionable(self) -> bool:
        return self in {ModStatus.OUTDATED, ModStatus.BEHIND, ModStatus.NO_BUILD}


class TargetAction(StrEnum):
    """What moving to the target Minecraft version would require of this mod."""

    COMPATIBLE = "compatible"  # the installed build already declares the target
    UPGRADE = "upgrade"  # a newer build is needed
    DOWNGRADE = "downgrade"  # an older build is needed
    MISSING = "missing"  # no build for the target exists at all

    @property
    def needs_change(self) -> bool:
        return self is not TargetAction.COMPATIBLE


class Download(BaseModel):
    """A file Modrinth will serve, and the hashes to check it against."""

    model_config = ConfigDict(frozen=True)

    url: str
    filename: str
    size: int = 0
    sha1: str = ""
    sha512: str = ""


def _download(version: dict | None) -> Download | None:
    if not version:
        return None
    for entry in version.get("files", []):
        if entry.get("primary", True) and entry.get("url"):
            hashes = entry.get("hashes", {})
            return Download(
                url=entry["url"],
                filename=entry.get("filename", ""),
                size=entry.get("size", 0),
                sha1=hashes.get("sha1", ""),
                sha512=hashes.get("sha512", ""),
            )
    return None


def _published(version: dict) -> datetime | None:
    raw = version.get("date_published")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class InstalledMod(BaseModel):
    """A jar on disk, plus whatever Modrinth knows about it."""

    model_config = ConfigDict(frozen=True)

    filename: str
    sha1: str
    size: int
    project_id: str = ""
    slug: str = ""
    name: str = ""
    version_number: str = ""
    game_versions: tuple[str, ...] = ()
    published: datetime | None = None
    url: str = ""  # CDN url for this exact file, when Modrinth hosts it

    @property
    def label(self) -> str:
        return self.name or self.filename

    @property
    def page(self) -> str:
        """The mod's page, for reading a changelog before swapping anything."""
        return f"https://modrinth.com/mod/{self.slug}" if self.slug else ""


class ModReport(BaseModel):
    """One mod's verdict."""

    model_config = ConfigDict(frozen=True)

    mod: InstalledMod
    status: ModStatus
    latest_version: str = ""
    target_version: str = ""
    target_action: TargetAction | None = None
    latest_download: Download | None = None
    target_download: Download | None = None

    def download_for(self, target: bool) -> Download | None:
        """The file to fetch: the target build, or the newest for this version."""
        return self.target_download if target else self.latest_download

    @property
    def target_detail(self) -> str:
        if self.target_action is TargetAction.COMPATIBLE:
            return "already compatible"
        if self.target_action is TargetAction.MISSING:
            return "no build exists"
        if self.target_version:
            return f"{self.mod.version_number} -> {self.target_version}"
        return ""

    @property
    def detail(self) -> str:
        if self.status is ModStatus.OUTDATED:
            return f"{self.mod.version_number} -> {self.latest_version}"
        if self.status is ModStatus.BEHIND:
            return f"built for {', '.join(self.mod.game_versions) or '?'}"
        if self.status is ModStatus.NO_BUILD:
            return "no matching build"
        if self.status is ModStatus.UNKNOWN:
            return "not on Modrinth"
        return self.mod.version_number


class ModScan(BaseModel):
    """The result of one audit."""

    model_config = ConfigDict(frozen=True)

    minecraft: str
    target: str = ""
    reports: list[ModReport] = []

    def of(self, *statuses: ModStatus) -> list[ModReport]:
        wanted = set(statuses)
        return [report for report in self.reports if report.status in wanted]

    @property
    def actionable(self) -> list[ModReport]:
        return [report for report in self.reports if report.status.is_actionable]

    def for_target(self, *actions: TargetAction) -> list[ModReport]:
        wanted = set(actions)
        return [report for report in self.reports if report.target_action in wanted]

    @property
    def blockers(self) -> list[ModReport]:
        """Mods with no build for the target -- what stops the move."""
        return self.for_target(TargetAction.MISSING)

    @property
    def needs_change(self) -> list[ModReport]:
        """Everything that would have to be swapped to run the target."""
        return [
            report
            for report in self.reports
            if report.target_action is not None and report.target_action.needs_change
        ]

    def counts(self) -> dict[ModStatus, int]:
        tally: dict[ModStatus, int] = {}
        for report in self.reports:
            tally[report.status] = tally.get(report.status, 0) + 1
        return tally


class ModScanner:
    """Hashes the jars, then asks Modrinth about all of them at once."""

    def __init__(self, paths: Paths | None = None, api: ModrinthApi | None = None) -> None:
        self.paths = paths or Paths.from_env()
        self.api = api or ModrinthApi()

    def jars(self, directory: Path | None = None) -> list[Path]:
        return sorted((directory or self.paths.mods_dir).glob("*.jar"))

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha1(path.read_bytes()).hexdigest()

    def scan(
        self,
        directory: Path | None = None,
        target: str = "",
        loaders: Sequence[str] = FABRIC,
        on_progress: Callable[[str], None] | None = None,
    ) -> ModScan:
        minecraft, _ = self.paths.versions()
        jars = self.jars(directory)
        if not jars:
            return ModScan(minecraft=minecraft, target=target)

        digests: dict[str, Path] = {}
        for path in jars:
            digests[self.digest(path)] = path
            if on_progress:
                on_progress(path.name)

        hashes = list(digests)
        known = self.api.versions_by_hash(hashes)
        latest = self.api.latest_by_hash(hashes, loaders, [minecraft])
        upgrade = (
            self.api.latest_by_hash(hashes, loaders, [target]) if target else {}
        )
        names = self._project_names(known.values())

        reports = [
            self._report(
                sha1, digests[sha1], known, latest, upgrade, names, minecraft, target
            )
            for sha1 in hashes
        ]
        return ModScan(
            minecraft=minecraft,
            target=target,
            reports=sorted(reports, key=lambda r: r.mod.label.lower()),
        )

    def _project_names(self, versions: Iterable[dict]) -> dict[str, tuple[str, str]]:
        """project id -> (title, slug)."""
        ids = {version.get("project_id", "") for version in versions}
        ids.discard("")
        if not ids:
            return {}
        return {
            project_id: (project.get("title", ""), project.get("slug", ""))
            for project_id, project in self.api.projects(sorted(ids)).items()
        }

    @staticmethod
    def _file_hash(version: dict) -> str:
        """The primary file's sha1, used to tell 'newest' from 'same'."""
        for entry in version.get("files", []):
            if entry.get("primary", True):
                return entry.get("hashes", {}).get("sha1", "")
        return ""

    @staticmethod
    def _file_url(version: dict, sha1: str) -> str:
        """The url for *this* file, which need not be the version's primary one.

        A manifest that rebuilds the folder has to name the jar that is actually
        installed; pointing at the primary file would silently substitute a
        different artefact for the handful of projects that ship several.
        """
        for entry in version.get("files", []):
            if entry.get("hashes", {}).get("sha1") == sha1:
                return entry.get("url", "")
        return ""

    def _report(
        self,
        sha1: str,
        path: Path,
        known: dict[str, dict],
        latest: dict[str, dict],
        upgrade: dict[str, dict],
        names: dict[str, tuple[str, str]],
        minecraft: str,
        target: str = "",
    ) -> ModReport:
        version = known.get(sha1)
        if version is None:
            mod = InstalledMod(filename=path.name, sha1=sha1, size=path.stat().st_size)
            # Nothing is known about it, so nothing can be said about a move.
            return ModReport(mod=mod, status=ModStatus.UNKNOWN)

        project_id = version.get("project_id", "")
        game_versions = tuple(version.get("game_versions", []))
        mod = InstalledMod(
            filename=path.name,
            sha1=sha1,
            size=path.stat().st_size,
            project_id=project_id,
            name=names.get(project_id, ("", ""))[0],
            slug=names.get(project_id, ("", ""))[1],
            version_number=version.get("version_number", ""),
            game_versions=game_versions,
            published=_published(version),
            url=self._file_url(version, sha1),
        )
        if target and target in game_versions:
            target_version, target_action = mod.version_number, TargetAction.COMPATIBLE
        elif target:
            target_version, target_action = self._target_plan(sha1, mod, upgrade)
        else:
            target_version, target_action = "", None

        newest = latest.get(sha1)
        if newest is None:
            status = ModStatus.NO_BUILD
            latest_version = ""
        elif self._file_hash(newest) == sha1:
            status = ModStatus.CURRENT
            latest_version = mod.version_number
        else:
            status = ModStatus.OUTDATED
            latest_version = newest.get("version_number", "")

        # A build that never claimed this Minecraft version is a separate
        # problem from being merely out of date, and a louder one.
        if game_versions and minecraft not in game_versions:
            status = ModStatus.BEHIND

        return ModReport(
            mod=mod,
            status=status,
            latest_version=latest_version,
            target_version=target_version,
            target_action=target_action,
            latest_download=_download(newest) if status is ModStatus.OUTDATED else None,
            target_download=(
                _download(upgrade.get(sha1))
                if target_action in (TargetAction.UPGRADE, TargetAction.DOWNGRADE)
                else None
            ),
        )

    @staticmethod
    def _target_plan(
        sha1: str, mod: InstalledMod, upgrade: dict[str, dict]
    ) -> tuple[str, TargetAction | None]:
        """What the target Minecraft version would require of this mod.

        The installed jar frequently declares several Minecraft versions at
        once, so the first question is whether it already covers the target --
        in which case nothing needs to change, whatever else Modrinth offers.
        Direction comes from the publish dates, since version strings are not
        comparable across projects.
        """
        candidate = upgrade.get(sha1)
        if candidate is None:
            return "", TargetAction.MISSING
        number = candidate.get("version_number", "")
        published = _published(candidate)
        if mod.published and published:
            direction = (
                TargetAction.DOWNGRADE if published < mod.published else TargetAction.UPGRADE
            )
        else:
            direction = TargetAction.UPGRADE
        return number, direction


class FetchResult(BaseModel):
    """One attempted download."""

    model_config = ConfigDict(frozen=True)

    download: Download
    path: Path | None = None
    skipped: bool = False  # already staged and verified
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class ModFetcher:
    """Downloads jars into a staging directory.

    Deliberately never writes into `mods/`. Swapping a mod on a live modded
    server is a judgement call -- which build, whether the world survives it,
    what else depends on it -- so this stages the files and stops.
    """

    def __init__(self, destination: Path, api: ModrinthApi | None = None) -> None:
        self.destination = destination
        self.api = api or ModrinthApi()

    @staticmethod
    def verify(blob: bytes, download: Download) -> str:
        """Empty string when the bytes match what Modrinth promised."""
        if download.sha512 and hashlib.sha512(blob).hexdigest() != download.sha512:
            return "sha512 mismatch"
        if download.sha1 and hashlib.sha1(blob).hexdigest() != download.sha1:
            return "sha1 mismatch"
        return ""

    def _already_staged(self, path: Path, download: Download) -> bool:
        if not path.exists():
            return False
        return not self.verify(path.read_bytes(), download)

    def fetch(
        self,
        downloads: Sequence[Download],
        on_progress: Callable[[int, Download], None] | None = None,
    ) -> list[FetchResult]:
        results: list[FetchResult] = []
        if downloads:
            self.destination.mkdir(parents=True, exist_ok=True)
        for index, download in enumerate(downloads, 1):
            results.append(self._one(download))
            if on_progress:
                on_progress(index, download)
        return results

    def _one(self, download: Download) -> FetchResult:
        target = self.destination / (download.filename or download.url.rsplit("/", 1)[-1])
        if self._already_staged(target, download):
            return FetchResult(download=download, path=target, skipped=True)
        try:
            blob = self.api.fetch_bytes(download.url)
        except ModrinthError as exc:
            return FetchResult(download=download, error=str(exc))

        problem = self.verify(blob, download)
        if problem:
            # A jar that does not match its hash is never written to disk.
            return FetchResult(download=download, error=problem)
        target.write_bytes(blob)
        return FetchResult(download=download, path=target)


MANIFEST_NAME = "manifest.json"


class StagedMod(BaseModel):
    """A downloaded jar and the exact file it is meant to replace.

    Recording the replacement is what makes install unambiguous. Modrinth's
    client_side/server_side fields cannot decide it: across this server's mods
    most projects declare "required" on both sides, so they say nothing about
    which directory a given jar belongs in. Where the existing jar lives does.
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    destination: Path
    replaces: Path | None = None
    label: str = ""
    from_version: str = ""
    to_version: str = ""
    sha1: str = ""
    sha512: str = ""

    @property
    def side(self) -> str:
        return "client" if "client-install" in str(self.destination) else "server"


class StageManifest(BaseModel):
    """What one fetch staged, so install can act on it later."""

    created: datetime
    minecraft: str = ""
    target: str = ""
    entries: list[StagedMod] = []

    @classmethod
    def load(cls, directory: Path) -> StageManifest | None:
        path = directory / MANIFEST_NAME
        try:
            return cls.model_validate_json(path.read_text())
        except (OSError, ValueError):
            return None

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / MANIFEST_NAME).write_text(self.model_dump_json(indent=2))


class InstallResult(BaseModel):
    """One attempted swap."""

    model_config = ConfigDict(frozen=True)

    staged: StagedMod
    installed: Path | None = None
    archived: Path | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class ModInstaller:
    """Moves staged jars into place, keeping what they replaced.

    The old jar is archived rather than deleted: a mod update that breaks the
    world needs to be undoable without a full restore.
    """

    def __init__(self, staging: Path, archive: Path) -> None:
        self.staging = staging
        self.archive = archive

    def install(self, entries: Sequence[StagedMod]) -> list[InstallResult]:
        """Install every entry, then clear the staged files that are fully done.

        One staged jar can serve several destinations -- a mod present in both
        the server and client sets shares a single download -- so entries copy
        from staging rather than moving out of it.
        """
        results = [self._one(entry) for entry in entries]

        failed = {r.staged.filename for r in results if not r.ok}
        for filename in {r.staged.filename for r in results if r.ok} - failed:
            (self.staging / filename).unlink(missing_ok=True)
        return results

    def _one(self, entry: StagedMod) -> InstallResult:
        source = self.staging / entry.filename
        if not source.exists():
            return InstallResult(staged=entry, error="not staged -- run 'mc mods fetch' first")
        if entry.sha512 and hashlib.sha512(source.read_bytes()).hexdigest() != entry.sha512:
            return InstallResult(staged=entry, error="staged file does not match its hash")
        if not entry.destination.is_dir():
            return InstallResult(staged=entry, error=f"no directory {entry.destination}")

        archived: Path | None = None
        if entry.replaces and entry.replaces.exists():
            self.archive.mkdir(parents=True, exist_ok=True)
            archived = self.archive / entry.replaces.name
            entry.replaces.replace(archived)

        installed = entry.destination / entry.filename
        try:
            shutil.copy2(source, installed)
        except OSError as exc:
            # Put the old jar back rather than leaving the set with neither.
            if archived is not None and entry.replaces is not None:
                archived.replace(entry.replaces)
            return InstallResult(staged=entry, error=str(exc))
        return InstallResult(staged=entry, installed=installed, archived=archived)
