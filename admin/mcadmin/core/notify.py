"""Telling Discord what the server just did.

The supervisor is the only process that witnesses every lifecycle transition:
it launches the JVM, waits with it, sees it exit, and decides whether that exit
was a stop, a restart or a crash. `mc stop` signals the supervisor rather than
doing the work itself, and `mc restart` asks the JVM to stop and leaves the
supervisor alone -- so raising events from there catches operator actions and
crashes through one code path, instead of bolting a notification onto each CLI
command and still missing every unattended restart.

Sending is REST, not a gateway connection. A bot token can POST straight to a
channel; the websocket exists to *receive* events, which nothing here needs.
That keeps notifications a library call inside the supervisor rather than a
second long-lived process to keep alive beside it. Adding chat relay or slash
commands later means adding that connection -- it does not mean changing this.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .models import Paths
from .units import human_seconds

API = "https://discord.com/api/v10"
# Discord requires bots to identify themselves, and rejects some default agents.
USER_AGENT = "DiscordBot (https://github.com/Mahoskian/Bastion, 0.1)"
TIMEOUT = 10.0
ENV_TOKEN = "MC_DISCORD_TOKEN"
ENV_CHANNEL = "MC_DISCORD_CHANNEL"


class NotifyError(RuntimeError):
    """A notification could not be configured or delivered.

    `status` carries Discord's HTTP status when there was one, so a caller can
    tell a permissions failure from an outage without matching on the message.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class EventKind(StrEnum):
    """The lifecycle moments worth waking a phone for."""

    STARTING = "starting"
    READY = "ready"
    RESTARTING = "restarting"
    STOPPED = "stopped"
    CRASHED = "crashed"
    ABANDONED = "abandoned"
    TEST = "test"


# Title and embed colour per event. Discord takes a colour as one integer, not
# as a hex string, so these are ints rather than the "#2ecc71" they look like.
PRESENTATION: dict[EventKind, tuple[str, int]] = {
    EventKind.STARTING: ("Server starting", 0xE5A50A),
    EventKind.READY: ("Server is up", 0x2ECC71),
    EventKind.RESTARTING: ("Server restarting", 0x3498DB),
    EventKind.STOPPED: ("Server stopped", 0x95A5A6),
    EventKind.CRASHED: ("Server crashed", 0xE74C3C),
    EventKind.ABANDONED: ("Server gave up", 0x992D22),
    EventKind.TEST: ("Bastion is connected", 0x5865F2),
}


class Notice(BaseModel):
    """One thing to say, and how it should look when said.

    The wording lives in the constructors below so that every caller phrasing
    the same event phrases it the same way, and so a test can assert on the
    event rather than on a string built at the call site.
    """

    model_config = ConfigDict(frozen=True)

    kind: EventKind
    detail: str | None = None

    @property
    def title(self) -> str:
        return PRESENTATION[self.kind][0]

    @property
    def color(self) -> int:
        return PRESENTATION[self.kind][1]

    def embed(self) -> dict[str, object]:
        embed: dict[str, object] = {"title": self.title, "color": self.color}
        if self.detail:
            embed["description"] = self.detail
        return embed

    # --------------------------------------------------------- the moments

    @classmethod
    def starting(cls, heap: str, restarts: int = 0) -> Notice:
        detail = f"Heap {heap}."
        if restarts:
            detail += f" Restart {restarts} of this supervisor."
        return cls(kind=EventKind.STARTING, detail=detail)

    @classmethod
    def ready(cls, booted_in: float) -> Notice:
        return cls(
            kind=EventKind.READY,
            detail=f"Answering RCON after {human_seconds(booted_in)}.",
        )

    @classmethod
    def stopped(cls, ran_for: float) -> Notice:
        return cls(
            kind=EventKind.STOPPED,
            detail=f"Stopped on request. This run lasted {human_seconds(ran_for)}.",
        )

    @classmethod
    def exited(cls, code: int, ran_for: float, restart_delay: float) -> Notice:
        """A JVM exit the supervisor is about to restart from.

        Exit code decides the wording, because it is the only thing that
        separates the two. `mc restart` works by asking the JVM to stop and
        leaving the supervisor's loop to bring it back, so an intentional
        restart reaches this point looking exactly like an unattended one --
        except that the JVM shut itself down cleanly and exited 0. A crash
        does not.
        """
        back_in = f"Back in {human_seconds(restart_delay)}."
        if code == 0:
            return cls(
                kind=EventKind.RESTARTING,
                detail=f"Ran for {human_seconds(ran_for)}. {back_in}",
            )
        return cls(
            kind=EventKind.CRASHED,
            detail=f"Exit code {code} after {human_seconds(ran_for)}. {back_in}",
        )

    @classmethod
    def abandoned(cls, crashes: int, healthy_after: float) -> Notice:
        return cls(
            kind=EventKind.ABANDONED,
            detail=(
                f"{crashes} crashes in under {human_seconds(healthy_after)} each. "
                "Not restarting again -- check logs/latest.log."
            ),
        )

    @classmethod
    def test(cls) -> Notice:
        return cls(
            kind=EventKind.TEST,
            detail="Sent by `mc notify test`. Lifecycle events will appear here.",
        )


