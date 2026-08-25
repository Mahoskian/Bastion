"""The Modrinth API, in bulk.

Everything here batches. Looking 162 jars up one at a time means 162 requests
and a politeness delay between each; the bulk endpoints answer the same
question in three, so the rate limit stops being something to design around.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence

API = "https://api.modrinth.com/v2"
USER_AGENT = "mcadmin/0.1 (Minecraft server admin CLI)"
# Conservative: the documented endpoints take a list, without a stated cap.
CHUNK = 100
TIMEOUT = 30.0


class ModrinthError(RuntimeError):
    pass


def chunked(items: Sequence[str], size: int = CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class ModrinthApi:
    """Bulk lookups against Modrinth. Unknown hashes are absent, not an error."""

    def __init__(self, timeout: float = TIMEOUT, user_agent: str = USER_AGENT) -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    # ------------------------------------------------------------- transport

    def _request(self, url: str, body: dict | None = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"User-Agent": self.user_agent}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ModrinthError(f"Modrinth returned {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModrinthError(f"could not reach Modrinth: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ModrinthError("Modrinth returned something that was not JSON") from exc

    # ------------------------------------------------------------- lookups

    def versions_by_hash(self, hashes: Sequence[str]) -> dict[str, dict]:
        """sha1 -> the version that file belongs to, for hashes Modrinth knows."""
        found: dict[str, dict] = {}
        for batch in chunked(list(hashes)):
            payload = self._request(
                f"{API}/version_files", {"hashes": list(batch), "algorithm": "sha1"}
            )
            if isinstance(payload, dict):
                found.update(payload)
        return found

    def latest_by_hash(
        self,
        hashes: Sequence[str],
        loaders: Sequence[str] = ("fabric",),
        game_versions: Sequence[str] = (),
    ) -> dict[str, dict]:
        """sha1 -> the newest version for those loaders and game versions.

        A hash missing from the result means that project has no build matching
        the filter at all -- which is the interesting answer before an upgrade.
        """
        found: dict[str, dict] = {}
        for batch in chunked(list(hashes)):
            payload = self._request(
                f"{API}/version_files/update",
                {
                    "hashes": list(batch),
                    "algorithm": "sha1",
                    "loaders": list(loaders),
                    "game_versions": list(game_versions),
                },
            )
            if isinstance(payload, dict):
                found.update(payload)
        return found

    def projects(self, ids: Sequence[str]) -> dict[str, dict]:
        """project id -> project, for titles and slugs."""
        found: dict[str, dict] = {}
        for batch in chunked(list(ids)):
            query = urllib.parse.urlencode({"ids": json.dumps(list(batch))})
            payload = self._request(f"{API}/projects?{query}")
            if isinstance(payload, list):
                for project in payload:
                    if isinstance(project, dict) and "id" in project:
                        found[project["id"]] = project
        return found

    def game_versions(self, releases_only: bool = True) -> list[str]:
        """Minecraft versions Modrinth knows, newest first.

        Used to catch a typo'd --target: an unknown version has no builds at
        all, which would otherwise read as "every mod is a blocker".
        """
        payload = self._request(f"{API}/tag/game_version")
        if not isinstance(payload, list):
            return []
        return [
            entry["version"]
            for entry in payload
            if isinstance(entry, dict)
            and "version" in entry
            and (not releases_only or entry.get("version_type") == "release")
        ]

    def fetch_bytes(self, url: str) -> bytes:
        """Download one file. The caller checks it against its hashes."""
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ModrinthError(f"download failed with {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModrinthError(f"download failed: {exc}") from exc

    def download_urls(self, hashes: Sequence[str]) -> dict[str, str]:
        """sha1 -> CDN url for that exact file. Used when building a pack."""
        urls: dict[str, str] = {}
        for sha1, version in self.versions_by_hash(hashes).items():
            for entry in version.get("files", []):
                if entry.get("hashes", {}).get("sha1") == sha1:
                    urls[sha1] = entry["url"]
                    break
        return urls
