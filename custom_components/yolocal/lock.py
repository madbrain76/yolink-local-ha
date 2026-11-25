"""Lock platform for YoLink Local integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import LOCK_V2_DEVICE_TYPE
from .coordinator import YoLocalCoordinator
from .entity import YoLocalEntity, async_setup_device_entities
from .event import event_changed_by, initial_changed_by
from .lock_users import LockUserManagementMixin

LOCK_DEVICE_TYPES = frozenset({"Lock", LOCK_V2_DEVICE_TYPE})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink locks from a config entry."""
    await async_setup_device_entities(hass, entry, async_add_entities, build_lock_entities)


def build_lock_entities(coordinator: YoLocalCoordinator, device) -> list[YoLocalLock]:
    """Build a lock entity for YoLink locks."""
    if device.device_type not in LOCK_DEVICE_TYPES:
        return []
    return [YoLocalLock(coordinator, device)]


class YoLocalLock(LockUserManagementMixin, YoLocalEntity, LockEntity):
    """Lock entity for YoLink smart locks (Lock and LockV2)."""

    _attr_name = None  # Use device name

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the lock."""
        super().__init__(coordinator, device)
        self._changed_by: str | None = None
        # Last published lock state, to notice a change nobody claimed.
        self._last_lock_state: str | None = None
        if self._is_v2:
            self._changed_by = initial_changed_by(self.nested_device_state)
            self._last_lock_state = self.state_value("lock")

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

    @callback
    def _handle_coordinator_update(self) -> None:
        """Publish the lock state together with who changed it."""
        if self._is_v2:
            self._update_changed_by()
        super()._handle_coordinator_update()

    def _update_changed_by(self) -> None:
        current = self.state_value("lock")
        event = self.coordinator.applying_device_event(self._device.device_id)
        operator = event_changed_by(*event) if event is not None else None
        if operator is not None:
            self._changed_by = operator
        elif (
            current is not None
            and self._last_lock_state is not None
            and current != self._last_lock_state
        ):
            # The state changed without saying who did it: an app or Home
            # Assistant command (which only echoes setState), a poll or a
            # command readback. The previous operator no longer applies.
            self._changed_by = None
        if current is not None:
            self._last_lock_state = current

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
