"""Tests for factoring-heavy business logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import asyncio
import logging

import aiohttp
from homeassistant.helpers import device_registry as dr

import custom_components.yolocal as yolocal_module
import custom_components.yolocal.api.client as client_module
import custom_components.yolocal.config_flow as config_flow_module
from custom_components.yolocal import _configured_hosts
from custom_components.yolocal.api.auth import TokenManager
from custom_components.yolocal.api.client import ApiError
from custom_components.yolocal.api.client import YoLinkClient
from custom_components.yolocal.api.device import Device
from custom_components.yolocal.api.mqtt import DeviceEvent
from custom_components.yolocal.const import DOMAIN
from custom_components.yolocal.coordinator import YoLocalCoordinator
from custom_components.yolocal.entity import YoLocalEntity
from custom_components.yolocal.sensor import (
    YoLocalBatterySensor,
    YoLocalLastReportedSensor,
    YoLocalOutletPowerSensor,
    YoLocalTHLimitSensor,
    build_sensor_entities,
)
from custom_components.yolocal.switch import YoLocalSwitch
from custom_components.yolocal.valve import YoLocalValve
from capture_yolink_payloads import sanitize_value


class FakeClosableSession:
    """Minimal aiohttp session returned by config flow tests."""

    async def close(self) -> None:
        """Close the fake session."""
        return None


class FakeMqttClient:
    """Minimal MQTT client for coordinator tests."""

    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def make_device_id(label: str = "device") -> str:
    """Create a generic test device id."""
    return f"test-{label}"


def make_app_eui(model_num: str) -> str:
    """Create a synthetic appEui with the requested model number."""
    return f"000000{model_num}000000"


def make_device(
    *,
    device_id: str = make_device_id(),
    device_type: str = "THSensor",
    display_type: str | None = None,
    model: str | None = "YS8001-UC",
) -> Device:
    """Create a test device."""
    return Device(
        device_id=device_id,
        name="Test Device",
        token="token",
        device_type=device_type,
        display_type=display_type or device_type,
        model=model,
    )


def make_coordinator() -> YoLocalCoordinator:
    """Create a coordinator with stub dependencies."""
    scheduled_coroutines: list[object] = []

    def async_create_task(coro):
        scheduled_coroutines.append(coro)
        return SimpleNamespace(coro=coro, done=lambda: False, cancel=lambda: None)

    def async_create_background_task(coro, _name):
        scheduled_coroutines.append(coro)
        return SimpleNamespace(coro=coro, done=lambda: False, cancel=lambda: None)

    hass = SimpleNamespace(
        async_create_task=async_create_task,
        async_create_background_task=async_create_background_task,
        _scheduled_coroutines=scheduled_coroutines,
    )
    token_manager = SimpleNamespace(client_id="client")
    client = SimpleNamespace(host="127.0.0.1")
    session = SimpleNamespace()
    return YoLocalCoordinator(
        hass=hass,
        client=client,
        token_manager=token_manager,
        session=session,
        config_entry_id="entry",
        net_id="net",
    )


class FakeApiResponse:
    """Minimal async response object for YoLinkClient tests."""

    def __init__(self, payload: dict[str, object]) -> None:
        """Initialize the fake response."""
        self._payload = payload

    def raise_for_status(self) -> None:
        """No-op status validation."""
        return None

    async def json(self) -> dict[str, object]:
        """Return the configured JSON payload."""
        return self._payload


class FakePostContext:
    """Minimal aiohttp request context manager."""

    def __init__(self, result: object) -> None:
        """Initialize the fake context."""
        self._result = result

    async def __aenter__(self) -> FakeApiResponse:
        """Enter the fake request context."""
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def __aexit__(self, *_args: object) -> None:
        """Exit the fake request context."""
        return None


class FakeSession:
    """Fake aiohttp session with queued POST results."""

    def __init__(self, results: list[object]) -> None:
        """Initialize the session."""
        self.results = results
        self.posts: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakePostContext:
        """Return the next queued POST context."""
        self.posts.append({"url": url, **kwargs})
        return FakePostContext(self.results.pop(0))


class FakeTokenManager:
    """Fake token manager for YoLinkClient tests."""

    client_id = "client"

    async def get_token(self) -> str:
        """Return a fake access token."""
        return "token"


class FakeHostTokenManager:
    """Fake token manager that returns host-specific MQTT tokens."""

    client_id = "client"

    def __init__(self) -> None:
        """Initialize token call tracking."""
        self.hosts: list[str] = []

    async def get_token_for_host(self, host: str) -> str:
        """Return a token tied to the requested host."""
        self.hosts.append(host)
        return f"token-for-{host}"


def test_capture_sanitizer_redacts_tokens_and_aliases_identifiers() -> None:
    """Capture artifacts should not persist hub secrets or device identifiers."""
    sanitized = sanitize_value(
        {
            "host": "hub.local",
            "net_id": "net-123",
            "request": {
                "targetDevice": "real-device-id",
                "token": "device-token",
            },
            "response": {
                "data": {
                    "devices": [
                        {
                            "deviceId": "real-device-id",
                            "name": "Kitchen Sensor",
                            "appEui": "0000008003000000",
                            "token": "device-list-token",
                        }
                    ]
                }
            },
            "topic": "ylsubnet/net-123/real-device-id/report",
        },
        device_aliases={
            "real-device-id": "THSensor-1",
            "Kitchen Sensor": "THSensor-1",
        },
        host="hub.local",
        net_id="net-123",
    )

    assert sanitized["host"] == "REDACTED_HOST"
    assert sanitized["net_id"] == "REDACTED_NET_ID"
    assert sanitized["request"] == {
        "targetDevice": "THSensor-1",
        "token": "REDACTED",
    }
    device = sanitized["response"]["data"]["devices"][0]
    assert device["deviceId"] == "THSensor-1"
    assert device["name"] == "THSensor-1"
    assert device["appEui"] == "REDACTED"
    assert device["token"] == "REDACTED"
    assert sanitized["topic"] == "ylsubnet/REDACTED_NET_ID/THSensor-1/report"


def test_get_devices_retries_transient_server_disconnect() -> None:
    """Read-only device list calls should retry transient hub disconnects."""
    old_delay = client_module.TRANSPORT_RETRY_DELAY
    client_module.TRANSPORT_RETRY_DELAY = 0
    try:
        session = FakeSession(
            [
                aiohttp.ClientConnectionError(),
                FakeApiResponse(
                    {
                        "code": "000000",
                        "data": {
                            "devices": [
                                {
                                    "deviceId": make_device_id("door-api"),
                                    "name": "Door",
                                    "token": "device-token",
                                    "type": "DoorSensor",
                                    "appEui": make_app_eui("7704"),
                                }
                            ]
                        },
                    }
                ),
            ]
        )
        client = YoLinkClient("hub.local", FakeTokenManager(), session)

        devices = asyncio.run(client.get_devices())
    finally:
        client_module.TRANSPORT_RETRY_DELAY = old_delay

    assert len(session.posts) == 2
    assert len(devices) == 1
    assert devices[0].device_id == make_device_id("door-api")


def test_set_state_does_not_retry_transport_disconnect() -> None:
    """State-changing commands should not be duplicated after transport errors."""
    session = FakeSession([aiohttp.ClientConnectionError()])
    client = YoLinkClient("hub.local", FakeTokenManager(), session)
    device = make_device(device_id=make_device_id("outlet"), device_type="Outlet")

    try:
        asyncio.run(client.set_state(device, {"state": "closed"}))
    except aiohttp.ClientConnectionError:
        pass
    else:
        raise AssertionError("expected ServerDisconnectedError")

    assert len(session.posts) == 1


def test_read_request_fails_over_to_secondary_host() -> None:
    """Read-only API retries should switch from primary to secondary host."""
    old_delay = client_module.TRANSPORT_RETRY_DELAY
    client_module.TRANSPORT_RETRY_DELAY = 0
    try:
        session = FakeSession(
            [
                FakeApiResponse({"access_token": "primary-token", "expires_in": 7200}),
                aiohttp.ClientConnectionError(),
                FakeApiResponse({"access_token": "secondary-token", "expires_in": 7200}),
                FakeApiResponse({"code": "000000", "data": {"devices": []}}),
            ]
        )
        token_manager = TokenManager(
            "primary.local",
            "client",
            "secret",
            session,
            hosts=["primary.local", "secondary.local"],
        )
        client = YoLinkClient(
            "primary.local",
            token_manager,
            session,
            hosts=["primary.local", "secondary.local"],
        )

        devices = asyncio.run(client.get_devices())
    finally:
        client_module.TRANSPORT_RETRY_DELAY = old_delay

    assert devices == []
    assert session.posts[0]["url"] == "http://primary.local:1080/open/yolink/token"
    assert session.posts[1]["url"] == "http://primary.local:1080/open/yolink/v2/api"
    assert session.posts[2]["url"] == "http://secondary.local:1080/open/yolink/token"
    assert session.posts[3]["url"] == "http://secondary.local:1080/open/yolink/v2/api"
    assert client.host == "secondary.local"


def test_token_manager_fetches_host_specific_tokens() -> None:
    """MQTT tokens should be cached by the host that issued them."""
    session = FakeSession(
        [
            FakeApiResponse({"access_token": "primary-token", "expires_in": 7200}),
            FakeApiResponse({"access_token": "secondary-token", "expires_in": 7200}),
        ]
    )
    token_manager = TokenManager(
        "primary.local",
        "client",
        "secret",
        session,
        hosts=["primary.local", "secondary.local"],
    )

    primary_token = asyncio.run(token_manager.get_token_for_host("primary.local"))
    secondary_token = asyncio.run(token_manager.get_token_for_host("secondary.local"))
    secondary_token_cached = asyncio.run(
        token_manager.get_token_for_host("secondary.local")
    )

    assert primary_token == "primary-token"
    assert secondary_token == "secondary-token"
    assert secondary_token_cached == "secondary-token"
    assert [post["url"] for post in session.posts] == [
        "http://primary.local:1080/open/yolink/token",
        "http://secondary.local:1080/open/yolink/token",
    ]


def test_configured_hosts_uses_entry_data_secondary_host() -> None:
    """Setup should use the secondary host stored in entry data."""
    entry = SimpleNamespace(
        data={"hub_ip": "primary.local", "secondary_hub_ip": "secondary.local"},
    )

    assert _configured_hosts(entry) == ["primary.local", "secondary.local"]


def test_config_flow_entry_title_lists_configured_hosts(monkeypatch) -> None:
    """Initial config entry title should include both hosts when configured."""

    async def fake_create_client(**_kwargs):
        return object(), object(), FakeClosableSession()

    monkeypatch.setattr(config_flow_module, "create_client", fake_create_client)
    flow = config_flow_module.YoLocalConfigFlow()

    result = asyncio.run(
        flow.async_step_user(
            {
                "hub_ip": "primary.local",
                "secondary_hub_ip": "secondary.local",
                "client_id": "client",
                "client_secret": "secret",
                "net_id": "net",
            }
        )
    )

    assert result["title"] == "YoLink Hub (primary.local, secondary.local)"


def test_setup_entry_updates_stale_title_with_secondary_host(monkeypatch) -> None:
    """Setup should normalize an existing entry title to include both hosts."""
    entry_updates: list[dict[str, object]] = []

    class FakeCoordinator:
        async def async_config_entry_first_refresh(self) -> None:
            return None

    async def fake_create_coordinator(**_kwargs):
        return FakeCoordinator()

    async def fake_forward_entry_setups(_entry, _platforms) -> None:
        return None

    def fake_update_entry(_entry, **kwargs) -> None:
        entry_updates.append(kwargs)

    monkeypatch.setattr(yolocal_module, "create_coordinator", fake_create_coordinator)
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=fake_forward_entry_setups,
            async_update_entry=fake_update_entry,
        ),
    )
    entry = SimpleNamespace(
        entry_id="entry",
        title="YoLink Hub (primary.local)",
        data={
            "hub_ip": "primary.local",
            "secondary_hub_ip": "secondary.local",
            "client_id": "client",
            "client_secret": "secret",
            "net_id": "net",
        },
        options={"secondary_hub_ip": "legacy-secondary.local"},
    )

    assert asyncio.run(yolocal_module.async_setup_entry(hass, entry)) is True
    assert entry_updates == [
        {
            "title": "YoLink Hub (primary.local, secondary.local)",
            "options": {},
        }
    ]


def test_reconfigure_updates_hub_hosts(monkeypatch) -> None:
    """Reconfigure should update primary and secondary hub hosts."""
    calls: list[dict[str, object]] = []

    async def fake_create_client(**kwargs):
        calls.append(kwargs)
        return object(), object(), FakeClosableSession()

    monkeypatch.setattr(config_flow_module, "create_client", fake_create_client)
    flow = config_flow_module.YoLocalConfigFlow()
    flow._reconfigure_entry = SimpleNamespace(
        data={
            "hub_ip": "primary.local",
            "client_id": "client",
            "client_secret": "secret",
            "net_id": "net",
        }
    )

    result = asyncio.run(
        flow.async_step_reconfigure(
            {
                "hub_ip": "new-primary.local",
                "secondary_hub_ip": "secondary.local",
                "client_id": "new-client",
                "client_secret": "new-secret",
                "net_id": "new-net",
            }
        )
    )

    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"
    assert result["title"] == "YoLink Hub (new-primary.local, secondary.local)"
    assert result["data"]["hub_ip"] == "new-primary.local"
    assert result["data"]["secondary_hub_ip"] == "secondary.local"
    assert result["data"]["client_id"] == "new-client"
    assert result["data"]["client_secret"] == "new-secret"
    assert result["data"]["net_id"] == "new-net"
    assert [call["host"] for call in calls] == [
        "new-primary.local",
        "secondary.local",
    ]
    assert [call["hosts"] for call in calls] == [
        ["new-primary.local"],
        ["secondary.local"],
    ]
    assert calls[0]["client_id"] == "new-client"
    assert calls[0]["client_secret"] == "new-secret"


def test_reconfigure_removes_empty_secondary_host(monkeypatch) -> None:
    """Reconfigure should remove a cleared secondary host."""
    calls: list[dict[str, object]] = []

    async def fake_create_client(**kwargs):
        calls.append(kwargs)
        return object(), object(), FakeClosableSession()

    monkeypatch.setattr(config_flow_module, "create_client", fake_create_client)
    flow = config_flow_module.YoLocalConfigFlow()
    flow._reconfigure_entry = SimpleNamespace(
        data={
            "hub_ip": "primary.local",
            "secondary_hub_ip": "old-secondary.local",
            "client_id": "client",
            "client_secret": "secret",
            "net_id": "net",
        }
    )

    result = asyncio.run(
        flow.async_step_reconfigure(
            {
                "hub_ip": "primary.local",
                "secondary_hub_ip": "  ",
                "client_id": "client",
                "client_secret": "secret",
                "net_id": "net",
            }
        )
    )

    assert result["data"]["hub_ip"] == "primary.local"
    assert result["title"] == "YoLink Hub (primary.local)"
    assert result["options"] == {}
    assert "secondary_hub_ip" not in result["data"]
    assert [call["hosts"] for call in calls] == [["primary.local"]]


def test_reconfigure_requires_secondary_host_connectivity(monkeypatch) -> None:
    """Reconfigure should fail if a configured secondary host cannot be reached."""
    calls: list[dict[str, object]] = []

    async def fake_create_client(**kwargs):
        calls.append(kwargs)
        if kwargs["host"] == "secondary.local":
            raise OSError("secondary unavailable")
        return object(), object(), FakeClosableSession()

    monkeypatch.setattr(config_flow_module, "create_client", fake_create_client)
    flow = config_flow_module.YoLocalConfigFlow()
    flow._reconfigure_entry = SimpleNamespace(
        data={
            "hub_ip": "primary.local",
            "secondary_hub_ip": "old-secondary.local",
            "client_id": "client",
            "client_secret": "secret",
            "net_id": "net",
        }
    )

    result = asyncio.run(
        flow.async_step_reconfigure(
            {
                "hub_ip": "primary.local",
                "secondary_hub_ip": "secondary.local",
                "client_id": "client",
                "client_secret": "secret",
                "net_id": "net",
            }
        )
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}
    assert [call["hosts"] for call in calls] == [
        ["primary.local"],
        ["secondary.local"],
    ]


def test_reconfigure_form_uses_suggested_values_not_defaults() -> None:
    """Reconfigure optional fields should be clearable in the HA UI."""
    flow = config_flow_module.YoLocalConfigFlow()
    flow._reconfigure_entry = SimpleNamespace(
        data={
            "hub_ip": "primary.local",
            "secondary_hub_ip": "old-secondary.local",
            "client_id": "client",
            "client_secret": "secret",
            "net_id": "net",
        }
    )

    result = asyncio.run(flow.async_step_reconfigure())
    schema = result["data_schema"].schema

    assert {key.key: key.default for key in schema} == {
        "hub_ip": None,
        "secondary_hub_ip": None,
        "client_id": None,
        "client_secret": None,
        "net_id": None,
    }


def test_merge_nested_state_covers_all_shapes() -> None:
    """Nested state merge should preserve prior detail across payload shapes."""
    coordinator = make_coordinator()

    assert coordinator._merge_nested_state({"battery": 4}, {"state": "alert"}) == {
        "battery": 4,
        "state": "alert",
    }
    assert coordinator._merge_nested_state("open", {"state": "closed"}) == {
        "state": "closed"
    }
    assert coordinator._merge_nested_state({"battery": 4}, {"humidity": 61}) == {
        "battery": 4,
        "humidity": 61,
    }
    assert coordinator._merge_nested_state({"battery": 4}, None) is None


def test_merge_device_state_preserves_existing_nested_fields() -> None:
    """Non-TH devices should deep-merge nested `state` payloads."""
    coordinator = make_coordinator()
    device_id = make_device_id("door")
    coordinator._devices[device_id] = make_device(
        device_id=device_id,
        device_type="DoorSensor",
    )
    coordinator._states[device_id] = {
        "online": True,
        "state": {"battery": 4, "state": "open"},
    }

    merged = coordinator._merge_device_state(
        device_id,
        {"state": {"state": "closed"}, "lastReportedAt": "2026-03-09T12:00:00+00:00"},
    )

    assert merged["online"] is True
    assert merged["lastReportedAt"] == "2026-03-09T12:00:00+00:00"
    assert merged["state"] == {"battery": 4, "state": "closed"}


def test_merge_device_state_folds_flat_battery_into_nested_state() -> None:
    """Non-TH MQTT diagnostics should be folded into canonical nested state."""
    coordinator = make_coordinator()
    device_id = make_device_id("motion-flat")
    coordinator._devices[device_id] = make_device(
        device_id=device_id,
        device_type="MotionSensor",
    )
    coordinator._states[device_id] = {
        "online": True,
        "state": {"battery": 2, "state": "normal"},
    }

    merged = coordinator._merge_device_state(
        device_id,
        {
            "state": "alert",
            "battery": 4,
            "lastReportedAt": "2026-03-09T12:00:00+00:00",
        },
    )

    assert merged["state"]["state"] == "alert"
    assert merged["state"]["battery"] == 4
    assert "battery" not in merged


def test_merge_device_state_revives_offline_device_on_fresh_report() -> None:
    """A new report should bring a device back online even without an `online` field."""
    coordinator = make_coordinator()
    device_id = make_device_id("motion")
    coordinator._devices[device_id] = make_device(
        device_id=device_id,
        device_type="MotionSensor",
    )
    coordinator._states[device_id] = {
        "online": False,
        "lastReportedAt": "2026-03-08T12:00:00+00:00",
        "state": {"battery": 1, "state": "normal"},
    }

    merged = coordinator._merge_device_state(
        device_id,
        {
            "lastReportedAt": "2026-03-09T12:00:00+00:00",
            "state": {"battery": 4},
        },
    )

    assert merged["online"] is True
    assert merged["state"] == {"battery": 4, "state": "normal"}


def test_merge_thsensor_state_preserves_diagnostics_and_ignores_empty_updates() -> None:
    """TH events should retain cached diagnostics and skip empty sentinel updates."""
    coordinator = make_coordinator()
    device_id = make_device_id("th")
    coordinator._devices[device_id] = make_device(
        device_id=device_id,
        device_type="THSensor",
    )
    coordinator._states[device_id] = {
        "version": "old-top-level",
        "state": {
            "battery": 4,
            "version": "1.0.0",
            "temperature": 21.5,
            "humidity": 48,
        },
    }

    merged = coordinator._merge_thsensor_state(
        device_id,
        {
            "temperature": None,
            "humidity": 52,
            "mode": None,
            "lastReportedAt": "2026-03-09T12:00:00+00:00",
            "state": {"battery": 3},
        },
    )

    assert merged["lastReportedAt"] == "2026-03-09T12:00:00+00:00"
    assert merged["state"]["battery"] == 3
    assert merged["state"]["humidity"] == 52
    assert merged["state"]["temperature"] == 21.5
    assert merged["state"]["version"] == "1.0.0"
    assert "lastReportedAt" not in merged["state"]


def test_merge_thsensor_state_revives_offline_device_on_fresh_report() -> None:
    """TH reports should also restore availability when the hub omits `online`."""
    coordinator = make_coordinator()
    device_id = make_device_id("th-revive")
    coordinator._devices[device_id] = make_device(
        device_id=device_id,
        device_type="THSensor",
    )
    coordinator._states[device_id] = {
        "online": False,
        "lastReportedAt": "2026-03-08T12:00:00+00:00",
        "state": {"temperature": 21.5, "humidity": 48},
    }

    merged = coordinator._merge_thsensor_state(
        device_id,
        {
            "lastReportedAt": "2026-03-09T12:00:00+00:00",
            "humidity": 52,
        },
    )

    assert merged["online"] is True
    assert merged["state"]["temperature"] == 21.5
    assert merged["state"]["humidity"] == 52


def test_merge_device_state_strips_inaccurate_battery_type() -> None:
    """Battery type should not be retained in cached state."""
    coordinator = make_coordinator()
    device_id = make_device_id("motion")
    coordinator._devices[device_id] = make_device(
        device_id=device_id,
        device_type="MotionSensor",
    )
    coordinator._states[device_id] = {
        "state": {
            "battery": 4,
            "batteryType": "Li",
            "state": "normal",
        }
    }

    merged = coordinator._merge_device_state(
        device_id,
        {
            "online": True,
            "state": {
                "battery": 4,
                "batteryType": "Li",
                "state": "alert",
            },
        },
    )

    assert "batteryType" not in merged
    assert "batteryType" not in merged["state"]
    assert merged["state"]["state"] == "alert"


def test_normalize_mqtt_event_wraps_flat_state_in_http_shape() -> None:
    """Flat MQTT payloads should be normalized to nested HTTP-like state."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("door"), device_type="DoorSensor")

    normalized = coordinator._normalize_mqtt_event(
        device,
        {
            "state": "open",
            "battery": 3,
            "version": "0420",
            "lastReportedAt": "2026-03-09T12:00:00+00:00",
        },
    )

    assert normalized["lastReportedAt"] == "2026-03-09T12:00:00+00:00"
    assert normalized["state"] == {
        "battery": 3,
        "state": "open",
        "version": "0420",
    }


