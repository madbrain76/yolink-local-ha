"""Lock platform for YoLink Local integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import LOCK_V2_DEVICE_TYPE
from .coordinator import YoLocalCoordinator
from .entity import YoLocalEntity, async_setup_device_entities
from .event import parse_lock_event
from .lock_services import async_register_lock_services
from .lock_users import LockUserManagementMixin

LOCK_DEVICE_TYPES = frozenset({"Lock", LOCK_V2_DEVICE_TYPE})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink locks from a config entry."""
    await async_setup_device_entities(hass, entry, async_add_entities, build_lock_entities)
    async_register_lock_services()


def build_lock_entities(coordinator: YoLocalCoordinator, device) -> list[YoLocalLock]:
    """Build a lock entity for YoLink locks."""
    if device.device_type not in LOCK_DEVICE_TYPES:
        return []
    return [YoLocalLock(coordinator, device)]


def lock_alert(nested_state: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (alert type, source) from either LockV2 or V1 alert shapes."""
    alert = nested_state.get("alert")
    if isinstance(alert, dict):
        return alert.get("type"), alert.get("source")
    return nested_state.get("alertType"), nested_state.get("source")


class YoLocalLock(LockUserManagementMixin, YoLocalEntity, LockEntity):
    """Lock entity for YoLink smart locks (Lock and LockV2)."""

    _attr_name = None  # Use device name

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the lock."""
        super().__init__(coordinator, device)
        self._changed_by: str | None = None
        if self._is_v2:
            _alert_type, self._changed_by = lock_alert(self.nested_device_state)

    @property
    def _is_v2(self) -> bool:
        return self._device.device_type == LOCK_V2_DEVICE_TYPE

    @property
    def is_locked(self) -> bool | None:
        """Return True if the lock is locked."""
        if self._is_v2:
            state = self.state_value("lock")
        else:
            state = self.device_state.get("state")
            if isinstance(state, dict):
                state = state.get("state")
        if state is None:
            return None
        return state == "locked"

    async def async_added_to_hass(self) -> None:
        """Follow live events to know who last operated the lock."""
        await super().async_added_to_hass()
        if not self._is_v2:
            return
        self.async_on_remove(
            self.coordinator.register_device_event_listener(
                self._device.device_id, self._handle_device_event
            )
        )

    def _handle_device_event(self, event_name: str, data: dict[str, Any]) -> None:
        _alert_type, lock_state, attributes = parse_lock_event(event_name, data)
        source = attributes.get("source")
        if lock_state is None or not source:
            return
        user = attributes.get("user")
        changed_by = f"{source} ({user})" if user else str(source)
        if changed_by != self._changed_by:
            self._changed_by = changed_by
            # The coordinator has already written the new lock state, so write
            # again or the state would carry the previous operator.
            self.async_write_ha_state()

    @property
    def changed_by(self) -> str | None:
        """Return how the lock was last operated (app, keypad, fingerprint...)."""
        return self._changed_by

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the device."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"state": {"lock": "locked"}} if self._is_v2 else {"state": "locked"},
        )

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the device."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"state": {"lock": "unlocked"}} if self._is_v2 else {"state": "unlocked"},
        )
