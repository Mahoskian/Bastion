"""Tests for the typed configuration and runtime state."""

import pytest
from pydantic import ValidationError

from mcadmin.core.models import JvmOptions, Paths, RuntimeState, ServerState


def test_state_knows_which_conditions_mean_a_jvm_exists():
    assert ServerState.RUNNING.is_live
    assert ServerState.BOOTING.is_live
    assert not ServerState.ORPHANED.is_live
    assert not ServerState.STOPPED.is_live


def test_every_state_has_a_description():
    for state in ServerState:
        assert state.description


@pytest.mark.parametrize("heap", ["12G", "512M", "24G"])
def test_valid_heaps(heap):
    assert JvmOptions(heap=heap).heap == heap


@pytest.mark.parametrize("heap", ["potato", "12", "12GB", "1.5G", ""])
def test_malformed_heaps_are_rejected(heap):
    with pytest.raises(ValidationError):
        JvmOptions(heap=heap)


def test_heap_too_small_to_boot_is_rejected():
    with pytest.raises(ValidationError, match="too small"):
        JvmOptions(heap="64M")


def test_jvm_options_are_frozen():
    with pytest.raises(ValidationError):
        JvmOptions().heap = "1G"


def test_command_pins_xms_to_xmx(tmp_path):
    command = JvmOptions(heap="8G").command(tmp_path / "server.jar", tmp_path / "logs")
    assert "-Xms8G" in command and "-Xmx8G" in command
    assert command[-2:] == [str(tmp_path / "server.jar"), "nogui"]
    assert "-XX:+UseG1GC" in command


def test_gc_log_flag_points_at_the_logs_directory(tmp_path):
    flag = JvmOptions(gc_log_files=3).gc_log_flag(tmp_path)
    assert str(tmp_path / "gc.log") in flag
    assert "filecount=3" in flag


def test_jar_is_found_by_glob(tmp_path):
    (tmp_path / "fabric-server-mc.26.2-loader.jar").write_text("")
    assert Paths(server_dir=tmp_path).jar().name.startswith("fabric-server")


def test_missing_jar_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="no fabric-server"):
        Paths(server_dir=tmp_path).jar()


def test_ambiguous_jar_is_an_error_not_a_guess(tmp_path):
    (tmp_path / "fabric-server-a.jar").write_text("")
    (tmp_path / "fabric-server-b.jar").write_text("")
    with pytest.raises(FileNotFoundError, match="several server jars"):
        Paths(server_dir=tmp_path).jar()


def test_paths_read_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MC_SERVER_DIR", str(tmp_path))
    monkeypatch.setenv("MC_BACKUP_DIR", str(tmp_path / "b"))
    paths = Paths.from_env()
    assert paths.admin_dir == tmp_path / "admin"
    assert paths.logs_dir == tmp_path / "logs"
    assert paths.runtime_file == tmp_path / "admin" / ".runtime.json"


def test_runtime_state_round_trips(tmp_path):
    path = tmp_path / "runtime.json"
    RuntimeState(supervisor_pid=42, jvm_pid=43, heap="8G", restarts=2).save(path)
    loaded = RuntimeState.load(path)
    assert loaded.supervisor_pid == 42
    assert loaded.jvm_pid == 43
    assert loaded.restarts == 2


def test_unreadable_runtime_state_is_none_not_an_exception(tmp_path):
    assert RuntimeState.load(tmp_path / "absent.json") is None
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert RuntimeState.load(corrupt) is None
