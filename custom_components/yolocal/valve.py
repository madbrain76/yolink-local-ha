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

from .const import DOMAIN
from .coordinator import YoLocalCoordinator
from .entity import YoLocalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink valve entities from a config entry."""
    coordinator: YoLocalCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities_by_device_id: dict[str, list[YoLocalValve]] = {}

    def build_entities(device) -> list[YoLocalValve]:
        if device.device_type != "Manipulator":
            return []
        return [YoLocalValve(coordinator, device)]

    def add_devices(devices) -> None:
        new_entities: list[YoLocalValve] = []
        for device in devices:
            if device.device_id in entities_by_device_id:
                continue
            built = build_entities(device)
            if not built:
                continue
            entities_by_device_id[device.device_id] = built
            new_entities.extend(built)
        if new_entities:
            async_add_entities(new_entities)

    async def remove_devices(device_ids: list[str]) -> None:
        for device_id in device_ids:
            for entity in entities_by_device_id.pop(device_id, []):
                if isinstance(entity, YoLocalEntity):
                    await entity.async_remove_from_hass()

    def handle_registry_change(added_devices, removed_devices) -> None:
        add_devices(added_devices)
        removed_ids = [device.device_id for device in removed_devices]
        if removed_ids:
            hass.async_create_task(remove_devices(removed_ids))

    entry.async_on_unload(
        coordinator.register_device_registry_listener(handle_registry_change)
    )
    add_devices(coordinator.devices.values())


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
