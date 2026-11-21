"""Select platform for YoLink Local integration (device settings)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import LOCK_V2_DEVICE_TYPE
from .coordinator import YoLocalCoordinator
from .entity import (
    YoLocalAttributeEntity,
    async_setup_device_entities,
    device_attributes,
    refresh_when_missing,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink select entities from a config entry."""
    await async_setup_device_entities(
        hass,
        entry,
        async_add_entities,
        build_select_entities,
        refresh_needed=refresh_when_missing(
            build_select_entities, frozenset({LOCK_V2_DEVICE_TYPE})
        ),
    )


def build_select_entities(
    coordinator: YoLocalCoordinator, device
) -> list[SelectEntity]:
    """Build setting selects for devices that report them."""
    if device.device_type != LOCK_V2_DEVICE_TYPE:
        return []
    if "rlSet" not in device_attributes(coordinator, device):
        return []
    return [YoLocalLockHandingSelect(coordinator, device)]


class YoLocalLockHandingSelect(YoLocalAttributeEntity, SelectEntity):
    """Door handing: which side the lock is mounted for (left/right)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:door"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        super().__init__(coordinator, device, "rlSet", "door_handing", "Door handing")
        self._attr_options = ["left", "right"]

    @property
    def current_option(self) -> str | None:
        value = self.attribute_value
        return value if value in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await self.async_set_attribute(option)
