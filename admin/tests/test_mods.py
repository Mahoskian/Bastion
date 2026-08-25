"""Tests for the mod audit. No network: the Modrinth API is faked."""

import hashlib

import pytest

from mcadmin.core.models import Paths
from mcadmin.core.modrinth import chunked
from mcadmin.core.mods import ModScanner, ModStatus


class FakeApi:
    """Serves canned answers and counts how many bulk calls were made."""

    def __init__(self, known=None, latest=None, upgrade=None, titles=None) -> None:
        self.known = known or {}
        self.latest = latest or {}
        self.upgrade = upgrade or {}
        self.titles = titles or {}
        self.calls = 0

    def versions_by_hash(self, hashes):
        self.calls += 1
        return {h: self.known[h] for h in hashes if h in self.known}

    def latest_by_hash(self, hashes, loaders=("fabric",), game_versions=()):
        self.calls += 1
        source = self.upgrade if "26.3" in list(game_versions) else self.latest
        return {h: source[h] for h in hashes if h in source}

    def projects(self, ids):
        self.calls += 1
        return {i: {"id": i, "title": self.titles.get(i, i)} for i in ids}


def version(project: str, number: str, sha1: str, game_versions=("26.2",)) -> dict:
    return {
        "project_id": project,
        "version_number": number,
        "game_versions": list(game_versions),
        "files": [{"primary": True, "hashes": {"sha1": sha1}}],
    }


@pytest.fixture
def server(tmp_path):
    (tmp_path / "fabric-server-mc.26.2-loader.0.19.3-launcher.1.1.2.jar").write_text("")
    mods = tmp_path / "mods"
    mods.mkdir()
    return Paths(server_dir=tmp_path)


def add_jar(paths: Paths, name: str, content: bytes) -> str:
    (paths.mods_dir / name).write_bytes(content)
    return hashlib.sha1(content).hexdigest()


# ------------------------------------------------------------------ statuses


def test_a_mod_on_the_newest_build_is_current(server):
    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(
        known={sha: version("p1", "1.0", sha)},
        latest={sha: version("p1", "1.0", sha)},
        titles={"p1": "Mod A"},
    )
    (report,) = ModScanner(server, api).scan().reports
    assert report.status is ModStatus.CURRENT
    assert report.mod.name == "Mod A"


def test_a_newer_build_makes_a_mod_outdated(server):
    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(
        known={sha: version("p1", "1.0", sha)},
        latest={sha: version("p1", "2.0", "different-hash")},
    )
    (report,) = ModScanner(server, api).scan().reports
    assert report.status is ModStatus.OUTDATED
    assert report.latest_version == "2.0"
    assert report.detail == "1.0 -> 2.0"


def test_a_file_modrinth_does_not_know_is_unknown(server):
    add_jar(server, "homemade.jar", b"x")
    (report,) = ModScanner(server, FakeApi()).scan().reports
    assert report.status is ModStatus.UNKNOWN
    assert report.mod.filename == "homemade.jar"


def test_a_build_that_never_claimed_this_minecraft_is_behind(server):
    """Distinct from merely outdated, and a louder problem."""
    sha = add_jar(server, "old.jar", b"old")
    api = FakeApi(
        known={sha: version("p1", "1.0", sha, game_versions=("26.1",))},
        latest={sha: version("p1", "1.0", sha)},
    )
    (report,) = ModScanner(server, api).scan().reports
    assert report.status is ModStatus.BEHIND
    assert "26.1" in report.detail


def test_a_build_declaring_this_minecraft_is_not_behind(server):
    """The filename may say 26.1.x while the build declares 26.2 as well."""
    sha = add_jar(server, "animalgarden-1.0-fabric-26.1.1.jar", b"ag")
    api = FakeApi(
        known={sha: version("p1", "1.0", sha, game_versions=("26.1", "26.1.2", "26.2"))},
        latest={sha: version("p1", "1.0", sha)},
    )
    (report,) = ModScanner(server, api).scan().reports
    assert report.status is ModStatus.CURRENT