def test_normalize_mqtt_event_skips_empty_th_overwrites() -> None:
    """TH MQTT normalization should avoid overwriting real values with sentinels."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("th-normalize"), device_type="THSensor")

    normalized = coordinator._normalize_mqtt_event(
        device,
        {
            "state": "normal",
            "temperature": None,
            "humidity": 52,
            "mode": None,
            "version": "1.0.0",
        },
    )

    assert normalized["state"] == {
        "humidity": 52,
        "state": "normal",
        "version": "1.0.0",
    }


def test_state_value_prefers_nested_with_top_level_fallback() -> None:
    """Entity helper should prefer nested state and only fall back when requested."""
    coordinator = make_coordinator()
    device = make_device()
    coordinator._states[device.device_id] = {
        "version": "top",
        "state": {"version": "nested"},
    }
    entity = YoLocalEntity(coordinator, device)

    assert entity.state_value("version") == "nested"
    assert entity.state_value("missing") is None
    assert entity.state_value("version", fallback=True) == "nested"
    assert entity.state_value("other", fallback=True) is None

    coordinator._states[device.device_id] = {"version": "top"}
    assert entity.state_value("version", fallback=True) == "top"


def test_outlet_does_not_create_battery_sensor_without_battery_payload() -> None:
    """Powered outlets should not get a synthetic battery diagnostic sensor."""
    coordinator = make_coordinator()
    device = make_device(device_type="Outlet", display_type="Outlet")
    coordinator._states[device.device_id] = {
        "state": {
            "delay": {"ch": 1, "off": 0, "on": 0},
            "power": 0,
            "state": "open",
            "version": "1404",
            "watt": 0,
        }
    }

    entities = build_sensor_entities(coordinator, device)

    assert not any(isinstance(entity, YoLocalBatterySensor) for entity in entities)


def test_outlet_creates_power_sensor_from_deciwatt_payload() -> None:
    """Outlet power should use the deciwatt payload field, not the zero watt field."""
    coordinator = make_coordinator()
    device = make_device(device_type="Outlet", display_type="Outlet")
    coordinator._states[device.device_id] = {
        "state": {
            "delay": {"ch": 1, "off": 0, "on": 0},
            "power": 57,
            "state": "open",
            "version": "1404",
            "watt": 0,
        }
    }

    entities = build_sensor_entities(coordinator, device)
    power_sensors = [
        entity for entity in entities if isinstance(entity, YoLocalOutletPowerSensor)
    ]

    assert len(power_sensors) == 1
    assert power_sensors[0].native_value == 5.7


def test_battery_sensor_is_created_only_when_payload_has_battery() -> None:
    """Battery sensors should be discovered from payload fields, not device type."""
    coordinator = make_coordinator()
    door = make_device(
        device_id=make_device_id("door-no-battery"),
        device_type="DoorSensor",
        display_type="DoorSensor",
    )
    unknown = make_device(
        device_id=make_device_id("unknown-with-battery"),
        device_type="UnknownDevice",
        display_type="UnknownDevice",
    )
    coordinator._states[unknown.device_id] = {"state": {"battery": 4}}

    door_entities = build_sensor_entities(coordinator, door)
    unknown_entities = build_sensor_entities(coordinator, unknown)

    assert not any(isinstance(entity, YoLocalBatterySensor) for entity in door_entities)
    assert any(isinstance(entity, YoLocalBatterySensor) for entity in unknown_entities)


def test_outlet_switch_reads_nested_and_flat_state() -> None:
    """Outlet switch state should handle HTTP-like nested and flat states."""
    coordinator = make_coordinator()
    device = make_device(device_type="Outlet", display_type="Outlet")
    entity = YoLocalSwitch(coordinator, device)

    coordinator._states[device.device_id] = {"state": {"state": "open"}}
    assert entity.is_on is True

    coordinator._states[device.device_id] = {"state": {"state": "closed"}}
    assert entity.is_on is False

    coordinator._states[device.device_id] = {"state": "open"}
    assert entity.is_on is True


def test_outlet_switch_tracks_physical_mqtt_state_changes() -> None:
    """Outlet physical toggles should not collapse back to off after MQTT updates."""
    coordinator = make_coordinator()
    device = make_device(device_type="Outlet", display_type="Outlet")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "delay": {"ch": 1, "off": 0, "on": 0},
        "power": 0,
        "state": "closed",
        "watt": 0,
    }
    entity = YoLocalSwitch(coordinator, device)

    open_event = coordinator._normalize_mqtt_event(
        device,
        {
            "state": "open",
            "loraInfo": {"devNetType": "A", "signal": 0, "gatewayId": "", "gateways": 1},
            "lastReportedAt": "2026-05-27T05:55:21.874000+00:00",
        },
    )
    coordinator._update_device_state(
        device.device_id,
        coordinator._merge_state_payload(
            coordinator.get_state(device.device_id),
            open_event,
        ),
    )

    assert coordinator.get_state(device.device_id)["state"]["state"] == "open"
    assert entity.is_on is True

    closed_event = coordinator._normalize_mqtt_event(
        device,
        {
            "state": "closed",
            "loraInfo": {"devNetType": "A", "signal": 0, "gatewayId": "", "gateways": 1},
            "lastReportedAt": "2026-05-27T05:54:59.326000+00:00",
        },
    )
    coordinator._update_device_state(
        device.device_id,
        coordinator._merge_state_payload(
            coordinator.get_state(device.device_id),
            closed_event,
        ),
    )

    assert coordinator.get_state(device.device_id)["state"]["state"] == "closed"
    assert entity.is_on is False


def test_duplicate_mqtt_events_are_ignored() -> None:
    """The same MQTT report received on two interfaces should update once."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("dup"), device_type="MotionSensor")
    coordinator._devices[device.device_id] = device
    update_calls: list[dict[str, object]] = []

    def update_device_state(device_id: str, state: dict[str, object]) -> None:
        update_calls.append(state)
        coordinator._states[device_id] = state

    coordinator._update_device_state = update_device_state
    event = DeviceEvent(
        device_id=device.device_id,
        event="report",
        data={"state": "alert", "lastReportedAt": "2026-03-09T12:00:00+00:00"},
        raw={
            "deviceId": device.device_id,
            "event": "report",
            "time": 1773057600000,
            "data": {"state": "alert"},
        },
    )

    coordinator._on_device_event(event)
    coordinator._on_device_event(event)

    assert len(update_calls) == 1


