"""Number platform for YoLink Local integration (device settings)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
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
    """Set up YoLink number entities from a config entry."""
    await async_setup_device_entities(
        hass,
        entry,
        async_add_entities,
        build_number_entities,
        refresh_needed=refresh_when_missing(
            build_number_entities, frozenset({LOCK_V2_DEVICE_TYPE})
        ),
    )


def build_number_entities(
    coordinator: YoLocalCoordinator, device
) -> list[NumberEntity]:
    """Build setting numbers for devices that report them."""
    if device.device_type != LOCK_V2_DEVICE_TYPE:
        return []
    attributes = device_attributes(coordinator, device)
    entities: list[NumberEntity] = []
    if "autoLock" in attributes:
        entities.append(YoLocalLockAutoLockNumber(coordinator, device))
    if "soundLevel" in attributes:
        entities.append(YoLocalLockSoundLevelNumber(coordinator, device))
    return entities


class _YoLocalAttributeNumber(YoLocalAttributeEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float | None:
        value = self.attribute_value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value

    async def async_set_native_value(self, value: float) -> None:
        await self.async_set_attribute(int(value))


class YoLocalLockAutoLockNumber(_YoLocalAttributeNumber):
    """Seconds after unlocking before the lock re-locks itself (0 = off)."""

    _attr_icon = "mdi:lock-clock"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        super().__init__(coordinator, device, "autoLock", "auto_lock", "Auto-lock delay")


class YoLocalLockSoundLevelNumber(_YoLocalAttributeNumber):
    """Keypad/motor sound volume, 0 (mute) to 3 (loudest)."""

    _attr_icon = "mdi:volume-high"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 3
    _attr_native_step = 1

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        super().__init__(coordinator, device, "soundLevel", "sound_level", "Sound level")
