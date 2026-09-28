"""Valve platform for YoLink Local integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import YoLocalCoordinator
from .entity import async_setup_device_entities, YoLocalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink valve entities from a config entry."""
    await async_setup_device_entities(hass, entry, async_add_entities, build_valve_entities)


def build_valve_entities(coordinator: YoLocalCoordinator, device) -> list[YoLocalValve]:
    """Build a valve for supported YoLink controllers."""
    if device.device_type not in {"Manipulator", "WaterMeterController"}:
        return []
    return [YoLocalValve(coordinator, device)]


class YoLocalValve(YoLocalEntity, ValveEntity):
    """Valve entity for a YoLink water-valve controller."""

    _attr_name = None  # Use device name
    _attr_device_class = ValveDeviceClass.WATER
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    _attr_reports_position = False

    @property
    def is_closed(self) -> bool | None:
        """Return True if the valve is closed, False if open, None if unknown."""
        state = self.device_state.get("state")
        if isinstance(state, dict):
            key = "valve" if self._device.device_type == "WaterMeterController" else "state"
            state = state.get(key)
        if state == "close" or state == "closed":
            return True
        if state == "open":
            return False
        return None

    async def async_open_valve(self, **kwargs: Any) -> None:
        """Open the valve."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"valve": "open"} if self._device.device_type == "WaterMeterController" else {"state": "open"},
        )

    async def async_close_valve(self, **kwargs: Any) -> None:
        """Close the valve."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"valve": "close"} if self._device.device_type == "WaterMeterController" else {"state": "close"},
        )