def test_distinct_mqtt_events_are_not_deduplicated() -> None:
    """Different reports from the same device should both be processed."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("distinct"), device_type="MotionSensor")
    coordinator._devices[device.device_id] = device
    update_calls: list[dict[str, object]] = []

    def update_device_state(device_id: str, state: dict[str, object]) -> None:
        update_calls.append(state)
        coordinator._states[device_id] = state

    coordinator._update_device_state = update_device_state
    first = DeviceEvent(
        device_id=device.device_id,
        event="report",
        data={"state": "alert", "lastReportedAt": "2026-03-09T12:00:00+00:00"},
        raw={
            "deviceId": device.device_id,
            "event": "report",
            "time": 1773057600000,
            "data": {"state": "alert"},
        },
    )
    second = DeviceEvent(
        device_id=device.device_id,
        event="report",
        data={"state": "normal", "lastReportedAt": "2026-03-09T12:00:01+00:00"},
        raw={
            "deviceId": device.device_id,
            "event": "report",
            "time": 1773057601000,
            "data": {"state": "normal"},
        },
    )

    coordinator._on_device_event(first)
    coordinator._on_device_event(second)

    assert len(update_calls) == 2


def test_async_send_command_refreshes_state_and_notifies_coordinator() -> None:
    """Command refresh should publish the authoritative post-command state."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {"state": {"state": "closed"}}

    calls: list[tuple[str, object]] = []

    async def set_state(_device: Device, params: dict[str, object]) -> dict[str, object]:
        calls.append(("set_state", params))
        return {"ok": True}

    async def get_state(_device: Device) -> dict[str, object]:
        calls.append(("get_state", None))
        return {
            "reportAt": "2026-05-22T12:00:00+00:00",
            "state": {"state": "open"},
        }

    coordinator._client = SimpleNamespace(set_state=set_state, get_state=get_state)

    result = asyncio.run(
        coordinator.async_send_command(device.device_id, {"state": "open"})
    )

    assert result == {"ok": True}
    assert calls == [("set_state", {"state": "open"}), ("get_state", None)]
    assert coordinator.get_state(device.device_id)["state"]["state"] == "open"
    assert coordinator.data == coordinator._states


