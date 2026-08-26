"""Tests for Discord notifications.

Nothing here reaches the network: `DiscordBot` is exercised against a fake
urlopen that records what was sent, which is also how the status-code messages
are pinned. The supervisor's side is checked by handing it a notifier that
records notices, so the assertion is about which events it raises, not about
what any of them said.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from mcadmin.core import notify
from mcadmin.core.models import Paths
from mcadmin.core.notify import (
    DiscordBot,
    DiscordConfig,
    EventKind,
    Notice,
    NotifyError,
    NullNotifier,
    notifier_for,
)

TOKEN = "MTIzNDU2Nzg5.GaBcDe.f4kE-t0ken-for-tests"
CHANNEL = "123456789012345678"


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    """The environment overrides the file, so tests must start with it clear."""
    monkeypatch.delenv(notify.ENV_TOKEN, raising=False)
    monkeypatch.delenv(notify.ENV_CHANNEL, raising=False)


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / ".notify.json"

    def write(**fields):
        path.write_text(json.dumps(fields))
        return path

    write.path = path
    return write


# ----------------------------------------------------------------- config


def test_absent_config_is_not_an_error(tmp_path):
    """Not asking for notifications is the normal case, not a failure."""
    assert DiscordConfig.load(tmp_path / "nothing.json") is None


def test_a_config_file_is_read(config_file):
    config = DiscordConfig.load(config_file(token=TOKEN, channel_id=CHANNEL))
    assert config is not None
    assert config.token == TOKEN
    assert config.channel_id == CHANNEL
    assert config.enabled


def test_a_channel_id_pasted_as_a_number_still_works(config_file):
    """Discord ids are snowflakes carried as strings, but nobody quotes them."""
    config = DiscordConfig.load(config_file(token=TOKEN, channel_id=int(CHANNEL)))
    assert config is not None
    assert config.channel_id == CHANNEL


def test_whitespace_around_a_pasted_token_is_trimmed(config_file):
    config = DiscordConfig.load(config_file(token=f"  {TOKEN}\n", channel_id=CHANNEL))
    assert config is not None
    assert config.token == TOKEN


def test_the_environment_overrides_the_file(config_file, monkeypatch):
    monkeypatch.setenv(notify.ENV_CHANNEL, "999999999999999999")
    config = DiscordConfig.load(config_file(token=TOKEN, channel_id=CHANNEL))
    assert config is not None
    assert config.channel_id == "999999999999999999"


def test_the_environment_alone_is_enough(tmp_path, monkeypatch):
    monkeypatch.setenv(notify.ENV_TOKEN, TOKEN)
    monkeypatch.setenv(notify.ENV_CHANNEL, CHANNEL)
    config = DiscordConfig.load(tmp_path / "nothing.json")
    assert config is not None
    assert config.token == TOKEN


def test_half_a_config_is_an_error_not_silence(config_file):
    """Silently treating a half-filled config as 'off' is how a server ends up
    notifying nobody for a month without anyone noticing."""
    with pytest.raises(NotifyError, match="channel id"):
        DiscordConfig.load(config_file(token=TOKEN))


def test_a_malformed_file_is_an_error(tmp_path):
    path = tmp_path / ".notify.json"
    path.write_text("{not json")
    with pytest.raises(NotifyError, match="not readable JSON"):
        DiscordConfig.load(path)


def test_a_channel_id_that_is_not_an_id_is_rejected(config_file):
    with pytest.raises(NotifyError):
        DiscordConfig.load(config_file(token=TOKEN, channel_id="#general"))


def test_the_token_is_redacted_for_display():
    config = DiscordConfig(token=TOKEN, channel_id=CHANNEL)
    assert TOKEN not in config.redacted_token
    assert config.redacted_token.startswith(TOKEN[:6])


# ----------------------------------------------------------------- factory


def test_an_unconfigured_server_gets_a_notifier_that_does_nothing(tmp_path):
    (tmp_path / "admin").mkdir()
    assert isinstance(notifier_for(Paths(server_dir=tmp_path)), NullNotifier)


def test_a_broken_config_warns_but_never_blocks_the_boot(tmp_path):
    (tmp_path / "admin").mkdir()
    paths = Paths(server_dir=tmp_path)
    paths.notify_config.write_text("{not json")
    warnings: list[str] = []

    notifier = notifier_for(paths, warnings.append)

    assert isinstance(notifier, NullNotifier), "a bad config must not stop the server"
    assert warnings and "notifications are off" in warnings[0]


def test_enabled_false_turns_it_off_without_deleting_the_token(tmp_path):
    (tmp_path / "admin").mkdir()
    paths = Paths(server_dir=tmp_path)
    paths.notify_config.write_text(
        json.dumps({"token": TOKEN, "channel_id": CHANNEL, "enabled": False})
    )
    assert isinstance(notifier_for(paths), NullNotifier)


# ----------------------------------------------------------------- notices


def test_a_clean_exit_reads_as_a_restart_not_a_crash():
    """`mc restart` asks the JVM to stop and lets the loop bring it back, so an
    intentional restart looks like any other exit -- except that it is a 0."""
    assert Notice.exited(0, 3600, 10).kind is EventKind.RESTARTING


def test_a_dirty_exit_reads_as_a_crash():
    notice = Notice.exited(1, 42, 10)
    assert notice.kind is EventKind.CRASHED
    assert "Exit code 1" in (notice.detail or "")


def test_an_embed_carries_a_title_and_a_colour():
    embed = Notice.exited(1, 42, 10).embed()
    assert embed["title"] == "Server crashed"
    assert isinstance(embed["color"], int)
    assert "42s" in str(embed["description"])


def test_a_notice_without_detail_omits_the_description():
    """The routine lifecycle notices are a title and nothing else: an operator
    watching the channel wants to know the server is up, not how it booted."""
    assert "description" not in Notice.ready().embed()
    assert "description" not in Notice.stopped().embed()
    assert "description" not in Notice.exited(0, 3600, 10).embed()


# ----------------------------------------------------------------- transport


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.fixture
def sent(monkeypatch):
    """Capture every request DiscordBot would have made."""
    calls: list[dict] = []

    def fake_urlopen(request, timeout=None):
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": dict(request.header_items()),
                "body": json.loads(request.data) if request.data else None,
            }
        )
        return FakeResponse({"username": "Bastion"})

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    return calls


def bot() -> DiscordBot:
    return DiscordBot(DiscordConfig(token=TOKEN, channel_id=CHANNEL))


def test_sending_posts_an_embed_to_the_channel(sent):
    bot().send(Notice.ready())
    assert len(sent) == 1
    call = sent[0]
    assert call["method"] == "POST"
    assert call["url"].endswith(f"/channels/{CHANNEL}/messages")
    assert call["body"]["embeds"][0]["title"] == "Server is up"


def test_the_bot_token_is_sent_as_a_bot_authorization(sent):
    """`Bot <token>` -- Discord rejects the bare token a webhook would not need."""
    bot().send(Notice.test())
    headers = {key.lower(): value for key, value in sent[0]["headers"].items()}
    assert headers["authorization"] == f"Bot {TOKEN}"
    assert headers["user-agent"].startswith("DiscordBot (")


def test_guilds_lists_the_servers_the_bot_was_added_to(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return FakeResponse([{"id": "1", "name": "Bastion SMP"}])

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert bot().guilds() == ["Bastion SMP"]


def test_a_bot_that_was_never_invited_lists_no_servers(monkeypatch):
    """It answers /users/@me perfectly well and then 403s on every channel in
    existence, which reads as a permissions problem and is not one."""

    def fake_urlopen(request, timeout=None):
        return FakeResponse([])

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    assert bot().guilds() == []


def test_identify_asks_who_the_bot_is_without_posting(sent):
    assert bot().identify() == "Bastion"
    assert sent[0]["method"] == "GET"
    assert sent[0]["url"].endswith("/users/@me")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "token"),
        (403, "cannot reach"),
        (404, "no channel"),
        (429, "rate-limiting"),
    ],
)
def test_http_failures_say_what_to_go_and_fix(monkeypatch, status, expected):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, status, "nope", {}, None)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(NotifyError, match=expected):
        bot().send(Notice.test())


def test_a_failure_carries_the_status_so_callers_need_not_read_the_message(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "nope", {}, None)

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(NotifyError) as caught:
        bot().send(Notice.test())
    assert caught.value.status == 403


def test_a_config_failure_carries_no_status():
    """Nothing HTTP happened, so there is no code to report."""
    assert NotifyError("bad config").status is None


def test_an_unreachable_discord_is_a_notify_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(NotifyError, match="could not reach Discord"):
        bot().send(Notice.test())
