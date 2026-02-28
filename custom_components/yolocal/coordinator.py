"""Data coordinator for YoLink Local integration."""

from __future__ import annotations

import asyncio
import logging
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


class YoLocalCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator for YoLink Local devices.

    Manages MQTT subscription for real-time updates and provides
    device state to entities.
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
        )
        self._client = client
        self._token_manager = token_manager
        self._session = session
        self._net_id = net_id
        self._mqtt_port = mqtt_port
        self._mqtt_client: YoLinkMQTTClient | None = None
        self._devices: dict[str, Device] = {}
        self._states: dict[str, dict[str, Any]] = {}

    @property
    def devices(self) -> dict[str, Device]:
        """Return the device registry."""
        return self._devices

    async def _async_setup(self) -> None:
        """Set up the coordinator: fetch devices and connect MQTT."""
        devices = await self._client.get_devices()
        self._devices = {d.device_id: d for d in devices}

        for device in devices:
            try:
                state = await self._client.get_state(device)
                self._states[device.device_id] = state
            except Exception:
                _LOGGER.warning("Failed to get initial state for %s", device.name)
                self._states[device.device_id] = {}

        await self._connect_mqtt()

    async def async_shutdown(self) -> None:
        """Shut down the coordinator."""
        if self._mqtt_client:
            await self._mqtt_client.disconnect()
            self._mqtt_client = None
        await self._session.close()

    async def _connect_mqtt(self) -> None:
        """Connect to MQTT broker."""
        token = await self._token_manager.get_token()
        host = self._client.host

        self._mqtt_client = YoLinkMQTTClient(
            host=host,
            net_id=self._net_id,
            client_id=self._token_manager.client_id,
            access_token=token,
            port=self._mqtt_port,
        )
        self._mqtt_client.subscribe(self._on_device_event)

        try:
            await self._mqtt_client.connect()
            _LOGGER.info("Connected to YoLink MQTT broker")
        except Exception:
            _LOGGER.exception("Failed to connect to MQTT broker")

    @callback
    def _on_device_event(self, event: DeviceEvent) -> None:
        """Handle a device event from MQTT."""
        device_id = event.device_id
        device = self._devices.get(device_id)
        if device is None:
            _LOGGER.debug("Ignoring event for unknown device: %s", device_id)
            return

        # TH/Temp: merge MQTT report into existing API state so partial reports do
        # not drop diagnostic fields (e.g. firmware/version).
        if device.device_type == "THSensor":
            existing_state = self._states.get(device_id, {})
            event_data = event.data if isinstance(event.data, dict) else {}

            new_state = {**existing_state, **event_data}

            merged_state_obj: dict[str, Any] = {}
            if isinstance(existing_state.get("state"), dict):
                merged_state_obj.update(existing_state["state"])

            event_state_obj = event_data.get("state")
            if isinstance(event_state_obj, dict):
                merged_state_obj.update(event_state_obj)
            elif event_state_obj is not None:
                merged_state_obj["state"] = event_state_obj

            # TH reports often publish flat keys at top-level (temperature, humidity, mode...)
            # Fold them into nested state while keeping top-level copies for fallback readers.
            for key, value in event_data.items():
                if key in {"state", "online", "reportAt"}:
                    continue
                if value is None and key in {"temperature", "humidity", "mode", "version"}:
                    continue
                merged_state_obj[key] = value

            if merged_state_obj:
                new_state["state"] = merged_state_obj

            self._states[device_id] = new_state
            self.async_set_updated_data(self._states.copy())
            return

        # Deep merge event data with existing state to preserve diagnostic info
        existing_state = self._states.get(device_id, {})
        new_state = {**existing_state, **event.data}

        # Merge nested "state" object if present in both
        # Handle both formats: state as dict {"state": {...}} or flat {"state": "alert"}
        if "state" in existing_state and "state" in event.data:
            existing_state_obj = existing_state["state"]
            event_state_obj = event.data["state"]

            # Both are dicts - merge them
            if isinstance(existing_state_obj, dict) and isinstance(event_state_obj, dict):
                new_state["state"] = {**existing_state_obj, **event_state_obj}
            # Event has dict, existing has string - use event's dict
            elif isinstance(event_state_obj, dict):
                new_state["state"] = event_state_obj
            # Event has string, existing has dict - update the nested "state" field
            elif isinstance(existing_state_obj, dict):
                new_state["state"] = {**existing_state_obj, "state": event_state_obj}
            # Both are strings - just use the event's value (already in new_state)

        self._states[device_id] = new_state
        self.async_set_updated_data(self._states.copy())

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