def test_no_build_for_this_minecraft(server):
    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(known={sha: version("p1", "1.0", sha)}, latest={})
    (report,) = ModScanner(server, api).scan().reports
    assert report.status is ModStatus.NO_BUILD


# ------------------------------------------------------------------ upgrades


def test_target_check_finds_what_would_block_an_upgrade(server):
    ready = add_jar(server, "ready.jar", b"ready")
    stuck = add_jar(server, "stuck.jar", b"stuck")
    api = FakeApi(
        known={ready: version("p1", "1.0", ready), stuck: version("p2", "1.0", stuck)},
        latest={ready: version("p1", "1.0", ready), stuck: version("p2", "1.0", stuck)},
        upgrade={ready: version("p1", "2.0", "newhash", game_versions=("26.3",))},
    )
    scan = ModScanner(server, api).scan(target="26.3")
    assert [r.mod.filename for r in scan.blockers] == ["stuck.jar"]


def test_no_blockers_when_every_mod_has_a_target_build(server):
    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(
        known={sha: version("p1", "1.0", sha)},
        latest={sha: version("p1", "1.0", sha)},
        upgrade={sha: version("p1", "2.0", "new", game_versions=("26.3",))},
    )
    assert ModScanner(server, api).scan(target="26.3").blockers == []


def test_blockers_are_empty_without_a_target(server):
    add_jar(server, "a.jar", b"a")
    assert ModScanner(server, FakeApi()).scan().blockers == []


# ------------------------------------------------------------------ shape


def test_the_whole_set_is_looked_up_in_bulk(server):
    for i in range(20):
        add_jar(server, f"mod{i}.jar", f"content{i}".encode())
    api = FakeApi()
    ModScanner(server, api).scan()
    # versions_by_hash + latest_by_hash, and no project lookup with nothing known.
    assert api.calls == 2


def test_reports_are_sorted_by_name(server):
    for name, content in (("b.jar", b"b"), ("a.jar", b"a")):
        add_jar(server, name, content)
    scan = ModScanner(server, FakeApi()).scan()
    assert [r.mod.label for r in scan.reports] == ["a.jar", "b.jar"]


def test_counts_tally_by_status(server):
    add_jar(server, "a.jar", b"a")
    add_jar(server, "b.jar", b"b")
    scan = ModScanner(server, FakeApi()).scan()
    assert scan.counts() == {ModStatus.UNKNOWN: 2}


def test_an_empty_mods_directory_scans_to_nothing(server):
    assert ModScanner(server, FakeApi()).scan().reports == []


def test_actionable_excludes_current_mods(server):
    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(known={sha: version("p1", "1.0", sha)}, latest={sha: version("p1", "1.0", sha)})
    assert ModScanner(server, api).scan().actionable == []


@pytest.mark.parametrize(
    ("status", "actionable"),
    [
        (ModStatus.CURRENT, False),
        (ModStatus.OUTDATED, True),
        (ModStatus.BEHIND, True),
        (ModStatus.NO_BUILD, True),
        (ModStatus.UNKNOWN, False),
    ],
)
def test_which_statuses_need_action(status, actionable):
    assert status.is_actionable is actionable


def test_chunking_splits_large_batches():
    assert [list(c) for c in chunked(list("abcde"), 2)] == [["a", "b"], ["c", "d"], ["e"]]
    assert list(chunked([], 2)) == []


# ------------------------------------------------------------------ target direction


def dated(project: str, number: str, sha1: str, published: str, game_versions=("26.2",)) -> dict:
    entry = version(project, number, sha1, game_versions)
    entry["date_published"] = published
    entry["files"][0].update(
        {"url": f"https://cdn.modrinth.com/{number}.jar", "filename": f"{number}.jar", "size": 10}
    )
    return entry


