"""Tests for the client pack's release history.

The interesting logic is not the bookkeeping but the pairing: a mod that was
updated arrives as a removal and an addition, and everything here is about
whether those get put back together correctly. The jar names are real ones out
of `client-install/mods/`, because the naming conventions are exactly what the
identity rule has to survive.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from mcadmin.core.changelog import Changelog, ChangelogError, Release, identity

WHEN = datetime(2026, 8, 26, 9, 46)


# ----------------------------------------------------------------- identity


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("lithium-fabric-0.25.3+mc26.2.jar", "lithium"),
        ("lithium-fabric-0.26.0+mc26.2.jar", "lithium"),
        ("Jade-mc26.2-Fabric-26.2.11.jar", "jade"),
        ("better-end-26.201.2.jar", "better-end"),
        ("EnchantingInfuser-v26.2.0-mc26.2.x-Fabric.jar", "enchantinginfuser"),
        ("Terralith_26.2_v2.6.4.jar", "terralith"),
        ("Katters Structures v2.5.jar", "katters-structures"),
        ("HopoBetterUnderwaterRuins-[26.2]-1.2.8.jar", "hopobetterunderwaterruins"),
        ("dungeons-and-taverns-5.3.0.jar", "dungeons-and-taverns"),
    ],
)
def test_identity_strips_the_build_and_keeps_the_mod(filename, expected):
    assert identity(filename) == expected


def test_a_digit_in_the_mods_own_name_survives():
    """`c2me` is the mod's name, not its version -- the first token is kept."""
    assert identity("c2me-fabric-mc26.2-0.4.1-beta.1.0.jar") == "c2me"


def test_the_loader_is_only_noise_after_the_first_token():
    """Dropping it everywhere would turn `fabric-api` into `api`."""
    assert identity("fabric-api-0.157.0+26.2.jar") == "fabric-api"


def test_two_mods_sharing_a_prefix_stay_apart():
    a = identity("dungeons-and-taverns-nether-fortress-overhaul-v3.1.jar")
    b = identity("dungeons-and-taverns-jungle-temple-overhaul-v2.1.jar")
    assert a != b


# ----------------------------------------------------------------- the diff


def test_the_first_build_is_recorded_as_a_beginning_not_as_150_additions():
    """Everything is "new" against an empty history, which is a fact about the
    record starting rather than about anything having been added."""
    log, release = Changelog().record(["a-1.0.jar", "b-1.0.jar"], "1.0.0", WHEN)
    assert release.initial
    assert release.added == ("a-1.0.jar", "b-1.0.jar")
    assert release.summary == "first recorded build -- 2 mods"
    assert log.mods == ("a-1.0.jar", "b-1.0.jar")


def test_additions_and_removals_are_reported_separately():
    log, _ = Changelog().record(["a-1.0.jar", "b-1.0.jar"], "1.0.0", WHEN)
    _, release = log.record(["a-1.0.jar", "c-1.0.jar"], "1.0.1", WHEN)
    assert release.added == ("c-1.0.jar",)
    assert release.removed == ("b-1.0.jar",)
    assert release.updated == ()
    assert not release.initial


def test_a_new_build_of_a_mod_is_an_update_not_a_swap():
    """The version is in the filename, so an upgrade looks exactly like one mod
    leaving and another arriving. Reporting it that way is useless."""
    log, _ = Changelog().record(["lithium-fabric-0.25.3+mc26.2.jar"], "1.0.0", WHEN)
    _, release = log.record(["lithium-fabric-0.26.0+mc26.2.jar"], "1.0.1", WHEN)
    assert release.added == () and release.removed == ()
    assert [(c.mod, c.before, c.after) for c in release.updated] == [
        ("lithium", "lithium-fabric-0.25.3+mc26.2.jar", "lithium-fabric-0.26.0+mc26.2.jar")
    ]
    assert release.summary == "1 updated"


def test_an_ambiguous_pairing_is_left_as_what_it_looks_like():
    """Two jars of one mod arriving is not an update, and guessing which
    replaced which would put a wrong line in a file nobody re-checks."""
    log, _ = Changelog().record(["mod-1.0.jar"], "1.0.0", WHEN)
    _, release = log.record(["mod-2.0.jar", "mod-3.0.jar"], "1.0.1", WHEN)
    assert release.updated == ()
    assert release.removed == ("mod-1.0.jar",)
    assert release.added == ("mod-2.0.jar", "mod-3.0.jar")


