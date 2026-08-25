"""A restic repository: deduplicated, verifiable snapshots.

The tar archives this replaces were ~99% redundant -- a 6.4G world produced a
2.8G archive every hour, and consecutive archives differed by about 1.5KB of
compressed output. restic chunks content, so an hourly snapshot costs only the
chunks that actually changed.

Everything here shells out to the restic binary and parses its `--json`
output; nothing reimplements the format.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Paths

BINARY = "restic"

# What a snapshot covers, relative to the server root: Minecraft state only.
# The admin tooling is source and belongs in git -- but its two databases are
# not, and nothing else would hold them, so they are named individually.
INCLUDES = (
    "world",
    "config",
    "mods/luckperms",
    "server.properties",
    "ops.json",
    "whitelist.json",
    "banned-players.json",
    "banned-ips.json",
    "usercache.json",
    "admin/.metrics.db",
    "admin/.log-baseline.db",
)

# Anchored: made absolute before restic sees them, so they match one path only.
EXCLUDES = (
    "world/session.lock",  # runtime lock, meaningless in a snapshot
    "mods/luckperms/libs",  # re-downloaded on demand
)

# Bare names, matched at any depth. The DH caches are why this distinction
# matters: they are derived LOD data that regenerates itself, and the overworld
# one alone is ~1G buried under world/dimensions/.
EXCLUDE_NAMES = (
    "DistantHorizons.sqlite",
    "DistantHorizons.sqlite-wal",
    "DistantHorizons.sqlite-shm",
    "*.pyc",
    "__pycache__",
)

# Shorter than this a selector means an index, not an id prefix.
MIN_PREFIX = 4
# restic prints RFC3339 with nanoseconds; datetime handles at most microseconds.
_SUBSECOND = re.compile(r"(\.\d{6})\d+")


class ResticError(RuntimeError):
    """A restic invocation failed. Carries the command's own message."""


class ResticMissing(ResticError):
    pass


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(_SUBSECOND.sub(r"\1", value))


class RetentionPolicy(BaseModel):
    """Tiered retention: hourly for a day, daily for a week, weekly for a month."""

    model_config = ConfigDict(frozen=True)

    hourly: int = Field(default=12, ge=0)
    daily: int = Field(default=7, ge=0)
    weekly: int = Field(default=4, ge=0)
    monthly: int = Field(default=0, ge=0)

    def forget_args(self) -> list[str]:
        args: list[str] = []
        for name in ("hourly", "daily", "weekly", "monthly"):
            count = getattr(self, name)
            if count:
                args += [f"--keep-{name}", str(count)]
        # Without this a repo with no matching snapshot could be emptied.
        return args or ["--keep-last", "1"]


class Snapshot(BaseModel):
    """One point-in-time snapshot."""

    model_config = ConfigDict(frozen=True)

    id: str
    short_id: str
    time: datetime
    hostname: str = ""
    paths: list[Path] = []
    tags: list[str] = []

    @field_validator("time", mode="before")
    @classmethod
    def _trim_nanoseconds(cls, value: object) -> object:
        return _parse_time(value) if isinstance(value, str) else value

    @property
    def common_parent(self) -> Path:
        """The directory restic strips when restoring this snapshot.

        restic records absolute paths but restores them with their common
        parent removed, so a snapshot of /srv/world plus /srv/config comes
        back as <target>/world and <target>/config. Knowing that directory is
        the difference between restoring over the server and scattering it
        across the filesystem root.
        """
        if not self.paths:
            raise ResticError(f"snapshot {self.short_id} records no paths")
        if len(self.paths) == 1:
            return self.paths[0].parent
        return Path(os.path.commonpath([str(path) for path in self.paths]))


