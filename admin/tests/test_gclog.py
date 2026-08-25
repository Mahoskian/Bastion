"""Tests for GC log parsing and heap sizing."""

from mcadmin.core import gclog as gl


def line(uptime: float, tags: str, body: str, *, day: int = 24, second: int = 0) -> str:
    stamp = f"2026-08-{day:02d}T17:{second // 60:02d}:{second % 60:02d}.000-0400"
    return f"[{stamp}][{uptime:.3f}s][info ][{tags:<12}] {body}\n"


def young(gc_id: int, uptime: float, before: int, after: int, ms: float, old: tuple[int, int]):
    """A young evacuation as the JVM logs it: regions first, summary last."""
    return (
        line(uptime, "gc,start", f"GC({gc_id}) Pause Young (Mixed) (G1 Evacuation Pause)")
        + line(uptime, "gc,heap", f"GC({gc_id}) Eden regions: 100->0(100)")
        + line(uptime, "gc,heap", f"GC({gc_id}) Survivor regions: 1->2(20)")
        + line(uptime, "gc,heap", f"GC({gc_id}) Old regions: {old[0]}->{old[1]}")
        + line(uptime, "gc,heap", f"GC({gc_id}) Humongous regions: 2->2")
        + line(
            uptime,
            "gc",
            f"GC({gc_id}) Pause Young (Mixed) (G1 Evacuation Pause) "
            f"{before}M->{after}M(4096M) {ms}ms",
        )
    )


HEADER = (
    line(0.4, "gc,init", "Heap Region Size: 8M")
    + line(0.4, "gc,init", "Heap Max Capacity: 4G")
    + line(0.4, "gc,init", "CPUs: 32 total, 32 available")
)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_paren_groups_handles_nested_cause():
    assert gl._paren_groups("(Mixed) (G1 Evacuation Pause)") == ["Mixed", "G1 Evacuation Pause"]
    assert gl._paren_groups("(System.gc())") == ["System.gc()"]
    assert gl._paren_groups("") == []


def test_percentile_nearest_rank():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert gl.percentile(values, 0.5) == 5.0
    assert gl.percentile(values, 0.99) == 10.0
    assert gl.percentile([], 0.5) == 0.0


def test_regions_attach_despite_preceding_the_summary(tmp_path):
    path = write(tmp_path, "gc.log", HEADER + young(1, 200.0, 900, 300, 5.0, old=(40, 38)))
    (run,) = gl.parse([path])
    (pause,) = run.pauses
    assert pause.regions["Old"] == (40, 38)
    assert pause.region_bytes("Old", run.region_size) == 38 * 8 * 1024**2


def test_uptime_reset_starts_a_new_run(tmp_path):
    text = (
        HEADER
        + young(1, 300.0, 900, 300, 5.0, old=(40, 38))
        + HEADER  # restart: uptime rewinds
        + young(1, 250.0, 800, 200, 4.0, old=(30, 28))
    )
    runs = gl.parse([write(tmp_path, "gc.log", text)])
    assert len(runs) == 2
    assert [len(r.pauses) for r in runs] == [1, 1]


def test_rotation_does_not_split_a_run(tmp_path):
    """Uptime keeps climbing across files, so it stays one run."""
    first = write(tmp_path, "gc.log.3", HEADER + young(1, 300.0, 900, 300, 5.0, old=(40, 38)))
    second = write(tmp_path, "gc.log", young(2, 600.0, 950, 310, 6.0, old=(38, 37)))
    (run,) = gl.parse([first, second])
    assert len(run.pauses) == 2


def test_log_files_order_by_first_timestamp_not_name(tmp_path):
    write(tmp_path, "gc.log", line(0.1, "gc,init", "x", day=24))
    write(tmp_path, "gc.log.0", line(0.1, "gc,init", "x", day=23))
    assert [p.name for p in gl.log_files(tmp_path)] == ["gc.log.0", "gc.log"]


