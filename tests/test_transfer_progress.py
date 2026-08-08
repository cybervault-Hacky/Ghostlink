"""Progress rendering helpers: bars, speeds, ETAs, tables (Phase 4)."""

from __future__ import annotations

import time

from ghostlink.transfer.models import TransferDirection, TransferSnapshot, TransferState
from ghostlink.transfer.progress import (
    SpeedMeter,
    format_speed,
    progress_line,
    render_bar,
    state_label,
    transfers_table,
)
from ghostlink.utils.text import format_bytes


def _snapshot(**overrides: object) -> TransferSnapshot:
    fields: dict[str, object] = {
        "transfer_id": "tf_ab12cd34",
        "direction": TransferDirection.SENDING,
        "state": TransferState.TRANSFERRING,
        "filename": "report.pdf",
        "size_bytes": 18_874_368,  # 18.0 MB exactly
        "mime": "application/pdf",
        "chunk_size": 4096,
        "total_chunks": 4608,
        "chunks_done": 3779,
        "bytes_done": 15_480_000,  # ~82%
        "peer": "Ravi",
        "created_at": time.time(),
        "last_activity_at": time.time(),
        "expires_at": time.time() + 3600,
        "error": None,
        "saved_path": None,
        "integrity": "ab" * 32,
    }
    fields.update(overrides)
    return TransferSnapshot(**fields)  # type: ignore[arg-type]


class TestFormatBytes:
    def test_units(self) -> None:
        assert format_bytes(0) == "0 B"
        assert format_bytes(512) == "512 B"
        assert format_bytes(2048) == "2.0 KB"
        assert format_bytes(18.4 * 1024 * 1024) == "18.4 MB"
        assert format_bytes(3 * 1024**3) == "3.0 GB"
        assert format_bytes(-5) == "0 B"

    def test_speed_suffix(self) -> None:
        assert format_speed(2.8 * 1024 * 1024) == "2.8 MB/s"


class TestSpeedMeter:
    def test_throughput_and_eta(self) -> None:
        meter = SpeedMeter(window_seconds=10.0)
        start = time.monotonic()
        meter.sample(0, at=start)
        meter.sample(500_000, at=start + 2.0)
        assert meter.bytes_per_second == 250_000.0
        assert meter.eta_seconds(remaining_bytes=1_000_000) == 4.0
        assert meter.eta_seconds(remaining_bytes=0) == 0.0

    def test_single_sample_has_no_speed(self) -> None:
        meter = SpeedMeter()
        meter.sample(100)
        assert meter.bytes_per_second == 0.0
        assert meter.eta_seconds(remaining_bytes=10) is None

    def test_window_eviction(self) -> None:
        meter = SpeedMeter(window_seconds=1.0)
        meter.sample(0, at=100.0)
        meter.sample(100, at=100.2)
        meter.sample(200, at=102.0)  # the oldest sample falls out
        assert meter.bytes_per_second > 0


class TestRenderBar:
    def test_fraction_to_cells(self) -> None:
        bar = render_bar(0.5, width=10)
        assert bar == "[█████░░░░░]"
        assert render_bar(1.0, width=6) == "[██████]"
        assert render_bar(0.0, width=6) == "[░░░░░░]"

    def test_clamps_and_minimum_width(self) -> None:
        assert render_bar(1.4, width=6) == "[██████]"
        assert len(render_bar(-1.0, width=1)) == 7  # minimum 5 cells + brackets


class TestProgressLine:
    def test_full_width_line_matches_ux_shape(self) -> None:
        meter = SpeedMeter()
        snapshot = _snapshot()
        meter.sample(1_000_000)
        meter.sample(15_480_000)
        line = progress_line(snapshot, meter=meter, width=100)
        assert line.startswith("[")
        assert "82%" in line
        assert "14.8 MB / 18.0 MB" in line
        assert "Speed:" in line

    def test_narrow_line_drops_extras_but_keeps_percent(self) -> None:
        line = progress_line(_snapshot(), meter=SpeedMeter(), width=32)
        assert "82%" in line
        assert line.startswith("[")

    def test_paused_line_hides_speed(self) -> None:
        meter = SpeedMeter()
        meter.sample(1)
        meter.sample(2)
        line = progress_line(_snapshot(state=TransferState.PAUSED), meter=meter, width=100)
        assert "Speed:" not in line


class TestTables:
    def test_state_labels_cover_every_state(self) -> None:
        for state in TransferState:
            assert state_label(state)

    def test_transfers_table_renders_rows(self) -> None:
        from ghostlink.ui.console import ConsoleManager
        from ghostlink.ui.themes import ThemeEngine

        console = ConsoleManager(ThemeEngine().get("phantom"), record=True, width=100)
        table = transfers_table([_snapshot(), _snapshot(transfer_id="tf_00ff00ff")])
        console.print(table)
        output = console.export_text()
        assert "tf_ab12cd34" in output
        assert "report.pdf" in output
        assert "transferring" in output
        assert "↑ out" in output
