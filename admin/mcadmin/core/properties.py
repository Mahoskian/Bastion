"""Reading server.properties as typed values."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .models import Paths

# Surfaced by `mc status`: the settings worth seeing at a glance.
NOTABLE_KEYS = (
    "difficulty",
    "max-players",
    "view-distance",
    "simulation-distance",
    "white-list",
    "enforce-whitelist",
    "spawn-protection",
    "enable-rcon",
)


class ServerProperties(BaseModel):
    """The parsed contents of server.properties."""

    model_config = ConfigDict(frozen=True)

    values: dict[str, str] = {}

    @classmethod
    def load(cls, path: Path) -> ServerProperties:
        if not path.exists():
            raise FileNotFoundError(f"server.properties not found at {path}")
        values: dict[str, str] = {}
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key] = value
        return cls(values=values)

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    @property
    def rcon_enabled(self) -> bool:
        return self.get("enable-rcon") == "true"

    @property
    def rcon_port(self) -> int:
        return int(self.get("rcon.port", "25575") or 25575)

    @property
    def rcon_password(self) -> str:
        return self.get("rcon.password")

    @property
    def max_players(self) -> int:
        return int(self.get("max-players", "20") or 20)

    def notable(self) -> dict[str, str]:
        return {key: self.get(key, "?") for key in NOTABLE_KEYS}


@lru_cache(maxsize=1)
def properties() -> ServerProperties:
    return ServerProperties.load(Paths.from_env().properties_file)
