"""Data coordinator for YoLink Local integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from math import isfinite
from time import monotonic
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .api import (
    ApiError,
    Device,
    DeviceEvent,
    TokenManager,
    YoLinkClient,
    YoLinkMQTTClient,
)
from .const import (
    DEVICE_DISCOVERY_INTERVAL,
    DOMAIN,
    EVENT_DATA_RECORD,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
type DeviceRegistryListener = Callable[[list[Device], list[Device]], None]
TRANSIENT_DEVICE_UNREACHABLE = "000201"
STALE_REPORT_AGE = timedelta(hours=12)
MQTT_EVENT_MAX_DELAY = timedelta(minutes=30)
SET_STATE_TRANSPORT_RETRY_DELAY = 2.0
HUB_HEALTH_FAILURE_THRESHOLD = 2
MQTT_DUPLICATE_WINDOW = 300.0
MQTT_DUPLICATE_CACHE_LIMIT = 256
MQTT_CONTACT_EVENT_WINDOW = timedelta(hours=24)
MQTT_CONTACT_EVENT_CACHE_LIMIT = 1024
CONTACT_STORE_VERSION = 1
TH_MEASUREMENT_FIELDS = ("temperature", "humidity")
TH_MEASUREMENT_TIMES = "_thMeasurementTimes"


def device_contact_timestamp(state: dict[str, Any]) -> datetime | None:
    """Return the most recent device-originated contact time, if any.

    ``lastContactAt`` is maintained by the coordinator from either a device
    ``reportAt`` value or a new MQTT device event.  ``lastReportedAt`` remains
    only as a migration fallback for state created by older releases.
    """
    candidates = (
        _parse_timestamp(state.get("lastContactAt")),
        _parse_timestamp(state.get("lastSeenAt")),
        _parse_timestamp(state.get("reportAt")),
        _parse_timestamp(state.get("lastReportedAt")),
    )
    return max((timestamp for timestamp in candidates if timestamp is not None), default=None)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a hub timestamp, accepting offset-naive UTC values."""
    if not value:
        return None
    try:
        parsed = dt_util.parse_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is not None and parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _thsensor_measurement_timestamp(state: dict[str, Any], key: str) -> datetime | None:
    """Read a field's sample time, falling back to the full device report."""
    times = state.get(TH_MEASUREMENT_TIMES)
    measured_at = _parse_timestamp(times.get(key)) if isinstance(times, dict) else None
    return measured_at or _parse_timestamp(state.get("reportAt"))


def state_has_battery(state: dict[str, Any]) -> bool:
    """Recognize battery capability from the device payload."""
    if "battery" in state:
        return True
    nested = state.get("state")
    return isinstance(nested, dict) and "battery" in nested


def _diagnostic_timestamp(value: Any) -> str:
    """Format a timestamp without writing untrusted payload text to the log."""
    if value is None:
        return "<missing>"
    if not isinstance(value, str):
        return "<invalid>"
    try:
        parsed = dt_util.parse_datetime(value)
    except (TypeError, ValueError):
        return "<invalid>"
    if parsed is None:
        return "<invalid>"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _diagnostic_online(value: Any) -> str:
    """Format only the expected API boolean, never an arbitrary payload."""
    if value is True:
        return "True"
    if value is False:
        return "False"
    return "<missing>" if value is None else "<invalid>"


