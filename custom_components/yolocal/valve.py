"""Valve platform for YoLink Local integration (Manipulator devices, e.g. YS-4909-UC)."""

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
    def build_entities(
        coordinator: YoLocalCoordinator,
        device,
    ) -> list[YoLocalValve]:
        if device.device_type != "Manipulator":
            return []
        return [YoLocalValve(coordinator, device)]

    await async_setup_device_entities(hass, entry, async_add_entities, build_entities)


class YoLocalValve(YoLocalEntity, ValveEntity):
    """Valve entity for YoLink Manipulator water-valve controller."""

    _attr_name = None  # Use device name
    _attr_device_class = ValveDeviceClass.WATER
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    _attr_reports_position = False

    @property
    def is_closed(self) -> bool | None:
        """Return True if the valve is closed, False if open, None if unknown."""
        state = self.device_state.get("state")
        if isinstance(state, dict):
            state = state.get("state")
        if state is None:
            return None
        return state == "closed"

    async def async_open_valve(self, **kwargs: Any) -> None:
        """Open the valve."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"state": "open"},
        )

    async def async_close_valve(self, **kwargs: Any) -> None:
        """Close the valve."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"state": "close"},
        )