def test_an_older_target_build_is_a_downgrade(server):
    from mcadmin.core.mods import TargetAction

    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(
        known={sha: dated("p1", "2.0", sha, "2026-08-10T00:00:00Z")},
        latest={sha: dated("p1", "2.0", sha, "2026-08-10T00:00:00Z")},
        upgrade={sha: dated("p1", "1.5", "old", "2026-07-01T00:00:00Z", ("26.1",))},
    )
    (report,) = ModScanner(server, api).scan(target="26.3").reports
    assert report.target_action is TargetAction.DOWNGRADE
    assert report.target_detail == "2.0 -> 1.5"


def test_a_newer_target_build_is_an_upgrade(server):
    from mcadmin.core.mods import TargetAction

    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(
        known={sha: dated("p1", "1.0", sha, "2026-07-01T00:00:00Z")},
        latest={sha: dated("p1", "1.0", sha, "2026-07-01T00:00:00Z")},
        upgrade={sha: dated("p1", "2.0", "new", "2026-08-10T00:00:00Z", ("26.3",))},
    )
    (report,) = ModScanner(server, api).scan(target="26.3").reports
    assert report.target_action is TargetAction.UPGRADE


def test_a_build_declaring_the_target_needs_no_change(server):
    """Half the real mod set declares several Minecraft versions at once."""
    from mcadmin.core.mods import TargetAction

    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(
        known={sha: dated("p1", "1.0", sha, "2026-07-01T00:00:00Z", ("26.1", "26.2"))},
        latest={sha: dated("p1", "1.0", sha, "2026-07-01T00:00:00Z", ("26.1", "26.2"))},
        upgrade={sha: dated("p1", "9.9", "other", "2026-08-01T00:00:00Z", ("26.1",))},
    )
    (report,) = ModScanner(server, api).scan(target="26.1").reports
    assert report.target_action is TargetAction.COMPATIBLE
    assert report.target_detail == "already compatible"
    assert report.target_download is None, "nothing to download when nothing changes"


def test_no_target_means_no_target_action(server):
    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(known={sha: dated("p1", "1.0", sha, "2026-07-01T00:00:00Z")})
    (report,) = ModScanner(server, api).scan().reports
    assert report.target_action is None


def test_needs_change_excludes_compatible_mods(server):
    ok = add_jar(server, "ok.jar", b"ok")
    move = add_jar(server, "move.jar", b"move")
    api = FakeApi(
        known={
            ok: dated("p1", "1.0", ok, "2026-07-01T00:00:00Z", ("26.1", "26.2")),
            move: dated("p2", "1.0", move, "2026-07-01T00:00:00Z"),
        },
        latest={},
        upgrade={move: dated("p2", "0.9", "older", "2026-06-01T00:00:00Z", ("26.1",))},
    )
    scan = ModScanner(server, api).scan(target="26.1")
    assert [r.mod.filename for r in scan.needs_change] == ["move.jar"]


# ------------------------------------------------------------------ downloads


def test_an_outdated_mod_carries_a_download(server):
    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(
        known={sha: dated("p1", "1.0", sha, "2026-07-01T00:00:00Z")},
        latest={sha: dated("p1", "2.0", "newhash", "2026-08-01T00:00:00Z")},
    )
    (report,) = ModScanner(server, api).scan().reports
    assert report.latest_download.filename == "2.0.jar"
    assert report.download_for(target=False) is report.latest_download


def test_a_current_mod_has_nothing_to_download(server):
    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(
        known={sha: dated("p1", "1.0", sha, "2026-07-01T00:00:00Z")},
        latest={sha: dated("p1", "1.0", sha, "2026-07-01T00:00:00Z")},
    )
    (report,) = ModScanner(server, api).scan().reports
    assert report.latest_download is None


def test_the_project_page_is_derived_from_the_slug(server):
    sha = add_jar(server, "a.jar", b"a")
    api = FakeApi(known={sha: dated("p1", "1.0", sha, "2026-07-01T00:00:00Z")})
    api.projects = lambda ids: {i: {"id": i, "title": "Mod A", "slug": "mod-a"} for i in ids}
    (report,) = ModScanner(server, api).scan().reports
    assert report.mod.page == "https://modrinth.com/mod/mod-a"