class SnapshotSummary(BaseModel):
    """What one `restic backup` run actually did."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str = ""
    files_new: int = 0
    files_changed: int = 0
    files_unmodified: int = 0
    total_files_processed: int = 0
    total_bytes_processed: int = 0
    data_added: int = 0
    total_duration: float = 0.0

    @property
    def dedup_ratio(self) -> float:
        """Bytes read per byte actually stored. The whole point of this module."""
        if not self.data_added:
            return float(self.total_bytes_processed) or 0.0
        return self.total_bytes_processed / self.data_added


class RepoStats(BaseModel):
    """Physical vs logical size -- the gap between them is the dedup win."""

    model_config = ConfigDict(frozen=True)

    total_size: int = 0  # bytes actually occupied by the repository
    restore_size: int = 0  # bytes a full restore of every snapshot would write
    total_file_count: int = 0
    snapshots: int = 0

    @property
    def dedup_ratio(self) -> float:
        return self.restore_size / self.total_size if self.total_size else 0.0


class ResticRepository:
    """Typed access to one restic repository."""

    def __init__(
        self,
        paths: Paths | None = None,
        retention: RetentionPolicy | None = None,
        binary: str = BINARY,
    ) -> None:
        self.paths = paths or Paths.from_env()
        self.retention = retention or RetentionPolicy()
        self.binary = binary

    # ------------------------------------------------------------- plumbing

    @staticmethod
    def available(binary: str = BINARY) -> bool:
        import shutil

        return shutil.which(binary) is not None

    @property
    def location(self) -> Path:
        return self.paths.restic_repo

    @property
    def password_file(self) -> Path:
        return self.paths.restic_password_file

    def _env(self) -> dict[str, str]:
        import os

        return {
            **os.environ,
            "RESTIC_REPOSITORY": str(self.location),
            "RESTIC_PASSWORD_FILE": str(self.password_file),
        }

    def _run(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if not self.available(self.binary):
            raise ResticMissing(
                f"{self.binary} is not installed -- `sudo apt install restic`"
            )
        result = subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            env=self._env(),
            cwd=str(cwd) if cwd else None,
        )
        if check and result.returncode != 0:
            message = (result.stderr.strip() or result.stdout.strip()).splitlines()
            raise ResticError(message[-1] if message else f"restic {args[0]} failed")
        return result

    # ------------------------------------------------------------- repo

    def exists(self) -> bool:
        if not self.available(self.binary) or not self.password_file.exists():
            return False
        return self._run(["cat", "config"], check=False).returncode == 0

    def init(self) -> None:
        """Create the repository and the password that unlocks it."""
        if self.exists():
            raise ResticError(f"a repository already exists at {self.location}")
        self.location.parent.mkdir(parents=True, exist_ok=True)
        if not self.password_file.exists():
            self.password_file.write_text(secrets.token_urlsafe(32) + "\n")
            self.password_file.chmod(0o600)
        self._run(["init"])

    # ------------------------------------------------------------- backup

    def exclude_args(self) -> list[str]:
        """Build the --exclude arguments.

        restic matches a pattern containing a slash against the whole path, so
        anchored entries are made absolute; bare names already match at any
        depth.
        """
        args: list[str] = []
        for anchored in EXCLUDES:
            args += ["--exclude", str(self.paths.server_dir / anchored)]
        for name in EXCLUDE_NAMES:
            args += ["--exclude", name]
        return args

    def backup(
        self,
        includes: Iterable[str] = INCLUDES,
        tags: Iterable[str] = ("mc",),
    ) -> SnapshotSummary:
        """Snapshot the server. Paths are relative to the server directory."""
        targets = [name for name in includes if (self.paths.server_dir / name).exists()]
        if not targets:
            raise ResticError("nothing to back up -- none of the included paths exist")
        args = ["backup", "--json", *self.exclude_args()]
        for tag in tags:
            args += ["--tag", tag]
        result = self._run([*args, *targets], cwd=self.paths.server_dir)
        return self._summary(result.stdout)

    @staticmethod
    def _summary(stdout: str) -> SnapshotSummary:
        """restic streams progress as JSON lines; the last one is the summary."""
        for line in reversed(stdout.strip().splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("message_type") == "summary":
                return SnapshotSummary.model_validate(payload)
        raise ResticError("restic produced no summary -- did the backup run?")

    # ------------------------------------------------------------- read

    def snapshots(self, tag: str | None = None) -> list[Snapshot]:
        """Newest first."""
        args = ["snapshots", "--json"]
        if tag:
            args += ["--tag", tag]
        payload = json.loads(self._run(args).stdout or "[]")
        found = [Snapshot.model_validate(entry) for entry in payload]
        return sorted(found, key=lambda s: s.time, reverse=True)

    def resolve(self, selector: str) -> Snapshot:
        """Accept 'latest', a 1-based index, or a snapshot id.

        An exact short_id is matched before an index on purpose: a short_id is
        eight hex characters, so roughly one in forty is all digits, and
        reading it as an index would make that snapshot unaddressable.
        """
        found = self.snapshots()
        if not found:
            raise LookupError("no snapshots yet -- run 'mc snapshot now'")
        if selector in ("", "latest"):
            return found[0]
        # An exact short_id wins outright, so an all-digit one stays reachable.
        for snapshot in found:
            if snapshot.short_id == selector:
                return snapshot
        if selector.isdigit():
            index = int(selector) - 1
            if 0 <= index < len(found):
                return found[index]
            raise LookupError(f"no snapshot {selector} -- the repo holds {len(found)}")
        # Prefixes only past the point where they mean something; "1" is an index.
        if len(selector) >= MIN_PREFIX:
            for snapshot in found:
                if snapshot.id.startswith(selector):
                    return snapshot
        raise LookupError(f"no snapshot matching {selector!r}")

    def _stats_mode(self, mode: str) -> dict:
        return json.loads(self._run(["stats", "--json", "--mode", mode]).stdout or "{}")

    def stats(self) -> RepoStats:
        """Two modes, because neither reports both numbers.

        `raw-data` gives what the repository occupies; `restore-size` gives
        what restoring every snapshot would write, and only that mode counts
        files.
        """
        raw = self._stats_mode("raw-data")
        logical = self._stats_mode("restore-size")
        return RepoStats(
            total_size=raw.get("total_size", 0),
            restore_size=logical.get("total_size", 0),
            total_file_count=logical.get("total_file_count", 0),
            snapshots=len(self.snapshots()),
        )

    # ------------------------------------------------------------- maintain

    @staticmethod
    def _leading_json(stdout: str) -> object:
        """`forget --prune` prints its JSON, then plain-text prune progress."""
        text = stdout.strip()
        if not text:
            return []
        return json.JSONDecoder().raw_decode(text)[0]

    def forget(self, dry_run: bool = False) -> list[str]:
        """Apply retention. Returns the short ids that were (or would be) removed."""
        args = ["forget", "--json", *self.retention.forget_args()]
        args += ["--dry-run"] if dry_run else ["--prune"]
        payload = self._leading_json(self._run(args).stdout)
        removed: list[str] = []
        for group in payload:
            for entry in group.get("remove") or []:
                removed.append(entry.get("short_id", entry.get("id", "?")))
        return removed

    def check(self, read_data: bool = False) -> str:
        """Verify repository integrity. Raises ResticError if it does not hold."""
        args = ["check"]
        if read_data:
            # Sampling beats nothing and stays affordable on a big repo.
            args += ["--read-data-subset", "5%"]
        return self._run(args).stdout.strip()

    def restore(self, snapshot: Snapshot | str, target: Path) -> None:
        """Restore into `target`.

        The snapshot's common parent is stripped, so its contents land
        directly under `target` -- restoring a server snapshot into /tmp/x
        gives /tmp/x/world, not /tmp/x/home/.../Server/world.
        """
        identifier = snapshot.id if isinstance(snapshot, Snapshot) else snapshot
        self._run(["restore", identifier, "--target", str(target)])

    def restore_in_place(self, snapshot: Snapshot) -> None:
        """Restore over the server directory it was taken from.

        Refuses when the snapshot's common parent is not the server directory,
        because the target is then not what the caller assumes -- passing `/`
        here would write world/, config/ and admin/ to the filesystem root.
        """
        parent = snapshot.common_parent
        if parent != self.paths.server_dir:
            raise ResticError(
                f"snapshot {snapshot.short_id} is rooted at {parent}, not "
                f"{self.paths.server_dir} -- restore it with an explicit --target"
            )
        self.restore(snapshot, self.paths.server_dir)