def test_async_send_command_retries_transient_failure_before_offline() -> None:
    """Command failures should be retried before marking a device offline."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "online": True,
        "state": {"power": 57, "state": "open", "watt": 0},
    }
    calls: list[tuple[str, object]] = []

    async def set_state(_device: Device, params: dict[str, object]) -> dict[str, object]:
        calls.append(("set_state", params))
        if len(calls) == 1:
            raise ApiError("000201", "Cannot connect to the device", "Outlet.setState")
        return {"ok": True}

    async def get_state(_device: Device) -> dict[str, object]:
        calls.append(("get_state", None))
        return {"state": {"power": 12, "state": "closed", "watt": 0}}

    coordinator._client = SimpleNamespace(set_state=set_state, get_state=get_state)

    result = asyncio.run(
        coordinator.async_send_command(device.device_id, {"state": "closed"})
    )

    assert result == {"ok": True}
    assert calls == [
        ("set_state", {"state": "closed"}),
        ("set_state", {"state": "closed"}),
        ("get_state", None),
    ]
    assert coordinator.get_state(device.device_id).get("online", True) is True
    assert coordinator.get_state(device.device_id)["state"]["state"] == "closed"


def test_async_send_command_marks_offline_after_repeated_transient_failures() -> None:
    """Repeated command radio failures should mark the device offline."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "online": True,
        "state": {"power": 57, "state": "open", "watt": 0},
    }
    calls = 0

    async def set_state(_device: Device, _params: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise ApiError("000201", "Cannot connect to the device", "Outlet.setState")

    coordinator._client = SimpleNamespace(set_state=set_state)

    try:
        asyncio.run(coordinator.async_send_command(device.device_id, {"state": "closed"}))
    except ApiError as err:
        assert err.code == "000201"
    else:
        raise AssertionError("expected ApiError")

    assert calls == 3
    assert coordinator.get_state(device.device_id)["online"] is False
    assert coordinator.get_state(device.device_id)["state"]["state"] == "open"


def test_async_send_command_accepts_matching_state_after_transport_error() -> None:
    """A transport error should be suppressed when read-back shows target state."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet-recovered"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {"state": {"state": "closed"}}
    calls: list[tuple[str, object]] = []
    sleeps: list[float] = []

    async def set_state(_device: Device, params: dict[str, object]) -> dict[str, object]:
        calls.append(("set_state", params))
        raise aiohttp.ClientConnectionError()

    async def get_state(_device: Device) -> dict[str, object]:
        calls.append(("get_state", None))
        return {"state": {"state": "open"}}

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    original_sleep = asyncio.sleep
    coordinator._client = SimpleNamespace(set_state=set_state, get_state=get_state)
    asyncio.sleep = fake_sleep
    try:
        result = asyncio.run(
            coordinator.async_send_command(device.device_id, {"state": "open"})
        )
    finally:
        asyncio.sleep = original_sleep

    assert result == {}
    assert calls == [
        ("set_state", {"state": "open"}),
        ("get_state", None),
        ("get_state", None),
    ]
    assert sleeps == [2.0]
    assert coordinator.get_state(device.device_id)["state"]["state"] == "open"


def test_async_send_command_retries_transport_error_when_readback_fails() -> None:
    """A transport error should retry once when read-back cannot verify success."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet-readback-fail"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {"state": {"state": "closed"}}
    calls: list[tuple[str, object]] = []

    async def set_state(_device: Device, params: dict[str, object]) -> dict[str, object]:
        calls.append(("set_state", params))
        if len([call for call in calls if call[0] == "set_state"]) == 1:
            raise aiohttp.ClientConnectionError()
        return {"ok": True}

    async def get_state(_device: Device) -> dict[str, object]:
        calls.append(("get_state", None))
        if len([call for call in calls if call[0] == "get_state"]) == 1:
            raise aiohttp.ClientConnectionError()
        return {"state": {"state": "open"}}

    async def fake_sleep(_delay: float) -> None:
        return None

    original_sleep = asyncio.sleep
    coordinator._client = SimpleNamespace(set_state=set_state, get_state=get_state)
    asyncio.sleep = fake_sleep
    try:
        result = asyncio.run(
            coordinator.async_send_command(device.device_id, {"state": "open"})
        )
    finally:
        asyncio.sleep = original_sleep

    assert result == {"ok": True}
    assert calls == [
        ("set_state", {"state": "open"}),
        ("get_state", None),
        ("set_state", {"state": "open"}),
        ("get_state", None),
    ]
    assert coordinator.get_state(device.device_id)["state"]["state"] == "open"


def test_async_send_command_retries_transport_error_when_state_not_target() -> None:
    """A transport error should retry once when read-back is not at target state."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet-retry-transport"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {"state": {"state": "closed"}}
    calls: list[tuple[str, object]] = []

    async def set_state(_device: Device, params: dict[str, object]) -> dict[str, object]:
        calls.append(("set_state", params))
        if len([call for call in calls if call[0] == "set_state"]) == 1:
            raise aiohttp.ClientConnectionError()
        return {"ok": True}

    async def get_state(_device: Device) -> dict[str, object]:
        calls.append(("get_state", None))
        if len([call for call in calls if call[0] == "get_state"]) == 1:
            return {"state": {"state": "closed"}}
        return {"state": {"state": "open"}}

    async def fake_sleep(_delay: float) -> None:
        return None

    original_sleep = asyncio.sleep
    coordinator._client = SimpleNamespace(set_state=set_state, get_state=get_state)
    asyncio.sleep = fake_sleep
    try:
        result = asyncio.run(
            coordinator.async_send_command(device.device_id, {"state": "open"})
        )
    finally:
        asyncio.sleep = original_sleep

    assert result == {"ok": True}
    assert calls == [
        ("set_state", {"state": "open"}),
        ("get_state", None),
        ("set_state", {"state": "open"}),
        ("get_state", None),
    ]
    assert coordinator.get_state(device.device_id)["state"]["state"] == "open"


def test_async_send_command_matches_valve_close_report_after_transport_error() -> None:
    """Manipulator close command should match reported state=closed."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("valve-recovered"), device_type="Manipulator")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {"state": {"state": "open"}}

    async def set_state(_device: Device, _params: dict[str, object]) -> dict[str, object]:
        raise aiohttp.ClientConnectionError()

    async def get_state(_device: Device) -> dict[str, object]:
        return {"state": {"state": "closed"}}

    async def fake_sleep(_delay: float) -> None:
        return None

    original_sleep = asyncio.sleep
    coordinator._client = SimpleNamespace(set_state=set_state, get_state=get_state)
    asyncio.sleep = fake_sleep
    try:
        result = asyncio.run(
            coordinator.async_send_command(device.device_id, {"state": "close"})
        )
    finally:
        asyncio.sleep = original_sleep

    assert result == {}
    assert coordinator.get_state(device.device_id)["state"]["state"] == "closed"


def test_entity_availability_handles_offline_and_stale_devices() -> None:
    """Availability logic should be conservative for offline or stale devices."""
    coordinator = make_coordinator()
    device = make_device(device_type="DoorSensor", display_type="DoorSensor")
    entity = YoLocalEntity(coordinator, device)

    coordinator._states[device.device_id] = {"online": False}
    assert entity.available is False

    stale = datetime.now(UTC) - timedelta(hours=13)
    coordinator._states[device.device_id] = {
        "online": True,
        "lastReportedAt": stale.isoformat(),
    }
    assert entity.available is False

    fresh = datetime.now(UTC) - timedelta(hours=1)
    coordinator._states[device.device_id] = {
        "online": True,
        "lastReportedAt": fresh.isoformat(),
    }
    assert entity.available is True


def test_entity_availability_ignores_transient_coordinator_health() -> None:
    """A hub-health blip should not make cached device entities unavailable."""
    coordinator = make_coordinator()
    device = make_device(device_type="Outlet", display_type="Outlet")
    entity = YoLocalEntity(coordinator, device)
    fresh = datetime.now(UTC) - timedelta(minutes=1)
    coordinator._states[device.device_id] = {
        "online": True,
        "lastReportedAt": fresh.isoformat(),
    }
    coordinator.async_set_update_error(aiohttp.ClientConnectionError("hub busy"))

    assert coordinator.last_update_success is False
    assert entity.available is True


def test_last_reported_sensor_keeps_value_during_partial_updates() -> None:
    """Partial payloads should not make the diagnostic timestamp flicker unavailable."""
    coordinator = make_coordinator()
    device = make_device(device_type="Outlet", display_type="Outlet")
    sensor = YoLocalLastReportedSensor(coordinator, device)

    coordinator._states[device.device_id] = {
        "lastReportedAt": "2026-05-27T05:55:21.874000+00:00"
    }
    initial_value = sensor.native_value

    assert initial_value is not None
    assert sensor.available is True

    coordinator._states[device.device_id] = {"state": {"state": "open"}}

    assert sensor.available is True
    assert sensor.native_value == initial_value

    coordinator._states[device.device_id] = {
        "reportAt": "2026-05-27T05:56:00.000000+00:00"
    }

    assert sensor.native_value == datetime(2026, 5, 27, 5, 56, tzinfo=UTC)


def test_th_limit_sensor_filters_sentinel_values() -> None:
    """Threshold sensor should reject unrealistic sentinel values."""
    coordinator = make_coordinator()
    device = make_device()
    coordinator._states[device.device_id] = {"state": {"tempLimit": {"max": 999}}}
    sensor = YoLocalTHLimitSensor(coordinator, device, "temperature", "max")
    assert sensor.native_value is None

    coordinator._states[device.device_id] = {"state": {"humidityLimit": {"min": -1}}}
    sensor = YoLocalTHLimitSensor(coordinator, device, "humidity", "min")
    assert sensor.native_value is None

    coordinator._states[device.device_id] = {"state": {"tempLimit": {"max": 32}}}
    sensor = YoLocalTHLimitSensor(coordinator, device, "temperature", "max")
    assert sensor.native_value == 32


def test_async_update_data_refreshes_battery_from_hub_state() -> None:
    """Scheduled refresh should update diagnostic fields like battery."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("refresh"), device_type="DoorSensor")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "online": True,
        "state": {"battery": 1, "state": "closed"},
    }

    async def get_state(_device: Device) -> dict[str, object]:
        return {
            "reportAt": "2026-03-09T12:00:00+00:00",
            "state": {"battery": 4},
        }

    coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")

    refreshed = asyncio.run(coordinator._async_update_data())

    assert refreshed[device.device_id]["lastReportedAt"] == "2026-03-09T12:00:00+00:00"
    assert refreshed[device.device_id]["state"] == {"battery": 4, "state": "closed"}
    assert coordinator._states[device.device_id]["state"]["battery"] == 4


def test_async_update_data_keeps_old_state_when_refresh_fails() -> None:
    """A failed per-device refresh should not drop the cached state."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("refresh-fail"), device_type="DoorSensor")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "online": True,
        "state": {"battery": 2, "state": "open"},
    }

    async def get_state(_device: Device) -> dict[str, object]:
        raise RuntimeError("boom")

    coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")

    refreshed = asyncio.run(coordinator._async_update_data())

    assert refreshed[device.device_id] == coordinator._states[device.device_id]
    assert refreshed[device.device_id]["state"]["battery"] == 2