def _went_offline(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Return True only for a new explicit offline state."""
    return bool(previous.get("online", True)) and not bool(current.get("online", True))


class YoLocalCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for YoLink Local devices.

    Manages MQTT subscription for real-time updates and provides
    device state to entities. Falls back to HTTP polling every 5 minutes
    to ensure state stays current if MQTT events are missed.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: YoLinkClient,
        token_manager: TokenManager,
        session: aiohttp.ClientSession,
        config_entry_id: str,
        net_id: str,
        mqtt_port: int = 18080,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="YoLink Local",
            update_interval=None,
        )
        self._client = client
        self._token_manager = token_manager
        self._session = session
        self._config_entry_id = config_entry_id
        self._net_id = net_id
        self._mqtt_port = mqtt_port
        self._mqtt_clients: dict[str, YoLinkMQTTClient] = {}
        self._retired_mqtt_clients: list[YoLinkMQTTClient] = []
        self._devices: dict[str, Device] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._reconnect_task: asyncio.Task[None] | None = None
        self._mqtt_reconnect_wakeup = asyncio.Event()
        self._discovery_task: asyncio.Task[None] | None = None
        self._state_refresh_task: asyncio.Task[None] | None = None
        self._device_registry_listeners: list[DeviceRegistryListener] = []
        self._shutdown = False
        self._hub_health_failures = 0
        self._recent_mqtt_events: dict[str, float] = {}
        self._contact_events: dict[str, str] = {}
        self._contacts: dict[str, dict[str, str]] = {}
        self._contact_store: Store[dict[str, Any]] = Store(
            hass,
            CONTACT_STORE_VERSION,
            f"{DOMAIN}.{config_entry_id}.contacts",
        )
        self._contact_store_enabled = False
        self._logged_stale_offline_reports: dict[str, str] = {}

    @property
    def devices(self) -> dict[str, Device]:
        """Return the device registry."""
        return self._devices

    def device_has_battery(self, device_id: str, state: dict[str, Any]) -> bool:
        """Recognize battery support from current or previously registered data."""
        if state_has_battery(state):
            return True
        registry = er.async_get(self.hass)
        return registry.async_get_entity_id(
            "sensor", DOMAIN, f"{device_id}_battery"
        ) is not None

    async def _async_load_contact_state(self) -> None:
        """Restore durable device-contact and replay-detection state."""
        try:
            stored = await self._contact_store.async_load()
        except Exception:  # Storage must not prevent integration startup.
            _LOGGER.debug("Could not load YoLink device contact state", exc_info=True)
            return

        self._contact_store_enabled = True
        if not isinstance(stored, dict):
            return

        contacts = stored.get("contacts")
        if isinstance(contacts, dict):
            self._contacts = {
                device_id: {"at": record["at"], "source": record["source"]}
                for device_id, record in contacts.items()
                if isinstance(device_id, str)
                and isinstance(record, dict)
                and isinstance(record.get("at"), str)
                and _parse_timestamp(record["at"]) is not None
                and record.get("source") in {"reportAt", "mqtt_event"}
            }

        contact_events = stored.get("contact_events")
        if isinstance(contact_events, dict):
            self._contact_events = {
                event_id: seen_at
                for event_id, seen_at in contact_events.items()
                if isinstance(event_id, str)
                and isinstance(seen_at, str)
                and _parse_timestamp(seen_at) is not None
            }
        self._prune_contact_events(dt_util.utcnow())

    def _contact_store_data(self) -> dict[str, Any]:
        """Return the bounded contact ledger persisted for the next restart."""
        return {
            "contacts": self._contacts,
            "contact_events": self._contact_events,
        }

    def _schedule_contact_store_save(self) -> None:
        """Persist contact evidence without blocking MQTT event processing."""
        if self._contact_store_enabled:
            self._contact_store.async_delay_save(self._contact_store_data, 1)

    def _prune_contact_state(self, device_ids: set[str]) -> None:
        """Discard contact records for devices no longer owned by this entry."""
        before = len(self._contacts)
        self._contacts = {
            device_id: record
            for device_id, record in self._contacts.items()
            if device_id in device_ids
        }
        if len(self._contacts) != before:
            self._schedule_contact_store_save()

    def _prune_contact_events(self, now: datetime) -> None:
        """Bound durable replay detection to the stale-contact interval."""
        cutoff = now - MQTT_CONTACT_EVENT_WINDOW
        self._contact_events = {
            event_id: seen_at
            for event_id, seen_at in self._contact_events.items()
            if (parsed := _parse_timestamp(seen_at)) is not None and parsed >= cutoff
        }
        if len(self._contact_events) > MQTT_CONTACT_EVENT_CACHE_LIMIT:
            newest = sorted(
                self._contact_events.items(),
                key=lambda item: _parse_timestamp(item[1]) or datetime.min.replace(tzinfo=UTC),
                reverse=True,
            )[:MQTT_CONTACT_EVENT_CACHE_LIMIT]
            self._contact_events = dict(newest)

    def _record_contact(
        self,
        device_id: str,
        timestamp: datetime,
        source: str,
    ) -> bool:
        """Record newer device contact and return whether it advanced."""
        existing = self._contacts.get(device_id)
        existing_at = _parse_timestamp(existing.get("at")) if existing else None
        if existing_at is not None and timestamp <= existing_at:
            return False
        self._contacts[device_id] = {"at": timestamp.isoformat(), "source": source}
        self._schedule_contact_store_save()
        return True

    def _apply_contact_record(
        self,
        device_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Overlay the durable contact record onto an API state payload."""
        record = self._contacts.get(device_id)
        if record is None:
            return state
        return {
            **state,
            "lastContactAt": record["at"],
            "lastContactSource": record["source"],
        }

    def _record_report_contact(self, device_id: str, state: dict[str, Any]) -> bool:
        """Use a device-provided report timestamp as authoritative contact evidence."""
        if (reported_at := _parse_timestamp(state.get("reportAt"))) is None:
            return False
        return self._record_contact(device_id, reported_at, "reportAt")

    def _merge_command_http_state(
        self,
        device_id: str,
        incoming_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge a command follow-up read without making HTTP proof of contact."""
        current_state = self._states.get(device_id, {})
        if self._is_older_report(current_state, incoming_state):
            return current_state
        merged = self._merge_state_payload(current_state, incoming_state)
        self._record_report_contact(device_id, merged)
        merged = self._apply_contact_record(device_id, merged)
        return self._battery_liveness_state(device_id, merged, current_state)

    def _mqtt_contact_identity(self, event: DeviceEvent) -> str | None:
        """Return the durable identity of an event, when the hub supplies one."""
        msgid = event.raw.get("msgid")
        if isinstance(msgid, (str, int)) and not isinstance(msgid, bool) and msgid != "":
            return f"{event.device_id}|{event.event}|{msgid}"
        return None

    def _is_replayed_contact_event(self, event: DeviceEvent) -> bool:
        """Ignore a previously seen MQTT event before it can revive a device."""
        event_id = self._mqtt_contact_identity(event)
        if event_id is None:
            return False
        now = dt_util.utcnow()
        self._prune_contact_events(now)
        if event_id in self._contact_events:
            return True
        self._contact_events[event_id] = now.isoformat()
        self._prune_contact_events(now)
        self._schedule_contact_store_save()
        return False

    def _battery_liveness_state(
        self,
        device_id: str,
        state: dict[str, Any],
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        """Use report age, without undoing a prior offline state on a heartbeat."""
        if not self.device_has_battery(device_id, state):
            return state
        last_contact = device_contact_timestamp(state)
        if last_contact is None:
            return state
        age = dt_util.utcnow() - last_contact
        if age < timedelta(0):
            return state
        if age > STALE_REPORT_AGE:
            return {**state, "online": False}
        previous_contact = device_contact_timestamp(previous)
        previous_report = _parse_timestamp(previous.get("reportAt"))
        current_report = _parse_timestamp(state.get("reportAt"))
        newer_device_report = (
            previous_report is not None
            and current_report is not None
            and current_report > previous_report
        )
        if (
            previous.get("online") is False
            and previous_contact is not None
            and last_contact <= previous_contact
            and not newer_device_report
        ):
            return {**state, "online": False}
        return {**state, "online": True}

    def register_device_registry_listener(
        self, listener: DeviceRegistryListener
    ) -> Callable[[], None]:
        """Subscribe to device registry changes."""
        self._device_registry_listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._device_registry_listeners:
                self._device_registry_listeners.remove(listener)

        return unsubscribe

    def _create_background_task(
        self,
        coro: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task[None] | Any:
        """Create a background task without blocking HA startup."""
        if hasattr(self.hass, "async_create_background_task"):
            return self.hass.async_create_background_task(coro, name)
        return self.hass.async_create_task(coro)

    async def _async_setup(self) -> None:
        """Set up the coordinator: fetch devices and connect MQTT."""
        await self._async_load_contact_state()
        devices = await self._client.get_devices()
        self._devices = {d.device_id: d for d in devices}
        self._prune_contact_state(set(self._devices))
        self._remove_stale_registry_devices(set(self._devices))

        self._on_mqtt_disconnect()

        if self._discovery_task is None:
            self._discovery_task = self._create_background_task(
                self._async_device_discovery_loop(),
                "yolocal_device_discovery",
            )
        if self._state_refresh_task is None:
            self._state_refresh_task = self._create_background_task(
                self._async_state_refresh_loop(),
                "yolocal_state_refresh",
            )

    async def _fetch_all_states(self) -> None:
        """Fetch current state for all devices via HTTP API."""
        self._states = await self._async_update_data()

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Refresh device state from the hub and merge it into the cache."""
        results = await asyncio.gather(
            *(
                self._async_refresh_device_state(
                    device_id,
                    device,
                )
                for device_id, device in self._devices.items()
            )
        )

        refreshed_states = self._states.copy()
        for device_id, incoming_state, unreachable in results:
            current_state = refreshed_states.get(device_id, {})
            if unreachable:
                refreshed_states[device_id] = self._mark_unreachable(
                    device_id,
                    current_state,
                )
                if _went_offline(current_state, refreshed_states[device_id]):
                    self._log_device_unavailable(
                        device_id, "getState", "000201", current_state
                    )
            elif incoming_state is not None:
                if self._is_older_report(current_state, incoming_state):
                    # Keep the stale-response policy, but record any rejected
                    # offline flag because these clocks can have different origins.
                    _LOGGER.debug(
                        "Ignoring stale getState for %s (reported %s, have %s)",
                        device_id,
                        _diagnostic_timestamp(incoming_state.get("lastReportedAt")),
                        _diagnostic_timestamp(current_state.get("lastReportedAt")),
                    )
                    if (
                        incoming_state.get("online") is False
                        and current_state.get("online", True)
                    ):
                        self._log_ignored_offline_snapshot(
                            device_id, current_state, incoming_state
                        )
                    continue
                merged = self._merge_state_payload(
                    current_state,
                    incoming_state,
                )
                self._record_report_contact(device_id, merged)
                merged = self._apply_contact_record(device_id, merged)
                refreshed_states[device_id] = self._battery_liveness_state(
                    device_id, merged, current_state
                )
                if (
                    incoming_state.get("online") is False
                    and current_state.get("online", True)
                    and refreshed_states[device_id].get("online", True)
                ):
                    self._log_ignored_offline_snapshot(
                        device_id, current_state, incoming_state
                    )
                if _went_offline(current_state, refreshed_states[device_id]):
                    self._log_device_unavailable(
                        device_id,
                        "getState",
                        "success",
                        current_state,
                        incoming_state,
                    )

        self._states = refreshed_states
        return refreshed_states.copy()

    async def _async_refresh_device_state(
        self,
        device_id: str,
        device: Device,
    ) -> tuple[str, dict[str, Any] | None, bool]:
        """Fetch one device state and return the HTTP payload to merge."""
        try:
            state = await self._async_get_state_with_retry(device)
            if state is None:
                return device_id, None, True
        except Exception as err:
            cached = self._states.get(device_id, {})
            error_code = (
                err.code
                if isinstance(err, ApiError)
                and isinstance(err.code, str)
                and len(err.code) == 6
                and err.code.isascii()
                and err.code.isdigit()
                else "<none>"
            )
            errno = getattr(err, "errno", None)
            if not isinstance(errno, int):
                errno = getattr(getattr(err, "os_error", None), "errno", None)
            if not isinstance(errno, int):
                errno = None
            _LOGGER.warning(
                "YoLink getState request failed for %s (%s): "
                "error_type=%s error_code=%s errno=%s "
                "cached_online=%s cached_reportAt=%s "
                "cached_lastReportedAt=%s lastSeenAt=%s",
                device.name,
                device.model,
                type(err).__name__,
                error_code,
                errno,
                _diagnostic_online(cached.get("online")),
                _diagnostic_timestamp(cached.get("reportAt")),
                _diagnostic_timestamp(cached.get("lastReportedAt")),
                _diagnostic_timestamp(cached.get("lastSeenAt")),
            )
            return device_id, None, False

        return device_id, self._normalize_http_state(state, device), False

    def _is_older_report(
        self,
        current_state: dict[str, Any],
        incoming_state: dict[str, Any],
    ) -> bool:
        """Compare device report times, falling back when one is unavailable."""
        current_report = _parse_timestamp(current_state.get("reportAt"))
        incoming_report = _parse_timestamp(incoming_state.get("reportAt"))
        if current_report is not None and incoming_report is not None:
            if incoming_report != current_report:
                return incoming_report < current_report
            # Equal reports cannot replace a later MQTT-delivered state.
            current_envelope = _parse_timestamp(current_state.get("lastReportedAt"))
            return current_envelope is not None and current_envelope > current_report

        current = _parse_timestamp(current_state.get("lastReportedAt"))
        incoming = _parse_timestamp(incoming_state.get("lastReportedAt"))
        return current is not None and incoming is not None and incoming < current

    async def _async_get_state_with_retry(
        self,
        device: Device,
        attempts: int = 3,
    ) -> dict[str, Any] | None:
        """Fetch device state, tolerating transient hub-to-device failures."""
        for attempt in range(1, attempts + 1):
            try:
                return await self._client.get_state(device)
            except ApiError as err:
                if err.code != TRANSIENT_DEVICE_UNREACHABLE:
                    raise
                if attempt < attempts:
                    await asyncio.sleep(1)
                    continue
                _LOGGER.debug(
                    "Device state temporarily unavailable for %s: code=000201",
                    device.name,
                )
                return None
        return None

    async def _async_set_state_with_retry(
        self,
        device: Device,
        params: dict[str, Any],
        attempts: int = 3,
    ) -> dict[str, Any]:
        """Set device state, retrying transient hub-to-device failures."""
        for attempt in range(1, attempts + 1):
            try:
                return await self._client.set_state(device, params)
            except ApiError as err:
                if err.code != TRANSIENT_DEVICE_UNREACHABLE:
                    raise
                if attempt < attempts:
                    await asyncio.sleep(1)
                    continue
                self._update_device_state(
                    device.device_id,
                    {**self._states.get(device.device_id, {}), "online": False},
                )
                raise
        raise RuntimeError("Failed to set device state")

    async def _async_send_command_with_transport_recovery(
        self,
        device: Device,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a command, recovering one ambiguous transport failure."""
        try:
            return await self._async_set_state_with_retry(device, params)
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as err:
            if hasattr(self._client, "switch_host"):
                self._client.switch_host()
            await asyncio.sleep(SET_STATE_TRANSPORT_RETRY_DELAY)
            try:
                state = await self._async_get_state_with_retry(device, attempts=1)
                if state is not None:
                    normalized_state = self._normalize_http_state(state, device)
                    normalized_state = self._merge_command_http_state(
                        device.device_id, normalized_state
                    )
                    self._update_device_state(device.device_id, normalized_state)
                    if self._state_matches_command(normalized_state, params):
                        return {}
            except Exception:
                _LOGGER.debug(
                    "Failed to verify state after transport error for %s",
                    device.name,
                    exc_info=True,
                )

            try:
                return await self._async_set_state_with_retry(device, params, attempts=1)
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                raise err

    def _state_matches_command(
        self,
        state: dict[str, Any],
        params: dict[str, Any],
    ) -> bool:
        """Return True when current state already reflects the command params."""
        for key, expected_value in params.items():
            if not self._value_matches_command(
                self._state_command_value(state, key),
                expected_value,
            ):
                return False
        return True

    def _state_command_value(self, state: dict[str, Any], key: str) -> Any:
        """Return a comparable state value for a command key."""
        nested_state = state.get("state")
        if isinstance(nested_state, dict) and key in nested_state:
            return nested_state.get(key)
        return state.get(key)

    def _value_matches_command(self, current_value: Any, expected_value: Any) -> bool:
        """Compare a current state value with a requested command value."""
        if isinstance(expected_value, dict):
            if not isinstance(current_value, dict):
                return False
            return all(
                self._value_matches_command(current_value.get(key), value)
                for key, value in expected_value.items()
            )
        if expected_value == "close":
            return current_value == "close" or current_value == "closed"
        return current_value == expected_value

    async def async_shutdown(self) -> None:
        """Shut down the coordinator."""
        self._shutdown = True
        if self._contact_store_enabled:
            await self._contact_store.async_save(self._contact_store_data())
        if self._discovery_task:
            self._discovery_task.cancel()
            self._discovery_task = None
        if self._state_refresh_task:
            self._state_refresh_task.cancel()
            self._state_refresh_task = None
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        for mqtt_client in [
            *self._retired_mqtt_clients,
            *self._mqtt_clients.values(),
        ]:
            await mqtt_client.disconnect()
        self._retired_mqtt_clients.clear()
        self._mqtt_clients.clear()
        await self._session.close()

    async def _connect_mqtt(self) -> None:
        """Connect to all configured MQTT brokers."""
        connected_hosts: list[str] = []
        last_error: Exception | None = None
        for host in self._mqtt_hosts:
            try:
                await self._connect_mqtt_host(host)
            except Exception as err:
                last_error = err
                _LOGGER.warning(
                    "Failed to connect to YoLink MQTT broker at %s",
                    host,
                    exc_info=True,
                )
            else:
                connected_hosts.append(host)

        if not connected_hosts:
            if last_error is not None:
                raise last_error
            raise ConnectionError("No YoLink MQTT hosts configured")

    @property
    def _mqtt_hosts(self) -> tuple[str, ...]:
        """Return configured MQTT hosts."""
        hosts = getattr(self._client, "hosts", None)
        if hosts:
            return tuple(hosts)
        return (self._client.host,)

    async def _connect_mqtt_host(self, host: str) -> None:
        """Connect to one MQTT broker address."""
        if host in self._mqtt_clients:
            return
        if hasattr(self._token_manager, "get_token_for_host"):
            token = await self._token_manager.get_token_for_host(host)
        else:
            token = await self._token_manager.get_token()

        mqtt_client = YoLinkMQTTClient(
            host=host,
            net_id=self._net_id,
            client_id=self._token_manager.client_id,
            access_token=token,
            port=self._mqtt_port,
        )
        mqtt_client.subscribe(self._on_device_event)
        mqtt_client.on_disconnect(
            lambda host=host, client=mqtt_client: self._on_mqtt_disconnect(host, client)
        )

        try:
            await mqtt_client.connect()
            self._mqtt_clients[host] = mqtt_client
            _LOGGER.info("Connected to YoLink MQTT broker at %s", host)
        except Exception:
            try:
                await mqtt_client.disconnect()
            except Exception:
                _LOGGER.debug(
                    "Error while cleaning up failed MQTT client",
                    exc_info=True,
                )
            raise

    @callback
    def _on_device_event(self, event: DeviceEvent) -> None:
        """Handle a device event from MQTT."""
        device_id = event.device_id
        device = self._devices.get(device_id)
        if device is None:
            _LOGGER.debug("Ignoring event for unknown device: %s", device_id)
            return
        if self._is_duplicate_mqtt_event(event):
            _LOGGER.debug(
                "Ignoring duplicate MQTT event for %s: %s",
                device_id,
                event.event,
            )
            return
        if self._mqtt_event_is_delayed(event):
            _LOGGER.debug(
                "Ignoring delayed MQTT event for %s: %s",
                device_id,
                event.event,
            )
            return
        if self._is_replayed_contact_event(event):
            _LOGGER.debug(
                "Ignoring replayed MQTT event for %s: %s",
                device_id,
                event.event,
            )
            return

        if device.device_type == "THSensor" and event.event == "THSensor.DataRecord":
            self._on_thsensor_data_record(event)
            return

        event_data = event.data if isinstance(event.data, dict) else {}
        normalized_event = self._normalize_mqtt_event(device, event_data)
        if device.device_type == "THSensor":
            measured_at = (
                _parse_timestamp(normalized_event.get("reportAt"))
                or self._mqtt_event_timestamp(event)
                or dt_util.utcnow()
            )
            normalized_event = self._timestamp_thsensor_measurements(
                normalized_event, measured_at
            )
        cached_report = _parse_timestamp(self._states.get(device_id, {}).get("reportAt"))
        event_report = _parse_timestamp(normalized_event.get("reportAt"))
        if (
            cached_report is not None
            and event_report is not None
            and event_report < cached_report
        ):
            _LOGGER.debug("Ignoring older MQTT report for %s", device_id)
            return
        merged = self._merge_state_payload(
            self._states.get(device_id, {}),
            normalized_event,
        )
        prior = self._states.get(device_id, {})
        if self._record_report_contact(device_id, normalized_event):
            merged["online"] = True
        elif "reportAt" not in normalized_event and "online" not in normalized_event:
            # The report topic is a device-event stream. For payloads without
            # reportAt, a new non-replayed event is the best available contact
            # evidence, but its local receipt time is kept distinct from a
            # device-originated report timestamp.
            if self._record_contact(device_id, dt_util.utcnow(), "mqtt_event"):
                merged["online"] = True
        merged = self._apply_contact_record(device_id, merged)
        merged = self._battery_liveness_state(device_id, merged, prior)
        if _went_offline(prior, merged):
            self._log_device_unavailable(
                device_id, "mqtt", "event", prior, normalized_event
            )
        self._update_device_state(device_id, merged)

    def _on_thsensor_data_record(self, event: DeviceEvent) -> None:
        """Apply recorded measurements without confusing sample and receipt times."""
        records = event.data.get("records")
        if not isinstance(records, list):
            return

        valid_records: list[dict[str, Any]] = []
        latest: dict[str, tuple[datetime, int | float]] = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("time"), str):
                continue
            recorded_at = _parse_timestamp(record["time"])
            if recorded_at is None:
                continue
            valid_record: dict[str, Any] = {"time": record["time"]}
            for key in TH_MEASUREMENT_FIELDS:
                value = record.get(key)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                try:
                    if not isfinite(value):
                        continue
                except OverflowError:
                    continue
                valid_record[key] = value
                if key not in latest or recorded_at > latest[key][0]:
                    latest[key] = (recorded_at, value)
            if len(valid_record) > 1:
                valid_records.append(valid_record)

        if not latest:
            return
        previous = self._states.get(event.device_id, {})
        previous_nested = previous.get("state")
        updates = {}
        for key, (recorded_at, value) in latest.items():
            current_at = _thsensor_measurement_timestamp(previous, key)
            if (
                isinstance(previous_nested, dict)
                and previous_nested.get(key) is not None
                and current_at is not None
                and recorded_at <= current_at
            ):
                continue
            updates[key] = (recorded_at, value)
        incoming = {
            "state": {key: value for key, (_, value) in updates.items()},
            TH_MEASUREMENT_TIMES: {
                key: recorded_at.isoformat() for key, (recorded_at, _) in updates.items()
            },
        }
        merged = self._merge_state_payload(previous, incoming)
        merged["state"].pop("records", None)
        # A historical sample proves contact no earlier than its sample time.
        # Receiving an old batch now must not reset the twelve-hour timeout.
        contact_at = max(recorded_at for recorded_at, _ in latest.values())
        source = "mqtt_event"
        reported_at = _parse_timestamp(event.data.get("reportAt"))
        if reported_at is not None and reported_at >= contact_at:
            contact_at = reported_at
            source = "reportAt"
        if self._record_contact(event.device_id, contact_at, source):
            merged["online"] = True
        merged = self._apply_contact_record(event.device_id, merged)
        merged = self._battery_liveness_state(event.device_id, merged, previous)
        if _went_offline(previous, merged):
            self._log_device_unavailable(event.device_id, "mqtt", "event", previous)
        self._update_device_state(event.device_id, merged)
        self.hass.bus.async_fire(
            EVENT_DATA_RECORD,
            {"device_id": event.device_id, "records": valid_records},
        )

    def _mqtt_event_timestamp(self, event: DeviceEvent) -> datetime | None:
        """Read the transport clock for ordering, independently of radio contact."""
        try:
            return datetime.fromtimestamp(float(event.raw.get("time")) / 1000, UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    def _mqtt_event_is_delayed(self, event: DeviceEvent) -> bool:
        """Reject an old transport packet without treating its time as contact."""
        # A device report time is stronger than the hub transport clock. Hub/HA
        # clock skew must not discard that authoritative contact evidence.
        if _parse_timestamp(event.data.get("reportAt")) is not None:
            return False
        event_time = self._mqtt_event_timestamp(event)
        if event_time is None:
            return False
        age = dt_util.utcnow() - event_time
        return age > MQTT_EVENT_MAX_DELAY

    def _is_duplicate_mqtt_event(self, event: DeviceEvent) -> bool:
        """Return True when the same MQTT event was recently processed."""
        now = monotonic()
        cutoff = now - MQTT_DUPLICATE_WINDOW
        for key, seen_at in list(self._recent_mqtt_events.items()):
            if seen_at < cutoff:
                self._recent_mqtt_events.pop(key, None)

        event_key = self._mqtt_event_key(event)
        if event_key is None:
            return False
        if event_key in self._recent_mqtt_events:
            self._recent_mqtt_events[event_key] = now
            return True

        self._recent_mqtt_events[event_key] = now
        if len(self._recent_mqtt_events) > MQTT_DUPLICATE_CACHE_LIMIT:
            oldest_key = min(self._recent_mqtt_events, key=self._recent_mqtt_events.get)
            self._recent_mqtt_events.pop(oldest_key, None)
        return False

    def _mqtt_event_key(self, event: DeviceEvent) -> str | None:
        """Identify copies of one report, not separate reports of the same state."""
        identity = self._mqtt_contact_identity(event)
        if identity is None:
            # The envelope clock identifies a packet here, never radio contact.
            timestamp = self._mqtt_event_timestamp(event) or _parse_timestamp(
                event.data.get("reportAt")
            )
            if timestamp is None:
                return None
            identity = timestamp.isoformat()
        event_data = dict(event.data)
        event_data.pop("lastReportedAt", None)
        try:
            raw = json.dumps(
                {
                    "device_id": event.device_id,
                    "event": event.event,
                    "identity": identity,
                    "data": event_data,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        except TypeError:
            raw = repr((event.device_id, event.event, identity, event_data))
        return hashlib.sha256(raw.encode()).hexdigest()

    def _update_device_state(self, device_id: str, state: dict[str, Any]) -> None:
        """Store updated device state and notify listeners."""
        self._states[device_id] = state
        self.async_set_updated_data(self._states.copy())

    def _log_device_unavailable(
        self,
        device_id: str,
        source: str,
        result: str,
        cached: dict[str, Any],
        incoming: dict[str, Any] | None = None,
    ) -> None:
        """Log only status fields needed to distinguish read and event failures."""
        device = self._devices[device_id]
        incoming = incoming or {}
        report_at = incoming.get("reportAt") or incoming.get("lastReportedAt")
        _LOGGER.warning(
            "YoLink device unavailable: %s (%s); source=%s result=%s "
            "online=%s reportAt=%s cached_online=%s cached_reportAt=%s "
            "cached_lastReportedAt=%s lastSeenAt=%s",
            device.name,
            device.model,
            source,
            result,
            _diagnostic_online(incoming.get("online")),
            _diagnostic_timestamp(report_at),
            _diagnostic_online(cached.get("online")),
            _diagnostic_timestamp(cached.get("reportAt")),
            _diagnostic_timestamp(cached.get("lastReportedAt")),
            _diagnostic_timestamp(cached.get("lastSeenAt")),
        )

    def _log_ignored_offline_snapshot(
        self,
        device_id: str,
        cached: dict[str, Any],
        incoming: dict[str, Any],
    ) -> None:
        """Record a discarded offline flag once per distinct HTTP report."""
        report_at = _diagnostic_timestamp(
            incoming.get("reportAt") or incoming.get("lastReportedAt")
        )
        if self._logged_stale_offline_reports.get(device_id) == report_at:
            return
        self._logged_stale_offline_reports[device_id] = report_at
        device = self._devices[device_id]
        _LOGGER.warning(
            "YoLink ignored getState online=False for %s (%s): "
            "reportAt=%s cached_online=%s cached_reportAt=%s "
            "cached_lastReportedAt=%s lastSeenAt=%s",
            device.name,
            device.model,
            report_at,
            _diagnostic_online(cached.get("online")),
            _diagnostic_timestamp(cached.get("reportAt")),
            _diagnostic_timestamp(cached.get("lastReportedAt")),
            _diagnostic_timestamp(cached.get("lastSeenAt")),
        )

    def _normalize_http_state(
        self, state: dict[str, Any], device: Device | None = None
    ) -> dict[str, Any]:
        """Normalize an HTTP getState payload to the coordinator's canonical shape."""
        normalized_state = (
            self._normalize_mqtt_event(device, state)
            if device is not None and device.device_type == "WaterMeterController"
            else self._sanitize_state_payload(state)
        )
        normalized_state.setdefault("online", True)
        if normalized_state.get("reportAt") and "lastReportedAt" not in normalized_state:
            normalized_state["lastReportedAt"] = normalized_state["reportAt"]
        if device is not None and device.device_type == "THSensor":
            normalized_state = self._timestamp_thsensor_measurements(
                normalized_state, _parse_timestamp(normalized_state.get("reportAt"))
            )
        return normalized_state

    def _mark_unreachable(
        self,
        device_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Mark a device offline after three explicit getState 000201 errors."""
        if state.get("online") is False:
            return state
        _LOGGER.debug("Marking %s offline after repeated getState 000201", device_id)
        return {**state, "online": False}

    def _normalize_mqtt_event(
        self,
        device: Device,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Normalize a flat MQTT event into the nested HTTP-like state shape."""
        normalized: dict[str, Any] = {}

        for key in ("online", "reportAt", "lastReportedAt"):
            if key in event_data:
                normalized[key] = event_data[key]

        nested_state: dict[str, Any] = {}
        event_state = event_data.get("state")
        if isinstance(event_state, dict):
            nested_state.update(event_state)
        elif event_state is not None:
            nested_state["state"] = event_state

        for key, value in event_data.items():
            if key in {"state", "online", "reportAt", "lastReportedAt"}:
                continue
            if (
                device.device_type == "THSensor"
                and value is None
                and key in {"temperature", "humidity", "mode", "version"}
            ):
                continue
            nested_state[key] = value

        if nested_state:
            normalized["state"] = nested_state

        return self._sanitize_state_payload(normalized)

    def _timestamp_thsensor_measurements(
        self,
        state: dict[str, Any],
        measured_at: datetime | None,
    ) -> dict[str, Any]:
        """Version TH readings without changing the device's report/contact time."""
        nested = state.get("state")
        if measured_at is None or not isinstance(nested, dict):
            return state
        times = {
            key: measured_at.isoformat()
            for key in TH_MEASUREMENT_FIELDS
            if nested.get(key) is not None
        }
        return {**state, TH_MEASUREMENT_TIMES: times} if times else state

    def _merge_thsensor_measurements(
        self,
        existing: dict[str, Any],
        incoming: dict[str, Any],
        merged: dict[str, Any],
    ) -> None:
        """Keep each TH reading's newest timestamp across MQTT and HTTP merges."""
        existing_times = existing.get(TH_MEASUREMENT_TIMES, {})
        incoming_times = incoming.get(TH_MEASUREMENT_TIMES, {})
        if not existing_times and not incoming_times:
            return
        existing_nested = existing.get("state")
        incoming_nested = incoming.get("state")
        if not isinstance(incoming_nested, dict):
            return
        if not isinstance(existing_nested, dict):
            existing_nested = {}
        times = dict(existing_times)
        for key in TH_MEASUREMENT_FIELDS:
            if key not in incoming_nested:
                continue
            current_at = _thsensor_measurement_timestamp(existing, key)
            incoming_at = _parse_timestamp(incoming_times.get(key))
            if (
                key in existing_nested
                and current_at is not None
                and (incoming_at is None or incoming_at < current_at)
            ):
                merged["state"][key] = existing_nested[key]
            elif incoming_at is not None:
                times[key] = incoming_at.isoformat()
        merged[TH_MEASUREMENT_TIMES] = times

    def _merge_state_payload(
        self,
        existing_state: dict[str, Any],
        incoming_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge canonical state payloads while preserving nested HTTP shape."""
        merged_state = self._sanitize_state_payload({**existing_state, **incoming_state})
        merged_nested_state = self._merge_nested_state(
            existing_state.get("state"),
            incoming_state.get("state"),
        )
        if isinstance(merged_nested_state, dict):
            merged_state["state"] = self._sanitize_nested_state(merged_nested_state)
        elif merged_nested_state is not None:
            merged_state["state"] = merged_nested_state
        self._merge_thsensor_measurements(existing_state, incoming_state, merged_state)
        self._apply_event_availability(existing_state, incoming_state, merged_state)
        return merged_state

    def _merge_device_state(
        self,
        device_id: str,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge a raw device event into the cached state shape."""
        device = self._devices[device_id]
        normalized_event = self._normalize_mqtt_event(device, event_data)
        return self._merge_state_payload(
            self._states.get(device_id, {}),
            normalized_event,
        )

    def _merge_thsensor_state(
        self,
        device_id: str,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge a raw THSensor event into the cached state shape."""
        return self._merge_device_state(device_id, event_data)

    def _apply_event_availability(
        self,
        existing_state: dict[str, Any],
        event_data: dict[str, Any],
        merged_state: dict[str, Any],
    ) -> None:
        """Mark a device online again when a fresh report arrives."""
        if "online" in event_data:
            return

        reported_at = event_data.get("lastReportedAt")
        if reported_at and reported_at != existing_state.get("lastReportedAt"):
            merged_state["online"] = True

    def _sanitize_state_payload(self, state: dict[str, Any]) -> dict[str, Any]:
        """Remove inaccurate fields from a state payload."""
        sanitized = dict(state)
        sanitized.pop("batteryType", None)
        nested_state = sanitized.get("state")
        if isinstance(nested_state, dict):
            sanitized["state"] = self._sanitize_nested_state(nested_state)
        return sanitized

    def _sanitize_nested_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Remove inaccurate fields from a nested state object."""
        sanitized = dict(state)
        sanitized.pop("batteryType", None)
        return sanitized

    def _merge_nested_state(
        self,
        existing_state: Any,
        event_state: Any,
    ) -> Any | None:
        """Merge the payload's nested `state` field while preserving prior details."""
        if event_state is None:
            return None
        if isinstance(event_state, dict):
            if isinstance(existing_state, dict):
                return {**existing_state, **event_state}
            return event_state
        if isinstance(existing_state, dict):
            return {**existing_state, "state": event_state}
        return event_state

    @callback
    def _on_mqtt_disconnect(
        self,
        host: str | None = None,
        client: YoLinkMQTTClient | None = None,
    ) -> None:
        """Reconnect MQTT when the broker disconnects."""
        if self._shutdown:
            return
        if host is not None:
            current_client = self._mqtt_clients.get(host)
            if current_client is None or (
                client is not None and current_client is not client
            ):
                return
            self._mqtt_clients.pop(host)
            self._retired_mqtt_clients.append(current_client)
        if self._reconnect_task and not self._reconnect_task.done():
            self._mqtt_reconnect_wakeup.set()
            return
        self._reconnect_task = self.hass.async_create_task(self._async_reconnect_mqtt())

    async def _async_reconnect_mqtt(self) -> None:
        """Reconnect missing MQTT clients with backoff."""
        backoff_seconds = 5
        while not self._shutdown:
            while self._retired_mqtt_clients:
                client = self._retired_mqtt_clients[0]
                try:
                    await client.disconnect()
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to close disconnected MQTT client: %s",
                        type(err).__name__,
                    )
                self._retired_mqtt_clients.pop(0)

            missing_hosts = [
                host for host in self._mqtt_hosts if host not in self._mqtt_clients
            ]
            if not missing_hosts:
                return

            for host in missing_hosts:
                try:
                    await self._connect_mqtt_host(host)
                except Exception:
                    _LOGGER.warning(
                        "Failed to reconnect YoLink MQTT broker at %s",
                        host,
                        exc_info=True,
                    )

            if (
                all(host in self._mqtt_clients for host in self._mqtt_hosts)
                and not self._retired_mqtt_clients
            ):
                return

            try:
                await asyncio.wait_for(
                    self._mqtt_reconnect_wakeup.wait(), timeout=backoff_seconds
                )
            except TimeoutError:
                backoff_seconds = min(backoff_seconds * 2, 300)
            else:
                backoff_seconds = 5
            finally:
                self._mqtt_reconnect_wakeup.clear()

    async def _async_device_discovery_loop(self) -> None:
        """Periodically check for device additions/removals."""
        interval = DEVICE_DISCOVERY_INTERVAL.total_seconds()
        while not self._shutdown:
            await asyncio.sleep(interval)
            await self._async_refresh_devices()

    async def _async_state_refresh_loop(self) -> None:
        """Periodically poll getState independent of HA coordinator scheduling."""
        interval = UPDATE_INTERVAL.total_seconds()
        while not self._shutdown:
            await asyncio.sleep(interval)
            try:
                refreshed_states = await self._async_update_data()
            except Exception:
                _LOGGER.warning("Failed to refresh YoLink device states", exc_info=True)
                continue
            self.async_set_updated_data(refreshed_states)

    async def _async_refresh_devices(self) -> bool:
        """Refresh the device registry and notify listeners if membership changed."""
        try:
            devices = await self._client.get_devices()
        except Exception as err:
            _LOGGER.warning("Failed to refresh device list", exc_info=True)
            self._mark_hub_api_failure(err)
            return False

        self._mark_hub_api_available()
        new_devices = {device.device_id: device for device in devices}
        if set(new_devices) == set(self._devices):
            self._devices = new_devices
            return False

        existing_device_ids = set(self._devices)
        added_ids = sorted(set(new_devices) - existing_device_ids)
        removed_ids = sorted(existing_device_ids - set(new_devices))
        added_devices = [new_devices[device_id] for device_id in added_ids]
        removed_devices = [self._devices[device_id] for device_id in removed_ids]
        _LOGGER.info(
            "Device registry changed; added=%s removed=%s.",
            added_ids,
            removed_ids,
        )
        self._devices = new_devices

        for device_id in removed_ids:
            self._states.pop(device_id, None)
            self._logged_stale_offline_reports.pop(device_id, None)
            self._remove_device_from_registry(device_id)

        for device in added_devices:
            try:
                state = await self._client.get_state(device)
                self._states[device.device_id] = self._normalize_http_state(state, device)
            except Exception:
                _LOGGER.warning("Failed to get initial state for %s", device.name)
                self._states[device.device_id] = {}

        for listener in list(self._device_registry_listeners):
            try:
                listener(added_devices, removed_devices)
            except Exception:
                _LOGGER.exception("Device registry listener failed")

        self.async_set_updated_data(self._states.copy())
        return True

    def _mark_hub_api_available(self) -> None:
        """Clear coordinator update error after a successful hub API call."""
        self._hub_health_failures = 0
        if not self.last_update_success:
            self.async_set_updated_data(self._states.copy())

    def _mark_hub_api_failure(self, err: Exception) -> None:
        """Mark coordinator unhealthy only after repeated hub API failures."""
        self._hub_health_failures += 1
        if self._hub_health_failures >= HUB_HEALTH_FAILURE_THRESHOLD:
            self.async_set_update_error(err)

    def _remove_device_from_registry(self, device_id: str) -> None:
        """Remove a deleted device from the HA device registry."""
        self._remove_entity_registry_entries(device_id)
        registry = dr.async_get(self.hass)
        device_entry = registry.async_get_device(identifiers={(DOMAIN, device_id)})
        if device_entry is not None:
            registry.async_remove_device(device_entry.id)

    def _remove_stale_registry_devices(self, active_device_ids: set[str]) -> None:
        """Remove registry devices that no longer exist on the hub."""
        registry = dr.async_get(self.hass)
        if hasattr(dr, "async_entries_for_config_entry"):
            device_entries = dr.async_entries_for_config_entry(
                registry,
                self._config_entry_id,
            )
        else:
            device_entries = list(getattr(registry, "devices", {}).values())

        for entry in list(device_entries):
            normalized_identifiers = set(getattr(entry, "identifiers", set()))
            if len(normalized_identifiers) != 1:
                continue
            domain, device_id = next(iter(normalized_identifiers))
            if domain != DOMAIN or device_id in active_device_ids:
                continue
            self._remove_entity_registry_entries(device_id)
            registry.async_remove_device(entry.id)

    def _remove_entity_registry_entries(self, device_id: str) -> None:
        """Remove orphaned entity-registry entries for a missing device."""
        registry = er.async_get(self.hass)
        if hasattr(er, "async_entries_for_config_entry"):
            entity_entries = er.async_entries_for_config_entry(
                registry,
                self._config_entry_id,
            )
        else:
            entity_entries = list(getattr(registry, "entities", {}).values())

        for entry in list(entity_entries):
            entity_id = getattr(entry, "entity_id", None)
            unique_id = getattr(entry, "unique_id", None)
            if unique_id == device_id or (
                isinstance(unique_id, str) and unique_id.startswith(f"{device_id}_")
            ):
                if entity_id is not None:
                    registry.async_remove(entity_id)

    def get_state(self, device_id: str) -> dict[str, Any]:
        """Get the current state for a device."""
        return self._states.get(device_id, {})

    async def async_send_command(
        self, device_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a command to a device and refresh its authoritative state."""
        device = self._devices.get(device_id)
        if not device:
            raise ValueError(f"Unknown device: {device_id}")
        result = await self._async_send_command_with_transport_recovery(device, params)
        try:
            state = await self._async_get_state_with_retry(device)
            if state is not None:
                normalized_state = self._normalize_http_state(state, device)
                self._update_device_state(
                    device_id,
                    self._merge_command_http_state(device_id, normalized_state),
                )
        except Exception:
            _LOGGER.warning(
                "Failed to refresh state after command for %s",
                device.name,
                exc_info=True,
            )
        return result


async def create_coordinator(
    hass: HomeAssistant,
    host: str,
    client_id: str,
    client_secret: str,
    config_entry_id: str,
    net_id: str,
    http_port: int = 1080,
    mqtt_port: int = 18080,
    hosts: list[str] | None = None,
) -> YoLocalCoordinator:
    """Create a coordinator.

    Home Assistant's first coordinator refresh performs setup and initial polling.

    Raises:
        AuthenticationError: If credentials are invalid.
        Exception: If setup fails.
    """
    session = aiohttp.ClientSession()
    try:
        token_manager = TokenManager(
            host,
            client_id,
            client_secret,
            session,
            http_port,
            hosts=hosts,
        )
        await token_manager.get_token()

        client = YoLinkClient(host, token_manager, session, http_port, hosts=hosts)

        return YoLocalCoordinator(
            hass, client, token_manager, session, config_entry_id, net_id, mqtt_port
        )
    except Exception:
        await session.close()
        raise
