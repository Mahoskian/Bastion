"""Telling Discord the things worth interrupting somebody for.

The supervisor is the only process that witnesses every lifecycle transition:
it launches the JVM, waits with it, sees it exit, and decides whether that exit
was a stop, a restart or a crash. `mc stop` signals the supervisor rather than
doing the work itself, and `mc restart` asks the JVM to stop and leaves the
supervisor alone -- so raising events from there catches operator actions and
crashes through one code path, instead of bolting a notification onto each CLI
command and still missing every unattended restart.

Not every transition is worth a message, though. Starting, stopping and
restarting are ordinary, and posting one each turned the channel into a feed
nobody could leave unmuted -- so those moved to `core.board`, which keeps a
single pinned message current instead. What is left here is what should still
arrive as a notification: a crash, and the supervisor giving up on restarting
after several. Both are transports over the same bot; the split is about which
things deserve a ping, not about how they are sent.

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
import uuid
from collections.abc import Callable, Sequence
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
# Uploading a modpack is megabytes over whatever uplink the box has, so it gets
# its own timeout. Ten seconds is right for a lifecycle embed and wrong here.
UPLOAD_TIMEOUT = 120.0
# What Discord accepts per message from a bot in a server with no boost level.
# A pack over this is refused with a 40005 rather than truncated, so the size is
# worth knowing *before* spending the upload.
UPLOAD_LIMIT = 10 * 1024 * 1024
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
    """The lifecycle moments worth waking a phone for.

    Short on purpose. A start or a stop is something you already know about,
    because you are the one who typed it; a crash at 3am is not. Everything
    that is merely *state* lives on the pinned board in `core.board`.
    """

    CRASHED = "crashed"
    ABANDONED = "abandoned"
    TEST = "test"


# Title and embed colour per event. Discord takes a colour as one integer, not
# as a hex string, so these are ints rather than the "#2ecc71" they look like.
PRESENTATION: dict[EventKind, tuple[str, int]] = {
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
    def crashed(cls, code: int, ran_for: float, restart_delay: float) -> Notice:
        """A JVM exit nobody asked for, which the supervisor will restart from.

        Only a dirty exit gets here. `mc restart` works by asking the JVM to
        stop and leaving the supervisor's loop to bring it back, so an
        intentional restart reaches the supervisor looking exactly like an
        unattended one -- except that the JVM shut itself down cleanly and
        exited 0. That case updates the board and says nothing; this one is the
        other branch, and it is the reason this module still exists.
        """
        return cls(
            kind=EventKind.CRASHED,
            detail=(
                f"Exit code {code} after {human_seconds(ran_for)}. "
                f"Back in {human_seconds(restart_delay)}."
            ),
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
            detail=(
                "Sent by `mc notify test`. Crashes will appear here, and the "
                "pinned status message keeps everything else."
            ),
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


class Attachment(BaseModel):
    """A file to send alongside an embed, already read into memory.

    Read rather than streamed because the multipart body has to be built whole
    to be signed with a boundary anyway, and because a pack that cannot fit in
    memory cannot fit in a Discord message either.
    """

    model_config = ConfigDict(frozen=True)

    filename: str
    content: bytes

    @classmethod
    def read(cls, path: Path) -> Attachment:
        return cls(filename=path.name, content=path.read_bytes())

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def fits(self) -> bool:
        """Whether Discord will take it. Asked before the upload, not after."""
        return self.size <= UPLOAD_LIMIT


class Notifier(Protocol):
    """Anything that can deliver a Notice."""

    def send(self, notice: Notice) -> None: ...


class NullNotifier:
    """The default. An unconfigured server notifies nobody, and says nothing."""

    def send(self, notice: Notice) -> None:
        return None


def _multipart(payload: dict[str, object], attachments: Sequence[Attachment]) -> tuple[bytes, str]:
    """A multipart/form-data body, and the Content-Type header that names it.

    Written out by hand rather than pulled from a library: the whole point of
    this module is that posting to Discord costs one stdlib import, and one
    boundary plus two part headers is not worth a dependency.
    """
    boundary = uuid.uuid4().hex
    marker = f"--{boundary}".encode()
    parts = [
        marker,
        b'Content-Disposition: form-data; name="payload_json"',
        b"Content-Type: application/json",
        b"",
        json.dumps(payload).encode(),
    ]
    for index, file in enumerate(attachments):
        parts += [
            marker,
            (
                f'Content-Disposition: form-data; name="files[{index}]"; '
                f'filename="{file.filename}"'
            ).encode(),
            b"Content-Type: application/octet-stream",
            b"",
            file.content,
        ]
    parts += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(parts), f"multipart/form-data; boundary={boundary}"


class DiscordBot:
    """Posts to one channel as a bot, over REST."""

    def __init__(self, config: DiscordConfig, timeout: float = TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout

    # ------------------------------------------------------------- transport

    def _send(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
    ) -> object:
        headers = {
            "Authorization": f"Bot {self.config.token}",
            "User-Agent": USER_AGENT,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise NotifyError(self._explain(exc.code), exc.code) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise NotifyError(f"could not reach Discord: {exc}") from exc
        except json.JSONDecodeError:
            # Some endpoints answer 204 with no body; that is still a success.
            return None

    def _request(self, method: str, path: str, body: dict | None = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        return self._send(method, path, data, "application/json" if data is not None else None)

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
        if status == 413:
            return (
                "Discord refused the upload as too large (413) -- this server's boost "
                "level allows less than the file being sent."
            )
        if status == 429:
            return "Discord is rate-limiting this bot (429)."
        return f"Discord returned {status}."

    # ------------------------------------------------------------- sending

    def send(self, notice: Notice) -> None:
        self.post(notice.embed())

    def post(
        self,
        embed: dict[str, object],
        attachments: Sequence[Attachment] = (),
    ) -> str | None:
        """Post one embed to the channel, optionally carrying files.

        Discord takes files as multipart with the message itself in a
        `payload_json` part -- there is no JSON-only way to attach one, which
        is why this is not simply another field on the body.

        Returns the new message's id, which is what makes a message editable
        later. Most callers post and forget; `core.board` keeps the id.
        """
        payload: dict[str, object] = {"embeds": [embed]}
        path = f"/channels/{self.config.channel_id}/messages"
        if not attachments:
            return self._message_id(self._request("POST", path, payload))

        oversized = [file for file in attachments if not file.fits]
        if oversized:
            names = ", ".join(
                f"{file.filename} ({file.size / 1_048_576:.1f}M)" for file in oversized
            )
            raise NotifyError(
                f"too large for Discord's {UPLOAD_LIMIT // 1_048_576}M limit: {names}"
            )
        # Naming every part here is what lets Discord match `files[n]` to the
        # attachment metadata; without it the filenames are Discord's guess.
        payload["attachments"] = [
            {"id": index, "filename": file.filename} for index, file in enumerate(attachments)
        ]
        body, content_type = _multipart(payload, attachments)
        return self._message_id(
            self._send("POST", path, body, content_type, timeout=UPLOAD_TIMEOUT)
        )

    @staticmethod
    def _message_id(created: object) -> str | None:
        """The id out of a created message, or None if Discord did not say."""
        if isinstance(created, dict) and "id" in created:
            return str(created["id"])
        return None

    def edit(self, message_id: str, embed: dict[str, object]) -> None:
        """Replace an existing message's embed, leaving the message in place.

        An edit notifies nobody -- no ping, no unread badge, no bump up the
        channel list. That silence is the entire reason `core.board` exists.
        """
        path = f"/channels/{self.config.channel_id}/messages/{message_id}"
        self._request("PATCH", path, {"embeds": [embed]})

    def pin(self, message_id: str) -> None:
        """Pin a message. Needs Manage Messages, which sending does not."""
        self._request("PUT", f"/channels/{self.config.channel_id}/pins/{message_id}")

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