def test_async_update_data_marks_offline_after_transient_device_unreachable(
    caplog,
) -> None:
    """Repeated read-only getState failures should mark the device offline."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    fresh = datetime.now(UTC) - timedelta(minutes=5)
    coordinator._states[device.device_id] = {
        "lastReportedAt": fresh.isoformat(),
        "online": True,
        "state": {"power": 57, "state": "open", "watt": 0}
    }
    calls = 0

    async def get_state(_device: Device) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise ApiError("000201", "Cannot connect to the device", "Outlet.getState")

    coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")
    caplog.set_level(logging.WARNING, logger="custom_components.yolocal.coordinator")

    refreshed = asyncio.run(coordinator._async_update_data())

    assert calls == 3
    assert refreshed[device.device_id] == coordinator._states[device.device_id]
    assert refreshed[device.device_id]["online"] is False
    assert refreshed[device.device_id]["state"]["state"] == "open"
    assert "Failed to refresh state" not in caplog.text


def test_async_update_data_keeps_online_when_retry_succeeds() -> None:
    """A transient getState failure should not mark offline if a retry succeeds."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet-retry"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "online": True,
        "state": {"power": 57, "state": "open", "watt": 0},
    }
    calls = 0

    async def get_state(_device: Device) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ApiError("000201", "Cannot connect to the device", "Outlet.getState")
        return {"state": {"power": 58, "state": "open", "watt": 0}}

    coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")

    refreshed = asyncio.run(coordinator._async_update_data())

    assert calls == 2
    assert refreshed[device.device_id].get("online", True) is True
    assert refreshed[device.device_id]["state"]["power"] == 58


def test_async_update_data_keeps_already_offline_device_offline() -> None:
    """Repeated getState failures should not republish a changed state when already offline."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet-offline"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    cached_state = {
        "online": False,
        "state": {"power": 57, "state": "open", "watt": 0},
    }
    coordinator._states[device.device_id] = cached_state

    async def get_state(_device: Device) -> dict[str, object]:
        raise ApiError("000201", "Cannot connect to the device", "Outlet.getState")

    coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")

    refreshed = asyncio.run(coordinator._async_update_data())

    assert refreshed[device.device_id] is cached_state


def test_http_success_marks_unreachable_device_online() -> None:
    """Successful getState should restore availability even without lastReportedAt."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("outlet-back"), device_type="Outlet")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "online": False,
        "state": {"power": 0, "state": "open", "watt": 0},
    }

    async def get_state(_device: Device) -> dict[str, object]:
        return {"state": {"power": 58, "state": "closed", "watt": 0}}

    coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")

    refreshed = asyncio.run(coordinator._async_update_data())

    assert refreshed[device.device_id]["online"] is True
    assert refreshed[device.device_id]["state"]["power"] == 58


