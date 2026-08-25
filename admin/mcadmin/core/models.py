"""Typed configuration, JVM options, and runtime state.

Everything the lifecycle code needs to know about *where things are* and *how
the JVM is launched* lives here, so the supervisor, the controller and the CLI
all read one definition rather than three.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

# This file lives at <server>/admin/mcadmin/core/models.py, so the server
# directory is four parents up. Deriving it beats hardcoding a path that
# breaks the moment the server directory moves; MC_SERVER_DIR still wins.
DEFAULT_SERVER_DIR = Path(__file__).resolve().parents[3]
# Inside the server directory, but outside everything a snapshot covers -- see
# INCLUDES in core.repository -- so the repository never tries to snapshot itself.
DEFAULT_BACKUP_DIR = DEFAULT_SERVER_DIR / "backups"
SESSION_NAME = "minecraft"
# fabric-server-mc.26.2-loader.0.19.3-launcher.1.1.2.jar
JAR_VERSION_RE = re.compile(r"mc\.(?P<minecraft>[\d.]+?)-loader\.(?P<loader>[\d.]+?)-")


class ServerState(StrEnum):
    """What the server is doing right now."""

    RUNNING = "running"
    BOOTING = "booting"  # JVM up, not yet answering RCON
    ORPHANED = "orphaned"  # tmux session alive with no JVM in it
    STOPPED = "stopped"

    @property
    def is_live(self) -> bool:
        """True when a JVM exists, whether or not it is ready for players."""
        return self in {ServerState.RUNNING, ServerState.BOOTING}

    @property
    def description(self) -> str:
        return {
            ServerState.RUNNING: "running",
            ServerState.BOOTING: "booting (not answering RCON yet)",
            ServerState.ORPHANED: "stopped, but its tmux session is still open",
            ServerState.STOPPED: "stopped",
        }[self]


class Paths(BaseModel):
    """Where everything lives. Frozen: nothing should relocate mid-run."""

    model_config = ConfigDict(frozen=True)

    server_dir: Path = DEFAULT_SERVER_DIR
    backup_dir: Path = DEFAULT_BACKUP_DIR

    @classmethod
    def from_env(cls) -> Paths:
        """MC_BACKUP_DIR wins; otherwise backups sit inside whichever server
        directory is in play, so an overridden MC_SERVER_DIR stays coherent."""
        server_dir = Path(os.environ.get("MC_SERVER_DIR", DEFAULT_SERVER_DIR))
        override = os.environ.get("MC_BACKUP_DIR")
        return cls(
            server_dir=server_dir,
            backup_dir=Path(override) if override else server_dir / "backups",
        )

    @property
    def admin_dir(self) -> Path:
        return self.server_dir / "admin"

    @property
    def logs_dir(self) -> Path:
        return self.server_dir / "logs"

    @property
    def latest_log(self) -> Path:
        return self.logs_dir / "latest.log"

    @property
    def properties_file(self) -> Path:
        return self.server_dir / "server.properties"

    @property
    def runtime_file(self) -> Path:
        return self.admin_dir / ".runtime.json"

    @property
    def mods_dir(self) -> Path:
        return self.server_dir / "mods"

    @property
    def fetch_dir(self) -> Path:
        """Staging for fetched jars. Never `mods/` -- installing is a decision."""
        return self.server_dir / "fetch-mods"

    @property
    def replaced_dir(self) -> Path:
        """Where install puts the jars it swaps out, so a bad build is undoable."""
        return self.fetch_dir / "replaced"

    def versions(self) -> tuple[str, str]:
        """(minecraft, loader), read from the launcher jar's name.

        The jar is the only thing that actually knows, so nothing else should
        carry a hard-coded copy of these.
        """
        match = JAR_VERSION_RE.search(self.jar().name)
        if match is None:
            raise ValueError(f"cannot read versions from {self.jar().name}")
        return match["minecraft"], match["loader"]

    @property
    def metrics_db(self) -> Path:
        """Time series of samples and recorded GC analyses.

        GC logs rotate away after ~50M, so anything worth comparing across
        weeks has to be copied out of them before they roll.
        """
        return self.admin_dir / ".metrics.db"

    @property
    def log_baseline(self) -> Path:
        """Fingerprints of entries already reported, so a digest can show what
        is new rather than merely what is frequent."""
        return self.admin_dir / ".log-baseline.db"

    # -- backups

    @property
    def restic_repo(self) -> Path:
        return self.backup_dir / "restic"

    @property
    def restic_password_file(self) -> Path:
        """Lives beside the repo it unlocks.

        That is fine while both are on this disk -- anyone holding the repo
        holds the disk anyway. Copy this file alongside the repo if it is ever
        moved offsite, or the snapshots are unrecoverable.
        """
        return self.backup_dir / ".restic-password"

    @property
    def paused_flag(self) -> Path:
        """Set by `mc snapshot pause`; scheduled runs skip while it exists."""
        return self.backup_dir / ".paused"

    # -- client pack

    @property
    def client_mods_dir(self) -> Path:
        return self.server_dir / "client-install" / "mods"

    @property
    def shaderpacks_dir(self) -> Path:
        return self.server_dir / "client-install" / "shaderpacks"

    @property
    def client_index(self) -> Path:
        """The client pack's Modrinth manifest, kept in git.

        The pack itself is not committed -- it embeds shaderpacks that may not
        be redistributed -- but this index names every mod in it by CDN url and
        hash, so the pack can be rebuilt from a clone.
        """
        return self.server_dir / "client-install" / "modrinth.index.json"

    def mods_manifest(self, mods_dir: Path) -> Path:
        """The tracked README that stands in for the jars in `mods_dir`."""
        return mods_dir / "README.md"

    @property
    def mrpack_dir(self) -> Path:
        return self.server_dir / "mrpacks"

    def jar(self) -> Path:
        """The Fabric launcher jar.

        Globbed rather than hard-coded so a loader upgrade does not need a code
        change -- but ambiguity is an error, not a coin flip.
        """
        found = sorted(self.server_dir.glob("fabric-server-*.jar"))
        if not found:
            raise FileNotFoundError(f"no fabric-server-*.jar in {self.server_dir}")
        if len(found) > 1:
            names = ", ".join(p.name for p in found)
            raise FileNotFoundError(f"several server jars in {self.server_dir}: {names}")
        return found[0]


class JvmOptions(BaseModel):
    """The JVM command line, as data.

    The GC flags are the Aikar set. Xms == Xmx is
    deliberate: it removes heap-resize pauses.
    """

    model_config = ConfigDict(frozen=True)

    heap: str = Field(default="12G", pattern=r"^\d+[MG]$")
    gc_log_files: int = Field(default=5, ge=1)
    gc_log_size: str = Field(default="10M", pattern=r"^\d+[MG]$")
    extra: tuple[str, ...] = ()

    @field_validator("heap")
    @classmethod
    def _sane_heap(cls, value: str) -> str:
        megabytes = int(value[:-1]) * (1024 if value.endswith("G") else 1)
        if megabytes < 512:
            raise ValueError(f"heap {value} is too small to boot a modded server")
        return value

    @property
    def gc_flags(self) -> tuple[str, ...]:
        return (
            "-XX:+UseG1GC",
            "-XX:+ParallelRefProcEnabled",
            "-XX:MaxGCPauseMillis=200",
            "-XX:+UnlockExperimentalVMOptions",
            "-XX:+DisableExplicitGC",
            "-XX:+AlwaysPreTouch",
            "-XX:G1NewSizePercent=30",
            "-XX:G1MaxNewSizePercent=40",
            "-XX:G1HeapRegionSize=8M",
            "-XX:G1ReservePercent=20",
            "-XX:G1HeapWastePercent=5",
            "-XX:G1MixedGCCountTarget=4",
            "-XX:InitiatingHeapOccupancyPercent=15",
            "-XX:G1MixedGCLiveThresholdPercent=90",
            "-XX:G1RSetUpdatingPauseTimePercent=5",
            "-XX:SurvivorRatio=32",
            "-XX:+PerfDisableSharedMem",
            "-XX:MaxTenuringThreshold=1",
        )

    def gc_log_flag(self, logs_dir: Path) -> str:
        """Rotating GC logs -- the input `mc gc` reads back."""
        target = logs_dir / "gc.log"
        return (
            f"-Xlog:gc*,gc+heap=debug,safepoint:file={target}"
            f":time,uptime,level,tags:filecount={self.gc_log_files},filesize={self.gc_log_size}"
        )

    def command(self, jar: Path, logs_dir: Path, java: str = "java") -> list[str]:
        return [
            java,
            f"-Xms{self.heap}",
            f"-Xmx{self.heap}",
            *self.gc_flags,
            self.gc_log_flag(logs_dir),
            *self.extra,
            "-jar",
            str(jar),
            "nogui",
        ]


class RuntimeState(BaseModel):
    """What the supervisor publishes about itself, for the CLI to read."""

    session: str = SESSION_NAME
    supervisor_pid: int
    jvm_pid: int | None = None
    heap: str = "12G"
    started_at: datetime = Field(default_factory=datetime.now)
    restarts: int = 0

    @classmethod
    def load(cls, path: Path) -> RuntimeState | None:
        """None when absent or unreadable -- a stale file must never be fatal."""
        try:
            return cls.model_validate_json(path.read_text())
        except (OSError, ValueError):
            return None

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2))
