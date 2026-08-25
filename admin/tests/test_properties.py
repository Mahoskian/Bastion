"""Tests for server.properties parsing and process detection."""

import subprocess

import pytest

from mcadmin.core import process
from mcadmin.core.properties import ServerProperties, properties


@pytest.fixture
def props_file(tmp_path, monkeypatch):
    (tmp_path / "server.properties").write_text(
        "#a comment\n\nwhite-list=true\nrcon.password=s3cret\nenable-rcon=true\nmalformed\n"
    )
    monkeypatch.setenv("MC_SERVER_DIR", str(tmp_path))
    properties.cache_clear()
    yield tmp_path
    properties.cache_clear()


def test_parses_and_skips_comments_and_junk(props_file):
    props = properties()
    assert props.get("white-list") == "true"
    assert props.rcon_password == "s3cret"
    assert "malformed" not in props.values


def test_typed_accessors_have_defaults():
    props = ServerProperties(values={"enable-rcon": "true"})
    assert props.rcon_enabled is True
    assert props.rcon_port == 25575  # absent -> documented default
    assert props.max_players == 20


def test_rcon_disabled_when_key_absent():
    assert ServerProperties().rcon_enabled is False


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        ServerProperties.load(tmp_path / "nope.properties")


def test_notable_marks_unknown_keys(props_file):
    assert properties().notable()["difficulty"] == "?"


def test_server_running_ignores_other_java_processes(monkeypatch):
    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(
            a, 0, stdout="  4321 /usr/bin/java -jar something-else.jar\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert process.server_running() is False
    assert process.server_pid() is None


def test_server_pid_matches_the_jar_marker(monkeypatch):
    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(
            a, 0, stdout="  4321 java -Xmx12G -jar fabric-server-mc.26.2.jar nogui\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert process.server_pid() == 4321


def test_ps_failure_is_not_fatal(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no ps")

    monkeypatch.setattr(subprocess, "run", boom)
    assert process.java_processes() == {}
    assert process.server_running() is False
