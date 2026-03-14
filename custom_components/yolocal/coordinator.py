"""Data coordinator for YoLink Local integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    Device,
    DeviceEvent,
    TokenManager,
    YoLinkClient,
    YoLinkMQTTClient,
)
from .api.auth import AuthenticationError

_LOGGER = logging.getLogger(__name__)

# Polling interval as fallback when MQTT events are missed
UPDATE_INTERVAL = timedelta(minutes=5)


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
        net_id: str,
        mqtt_port: int = 18080,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="YoLink Local",
            update_interval=UPDATE_INTERVAL,
        )
        self._client = client
        self._token_manager = token_manager
        self._session = session
        self._net_id = net_id
        self._mqtt_port = mqtt_port
        self._mqtt_client: YoLinkMQTTClient | None = None
        self._devices: dict[str, Device] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._reconnect_task: asyncio.Task[None] | None = None
        self._shutdown = False

    @property
    def devices(self) -> dict[str, Device]:
        """Return the device registry."""
        return self._devices

    async def _async_setup(self) -> None:
        """Set up the coordinator: fetch devices and connect MQTT."""
        devices = await self._client.get_devices()
        self._devices = {d.device_id: d for d in devices}

        await self._fetch_all_states()
        try:
            await self._connect_mqtt()
        except Exception:
            _LOGGER.warning(
                "Initial MQTT connection failed; reconnecting in background",
                exc_info=True,
            )
            self._on_mqtt_disconnect()

        self.async_set_updated_data(self._states.copy())

    async def _fetch_all_states(self) -> None:
        """Fetch current state for all devices via HTTP API."""
        for device in self._devices.values():
            try:
                state = await self._client.get_state(device)
                self._states[device.device_id] = self._normalize_http_state(state)
            except Exception:
                _LOGGER.warning("Failed to get state for %s", device.name)
                self._states.setdefault(device.device_id, {})

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Refresh device state from the hub and merge it into the cache."""
        refreshed_states = self._states.copy()

        for device_id, device in self._devices.items():
            try:
                state = await self._client.get_state(device)
            except Exception:
                _LOGGER.warning("Failed to refresh state for %s", device.name)
                continue

            normalized_state = self._normalize_http_state(state)
            refreshed_states[device_id] = self._merge_state_payload(
                refreshed_states.get(device_id, {}),
                normalized_state,
            )

        self._states = refreshed_states
        return refreshed_states.copy()

    async def async_shutdown(self) -> None:
        """Shut down the coordinator."""
        self._shutdown = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        if self._mqtt_client:
            await self._mqtt_client.disconnect()
            self._mqtt_client = None
        await self._session.close()

    async def _connect_mqtt(self) -> None:
        """Connect to MQTT broker."""
        token = await self._token_manager.get_token()
        host = self._client.host

        mqtt_client = YoLinkMQTTClient(
            host=host,
            net_id=self._net_id,
            client_id=self._token_manager.client_id,
            access_token=token,
            port=self._mqtt_port,
        )
        mqtt_client.subscribe(self._on_device_event)
        mqtt_client.on_disconnect(self._on_mqtt_disconnect)

        try:
            await mqtt_client.connect()
            self._mqtt_client = mqtt_client
            _LOGGER.info("Connected to YoLink MQTT broker")
        except Exception:
            try:
                await mqtt_client.disconnect()
            except Exception:
                _LOGGER.debug(
                    "Error while cleaning up failed MQTT client",
                    exc_info=True,
                )
            _LOGGER.exception("Failed to connect to MQTT broker")
            raise

    @callback
    def _on_device_event(self, event: DeviceEvent) -> None:
        """Handle a device event from MQTT."""
        device_id = event.device_id
        device = self._devices.get(device_id)
        if device is None:
            _LOGGER.debug("Ignoring event for unknown device: %s", device_id)
            return

        event_data = event.data if isinstance(event.data, dict) else {}
        normalized_event = self._normalize_mqtt_event(device, event_data)
        self._update_device_state(
            device_id,
            self._merge_state_payload(self._states.get(device_id, {}), normalized_event),
        )

    def _update_device_state(self, device_id: str, state: dict[str, Any]) -> None:
        """Store updated device state and notify listeners."""
        self._states[device_id] = state
        self.async_set_updated_data(self._states.copy())

    def _normalize_http_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Normalize an HTTP getState payload to the coordinator's canonical shape."""
        normalized_state = self._sanitize_state_payload(state)
        if normalized_state.get("reportAt") and "lastReportedAt" not in normalized_state:
            normalized_state["lastReportedAt"] = normalized_state["reportAt"]
        return normalized_state

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
        self._apply_event_availability(existing_state, incoming_state, merged_state)
        return merged_state

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
    def _on_mqtt_disconnect(self) -> None:
        """Reconnect MQTT when the broker disconnects."""
        if self._shutdown:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = self.hass.async_create_task(self._async_reconnect_mqtt())

    async def _async_reconnect_mqtt(self) -> None:
        """Reconnect the MQTT client with backoff."""
        backoff_seconds = 5
        while not self._shutdown:
            try:
                if self._mqtt_client:
                    await self._mqtt_client.disconnect()
                    self._mqtt_client = None
                await self._connect_mqtt()
                if self._mqtt_client is not None:
                    return
            except Exception:
                _LOGGER.exception("Failed to reconnect MQTT broker")

            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 300)

    def get_state(self, device_id: str) -> dict[str, Any]:
        """Get the current state for a device."""
        return self._states.get(device_id, {})

    async def async_send_command(
        self, device_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a command to a device."""
        device = self._devices.get(device_id)
        if not device:
            raise ValueError(f"Unknown device: {device_id}")
        return await self._client.set_state(device, params)


async def create_coordinator(
    hass: HomeAssistant,
    host: str,
    client_id: str,
    client_secret: str,
    net_id: str,
    http_port: int = 1080,
    mqtt_port: int = 18080,
) -> YoLocalCoordinator:
    """Create and initialize a coordinator.

    Returns a fully-initialized, connected coordinator ready for use.

    Raises:
        AuthenticationError: If credentials are invalid.
        Exception: If setup fails.
    """
    session = aiohttp.ClientSession()
    try:
        token_manager = TokenManager(host, client_id, client_secret, session, http_port)
        await token_manager.get_token()

        client = YoLinkClient(host, token_manager, session, http_port)

        coordinator = YoLocalCoordinator(
            hass, client, token_manager, session, net_id, mqtt_port
        )
        await coordinator._async_setup()

        return coordinator
    except Exception:
        await session.close()
        raise
