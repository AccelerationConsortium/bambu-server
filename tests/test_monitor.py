"""Activity-axis behaviour of the monitor (STATUS_SPEC §2.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bambu_server.backend import PrinterReading
from bambu_server.config import PrinterDefinition
from bambu_server.monitor import PrinterMonitor

from .conftest import FakeBackend


def _reading(gcode_state: str, *, age_seconds: float = 0.0) -> PrinterReading:
    return PrinterReading(
        data_updated_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        connected=True,
        data_ready=True,
        gcode_state=gcode_state,
        activity=gcode_state,
    )


def _monitor(backend: FakeBackend) -> PrinterMonitor:
    return PrinterMonitor(
        PrinterDefinition(
            id="bambu_test_01",
            name="Bambu Test 01",
            model="X1 Carbon",
            env_prefix="BAMBU_TEST_01",
        ),
        backend,
        poll_interval_seconds=2.0,
        stale_after_seconds=20.0,
    )


async def test_activity_since_is_null_until_a_transition_is_observed() -> None:
    """A cold start mid-print cannot know when the job began."""
    monitor = _monitor(FakeBackend(_reading("RUNNING")))
    await monitor._collect_once()

    status = monitor.status()
    assert status.equipment_status == "busy"
    assert status.activity == "running"
    assert status.activity_since is None


async def test_activity_since_marks_the_observed_transition() -> None:
    backend = FakeBackend(_reading("IDLE"))
    monitor = _monitor(backend)
    await monitor._collect_once()
    assert monitor.status().activity == "idle"

    backend.reading = _reading("RUNNING")
    await monitor._collect_once()

    started = monitor.status().activity_since
    assert started is not None
    assert monitor.status().activity == "running"

    # A later poll with unchanged activity must not move the timestamp, and
    # neither must repeated status builds.
    backend.reading = _reading("RUNNING")
    await monitor._collect_once()
    assert monitor.status().activity_since == started
    assert monitor.status().activity_since == started


async def test_stale_telemetry_clears_activity_mid_job() -> None:
    backend = FakeBackend(_reading("IDLE"))
    monitor = _monitor(backend)
    await monitor._collect_once()
    backend.reading = _reading("RUNNING")
    await monitor._collect_once()
    assert monitor.status().activity_since is not None

    # Telemetry stops arriving: the printer may well still be printing, but the
    # service can no longer observe it.
    backend.reading = _reading("RUNNING", age_seconds=600)
    await monitor._collect_once()

    status = monitor.status()
    assert status.equipment_status == "unknown"
    assert status.activity == "unknown"
    assert status.activity_since is None


async def test_monitor_failure_reports_unknown_activity() -> None:
    class BrokenBackend(FakeBackend):
        def read(self) -> PrinterReading:
            raise RuntimeError("mqtt exploded")

    monitor = _monitor(BrokenBackend(_reading("RUNNING")))
    await monitor._collect_once()

    status = monitor.status()
    assert status.equipment_status == "unknown"
    assert status.activity == "unknown"
    assert status.activity_since is None
