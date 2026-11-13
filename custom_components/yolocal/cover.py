"""Cover platform for YoLink Local garage door controllers."""

from __future__ import annotations

from time import monotonic
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .api import ApiError
from .const import GARAGE_DOOR_DEVICE_TYPES
from .coordinator import YoLocalCoordinator
from .entity import YoLocalEntity, async_setup_device_entities

# How long to report "opening"/"closing" after a command if the paired
# sensor hasn't reached the target state yet.
TRAVEL_TIMEOUT_SECONDS = 60.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink garage door covers from a config entry."""
    await async_setup_device_entities(
        hass, entry, async_add_entities, build_cover_entities
    )


def build_cover_entities(
    coordinator: YoLocalCoordinator, device
) -> list[YoLocalGarageDoor]:
    """Build a cover for garage door controllers."""
    if device.device_type not in GARAGE_DOOR_DEVICE_TYPES:
        return []
    return [YoLocalGarageDoor(coordinator, device)]


class YoLocalGarageDoor(YoLocalEntity, CoverEntity):
    """Garage door controlled by a YoLink GarageDoor/Finger device.

    The controller only pulses a relay (``toggle``); open/closed state comes
    from the door or tilt sensor paired with it in the YoLink app.
    """

    _attr_name = None  # Use device name
    _attr_device_class = CoverDeviceClass.GARAGE
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the garage door."""
        super().__init__(coordinator, device)
        self._target: str | None = None
        self._target_set_at = 0.0
        self._cancel_travel_timeout: CALLBACK_TYPE | None = None

    async def async_will_remove_from_hass(self) -> None:
        """Drop a pending travel-timeout refresh."""
        await super().async_will_remove_from_hass()
        self._clear_travel_timeout()

    def _clear_travel_timeout(self) -> None:
        if self._cancel_travel_timeout is not None:
            self._cancel_travel_timeout()
            self._cancel_travel_timeout = None

    @property
    def _sensor_id(self) -> str | None:
        return self.coordinator.paired_sensor_id(self._device.device_id)

    @property
    def _sensor_position(self) -> str | None:
        """Return "open"/"closed" from the paired sensor, or None."""
        sensor_id = self._sensor_id
        if sensor_id is None:
            return None
        sensor_state = self.coordinator.get_state(sensor_id)
        if sensor_state.get("online") is False:
            return None
        value = sensor_state.get("state")
        if isinstance(value, dict):
            value = value.get("state")
        if value in ("open", "closed"):
            return value
        return None

    @property
    def _moving_toward(self) -> str | None:
        """Return the commanded target while the door is still travelling."""
        if self._target is None:
            return None
        if monotonic() - self._target_set_at > TRAVEL_TIMEOUT_SECONDS:
            self._target = None
            return None
        if self._sensor_position == self._target:
            self._target = None
            return None
        return self._target

    @property
    def available(self) -> bool:
        """Available while the controller or its paired sensor is reporting.

        Some hubs can't read the controller's own status even though it still
        accepts toggles, and the door state comes from the sensor anyway.
        """
        if super().available:
            return True
        return self._sensor_position is not None

    @property
    def is_closed(self) -> bool | None:
        """Return True if the door is closed, None if unknown."""
        position = self._sensor_position
        if position is None:
            return None
        return position == "closed"

    @property
    def is_opening(self) -> bool:
        """Return True while an open command is in progress."""
        return self._moving_toward == "open"

    @property
    def is_closing(self) -> bool:
        """Return True while a close command is in progress."""
        return self._moving_toward == "closed"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose which sensor provides the door state."""
        sensor_id = self._sensor_id
        sensor = self.coordinator.devices.get(sensor_id) if sensor_id else None
        return {"paired_sensor": sensor.name if sensor else None}

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the garage door (no-op if it is already open)."""
        if self.is_closed is False and not self.is_closing:
            return
        await self._async_toggle_toward("open")

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the garage door (no-op if it is already closed)."""
        if self.is_closed is True and not self.is_opening:
            return
        await self._async_toggle_toward("closed")

    async def _async_toggle_toward(self, target: str) -> None:
        # The controller only toggles, so without a known position "close"
        # could just as well open the door.
        if self.is_closed is None:
            raise HomeAssistantError(
                f"{self._device.name} position is unknown; pair a door sensor "
                "with it in the YoLink app before controlling it"
            )
        try:
            await self.coordinator.async_toggle(self._device.device_id)
        except ApiError as err:
            raise HomeAssistantError(
                f"YoLink hub rejected toggle for {self._device.name}: {err}"
            ) from err
        self._target = target
        self._target_set_at = monotonic()
        self.async_write_ha_state()
        self._clear_travel_timeout()
        self._cancel_travel_timeout = async_call_later(
            self.hass, TRAVEL_TIMEOUT_SECONDS + 1, self._async_travel_timeout
        )

    def _async_travel_timeout(self, _now: Any) -> None:
        """Refresh state once the travel window has elapsed."""
        self._cancel_travel_timeout = None
        self.async_write_ha_state()