def test_a_rebuild_that_changed_nothing_is_not_a_release():
    """`mc mrpack` runs whenever a pack is wanted, not only when mods moved."""
    log, _ = Changelog().record(["a-1.0.jar"], "1.0.0", WHEN)
    after, release = log.record(["a-1.0.jar"], "1.0.1", WHEN)
    assert not release.changed
    assert release.summary == "no mod changes"
    assert after.releases == log.releases, "the history must not grow"


def test_releases_are_newest_first():
    log, _ = Changelog().record(["a-1.0.jar"], "1.0.0", WHEN)
    log, _ = log.record(["a-1.0.jar", "b-1.0.jar"], "1.0.1", WHEN)
    log, _ = log.record(["b-1.0.jar"], "1.0.2", WHEN)
    assert [entry.version for entry in log.releases] == ["1.0.2", "1.0.1", "1.0.0"]
    assert log.latest.version == "1.0.2"


def test_release_inspects_without_recording():
    """`--post --no-changelog` needs the diff without keeping it."""
    log, _ = Changelog().record(["a-1.0.jar"], "1.0.0", WHEN)
    release = log.release(["a-1.0.jar", "b-1.0.jar"], "1.0.1", WHEN)
    assert release.added == ("b-1.0.jar",)
    assert log.mods == ("a-1.0.jar",), "asking must not change the history"


# ----------------------------------------------------------------- the files


def test_the_history_round_trips_through_its_file(tmp_path):
    log, _ = Changelog().record(["a-1.0.jar"], "1.0.0", WHEN)
    log, _ = log.record(["a-2.0.jar"], "1.0.1", WHEN)
    path = tmp_path / "pack-history.json"
    assert log.save(path) is True
    assert Changelog.load(path) == log


def test_saving_an_unchanged_history_does_not_touch_the_file(tmp_path):
    """The same rule the mod manifests follow: an unchanged rebuild is a no-op,
    so a real diff in the file always means a real change in the pack."""
    log, _ = Changelog().record(["a-1.0.jar"], "1.0.0", WHEN)
    path = tmp_path / "pack-history.json"
    log.save(path)
    assert log.save(path) is False


def test_an_absent_history_is_not_an_error(tmp_path):
    assert Changelog.load(tmp_path / "nothing.json") == Changelog()


def test_an_unreadable_history_is_an_error(tmp_path):
    """Reading it as empty would record the whole pack as new and overwrite the
    only copy of what was there before."""
    path = tmp_path / "pack-history.json"
    path.write_text("{not json")
    with pytest.raises(ChangelogError):
        Changelog.load(path)


def test_the_markdown_names_what_changed_and_when(tmp_path):
    log, _ = Changelog().record(["lithium-fabric-0.25.3+mc26.2.jar"], "1.0.0", WHEN)
    log, _ = log.record(
        ["lithium-fabric-0.26.0+mc26.2.jar", "jei-26.2-fabric-30.24.0.165.jar"], "1.0.1", WHEN
    )
    path = tmp_path / "CHANGELOG.md"
    assert log.export(path) is True
    text = path.read_text()

    assert "## 1.0.1 -- 2026-08-26 09:46" in text
    assert "1 added, 1 updated" in text
    assert "`jei-26.2-fabric-30.24.0.165.jar`" in text
    assert "`lithium`" in text and "to `lithium-fabric-0.26.0+mc26.2.jar`" in text
    assert text.index("## 1.0.1") < text.index("## 1.0.0"), "newest first"


def test_the_markdown_says_so_when_there_is_nothing_yet():
    assert "Nothing recorded yet." in Changelog().render()


def test_the_history_file_is_json_a_person_can_read(tmp_path):
    """It is the state the next build diffs against, so it has to be checkable
    by eye when a diff comes out looking wrong."""
    log, _ = Changelog().record(["a-1.0.jar"], "1.0.0", WHEN)
    path = tmp_path / "pack-history.json"
    log.save(path)
    loaded = json.loads(path.read_text())
    assert loaded["mods"] == ["a-1.0.jar"]
    assert loaded["releases"][0]["version"] == "1.0.0"


def test_a_release_carries_the_time_it_was_built(tmp_path):
    _, release = Changelog().record(["a-1.0.jar"], "1.0.0", WHEN)
    assert release.built_at == WHEN


def test_an_empty_release_is_still_a_release_object():
    """The announcement has something to say even when nothing moved."""
    assert Release(version="1.0.0", built_at=WHEN).changed is False
