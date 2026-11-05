"""Regression coverage for recorded THSensor measurements and live reports."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from custom_components.yolocal.api import Device, DeviceEvent
from custom_components.yolocal.const import DOMAIN
from custom_components.yolocal.coordinator import YoLocalCoordinator
from custom_components.yolocal.entity import YoLocalEntity
from custom_components.yolocal.sensor import (
    YoLocalBatterySensor,
    YoLocalHumiditySensor,
    YoLocalTemperatureSensor,
    async_setup_entry,
    build_sensor_entities,
)


@pytest.fixture
def thsensor(monkeypatch):
    now = datetime(2026, 10, 24, 21, 30, 56, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=Mock()))
    coordinator = YoLocalCoordinator(
        hass=hass,
        client=SimpleNamespace(host="127.0.0.1"),
        token_manager=SimpleNamespace(client_id="test"),
        session=SimpleNamespace(),
        config_entry_id="entry",
        net_id="test",
    )
    device = Device("test-th", "Test TH", "unused", "THSensor", "THSensor", "YS8015-UC")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = coordinator._normalize_http_state(
        {
            "online": True,
            "reportAt": (now - timedelta(hours=4)).isoformat(),
            "state": {
                "temperature": 15.0,
                "humidity": 45.0,
                "battery": 4,
                "interval": 0,
                "recordInterval": 10,
            },
        },
        device,
    )
    return coordinator, device, now


def data_record(device, now, records, *, msgid="test-batch"):
    return DeviceEvent.from_payload(
        {
            "event": "THSensor.DataRecord",
            "deviceId": device.device_id,
            "time": int(now.timestamp() * 1000),
            "msgid": msgid,
            "data": {"records": records},
        }
    )


def sample(now, minutes=10, temperature=17.1, humidity=49.2):
    return {
        "temperature": temperature,
        "humidity": humidity,
        "time": (now - timedelta(minutes=minutes)).isoformat(),
    }


def readings(coordinator, device):
    return (
        YoLocalTemperatureSensor(coordinator, device).native_value,
        YoLocalHumiditySensor(coordinator, device).native_value,
    )


def test_data_record_uses_newest_sample_and_publishes_history(thsensor):
    coordinator, device, now = thsensor
    records = [sample(now, 20, 17, 49.6), sample(now), sample(now, 30, 16.8, 50.1)]

    coordinator._on_device_event(data_record(device, now, records))

    assert readings(coordinator, device) == (17.1, 49.2)
    assert "records" not in coordinator.get_state(device.device_id)["state"]
    coordinator.hass.bus.async_fire.assert_called_once_with(
        "yolocal_data_record", {"device_id": device.device_id, "records": records}
    )


@pytest.mark.parametrize("report_first", [True, False])
def test_live_report_wins_over_earlier_recorded_samples(thsensor, report_first):
    coordinator, device, now = thsensor
    report = DeviceEvent.from_payload(
        {
            "deviceId": device.device_id,
            "event": "THSensor.Report",
            "time": int(now.timestamp() * 1000),
            "data": {"temperature": 17.2, "humidity": 49.7, "battery": 3},
        }
    )
    batch = data_record(device, now + timedelta(seconds=1), [sample(now)])

    for event in (report, batch) if report_first else (batch, report):
        coordinator._on_device_event(event)

    assert readings(coordinator, device) == (17.2, 49.7)
    assert coordinator.get_state(device.device_id)["state"]["battery"] == 3
    assert coordinator.hass.bus.async_fire.call_count == 1


def test_out_of_order_record_batches_do_not_roll_back_measurements(thsensor):
    coordinator, device, now = thsensor
    coordinator._on_device_event(data_record(device, now, [sample(now)]))
    coordinator._on_device_event(
        data_record(device, now, [sample(now, 20, 16.8, 50.1)], msgid="older-batch")
    )

    assert readings(coordinator, device) == (17.1, 49.2)
    assert coordinator.hass.bus.async_fire.call_count == 2


@pytest.mark.parametrize("report_first", [True, False])
def test_full_report_wins_when_record_and_report_have_equal_timestamps(
    thsensor, report_first
):
    coordinator, device, now = thsensor
    report = DeviceEvent.from_payload(
        {
            "deviceId": device.device_id,
            "event": "THSensor.Report",
            "time": int(now.timestamp() * 1000),
            "data": {"temperature": 17.2, "humidity": 49.7},
        }
    )
    batch = data_record(device, now, [sample(now, 0)])

    for event in (report, batch) if report_first else (batch, report):
        coordinator._on_device_event(event)

    assert readings(coordinator, device) == (17.2, 49.7)


@pytest.mark.parametrize("report_age", [240, 20, None])
def test_cached_http_keeps_newer_samples_but_refreshes_diagnostics(
    thsensor, report_age
):
    coordinator, device, now = thsensor
    coordinator._on_device_event(data_record(device, now, [sample(now)]))
    response = {
        "online": False,
        "state": {"temperature": 15.0, "humidity": 45.0, "battery": 3, "interval": 5},
    }
    if report_age is not None:
        response["reportAt"] = (now - timedelta(minutes=report_age)).isoformat()

    async def get_state(_device):
        return response

    coordinator._client.get_state = get_state
    asyncio.run(coordinator._async_update_data())

    assert readings(coordinator, device) == (17.1, 49.2)
    assert coordinator.get_state(device.device_id)["state"]["battery"] == 3
    assert coordinator.get_state(device.device_id)["state"]["interval"] == 5
    assert YoLocalEntity(coordinator, device).available is True


def test_newer_http_measurement_replaces_recorded_sample(thsensor):
    coordinator, device, now = thsensor
    coordinator._on_device_event(data_record(device, now, [sample(now)]))

    async def get_state(_device):
        return {
            "online": True,
            "reportAt": (now - timedelta(minutes=5)).isoformat(),
            "state": {"temperature": 17.4, "humidity": 50.0, "battery": 3},
        }

    coordinator._client.get_state = get_state
    asyncio.run(coordinator._async_update_data())

    assert readings(coordinator, device) == (17.4, 50.0)


def test_record_arriving_during_http_poll_preserves_new_measurements(thsensor):
    coordinator, device, now = thsensor

    async def get_state(_device):
        coordinator._on_device_event(data_record(device, now, [sample(now)]))
        return {
            "reportAt": (now - timedelta(hours=4)).isoformat(),
            "state": {"temperature": 15.0, "humidity": 45.0, "battery": 3},
        }

    coordinator._client.get_state = get_state
    asyncio.run(coordinator._async_update_data())

    assert readings(coordinator, device) == (17.1, 49.2)
    assert coordinator.get_state(device.device_id)["state"]["battery"] == 3


def test_record_time_accepts_naive_utc_and_offset_aware_values(thsensor):
    coordinator, device, now = thsensor
    older = sample(now, 20, 16.8, 50.1)
    older["time"] = (now - timedelta(minutes=20)).replace(tzinfo=None).isoformat()
    newer = sample(now)
    coordinator._on_device_event(data_record(device, now, [older, newer]))

    assert readings(coordinator, device) == (17.1, 49.2)


def test_command_readback_cannot_replace_newer_samples(thsensor):
    coordinator, device, now = thsensor
    coordinator._on_device_event(data_record(device, now, [sample(now)]))
    cached = coordinator._normalize_http_state(
        {
            "reportAt": (now - timedelta(hours=4)).isoformat(),
            "state": {"temperature": 15.0, "humidity": 45.0, "interval": 5},
        },
        device,
    )

    state = coordinator._merge_command_http_state(device.device_id, cached)
    coordinator._update_device_state(device.device_id, state)

    assert readings(coordinator, device) == (17.1, 49.2)
    assert state["state"]["interval"] == 5


def test_partial_records_track_temperature_and_humidity_independently(thsensor):
    coordinator, device, now = thsensor
    records = [
        {"time": sample(now)["time"], "temperature": 17.1},
        {"time": sample(now, 20)["time"], "humidity": 49.2},
    ]
    coordinator._on_device_event(data_record(device, now, records))
    coordinator._on_device_event(
        data_record(device, now, [sample(now, 15, 16.0, 50.0)], msgid="partial-update")
    )

    assert readings(coordinator, device) == (17.1, 50.0)


@pytest.mark.parametrize(
    "records",
    [
        None,
        [],
        "invalid",
        [None, {}],
        [{"time": [], "temperature": 17.1}],
        [{"time": "not-a-date", "temperature": 17.1}],
        [{"time": "2026-10-24T21:20:00Z", "temperature": True}],
        [{"time": "2026-10-24T21:20:00Z", "temperature": float("nan")}],
        [{"time": "2026-10-24T21:20:00Z", "humidity": float("inf")}],
    ],
)
def test_invalid_records_do_not_change_state_or_prove_contact(thsensor, records):
    coordinator, device, now = thsensor
    before = coordinator.get_state(device.device_id)

    coordinator._on_device_event(data_record(device, now, records))

    assert coordinator.get_state(device.device_id) == before
    coordinator.hass.bus.async_fire.assert_not_called()


def test_history_event_contains_only_valid_measurements_and_times(thsensor):
    coordinator, device, now = thsensor
    valid = sample(now)
    coordinator._on_device_event(
        data_record(
            device,
            now,
            [None, {"time": "bad", "temperature": 100}, {**valid, "token": "private"}],
        )
    )

    assert readings(coordinator, device) == (17.1, 49.2)
    coordinator.hass.bus.async_fire.assert_called_once_with(
        "yolocal_data_record", {"device_id": device.device_id, "records": [valid]}
    )


def test_duplicate_batches_on_both_interfaces_publish_only_once(thsensor):
    coordinator, device, now = thsensor
    coordinator._on_device_event(data_record(device, now, [sample(now)]))
    coordinator._on_device_event(
        data_record(device, now + timedelta(seconds=1), [sample(now)])
    )
    coordinator._recent_mqtt_events.clear()
    coordinator._on_device_event(data_record(device, now, [sample(now)]))

    assert readings(coordinator, device) == (17.1, 49.2)
    assert coordinator.hass.bus.async_fire.call_count == 1


def test_records_recover_offline_sensor_using_sample_contact_time(thsensor):
    coordinator, device, now = thsensor
    coordinator._states[device.device_id]["online"] = False

    coordinator._on_device_event(data_record(device, now, [sample(now)]))

    state = coordinator.get_state(device.device_id)
    assert YoLocalEntity(coordinator, device).available is True
    assert YoLocalBatterySensor(coordinator, device).native_value == 100
    assert dt_util.parse_datetime(state["lastContactAt"]) == now - timedelta(minutes=10)
    assert state["reportAt"] == (now - timedelta(hours=4)).isoformat()


def test_old_record_batch_does_not_revive_device_on_recent_envelope(thsensor):
    coordinator, device, now = thsensor
    coordinator._states[device.device_id] = {
        "online": False,
        "reportAt": (now - timedelta(hours=14)).isoformat(),
        "state": {"temperature": 15.0, "humidity": 45.0, "battery": 4},
    }

    coordinator._on_device_event(data_record(device, now, [sample(now, 13 * 60)]))

    assert YoLocalEntity(coordinator, device).available is False
    assert YoLocalBatterySensor(coordinator, device).native_value == 0


def test_record_contact_honors_a_newer_explicit_device_report_time(thsensor):
    coordinator, device, now = thsensor
    coordinator._states[device.device_id]["online"] = False
    batch = data_record(device, now, [sample(now, 20)])
    batch.data["reportAt"] = (now - timedelta(minutes=5)).isoformat()

    coordinator._on_device_event(batch)

    state = coordinator.get_state(device.device_id)
    assert YoLocalEntity(coordinator, device).available is True
    assert state["lastContactSource"] == "reportAt"
    assert dt_util.parse_datetime(state["lastContactAt"]) == now - timedelta(minutes=5)


def test_record_interval_and_alert_interval_have_distinct_entities(thsensor):
    coordinator, device, _now = thsensor
    entities = {
        entity.unique_id: entity
        for entity in build_sensor_entities(coordinator, device)
    }
    alert = entities[f"{device.device_id}_interval"]
    record = entities[f"{device.device_id}_record_interval"]

    assert alert._attr_name == "Continuous alert interval"
    assert alert.native_value == 0
    assert record._attr_name == "Record interval"
    assert record.native_value == 10
    assert record._attr_native_unit_of_measurement == "min"


def test_record_interval_is_discovered_when_report_arrives_after_setup(thsensor):
    coordinator, device, now = thsensor
    coordinator._states[device.device_id]["state"].pop("recordInterval")
    coordinator.hass.data = {DOMAIN: {"entry": coordinator}}
    added = []
    asyncio.run(async_setup_entry(coordinator.hass, ConfigEntry(), added.extend))
    assert not any(entity.unique_id.endswith("_record_interval") for entity in added)

    coordinator._on_device_event(
        DeviceEvent.from_payload(
            {
                "deviceId": device.device_id,
                "event": "THSensor.Report",
                "time": int(now.timestamp() * 1000),
                "data": {"recordInterval": 10},
            }
        )
    )

    record_entities = [
        entity for entity in added if entity.unique_id.endswith("_record_interval")
    ]
    assert len(record_entities) == 1
    assert record_entities[0].native_value == 10