def test_async_update_data_polls_even_for_recent_report() -> None:
    """Periodic refresh should poll even when MQTT reported recently."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("recent"), device_type="DoorSensor")
    coordinator._devices[device.device_id] = device
    fresh = datetime.now(UTC) - timedelta(minutes=5)
    coordinator._states[device.device_id] = {
        "online": True,
        "lastReportedAt": fresh.isoformat(),
        "state": {"battery": 3, "state": "closed"},
    }

    calls: list[str] = []

    async def get_state(_device: Device) -> dict[str, object]:
        calls.append("called")
        return {"state": {"battery": 4}}

    coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")

    refreshed = asyncio.run(coordinator._async_update_data())

    assert calls == ["called"]
    assert refreshed[device.device_id]["state"]["battery"] == 4


def test_async_update_data_polls_when_report_is_old() -> None:
    """Old reports should still trigger repair polling."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("old"), device_type="DoorSensor")
    coordinator._devices[device.device_id] = device
    stale = datetime.now(UTC) - timedelta(minutes=11)
    coordinator._states[device.device_id] = {
        "online": True,
        "lastReportedAt": stale.isoformat(),
        "state": {"battery": 1, "state": "closed"},
    }

    calls: list[str] = []

    async def get_state(_device: Device) -> dict[str, object]:
        calls.append("called")
        return {
            "reportAt": "2026-03-09T12:00:00+00:00",
            "state": {"battery": 4},
        }

    coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")

    refreshed = asyncio.run(coordinator._async_update_data())

    assert calls == ["called"]
    assert refreshed[device.device_id]["state"]["battery"] == 4


def test_async_update_data_refreshes_devices_concurrently() -> None:
    """State polling should not serialize startup across every device."""
    coordinator = make_coordinator()
    first = make_device(device_id=make_device_id("first"), device_type="DoorSensor")
    second = make_device(device_id=make_device_id("second"), device_type="DoorSensor")
    coordinator._devices = {
        first.device_id: first,
        second.device_id: second,
    }

    async def run_refresh() -> dict[str, dict[str, object]]:
        started: list[str] = []
        both_started = asyncio.Event()

        async def get_state(device: Device) -> dict[str, object]:
            started.append(device.device_id)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            return {"state": {"battery": 4}}

        coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")
        return await asyncio.wait_for(coordinator._async_update_data(), timeout=1)

    refreshed = asyncio.run(run_refresh())

    assert refreshed[first.device_id]["state"]["battery"] == 4
    assert refreshed[second.device_id]["state"]["battery"] == 4


def test_async_update_data_merges_http_results_with_current_state() -> None:
    """HTTP refresh completion should not discard MQTT updates from the same window."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("race"), device_type="DoorSensor")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "state": {"battery": 2, "state": "closed"},
    }

    async def get_state(_device: Device) -> dict[str, object]:
        coordinator._states[device.device_id] = {
            "state": {"battery": 2, "state": "open"},
            "lastReportedAt": "2026-03-09T12:00:00+00:00",
        }
        return {"state": {"battery": 4}}

    coordinator._client = SimpleNamespace(get_state=get_state, host="127.0.0.1")

    refreshed = asyncio.run(coordinator._async_update_data())

    assert refreshed[device.device_id]["state"] == {"battery": 4, "state": "open"}
    assert (
        refreshed[device.device_id]["lastReportedAt"]
        == "2026-03-09T12:00:00+00:00"
    )


def test_async_setup_defers_initial_data_publication() -> None:
    """Initial setup should leave state polling to the first coordinator refresh."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("setup"), device_type="DoorSensor")
    get_state_calls: list[str] = []

    async def get_devices() -> list[Device]:
        return [device]

    async def get_state(_device: Device) -> dict[str, object]:
        get_state_calls.append("called")
        return {
            "reportAt": "2026-03-09T12:00:00+00:00",
            "state": {"battery": 4, "state": "closed"},
        }

    coordinator._client = SimpleNamespace(
        get_devices=get_devices,
        get_state=get_state,
        host="127.0.0.1",
    )

    asyncio.run(coordinator._async_setup())

    assert coordinator.data is None
    assert coordinator._state_refresh_task is not None
    assert coordinator._reconnect_task is not None
    assert coordinator.get_state(device.device_id) == {}
    assert get_state_calls == []
    for coro in coordinator.hass._scheduled_coroutines:
        coro.close()


def test_connect_mqtt_subscribes_to_all_configured_hosts(monkeypatch) -> None:
    """MQTT should subscribe through every configured hub interface."""
    coordinator = make_coordinator()
    coordinator._client = SimpleNamespace(
        host="ethernet.local",
        hosts=("ethernet.local", "wifi.local"),
    )
    coordinator._token_manager = SimpleNamespace(
        client_id="client",
        get_token=lambda: asyncio.sleep(0, result="token"),
    )
    connected_hosts: list[str] = []

    async def connect_host(host: str) -> None:
        connected_hosts.append(host)
        coordinator._mqtt_clients[host] = FakeMqttClient()

    coordinator._connect_mqtt_host = connect_host

    asyncio.run(coordinator._connect_mqtt())

    assert connected_hosts == ["ethernet.local", "wifi.local"]
    assert set(coordinator._mqtt_clients) == {"ethernet.local", "wifi.local"}


def test_connect_mqtt_keeps_secondary_when_primary_fails() -> None:
    """Startup MQTT connection should continue when one interface is down."""
    coordinator = make_coordinator()
    coordinator._client = SimpleNamespace(
        host="ethernet.local",
        hosts=("ethernet.local", "wifi.local"),
    )
    connected_hosts: list[str] = []

    async def connect_host(host: str) -> None:
        if host == "ethernet.local":
            raise ConnectionError("ethernet down")
        connected_hosts.append(host)
        coordinator._mqtt_clients[host] = FakeMqttClient()

    coordinator._connect_mqtt_host = connect_host

    asyncio.run(coordinator._connect_mqtt())

    assert connected_hosts == ["wifi.local"]
    assert set(coordinator._mqtt_clients) == {"wifi.local"}


def test_connect_mqtt_raises_only_when_all_hosts_fail() -> None:
    """Startup MQTT connection should fail only when no interface connects."""
    coordinator = make_coordinator()
    coordinator._client = SimpleNamespace(
        host="ethernet.local",
        hosts=("ethernet.local", "wifi.local"),
    )

    async def connect_host(host: str) -> None:
        raise ConnectionError(f"{host} down")

    coordinator._connect_mqtt_host = connect_host

    try:
        asyncio.run(coordinator._connect_mqtt())
    except ConnectionError as err:
        assert "wifi.local down" in str(err)
    else:
        raise AssertionError("expected all-host MQTT failure")


def test_connect_mqtt_host_uses_host_specific_token(monkeypatch) -> None:
    """Each MQTT interface should authenticate against that exact host."""
    coordinator = make_coordinator()
    token_manager = FakeHostTokenManager()
    coordinator._token_manager = token_manager
    created_clients: list[object] = []

    class FakeYoLinkMQTTClient:
        def __init__(
            self,
            *,
            host: str,
            net_id: str,
            client_id: str,
            access_token: str,
            port: int,
        ) -> None:
            self.host = host
            self.net_id = net_id
            self.client_id = client_id
            self.access_token = access_token
            self.port = port
            self.callbacks = []
            self.disconnect_callbacks = []
            created_clients.append(self)

        def subscribe(self, callback):
            self.callbacks.append(callback)

        def on_disconnect(self, callback):
            self.disconnect_callbacks.append(callback)

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(
        "custom_components.yolocal.coordinator.YoLinkMQTTClient",
        FakeYoLinkMQTTClient,
    )

    asyncio.run(coordinator._connect_mqtt_host("wifi.local"))

    assert token_manager.hosts == ["wifi.local"]
    assert len(created_clients) == 1
    assert created_clients[0].host == "wifi.local"
    assert created_clients[0].access_token == "token-for-wifi.local"
    assert "wifi.local" in coordinator._mqtt_clients


def test_mqtt_disconnect_reconnects_missing_host_only() -> None:
    """A failed interface should reconnect without dropping the other MQTT path."""
    coordinator = make_coordinator()
    coordinator._client = SimpleNamespace(
        host="ethernet.local",
        hosts=("ethernet.local", "wifi.local"),
    )
    ethernet_client = FakeMqttClient()
    wifi_client = FakeMqttClient()
    coordinator._mqtt_clients = {
        "ethernet.local": ethernet_client,
        "wifi.local": wifi_client,
    }
    reconnected_hosts: list[str] = []

    async def connect_host(host: str) -> None:
        reconnected_hosts.append(host)
        coordinator._mqtt_clients[host] = FakeMqttClient()

    coordinator._connect_mqtt_host = connect_host
    coordinator._on_mqtt_disconnect("ethernet.local")
    reconnect_coro = coordinator.hass._scheduled_coroutines.pop()

    asyncio.run(reconnect_coro)

    assert reconnected_hosts == ["ethernet.local"]
    assert "wifi.local" in coordinator._mqtt_clients
    assert wifi_client.disconnect_calls == 0


