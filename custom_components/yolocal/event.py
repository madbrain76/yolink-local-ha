"""Event platform for YoLink Local integration (lock doorbell and activity)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import LOCK_V2_DEVICE_TYPE
from .coordinator import YoLocalCoordinator
from .entity import YoLocalEntity, async_setup_device_entities

LOCK_EVENT_DEVICE_TYPES = frozenset({LOCK_V2_DEVICE_TYPE})
DOORBELL_ALERTS = frozenset({"bell", "doorbell"})
ALERT_TO_ACTIVITY = {
    "lock": "locked",
    "locked": "locked",
    "unlock": "unlocked",
    "unlocked": "unlocked",
    "lockfailed": "lock_failed",
    "unlockfailed": "unlock_failed",
}
ACTIVITY_EVENT_TYPES = ["locked", "unlocked", "lock_failed", "unlock_failed", "alert"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink event entities from a config entry."""
    await async_setup_device_entities(
        hass, entry, async_add_entities, build_event_entities
    )


def build_event_entities(
    coordinator: YoLocalCoordinator, device
) -> list[EventEntity]:
    """Build doorbell and activity events for locks."""
    if device.device_type not in LOCK_EVENT_DEVICE_TYPES:
        return []
    return [
        YoLocalLockDoorbellEvent(coordinator, device),
        YoLocalLockActivityEvent(coordinator, device),
    ]


def parse_lock_event(
    event_name: str, data: dict[str, Any]
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Return (alert type, reported lock state, extra attributes) for an MQTT event.

    LockV2 echoes its last alert in getState and other reports. Only `.Alert`
    events describe a new operation, so the alert's type, source and user slot
    are read from those alone; otherwise a report after an app unlock would be
    attributed to an earlier failed fingerprint attempt.
    """
    alert_type = None
    source = None
    user = None
    credential = None
    if event_name.endswith(".Alert"):
        alert = data.get("alert")
        if isinstance(alert, dict):
            alert_type = alert.get("type")
            source = alert.get("source")
            user = alert.get("userId")
            credential = alert.get("credentialId")
        else:
            alert_type = data.get("alertType")
        source = source or data.get("source")
        if user is None:
            user = data.get("user")

    state = data.get("state")
    lock_state = state.get("lock") if isinstance(state, dict) else state
    if lock_state not in ("locked", "unlocked"):
        lock_state = None

    attributes: dict[str, Any] = {}
    if source:
        attributes["source"] = source
    # Slot 0 is a real value (the hub reports it for mechanical and local
    # keypad operations), so test for None rather than truthiness.
    if user is not None:
        attributes["user"] = user
    if credential is not None:
        attributes["credential"] = credential
    return alert_type, lock_state, attributes


def _operator(source: Any, user: Any) -> str:
    return f"{source} ({user})" if user is not None else str(source)


def event_changed_by(event_name: str, data: dict[str, Any]) -> str | None:
    """Return who operated the lock in this event, or None if it doesn't say."""
    _alert_type, lock_state, attributes = parse_lock_event(event_name, data)
    source = attributes.get("source")
    if lock_state is None or not source:
        return None
    return _operator(source, attributes.get("user"))


def initial_changed_by(nested_state: dict[str, Any]) -> str | None:
    """Return who last operated a LockV2, from the alert its getState echoes.

    Only a successful lock or unlock names the last operator; an echoed
    failure (for example a rejected fingerprint) does not.
    """
    alert = nested_state.get("alert")
    if not isinstance(alert, dict) or not alert.get("source"):
        return None
    if ALERT_TO_ACTIVITY.get(str(alert.get("type", "")).lower()) not in ("locked", "unlocked"):
        return None
    return _operator(alert["source"], alert.get("userId"))


class _YoLocalLockEvent(YoLocalEntity, EventEntity):
    """Base event entity fed by the coordinator's raw MQTT events."""

    _key: str

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_{self._key}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.register_device_event_listener(
                self._device.device_id, self._handle_device_event
            )
        )

    def _handle_device_event(self, event_name: str, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def _fire(self, event_type: str, attributes: dict[str, Any] | None = None) -> None:
        self._trigger_event(event_type, attributes or None)
        self.async_write_ha_state()


class YoLocalLockDoorbellEvent(_YoLocalLockEvent):
    """Doorbell button on the lock's keypad."""

    _key = "doorbell"
    _attr_name = "Doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_event_types = ["ring"]

    def _handle_device_event(self, event_name: str, data: dict[str, Any]) -> None:
        alert_type, _lock_state, _attrs = parse_lock_event(event_name, data)
        if alert_type and str(alert_type).lower() in DOORBELL_ALERTS:
            self._fire("ring")


class YoLocalLockActivityEvent(_YoLocalLockEvent):
    """Lock/unlock activity with source (app, keypad, fingerprint...) and failures."""

    _key = "activity"
    _attr_name = "Activity"
    _attr_icon = "mdi:lock-clock"
    _attr_event_types = ACTIVITY_EVENT_TYPES

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._last_lock_state = self._current_lock_state()

    def _current_lock_state(self) -> str | None:
        value = self.state_value("lock")
        if value is None:
            value = self.state_value("state")
        return value if value in ("locked", "unlocked") else None

    def _handle_device_event(self, event_name: str, data: dict[str, Any]) -> None:
        alert_type, lock_state, attributes = parse_lock_event(event_name, data)

        if alert_type is not None:
            normalized = str(alert_type).lower()
            if normalized in DOORBELL_ALERTS:
                return  # handled by the doorbell entity
            activity = ALERT_TO_ACTIVITY.get(normalized)
            if activity in ("locked", "unlocked"):
                if lock_state is None:
                    lock_state = activity
            elif activity is not None:
                self._fire(activity, attributes)
                return
            else:
                self._fire("alert", {**attributes, "type": alert_type})
                return

        # Fire locked/unlocked once per actual change, however many events
        # (setState echo, StatusChange, Alert) the lock sends for it.
        if lock_state is not None and lock_state != self._last_lock_state:
            self._last_lock_state = lock_state
            self._fire(lock_state, attributes)
        elif lock_state is not None:
            self._last_lock_state = lock_state
