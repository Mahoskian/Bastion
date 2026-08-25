"""Rendering the metrics time series."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from ..core.metrics import GcRecord, MetricsStore, Sample, Series, cpu_percent, series
from ..core.units import human_bytes, human_seconds
from .console import console

BLOCKS = "▁▂▃▄▅▆▇█"
WIDTH = 46
# A series that varies by less than this fraction of its peak is flat. Without
# the floor, min-max scaling turns a rounding-level wobble in a 14G RSS into a
# full-height cliff, which reads as a real event and is not one.
FLAT_THRESHOLD = 0.01


def sparkline(values: list[float], width: int = WIDTH) -> str:
    """A fixed-width glyph line. Buckets are averaged, so the shape holds
    whether there are 20 points or 20,000."""
    if not values:
        return ""
    if len(values) > width:
        size = len(values) / width
        buckets = [
            values[int(i * size) : max(int((i + 1) * size), int(i * size) + 1)]
            for i in range(width)
        ]
        values = [sum(bucket) / len(bucket) for bucket in buckets if bucket]
    low, high = min(values), max(values)
    span = high - low
    if span <= abs(high) * FLAT_THRESHOLD:
        return BLOCKS[0] * len(values)
    top = len(BLOCKS) - 1
    return "".join(BLOCKS[min(top, int((value - low) / span * len(BLOCKS)))] for value in values)


def _row(table: Table, label: Series, fmt) -> None:
    if not label.points:
        return
    table.add_row(
        label.name,
        fmt(label.latest),
        fmt(label.mean),
        fmt(label.peak),
        sparkline(label.values),
    )


def show_metrics(samples: list[Sample], store: MetricsStore) -> None:
    if not samples:
        console.print("No samples yet. Run [bold]mc metrics sample[/], or let cron do it.")
        return

    span = samples[-1].at - samples[0].at
    console.print(
        Panel(
            f"[bold]{samples[0].at:%Y-%m-%d %H:%M}[/] -> [bold]{samples[-1].at:%Y-%m-%d %H:%M}[/]"
            f"  [dim]{len(samples)} samples over {human_seconds(span.total_seconds())}"
            f", {store.count()} total on file[/]",
            expand=False,
        )
    )

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("metric")
    for name in ("now", "mean", "peak"):
        table.add_column(name, justify="right")
    table.add_column("trend")

    _row(table, series(samples, "players"), lambda v: f"{v:.0f}")
    _row(table, cpu_percent(samples), lambda v: f"{v:.0f}%")
    _row(table, series(samples, "heap_used").model_copy(update={"name": "heap"}), human_bytes)
    _row(table, series(samples, "rss_bytes").model_copy(update={"name": "rss"}), human_bytes)
    _row(table, series(samples, "world_bytes").model_copy(update={"name": "world"}), human_bytes)
    console.print(table)

    states = {sample.state for sample in samples}
    if len(states) > 1:
        console.print(f"[dim]states seen: {', '.join(sorted(s.value for s in states))}[/]")


def show_gc_history(records: list[GcRecord]) -> None:
    if not records:
        console.print(
            "No GC runs recorded yet. Run [bold]mc gc --record[/] to keep the current "
            "analysis past the log's rotation."
        )
        return
    table = Table(header_style="bold")
    table.add_column("started")
    for name in ("ran", "GCs", "full", "live", "alloc/s", "p99", "ovhd", "rec"):
        table.add_column(name, justify="right")
    for record in records:
        table.add_row(
            f"{record.run_started:%m-%d %H:%M}",
            human_seconds(record.seconds),
            str(record.collections),
            f"[red]{record.full_gcs}[/]" if record.full_gcs else "0",
            human_bytes(record.live_bytes),
            human_bytes(record.alloc_rate),
            f"{record.p99_ms:.1f}ms",
            f"{record.gc_overhead * 100:.2f}%",
            human_bytes(record.recommended),
        )
    console.print(table)
    console.print(f"[dim]{len(records)} run(s) recorded[/]")


def show_sample(sample: Sample) -> None:
    parts = [f"state={sample.state.value}"]
    if sample.players is not None:
        parts.append(f"players={sample.players}")
    if sample.heap_used:
        parts.append(f"heap={human_bytes(sample.heap_used)}")
    if sample.rss_bytes:
        parts.append(f"rss={human_bytes(sample.rss_bytes)}")
    if sample.world_bytes:
        parts.append(f"world={human_bytes(sample.world_bytes)}")
    console.print(f"[dim]{sample.at:%H:%M:%S}[/] " + "  ".join(parts))