def test_async_setup_removes_stale_registry_devices() -> None:
    """Startup should purge registry devices no longer present on the hub."""
    coordinator = make_coordinator()
    active = make_device(device_id=make_device_id("active"), device_type="DoorSensor")
    stale_device_id = make_device_id("stale")

    registry = dr.async_get(coordinator.hass)
    registry.devices[frozenset({(DOMAIN, stale_device_id)})] = SimpleNamespace(
        id="stale-device-id",
        identifiers={(DOMAIN, stale_device_id)},
        config_entry_id="entry",
    )
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(coordinator.hass)
    entity_registry.entities["sensor.stale_battery"] = SimpleNamespace(
        entity_id="sensor.stale_battery",
        unique_id=f"{stale_device_id}_battery",
        config_entry_id="entry",
    )
    entity_registry.entities["binary_sensor.stale_sensor"] = SimpleNamespace(
        entity_id="binary_sensor.stale_sensor",
        unique_id=stale_device_id,
        config_entry_id="entry",
    )

    async def get_devices() -> list[Device]:
        return [active]

    async def get_state(_device: Device) -> dict[str, object]:
        return {
            "reportAt": "2026-03-09T12:00:00+00:00",
            "state": {"battery": 4, "state": "closed"},
        }

    async def connect_mqtt() -> None:
        return None

    coordinator._client = SimpleNamespace(
        get_devices=get_devices,
        get_state=get_state,
        host="127.0.0.1",
    )
    coordinator._connect_mqtt = connect_mqtt

    asyncio.run(coordinator._async_setup())

    assert registry.async_get_device(identifiers={(DOMAIN, stale_device_id)}) is None
    assert entity_registry.async_get("sensor.stale_battery") is None
    assert entity_registry.async_get("binary_sensor.stale_sensor") is None
    for coro in coordinator.hass._scheduled_coroutines:
        coro.close()


def test_async_refresh_devices_notifies_listeners_for_new_device() -> None:
    """Device additions should notify listeners and seed initial state."""
    listener_calls: list[tuple[list[str], list[str]]] = []

    coordinator = make_coordinator()
    existing = make_device(device_id=make_device_id("existing"), device_type="DoorSensor")
    added = make_device(device_id=make_device_id("added"), device_type="DoorSensor")
    coordinator._devices = {existing.device_id: existing}
    coordinator._states[existing.device_id] = {"state": {"battery": 1}}
    coordinator.register_device_registry_listener(
        lambda added_devices, removed_devices: listener_calls.append(
            (
                [device.device_id for device in added_devices],
                [device.device_id for device in removed_devices],
            )
        )
    )

    async def get_devices() -> list[Device]:
        return [existing, added]

    async def get_state(device: Device) -> dict[str, object]:
        if device.device_id == added.device_id:
            return {
                "reportAt": "2026-03-09T12:00:00+00:00",
                "state": {"battery": 4, "state": "closed"},
            }
        raise AssertionError("unexpected get_state call")

    coordinator._client = SimpleNamespace(
        get_devices=get_devices,
        get_state=get_state,
        host="127.0.0.1",
    )

    refreshed = asyncio.run(coordinator._async_refresh_devices())

    assert refreshed is True
    assert coordinator._states[existing.device_id]["state"]["battery"] == 1
    assert coordinator._states[added.device_id]["state"]["battery"] == 4
    assert set(coordinator._devices) == {existing.device_id, added.device_id}
    assert listener_calls == [([added.device_id], [])]


def test_async_refresh_devices_notifies_listeners_for_removed_device() -> None:
    """Device removals should notify listeners and drop stale cached state."""
    listener_calls: list[tuple[list[str], list[str]]] = []

    coordinator = make_coordinator()
    kept = make_device(device_id=make_device_id("kept"), device_type="DoorSensor")
    removed = make_device(device_id=make_device_id("removed"), device_type="DoorSensor")
    coordinator._devices = {
        kept.device_id: kept,
        removed.device_id: removed,
    }
    coordinator._states[kept.device_id] = {"state": {"battery": 1}}
    coordinator._states[removed.device_id] = {"state": {"battery": 4}}
    coordinator.register_device_registry_listener(
        lambda added_devices, removed_devices: listener_calls.append(
            (
                [device.device_id for device in added_devices],
                [device.device_id for device in removed_devices],
            )
        )
    )

    async def get_devices() -> list[Device]:
        return [kept]

    coordinator._client = SimpleNamespace(
        get_devices=get_devices,
        get_state=None,
        host="127.0.0.1",
    )

    refreshed = asyncio.run(coordinator._async_refresh_devices())

    assert refreshed is True
    assert removed.device_id not in coordinator._states
    assert set(coordinator._devices) == {kept.device_id}
    assert listener_calls == [([], [removed.device_id])]


def test_async_refresh_devices_removes_device_registry_entry() -> None:
    """Removed devices should be deleted from the HA device registry."""
    coordinator = make_coordinator()
    kept = make_device(device_id=make_device_id("kept-reg"), device_type="DoorSensor")
    removed = make_device(
        device_id=make_device_id("removed-reg"),
        device_type="DoorSensor",
    )
    coordinator._devices = {
        kept.device_id: kept,
        removed.device_id: removed,
    }

    registry = dr.async_get(coordinator.hass)
    removed_identifiers = frozenset({(DOMAIN, removed.device_id)})
    registry.devices[removed_identifiers] = SimpleNamespace(
        id="device-registry-id",
        identifiers={(DOMAIN, removed.device_id)},
        config_entry_id="entry",
    )

    async def get_devices() -> list[Device]:
        return [kept]

    coordinator._client = SimpleNamespace(
        get_devices=get_devices,
        get_state=None,
        host="127.0.0.1",
    )

    refreshed = asyncio.run(coordinator._async_refresh_devices())

    assert refreshed is True
    assert registry.async_get_device(identifiers={(DOMAIN, removed.device_id)}) is None


def test_async_refresh_devices_debounces_hub_health_failure() -> None:
    """A single device-list failure should not mark the coordinator unhealthy."""
    coordinator = make_coordinator()
    failure = aiohttp.ClientConnectionError("all hosts offline")

    async def get_devices() -> list[Device]:
        raise failure

    coordinator._client = SimpleNamespace(
        get_devices=get_devices,
        get_state=None,
        host="127.0.0.1",
    )

    first_refreshed = asyncio.run(coordinator._async_refresh_devices())

    assert first_refreshed is False
    assert coordinator.last_update_success is True
    assert coordinator.last_exception is None

    second_refreshed = asyncio.run(coordinator._async_refresh_devices())

    assert second_refreshed is False
    assert coordinator.last_update_success is False
    assert coordinator.last_exception is failure


def test_async_refresh_devices_clears_unhealthy_status_after_hub_recovery() -> None:
    """A later successful device-list call should clear the coordinator error."""
    coordinator = make_coordinator()
    coordinator.async_set_update_error(aiohttp.ClientConnectionError("offline"))
    device = make_device(device_id=make_device_id("recovered"), device_type="DoorSensor")
    coordinator._devices = {device.device_id: device}
    coordinator._states = {device.device_id: {"state": {"battery": 4}}}

    async def get_devices() -> list[Device]:
        return [device]

    coordinator._client = SimpleNamespace(
        get_devices=get_devices,
        get_state=None,
        host="127.0.0.1",
    )

    refreshed = asyncio.run(coordinator._async_refresh_devices())

    assert refreshed is False
    assert coordinator.last_update_success is True
    assert coordinator.last_exception is None
    assert coordinator.data == coordinator._states


