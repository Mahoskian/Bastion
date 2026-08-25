"""Tests for the generated mod manifests. No network: scans are built by hand."""

import pytest

from mcadmin.core.manifest import BANNER, Manifest
from mcadmin.core.mods import InstalledMod, ModReport, ModScan, ModStatus


def report(
    filename: str,
    *,
    name: str = "",
    slug: str = "",
    version: str = "1.0.0",
    url: str = "",
    size: int = 1024,
    status: ModStatus = ModStatus.CURRENT,
) -> ModReport:
    return ModReport(
        mod=InstalledMod(
            filename=filename,
            sha1="0" * 40,
            size=size,
            name=name,
            slug=slug,
            version_number=version,
            url=url,
        ),
        status=status,
    )


def scan(*reports: ModReport) -> ModScan:
    return ModScan(minecraft="26.2", reports=list(reports))


@pytest.fixture
def hosted() -> ModReport:
    return report(
        "jei-26.2-fabric-30.24.0.165.jar",
        name="Just Enough Items",
        slug="jei",
        version="30.24.0.165",
        url="https://cdn.modrinth.com/data/x/jei.jar",
        size=1_887_436,
    )


# ------------------------------------------------------------------ rendering


def test_a_hosted_mod_becomes_a_linked_row(hosted):
    body = Manifest.for_server(scan(hosted), "0.19.3").render()
    assert "[Just Enough Items](https://modrinth.com/mod/jei)" in body
    assert "[`jei-26.2-fabric-30.24.0.165.jar`](https://cdn.modrinth.com/data/x/jei.jar)" in body
    assert "`30.24.0.165`" in body


def test_the_header_states_what_the_folder_holds(hosted):
    body = Manifest.for_server(scan(hosted, report("b.jar", url="https://x/b.jar")), "0.19.3")
    rendered = body.render()
    assert "Minecraft **26.2**" in rendered
    assert "Fabric Loader **0.19.3**" in rendered
    assert "**2** mods" in rendered


def test_unhosted_mods_are_listed_rather_than_dropped():
    """A manifest that omitted them would describe a set that does not run."""
    manifest = Manifest.for_server(
        scan(
            report("hosted.jar", url="https://x/hosted.jar"),
            report("hand-built.jar", status=ModStatus.UNKNOWN),
        ),
        "0.19.3",
    )
    assert [r.mod.filename for r in manifest.unhosted] == ["hand-built.jar"]

    rendered = manifest.render()
    assert "## Not on Modrinth (1)" in rendered
    assert "`hand-built.jar`" in rendered
    # ...and it is not silently counted as rebuildable
    assert "[`hand-built.jar`](" not in rendered


def test_no_unhosted_section_when_everything_resolves(hosted):
    assert "Not on Modrinth" not in Manifest.for_server(scan(hosted), "0.19.3").render()


def test_a_mod_without_a_modrinth_page_is_still_named():
    rendered = Manifest.for_server(scan(report("odd.jar", url="https://x/odd.jar")), "0.19.3")
    assert "| odd.jar |" in rendered.render()


def test_generated_files_say_so(hosted):
    assert BANNER in Manifest.for_server(scan(hosted), "0.19.3").render()


def test_server_and_client_manifests_point_at_each_other(hosted):
    assert "client-install/" in Manifest.for_server(scan(hosted), "0.19.3").render()
    assert "modrinth.index.json" in Manifest.for_client(scan(hosted), "0.19.3").render()


def test_total_size_is_the_sum_of_the_jars():
    manifest = Manifest.for_server(
        scan(report("a.jar", size=1000, url="https://x/a"), report("b.jar", size=2000)),
        "0.19.3",
    )
    assert manifest.total_size == 3000


# ------------------------------------------------------------------ writing


def test_write_reports_whether_anything_changed(tmp_path, hosted):
    manifest = Manifest.for_server(scan(hosted), "0.19.3")
    target = tmp_path / "README.md"

    assert manifest.write(target) is True
    assert manifest.write(target) is False, "an unchanged folder must produce an empty diff"


def test_write_creates_the_folder_if_it_is_missing(tmp_path, hosted):
    target = tmp_path / "nested" / "README.md"
    Manifest.for_server(scan(hosted), "0.19.3").write(target)
    assert target.exists()


def test_matches_detects_drift(tmp_path, hosted):
    manifest = Manifest.for_server(scan(hosted), "0.19.3")
    target = tmp_path / "README.md"
    manifest.write(target)
    assert manifest.matches(target)

    grown = Manifest.for_server(scan(hosted, report("new.jar", url="https://x/new")), "0.19.3")
    assert not grown.matches(target)


def test_a_missing_manifest_never_matches(tmp_path, hosted):
    assert not Manifest.for_server(scan(hosted), "0.19.3").matches(tmp_path / "absent.md")
