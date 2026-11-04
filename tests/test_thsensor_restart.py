"""Replay YS8015-UC captures across a restart.

The hub's ``getState`` carries only the last full ``THSensor.Report`` (up to
4 hours old), never the ``THSensor.DataRecord`` samples that arrive every 30
minutes.  ``captures/devices/THSensor-2/http_initial.json`` shows it: read at
21:29Z, it still reports the 17:30Z report although the device had sent a
batch of samples every half hour since.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.yolocal.api import Device, DeviceEvent
from custom_components.yolocal.coordinator import YoLocalCoordinator
from custom_components.yolocal.sensor import (
    YoLocalHumiditySensor,
    YoLocalTemperatureSensor,
)

CAPTURES = Path(__file__).resolve().parents[1] / "captures/devices/THSensor-2"
DEVICE = Device("THSensor-2", "Backyard Air", "token", "THSensor", "THSensor", "YS8015-UC")


def load(name: str) -> dict:
    return json.loads((CAPTURES / name).read_text())


def make_coordinator(stored: dict | None = None) -> YoLocalCoordinator:
    coordinator = YoLocalCoordinator(
        hass=SimpleNamespace(bus=SimpleNamespace(async_fire=Mock())),
        client=SimpleNamespace(host="127.0.0.1"),
        token_manager=SimpleNamespace(client_id="test"),
        session=SimpleNamespace(),
        config_entry_id="entry",
        net_id="test",
    )
    coordinator._contact_store.data = stored
    asyncio.run(coordinator._async_load_contact_state())
    coordinator._devices[DEVICE.device_id] = DEVICE
    return coordinator


def set_now(monkeypatch, when: str) -> None:
    now = datetime.fromisoformat(when).astimezone(UTC)
    monkeypatch.setattr(
        "custom_components.yolocal.coordinator.dt_util.utcnow", lambda: now
    )
    monkeypatch.setattr(
        "custom_components.yolocal.coordinator.monotonic", lambda: now.timestamp()
    )


def replay(coordinator, monkeypatch, name: str) -> None:
    capture = load(name)
    set_now(monkeypatch, capture["captured_at"])
    coordinator._on_device_event(DeviceEvent.from_payload(capture["mqtt_payload"]))


def poll(coordinator, monkeypatch, when: str, get_state: dict) -> None:
    set_now(monkeypatch, when)

    async def get_state_with_retry(_device):
        return copy.deepcopy(get_state)

    coordinator._async_get_state_with_retry = get_state_with_retry
    asyncio.run(coordinator._async_update_data())


def readings(coordinator) -> tuple[float, float]:
    return (
        YoLocalTemperatureSensor(coordinator, DEVICE).native_value,
        YoLocalHumiditySensor(coordinator, DEVICE).native_value,
    )


def get_state_after(report_name: str) -> dict:
    """The hub's getState as it reads after a full report: that report's values."""
    state = copy.deepcopy(load("http_initial.json")["response"]["data"])
    report = load(report_name)
    data = report["mqtt_payload"]["data"]
    state["state"].update(temperature=data["temperature"], humidity=data["humidity"])
    state["reportAt"] = report["captured_at"]
    return state


def run_first_session(monkeypatch) -> dict:
    """Report at 21:30Z, then the 04:30Z batch (newest sample 04:20:56Z)."""
    coordinator = make_coordinator()
    poll(
        coordinator,
        monkeypatch,
        "2026-10-24T21:29:00+00:00",
        load("http_initial.json")["response"]["data"],
    )
    assert readings(coordinator) == (12.7, 61.6)
    for name in ("mqtt_0001.json", "mqtt_0002.json", "mqtt_0003.json"):
        replay(coordinator, monkeypatch, name)
    assert readings(coordinator) == (14.3, 57.9)
    return coordinator._contact_store.data


def test_restart_keeps_recorded_samples_newer_than_get_state(monkeypatch) -> None:
    stored = run_first_session(monkeypatch)

    restarted = make_coordinator(stored)
    poll(restarted, monkeypatch, "2026-10-25T04:45:00+00:00", get_state_after("mqtt_0001.json"))

    # Without the stored samples this would replay the 21:30Z report (17.2, 49.7).
    assert readings(restarted) == (14.3, 57.9)
    assert restarted.get_state(DEVICE.device_id)["reportAt"] == load("mqtt_0001.json")["captured_at"]

    replay(restarted, monkeypatch, "mqtt_0004.json")
    assert readings(restarted) == (14, 61.7)


def test_newer_full_report_beats_stored_samples_after_restart(monkeypatch) -> None:
    stored = run_first_session(monkeypatch)

    restarted = make_coordinator(stored)
    poll(restarted, monkeypatch, "2026-10-25T05:31:30+00:00", get_state_after("mqtt_0005.json"))

    report = load("mqtt_0005.json")["mqtt_payload"]["data"]
    assert readings(restarted) == (report["temperature"], report["humidity"])


def test_samples_are_stored_with_their_sample_time(monkeypatch) -> None:
    stored = run_first_session(monkeypatch)

    assert stored["th_measurements"] == {
        DEVICE.device_id: {
            "temperature": [14.3, "2026-10-25T04:20:56+00:00"],
            "humidity": [57.9, "2026-10-25T04:20:56+00:00"],
        }
    }


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "bad",
        {"temperature": ["14.3", "2026-10-25T04:20:56+00:00"]},
        {"temperature": [True, "2026-10-25T04:20:56+00:00"]},
        {"temperature": [14.3, "not a time"]},
        {"temperature": [14.3]},
        {"pressure": [1013, "2026-10-25T04:20:56+00:00"]},
    ],
)
def test_malformed_stored_samples_are_ignored(monkeypatch, entry) -> None:
    restarted = make_coordinator({"th_measurements": {DEVICE.device_id: entry}})
    poll(restarted, monkeypatch, "2026-10-25T04:45:00+00:00", get_state_after("mqtt_0001.json"))

    assert readings(restarted) == (17.2, 49.7)
