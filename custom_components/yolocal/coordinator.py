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
        """Poll device states via HTTP as a fallback.

        This runs periodically (every 5 minutes) to ensure state stays
        current even if MQTT events are missed or the connection drops.
        """
        await self._fetch_all_states()
        return self._states.copy()

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
        """Handle a device event from MQTT.

        Merges incoming event data with the existing device state so that
        partial events (e.g. connectivity-only updates) don't wipe out
        previously known sensor readings like temperature and humidity.
        """
        device_id = event.device_id
        if device_id not in self._devices:
            _LOGGER.debug("Ignoring event for unknown device: %s", device_id)
            return

        existing = self._states.get(device_id, {})
        self._states[device_id] = self._merge_event_data(existing, event.data)
        self.async_set_updated_data(self._states.copy())

    def _merge_event_data(
        self,
        existing_state: dict[str, Any],
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge an MQTT event into cached state and restore online status on reports."""
        merged_state = {**existing_state, **event_data}
        reported_at = event_data.get("lastReportedAt")
        if (
            "online" not in event_data
            and reported_at
            and reported_at != existing_state.get("lastReportedAt")
        ):
            merged_state["online"] = True
        return merged_state

    def _normalize_http_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Normalize an HTTP getState payload to the coordinator's internal shape."""
        normalized_state = dict(state)
        if (
            normalized_state.get("reportAt")
            and "lastReportedAt" not in normalized_state
        ):
            normalized_state["lastReportedAt"] = normalized_state["reportAt"]
        return normalized_state

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