# ------------------------------------------------------------------ fetching


class FakeDownloader:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.requested: list[str] = []

    def fetch_bytes(self, url: str) -> bytes:
        self.requested.append(url)
        if url not in self.blobs:
            from mcadmin.core.modrinth import ModrinthError

            raise ModrinthError("404")
        return self.blobs[url]


def download_for(blob: bytes, name: str = "mod.jar"):
    from mcadmin.core.mods import Download

    return Download(
        url=f"https://cdn/{name}",
        filename=name,
        size=len(blob),
        sha1=hashlib.sha1(blob).hexdigest(),
        sha512=hashlib.sha512(blob).hexdigest(),
    )


def test_a_verified_download_is_written(tmp_path):
    from mcadmin.core.mods import ModFetcher

    blob = b"a real jar"
    download = download_for(blob)
    fetcher = ModFetcher(tmp_path / "staged", FakeDownloader({download.url: blob}))
    (result,) = fetcher.fetch([download])
    assert result.ok and not result.skipped
    assert result.path.read_bytes() == blob


def test_a_tampered_download_is_never_written(tmp_path):
    """A jar that does not match its hash must not reach the disk."""
    from mcadmin.core.mods import ModFetcher

    download = download_for(b"a real jar")
    fetcher = ModFetcher(tmp_path / "staged", FakeDownloader({download.url: b"something else"}))
    (result,) = fetcher.fetch([download])
    assert not result.ok
    assert "mismatch" in result.error
    assert not (tmp_path / "staged" / "mod.jar").exists()


def test_an_already_staged_file_is_skipped(tmp_path):
    from mcadmin.core.mods import ModFetcher

    blob = b"a real jar"
    download = download_for(blob)
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "mod.jar").write_bytes(blob)
    downloader = FakeDownloader({download.url: blob})
    (result,) = ModFetcher(staged, downloader).fetch([download])
    assert result.skipped
    assert downloader.requested == [], "a verified file must not be re-downloaded"


def test_a_corrupt_staged_file_is_replaced(tmp_path):
    from mcadmin.core.mods import ModFetcher

    blob = b"a real jar"
    download = download_for(blob)
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "mod.jar").write_bytes(b"truncated")
    (result,) = ModFetcher(staged, FakeDownloader({download.url: blob})).fetch([download])
    assert result.ok and not result.skipped
    assert (staged / "mod.jar").read_bytes() == blob


def test_a_failed_download_is_reported_not_raised(tmp_path):
    from mcadmin.core.mods import ModFetcher

    download = download_for(b"jar")
    (result,) = ModFetcher(tmp_path / "staged", FakeDownloader({})).fetch([download])
    assert not result.ok and result.path is None


def test_fetching_nothing_creates_no_directory(tmp_path):
    from mcadmin.core.mods import ModFetcher

    staged = tmp_path / "staged"
    assert ModFetcher(staged, FakeDownloader({})).fetch([]) == []
    assert not staged.exists()


# ------------------------------------------------------------------ installing


def staged(tmp_path, name: str, blob: bytes, destination, replaces=None):
    from mcadmin.core.mods import StagedMod

    (tmp_path / name).write_bytes(blob)
    return StagedMod(
        filename=name,
        destination=destination,
        replaces=replaces,
        label=name,
        sha512=hashlib.sha512(blob).hexdigest(),
    )


@pytest.fixture
def staging(tmp_path):
    stage = tmp_path / "fetch-mods"
    stage.mkdir()
    mods = tmp_path / "mods"
    mods.mkdir()
    return stage, mods