def test_state_refresh_failure_does_not_mark_hub_unhealthy() -> None:
    """Per-device state refresh failures should not mark hub health bad."""
    coordinator = make_coordinator()

    async def update_data() -> dict[str, dict[str, object]]:
        raise aiohttp.ClientConnectionError("state refresh failed")

    sleep_calls = 0

    async def fake_sleep(_interval: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise RuntimeError("stop")

    original_sleep = asyncio.sleep
    coordinator._async_update_data = update_data
    asyncio.sleep = fake_sleep
    try:
        try:
            asyncio.run(coordinator._async_state_refresh_loop())
        except RuntimeError as exc:
            assert str(exc) == "stop"
    finally:
        asyncio.sleep = original_sleep

    assert coordinator.last_update_success is True
    assert coordinator.last_exception is None


def test_device_discovery_loop_keeps_running_after_change() -> None:
    """Discovery loop should keep polling after a membership change."""
    coordinator = make_coordinator()
    sleep_calls: list[float] = []
    refresh_results = iter([True, False, RuntimeError("stop")])
    refresh_calls: list[str] = []

    async def fake_sleep(interval: float) -> None:
        sleep_calls.append(interval)

    async def fake_refresh_devices() -> bool:
        refresh_calls.append("called")
        result = next(refresh_results)
        if isinstance(result, Exception):
            raise result
        return result

    original_sleep = asyncio.sleep
    coordinator._async_refresh_devices = fake_refresh_devices
    asyncio.sleep = fake_sleep
    try:
        try:
            asyncio.run(coordinator._async_device_discovery_loop())
        except RuntimeError as exc:
            assert str(exc) == "stop"
    finally:
        asyncio.sleep = original_sleep

    assert len(refresh_calls) == 3
    assert len(sleep_calls) == 3


def test_device_from_api_preserves_motion_sensor_type_for_7805() -> None:
    """YS7805-UC should remain a MotionSensor with the derived model number."""
    device = Device.from_api(
        {
            "deviceId": make_device_id("motion-api"),
            "name": "Terrace motion sensor",
            "token": "token",
            "type": "MotionSensor",
            "appEui": make_app_eui("7805"),
        }
    )

    assert device.device_type == "MotionSensor"
    assert device.display_type == "MotionSensor"
    assert device.model == "YS7805-UC"


def test_device_from_api_preserves_door_sensor_type_for_7707() -> None:
    """YS7707-UC should follow generic door-sensor handling when the hub reports it."""
    device = Device.from_api(
        {
            "deviceId": make_device_id("door-api"),
            "name": "Side door contact",
            "token": "token",
            "type": "DoorSensor",
            "appEui": make_app_eui("7707"),
        }
    )

    assert device.device_type == "DoorSensor"
    assert device.display_type == "DoorSensor"
    assert device.model == "YS7707-UC"


def test_valve_is_closed_reads_nested_state() -> None:
    """Valve should report closed/open correctly from HTTP-style nested state."""
    coordinator = make_coordinator()
    device = make_device(device_type="Manipulator", display_type="Manipulator")
    entity = YoLocalValve(coordinator, device)

    coordinator._states[device.device_id] = {"state": {"state": "closed"}}
    assert entity.is_closed is True

    coordinator._states[device.device_id] = {"state": {"state": "open"}}
    assert entity.is_closed is False


def test_valve_is_closed_reads_flat_state() -> None:
    """Valve should fall back to reading a flat state string."""
    coordinator = make_coordinator()
    device = make_device(device_type="Manipulator", display_type="Manipulator")
    entity = YoLocalValve(coordinator, device)

    coordinator._states[device.device_id] = {"state": "closed"}
    assert entity.is_closed is True

    coordinator._states[device.device_id] = {"state": "open"}
    assert entity.is_closed is False


def test_valve_is_closed_returns_none_when_state_unknown() -> None:
    """Valve should return None when no state information is available."""
    coordinator = make_coordinator()
    device = make_device(device_type="Manipulator", display_type="Manipulator")
    entity = YoLocalValve(coordinator, device)

    coordinator._states[device.device_id] = {}
    assert entity.is_closed is None

    coordinator._states[device.device_id] = {"state": {"battery": 4}}
    assert entity.is_closed is None


def test_valve_tracks_physical_mqtt_open_event() -> None:
    """Physical valve open via MQTT should update is_closed to False."""
    coordinator = make_coordinator()
    device = make_device(device_type="Manipulator", display_type="Manipulator")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "state": "closed",
    }
    entity = YoLocalValve(coordinator, device)

    open_event = coordinator._normalize_mqtt_event(
        device,
        {
            "state": "open",
            "lastReportedAt": "2026-05-27T06:00:00.000000+00:00",
        },
    )
    coordinator._update_device_state(
        device.device_id,
        coordinator._merge_state_payload(
            coordinator.get_state(device.device_id),
            open_event,
        ),
    )

    assert coordinator.get_state(device.device_id)["state"]["state"] == "open"
    assert entity.is_closed is False


def test_valve_tracks_physical_mqtt_close_event() -> None:
    """Physical valve close via MQTT should update is_closed to True."""
    coordinator = make_coordinator()
    device = make_device(device_type="Manipulator", display_type="Manipulator")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "state": "open",
    }
    entity = YoLocalValve(coordinator, device)

    close_event = coordinator._normalize_mqtt_event(
        device,
        {
            "state": "closed",
            "lastReportedAt": "2026-05-27T06:01:00.000000+00:00",
        },
    )
    coordinator._update_device_state(
        device.device_id,
        coordinator._merge_state_payload(
            coordinator.get_state(device.device_id),
            close_event,
        ),
    )

    assert coordinator.get_state(device.device_id)["state"]["state"] == "closed"
    assert entity.is_closed is True


def test_async_open_valve_sends_open_and_refreshes_state() -> None:
    """async_open_valve should send state=open and pull fresh state from the hub."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("valve"), device_type="Manipulator")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {"state": {"state": "closed"}}
    entity = YoLocalValve(coordinator, device)

    calls: list[tuple[str, object]] = []

    async def set_state(_device, params):
        calls.append(("set_state", params))
        return {"ok": True}

    async def get_state(_device):
        calls.append(("get_state", None))
        return {"state": {"state": "open"}}

    coordinator._client = SimpleNamespace(set_state=set_state, get_state=get_state)

    asyncio.run(entity.async_open_valve())

    assert calls == [("set_state", {"state": "open"}), ("get_state", None)]
    assert coordinator.get_state(device.device_id)["state"]["state"] == "open"
    assert entity.is_closed is False


def test_async_close_valve_sends_close_not_closed() -> None:
    """async_close_valve must send state='close', not state='closed'."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("valve-close"), device_type="Manipulator")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {"state": {"state": "open"}}
    entity = YoLocalValve(coordinator, device)

    calls: list[tuple[str, object]] = []

    async def set_state(_device, params):
        calls.append(("set_state", params))
        return {"ok": True}

    async def get_state(_device):
        calls.append(("get_state", None))
        return {"state": {"state": "closed"}}

    coordinator._client = SimpleNamespace(set_state=set_state, get_state=get_state)

    asyncio.run(entity.async_close_valve())

    # The YoLink API uses "close" as the command, not "closed"
    assert calls[0] == ("set_state", {"state": "close"})
    assert calls[1] == ("get_state", None)
    assert coordinator.get_state(device.device_id)["state"]["state"] == "closed"
    assert entity.is_closed is True


def test_valve_open_retries_transient_failure() -> None:
    """A single radio failure on open should be retried before succeeding."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("valve-retry"), device_type="Manipulator")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "online": True,
        "state": {"state": "closed"},
    }
    entity = YoLocalValve(coordinator, device)
    calls: list[str] = []

    async def set_state(_device, _params):
        calls.append("set_state")
        if len(calls) == 1:
            raise ApiError("000201", "Cannot connect to the device", "Manipulator.setState")
        return {"ok": True}

    async def get_state(_device):
        calls.append("get_state")
        return {"state": {"state": "open"}}

    coordinator._client = SimpleNamespace(set_state=set_state, get_state=get_state)

    asyncio.run(entity.async_open_valve())

    assert calls == ["set_state", "set_state", "get_state"]
    assert coordinator.get_state(device.device_id).get("online", True) is True


def test_valve_close_marks_offline_after_repeated_failures() -> None:
    """Repeated radio failures on close should mark the valve offline."""
    coordinator = make_coordinator()
    device = make_device(device_id=make_device_id("valve-offline"), device_type="Manipulator")
    coordinator._devices[device.device_id] = device
    coordinator._states[device.device_id] = {
        "online": True,
        "state": {"state": "open"},
    }
    entity = YoLocalValve(coordinator, device)

    async def set_state(_device, _params):
        raise ApiError("000201", "Cannot connect to the device", "Manipulator.setState")

    coordinator._client = SimpleNamespace(set_state=set_state)

    try:
        asyncio.run(entity.async_close_valve())
    except ApiError as err:
        assert err.code == "000201"
    else:
        raise AssertionError("expected ApiError")

    assert coordinator.get_state(device.device_id)["online"] is False
    # Cached state should be unchanged despite the failure
    assert coordinator.get_state(device.device_id)["state"]["state"] == "open"


def test_manipulator_device_creates_valve_entity() -> None:
    """build_entities should produce a YoLocalValve only for Manipulator devices."""
    from custom_components.yolocal.valve import YoLocalValve as _Valve

    coordinator = make_coordinator()

    manipulator = make_device(
        device_id=make_device_id("manipulator"),
        device_type="Manipulator",
        display_type="Manipulator",
    )
    door = make_device(
        device_id=make_device_id("door-not-valve"),
        device_type="DoorSensor",
        display_type="DoorSensor",
    )

    def build_entities(device):
        if device.device_type != "Manipulator":
            return []
        return [_Valve(coordinator, device)]

    assert len(build_entities(manipulator)) == 1
    assert isinstance(build_entities(manipulator)[0], _Valve)
    assert build_entities(door) == []


def test_valve_creates_battery_sensor_when_payload_has_battery() -> None:
    """Manipulator devices that report battery should get a battery diagnostic."""
    coordinator = make_coordinator()
    device = make_device(
        device_id=make_device_id("valve-battery"),
        device_type="Manipulator",
        display_type="Manipulator",
    )
    coordinator._states[device.device_id] = {"state": {"battery": 3, "state": "closed"}}

    entities = build_sensor_entities(coordinator, device)

    assert any(isinstance(e, YoLocalBatterySensor) for e in entities)


def test_valve_omits_battery_sensor_without_battery_in_payload() -> None:
    """Manipulator devices with no battery field should not get a battery sensor."""
    coordinator = make_coordinator()
    device = make_device(
        device_id=make_device_id("valve-no-battery"),
        device_type="Manipulator",
        display_type="Manipulator",
    )
    coordinator._states[device.device_id] = {"state": {"state": "closed"}}

    entities = build_sensor_entities(coordinator, device)

    assert not any(isinstance(e, YoLocalBatterySensor) for e in entities)


def test_device_from_api_recognises_manipulator_type() -> None:
    """Device.from_api should preserve Manipulator as device_type and display_type."""
    device = Device.from_api(
        {
            "deviceId": make_device_id("valve-api"),
            "name": "Garden water valve",
            "token": "token",
            "type": "Manipulator",
            "appEui": make_app_eui("4909"),
        }
    )

    assert device.device_type == "Manipulator"
    assert device.display_type == "Manipulator"
    assert device.model == "YS4909-UC"
