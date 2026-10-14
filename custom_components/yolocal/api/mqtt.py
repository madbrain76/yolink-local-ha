"""MQTT client for YoLink Local Hub real-time events."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)


def _normalize_mqtt_time(timestamp_ms: Any) -> str | None:
    """Convert hub MQTT `time` (epoch milliseconds) to ISO-8601 UTC."""
    if timestamp_ms is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp_ms) / 1000, UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


@dataclass
class DeviceEvent:
    """Represents an event received from a device."""

    device_id: str
    event: str
    data: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DeviceEvent:
        """Create a DeviceEvent from MQTT payload."""
        raw_data = payload.get("data")
        if raw_data is None and isinstance(payload.get("params"), dict):
            params = payload["params"]
            raw_data = params.get("data", params)
        if raw_data is None and "state" in payload:
            raw_data = {"state": payload.get("state")}

        if isinstance(raw_data, dict):
            event_data = dict(raw_data)
        elif raw_data is not None:
            event_data = {"state": raw_data}
        else:
            event_data = {}

        if mqtt_time := _normalize_mqtt_time(payload.get("time")):
            event_data["lastReportedAt"] = mqtt_time
        if "online" not in event_data and "online" in payload:
            event_data["online"] = payload.get("online")

        return cls(
            device_id=payload.get("deviceId", ""),
            event=payload.get("event", ""),
            data=event_data,
            raw=payload,
        )


EventCallback = Callable[[DeviceEvent], None]
DisconnectCallback = Callable[[], None]

# CONNACK reason codes that mean the hub rejected our credentials (MQTT 3.1.1
# rc 4/5 and their MQTT 5 equivalents).
_AUTH_FAILURE_CODES = {4, 5, 134, 135}


class MQTTAuthError(ConnectionError):
    """The hub refused the MQTT login (stale or revoked access token)."""


class YoLinkMQTTClient:
    """MQTT client for receiving real-time device events."""

    def __init__(
        self,
        host: str,
        net_id: str,
        client_id: str,
        access_token: str,
        port: int = 18080,
    ) -> None:
        """Initialize the MQTT client."""
        self._host = host
        self._port = port
        self._net_id = net_id
        self._client_id = client_id
        self._access_token = access_token
        self._client: mqtt.Client | None = None
        self._callbacks: list[EventCallback] = []
        self._disconnect_callbacks: list[DisconnectCallback] = []
        self._connected = asyncio.Event()
        self._connect_failed = asyncio.Event()
        self._connect_error: ConnectionError | None = None
        self._closing = False
        # Set and read only on paho's network thread, so a disconnect that
        # races the CONNACK can't be mistaken for a refused connect.
        self._session_up = False
        self._disconnect_notified = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_connected(self) -> bool:
        """Return True while the broker session is up."""
        return self._connected.is_set()

    @property
    def topic(self) -> str:
        """Return the subscription topic."""
        return f"ylsubnet/{self._net_id}/+/report"

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        """Subscribe to device events. Returns unsubscribe function."""
        self._callbacks.append(callback)
        return lambda: self._callbacks.remove(callback)

    def on_disconnect(self, callback: DisconnectCallback) -> Callable[[], None]:
        """Subscribe to disconnect notifications. Returns unsubscribe function."""
        self._disconnect_callbacks.append(callback)
        return lambda: self._disconnect_callbacks.remove(callback)

    async def connect(self) -> None:
        """Connect to the MQTT broker."""
        self._loop = asyncio.get_running_loop()
        # paho must not reconnect on its own: it would retry forever with the
        # token it was created with, which the hub stops accepting once it
        # rotates tokens. The coordinator owns reconnection and builds a new
        # client with a fresh token instead.
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            reconnect_on_failure=False,
        )
        self._client.username_pw_set(self._client_id, self._access_token)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

        # Wait for the CONNACK (either way) with a timeout
        connected = asyncio.ensure_future(self._connected.wait())
        failed = asyncio.ensure_future(self._connect_failed.wait())
        try:
            await asyncio.wait(
                {connected, failed},
                timeout=10.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            connected.cancel()
            failed.cancel()
        if self._connected.is_set():
            return
        if self._connect_error is not None:
            raise self._connect_error
        raise ConnectionError("Timed out connecting to MQTT broker")

    async def disconnect(self) -> None:
        """Disconnect from the MQTT broker."""
        self._closing = True
        if self._client:
            client, self._client = self._client, None
            client.disconnect()
            # loop_stop() joins paho's thread, which can sit in a socket
            # connect or a 1 s reconnect sleep; keep that off the event loop.
            await asyncio.get_running_loop().run_in_executor(None, client.loop_stop)
        self._connected.clear()

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        rc: Any,
        properties: Any = None,
    ) -> None:
        """Handle connection established."""
        if rc == 0 or str(rc) == "Success":
            _LOGGER.info("Connected to YoLink MQTT broker")
            self._session_up = True
            client.subscribe(self.topic)
            if self._loop:
                self._loop.call_soon_threadsafe(self._connected.set)
        else:
            _LOGGER.error("MQTT connection to %s failed: %s", self._host, rc)
            value = getattr(rc, "value", rc)
            if value in _AUTH_FAILURE_CODES:
                self._connect_error = MQTTAuthError(
                    f"MQTT login to {self._host} refused: {rc}"
                )
            else:
                self._connect_error = ConnectionError(
                    f"MQTT connection to {self._host} refused: {rc}"
                )
            if self._loop:
                self._loop.call_soon_threadsafe(self._connect_failed.set)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any,
        rc: Any,
        properties: Any = None,
    ) -> None:
        """Handle disconnection."""
        was_connected = self._session_up
        self._session_up = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._connected.clear)
        else:
            self._connected.clear()
        if self._closing or self._disconnect_notified:
            return
        if not was_connected:
            # A refused or timed-out connect attempt; connect() reports it.
            return
        _LOGGER.warning("Disconnected from MQTT broker at %s: %s", self._host, rc)
        # Each client reports its loss once. The coordinator replaces it.
        self._disconnect_notified = True
        for callback in self._disconnect_callbacks:
            try:
                if self._loop:
                    self._loop.call_soon_threadsafe(callback)
                else:
                    callback()
            except Exception:
                _LOGGER.exception("Error in disconnect callback")

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        msg: mqtt.MQTTMessage,
    ) -> None:
        """Handle incoming message."""
        try:
            payload = json.loads(msg.payload.decode())
            event = DeviceEvent.from_payload(payload)
            for callback in self._callbacks:
                try:
                    if self._loop:
                        self._loop.call_soon_threadsafe(callback, event)
                    else:
                        callback(event)
                except Exception:
                    _LOGGER.exception("Error in event callback")
        except json.JSONDecodeError:
            _LOGGER.error("Failed to decode MQTT message: %s", msg.payload)
        except Exception:
            _LOGGER.exception("Error processing MQTT message")