class DiscordConfig(BaseModel):
    """Everything needed to post as a bot. Frozen, like the rest of core."""

    model_config = ConfigDict(frozen=True)

    token: str = Field(min_length=1)
    channel_id: str = Field(pattern=r"^\d+$")
    enabled: bool = True

    @field_validator("token", "channel_id", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        """Tolerate a pasted id written as a number, and stray whitespace.

        Discord ids are 64-bit snowflakes that JSON would round if they were
        ever read as floats, so they are carried as strings -- but nobody
        pasting one out of the client thinks to quote it.
        """
        if isinstance(value, int):
            return str(value)
        return value.strip() if isinstance(value, str) else value

    @property
    def redacted_token(self) -> str:
        """Enough to tell two tokens apart, not enough to use one."""
        return f"{self.token[:6]}...{self.token[-4:]}" if len(self.token) > 14 else "set"

    @classmethod
    def load(cls, path: Path) -> DiscordConfig | None:
        """The config file, with the environment overriding it.

        None means "nobody asked for notifications", which is not an error.
        A file that exists but does not parse *is* an error: silently treating
        a typo'd token as "unconfigured" is how a server ends up notifying
        nobody for a month without anyone noticing.
        """
        data: dict[str, object] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise NotifyError(f"{path.name} is not readable JSON: {exc}") from exc
            if not isinstance(loaded, dict):
                raise NotifyError(f"{path.name} should hold a JSON object.")
            data = loaded

        token = os.environ.get(ENV_TOKEN) or data.get("token")
        channel = os.environ.get(ENV_CHANNEL) or data.get("channel_id")
        if not token and not channel:
            return None
        if not token or not channel:
            raise NotifyError(
                f"both a bot token and a channel id are needed -- put them in {path}, "
                f"or set {ENV_TOKEN} and {ENV_CHANNEL}."
            )
        try:
            return cls(token=token, channel_id=channel, enabled=bool(data.get("enabled", True)))
        except ValidationError as exc:
            raise NotifyError(f"{path.name}: {exc}") from exc


class Notifier(Protocol):
    """Anything that can deliver a Notice."""

    def send(self, notice: Notice) -> None: ...


class NullNotifier:
    """The default. An unconfigured server notifies nobody, and says nothing."""

    def send(self, notice: Notice) -> None:
        return None


class DiscordBot:
    """Posts to one channel as a bot, over REST."""

    def __init__(self, config: DiscordConfig, timeout: float = TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout

    # ------------------------------------------------------------- transport

    def _request(self, method: str, path: str, body: dict | None = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bot {self.config.token}",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise NotifyError(self._explain(exc.code), exc.code) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise NotifyError(f"could not reach Discord: {exc}") from exc
        except json.JSONDecodeError:
            # Some endpoints answer 204 with no body; that is still a success.
            return None

    def _explain(self, status: int) -> str:
        """Discord's status codes, as the thing you have to go and fix."""
        channel = self.config.channel_id
        if status == 401:
            return "Discord rejected the bot token (401) -- regenerate it in the developer portal."
        if status == 403:
            return (
                f"the bot cannot reach channel {channel} (403). Either it has not been "
                "added to that server, or it lacks View Channel, Send Messages and "
                "Embed Links there -- check the category's overwrites too."
            )
        if status == 404:
            return (
                f"there is no channel {channel} (404) -- turn on Developer Mode in Discord "
                "and copy the id from the channel's right-click menu."
            )
        if status == 429:
            return "Discord is rate-limiting this bot (429)."
        return f"Discord returned {status}."

    # ------------------------------------------------------------- sending

    def send(self, notice: Notice) -> None:
        self._request(
            "POST",
            f"/channels/{self.config.channel_id}/messages",
            {"embeds": [notice.embed()]},
        )

    def identify(self) -> str:
        """The bot's own username -- proof the token works, without posting."""
        me = self._request("GET", "/users/@me")
        if not isinstance(me, dict) or "username" not in me:
            raise NotifyError("Discord did not say who this bot is.")
        return str(me["username"])

    def guilds(self) -> list[str]:
        """The servers this bot has been added to, by name.

        Only worth asking once something has already failed: a bot that was
        created but never invited answers /users/@me perfectly well and then
        403s on every channel in existence, which reads exactly like a
        permissions problem and is not one. Discord rate-limits this endpoint
        far harder than it does sending, so it stays off the happy path.
        """
        found = self._request("GET", "/users/@me/guilds")
        if not isinstance(found, list):
            raise NotifyError("Discord did not list the bot's servers.")
        return [str(guild.get("name", guild.get("id", "?"))) for guild in found]


def notifier_for(
    paths: Paths | None = None,
    warn: Callable[[str], None] = lambda _message: None,
) -> Notifier:
    """The notifier this server is configured for, or one that does nothing.

    A broken config warns and degrades rather than raising. Nothing about
    Discord should be able to stop the server from coming up -- but the
    supervisor passes its own logger in as `warn`, so a typo is visible in the
    console instead of being swallowed.
    """
    paths = paths or Paths.from_env()
    try:
        config = DiscordConfig.load(paths.notify_config)
    except NotifyError as exc:
        warn(f"Discord notifications are off -- {exc}")
        return NullNotifier()
    if config is None or not config.enabled:
        return NullNotifier()
    return DiscordBot(config)