def test_install_puts_the_jar_in_place_and_archives_the_old_one(staging, tmp_path):
    from mcadmin.core.mods import ModInstaller

    stage, mods = staging
    old = mods / "mod-1.0.jar"
    old.write_bytes(b"old")
    entry = staged(stage, "mod-2.0.jar", b"new", mods, replaces=old)

    archive = tmp_path / "replaced"
    (result,) = ModInstaller(stage, archive).install([entry])

    assert result.ok
    assert (mods / "mod-2.0.jar").read_bytes() == b"new"
    assert not old.exists(), "the replaced jar must not be left alongside its replacement"
    assert (archive / "mod-1.0.jar").read_bytes() == b"old"
    assert not (stage / "mod-2.0.jar").exists(), "staging is cleared once installed"


def test_one_staged_jar_can_serve_both_mod_sets(tmp_path):
    """Mods present on both sides share a single download."""
    from mcadmin.core.mods import ModInstaller

    stage = tmp_path / "fetch-mods"
    stage.mkdir()
    server_mods = tmp_path / "mods"
    client_mods = tmp_path / "client-install" / "mods"
    server_mods.mkdir()
    client_mods.mkdir(parents=True)

    blob = b"shared jar"
    entries = [
        staged(stage, "shared-2.0.jar", blob, server_mods),
        staged(stage, "shared-2.0.jar", blob, client_mods),
    ]
    results = ModInstaller(stage, tmp_path / "replaced").install(entries)

    assert all(r.ok for r in results)
    assert (server_mods / "shared-2.0.jar").read_bytes() == blob
    assert (client_mods / "shared-2.0.jar").read_bytes() == blob


def test_a_staged_file_that_fails_verification_is_not_installed(staging, tmp_path):
    from mcadmin.core.mods import ModInstaller, StagedMod

    stage, mods = staging
    (stage / "mod.jar").write_bytes(b"corrupted in staging")
    entry = StagedMod(filename="mod.jar", destination=mods, sha512="0" * 128)
    (result,) = ModInstaller(stage, tmp_path / "replaced").install([entry])
    assert not result.ok
    assert not (mods / "mod.jar").exists()


def test_a_missing_staged_file_is_reported(staging, tmp_path):
    from mcadmin.core.mods import ModInstaller, StagedMod

    stage, mods = staging
    entry = StagedMod(filename="absent.jar", destination=mods)
    (result,) = ModInstaller(stage, tmp_path / "replaced").install([entry])
    assert "not staged" in result.error


def test_a_missing_destination_is_reported(staging, tmp_path):
    from mcadmin.core.mods import ModInstaller

    stage, _ = staging
    entry = staged(stage, "mod.jar", b"x", tmp_path / "nowhere")
    (result,) = ModInstaller(stage, tmp_path / "replaced").install([entry])
    assert "no directory" in result.error


def test_installing_without_a_previous_version_archives_nothing(staging, tmp_path):
    from mcadmin.core.mods import ModInstaller

    stage, mods = staging
    entry = staged(stage, "brand-new.jar", b"x", mods)
    (result,) = ModInstaller(stage, tmp_path / "replaced").install([entry])
    assert result.ok and result.archived is None


def test_the_side_is_read_from_the_destination(tmp_path):
    from mcadmin.core.mods import StagedMod

    server = StagedMod(filename="a.jar", destination=tmp_path / "mods")
    client = StagedMod(filename="a.jar", destination=tmp_path / "client-install" / "mods")
    assert server.side == "server"
    assert client.side == "client"


def test_manifest_round_trips(tmp_path):
    from datetime import datetime

    from mcadmin.core.mods import StagedMod, StageManifest

    manifest = StageManifest(
        created=datetime.now(),
        minecraft="26.2",
        entries=[StagedMod(filename="a.jar", destination=tmp_path / "mods", label="A")],
    )
    manifest.save(tmp_path)
    loaded = StageManifest.load(tmp_path)
    assert loaded.entries[0].label == "A"
    assert loaded.minecraft == "26.2"


def test_a_missing_manifest_is_none_not_an_error(tmp_path):
    from mcadmin.core.mods import StageManifest

    assert StageManifest.load(tmp_path) is None
