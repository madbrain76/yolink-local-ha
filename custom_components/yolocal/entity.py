"""Base entity for YoLink Local integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import Device
from .const import DOMAIN
from .coordinator import STALE_REPORT_AGE, YoLocalCoordinator

EntityBuilder = Callable[[YoLocalCoordinator, Device], Iterable[Entity]]


async def async_setup_device_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build_entities: EntityBuilder,
) -> None:
    """Set up entities that follow the YoLink dynamic device registry."""
    coordinator: YoLocalCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities_by_device_id: dict[str, list[Entity]] = {}

    def add_devices(devices: Iterable[Device]) -> None:
        new_entities: list[Entity] = []
        for device in devices:
            if device.device_id in entities_by_device_id:
                continue
            built = list(build_entities(coordinator, device))
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

    def handle_registry_change(
        added_devices: list[Device],
        removed_devices: list[Device],
    ) -> None:
        add_devices(added_devices)
        removed_ids = [device.device_id for device in removed_devices]
        if removed_ids:
            hass.async_create_task(remove_devices(removed_ids))

    entry.async_on_unload(
        coordinator.register_device_registry_listener(handle_registry_change)
    )
    add_devices(coordinator.devices.values())


class YoLocalEntity(CoordinatorEntity[YoLocalCoordinator]):
    """Base entity for YoLink Local devices."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: YoLocalCoordinator,
        device: Device,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = device.device_id

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this entity."""
        if self._device.model:
            model = f"{self._device.model} ({self._device.display_type})"
        else:
            model = self._device.display_type

        return DeviceInfo(
            identifiers={(DOMAIN, self._device.device_id)},
            name=self._device.name,
            manufacturer="YoLink",
            model=model,
            serial_number=self._device.device_id,
        )

    @property
    def device_state(self) -> dict[str, Any]:
        """Return the current device state."""
        return self.coordinator.get_state(self._device.device_id)

    @property
    def nested_device_state(self) -> dict[str, Any]:
        """Return the nested `state` object when available."""
        state = self.device_state.get("state")
        if isinstance(state, dict):
            return state
        return {}

    def state_value(self, key: str, fallback: bool = False) -> Any:
        """Return a nested state value, optionally falling back to top-level state."""
        value = self.nested_device_state.get(key)
        if value is not None or not fallback:
            return value
        return self.device_state.get(key)

    async def async_remove_from_hass(self) -> None:
        """Remove this entity from HA, including its registry entry when present."""
        entity_id = getattr(self, "entity_id", None)
        if entity_id:
            registry = er.async_get(self.coordinator.hass)
            if registry.async_get(entity_id) is not None:
                registry.async_remove(entity_id)
                return
        if hasattr(self, "async_remove"):
            await self.async_remove()

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        state = self.device_state
        if not state.get("online", True):
            return False

        report_at = state.get("lastReportedAt")
        if report_at:
            try:
                last_report = dt_util.parse_datetime(report_at)
                if last_report is not None:
                    if dt_util.utcnow() - last_report > STALE_REPORT_AGE:
                        return False
            except Exception:
                pass

        return True