def test_live_set_comes_from_mixed_collections(tmp_path):
    text = (
        HEADER
        + young(1, 200.0, 3000, 800, 5.0, old=(120, 100))
        + young(2, 400.0, 3000, 600, 5.0, old=(100, 74))
    )
    (run,) = gl.parse([write(tmp_path, "gc.log", text)])
    analysis = gl.analyze(run)
    assert analysis.live_bytes == 600 * 1024**2
    assert analysis.live_source == "after mixed GC"
    # Old (74) + humongous (2) regions, cross-checking the heap figure.
    assert analysis.old_bytes == 76 * 8 * 1024**2


def test_live_set_falls_back_and_says_so(tmp_path):
    text = HEADER + line(300.0, "gc", "GC(1) Pause Remark 900M->880M(4096M) 3.0ms")
    (run,) = gl.parse([write(tmp_path, "gc.log", text)])
    analysis = gl.analyze(run)
    assert analysis.live_bytes == 880 * 1024**2
    assert "no mixed GC" in analysis.live_source


def test_allocation_rate_spans_the_gap_between_collections(tmp_path):
    # 300M left after GC 1; 2300M present at GC 2, 200s later -> 10M/s.
    text = (
        HEADER
        + young(1, 200.0, 3000, 300, 5.0, old=(40, 38))
        + young(2, 400.0, 2300, 300, 5.0, old=(38, 38))
    )
    (run,) = gl.parse([write(tmp_path, "gc.log", text)])
    assert gl.analyze(run).alloc_rate == 10 * 1024**2


def test_promotion_counts_only_growth_in_old(tmp_path):
    text = (
        HEADER
        + young(1, 200.0, 3000, 300, 5.0, old=(40, 48))  # +8 regions
        + young(2, 300.0, 3000, 300, 5.0, old=(48, 20))  # mixed reclaim, not promotion
    )
    (run,) = gl.parse([write(tmp_path, "gc.log", text)])
    assert gl.analyze(run).promo_rate == 8 * 8 * 1024**2 / 100.0


def test_recommendation_tracks_the_live_set(tmp_path):
    text = HEADER + young(1, 200.0, 3000, 1000, 5.0, old=(120, 100))
    (run,) = gl.parse([write(tmp_path, "gc.log", text)])
    # 1000M live x3 = 2.93G, rounded up to a whole GiB.
    assert gl.analyze(run).recommended == 3 * 1024**3


def test_recommendation_has_a_floor(tmp_path):
    text = HEADER + young(1, 200.0, 400, 100, 2.0, old=(12, 10))
    (run,) = gl.parse([write(tmp_path, "gc.log", text)])
    assert gl.analyze(run).recommended == gl.MIN_RECOMMENDED


def test_full_gc_is_called_out(tmp_path, monkeypatch):
    monkeypatch.setattr(gl, "jvm_flags", dict)
    text = (
        HEADER
        + young(1, 200.0, 3000, 900, 5.0, old=(120, 100))
        + line(300.0, "gc", "GC(2) Pause Full (System.gc()) 3800M->900M(4096M) 812.0ms")
    )
    (run,) = gl.parse([write(tmp_path, "gc.log", text)])
    analysis = gl.analyze(run)
    assert analysis.by_label["Full"] == (1, 812.0)
    assert analysis.causes["System.gc()"] == 1
    assert any("full GC" in note.message for note in analysis.notes)
    assert any(note.level is gl.Level.ERROR for note in analysis.notes)


def test_safepoints_split_gc_from_everything_else(tmp_path):
    text = HEADER + "".join(
        line(
            200.0,
            "safepoint",
            f'Safepoint "{name}", Time since last: 100 ns, Reaching safepoint: 10 ns, '
            "At safepoint: 900 ns, Leaving safepoint: 5 ns, Total: 1000000 ns, "
            "Threads: 1 runnable, 17 total",
        )
        for name in ("G1CollectForAllocation", "CollectForMetadataAllocation", "ThreadDump")
    )
    (run,) = gl.parse([write(tmp_path, "gc.log", text)])
    analysis = gl.analyze(run)
    assert analysis.stw_total_ms == 3.0
    assert analysis.non_gc_stw_ms == 1.0  # only ThreadDump
    assert "ThreadDump" in analysis.non_gc_worst
