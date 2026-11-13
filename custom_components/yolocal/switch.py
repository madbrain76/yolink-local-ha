"""Switch platform for YoLink Local integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import LOCK_V2_DEVICE_TYPE
from .coordinator import YoLocalCoordinator
from .entity import (
    YoLocalAttributeEntity,
    YoLocalEntity,
    async_setup_device_entities,
    device_attributes,
    refresh_when_missing,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink switches from a config entry."""
    def build_entities(
        coordinator: YoLocalCoordinator,
        device,
    ) -> list[SwitchEntity]:
        if device.device_type == "Outlet":
            return [YoLocalSwitch(coordinator, device)]
        if (
            device.device_type == LOCK_V2_DEVICE_TYPE
            and "enableSetButton" in device_attributes(coordinator, device)
        ):
            return [YoLocalLockSetButtonSwitch(coordinator, device)]
        return []

    await async_setup_device_entities(
        hass,
        entry,
        async_add_entities,
        build_entities,
        refresh_needed=refresh_when_missing(
            build_entities, frozenset({LOCK_V2_DEVICE_TYPE})
        ),
    )


class YoLocalSwitch(YoLocalEntity, SwitchEntity):
    """Switch entity for YoLink outlet."""

    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_name = None  # Use device name

    @property
    def is_on(self) -> bool | None:
        """Return True if the switch is on."""
        state = self.device_state.get("state")
        if isinstance(state, dict):
            state = state.get("state")
        if state is None:
            return None
        # YoLink uses "open" for on, "closed" for off (relay terminology)
        return state == "open"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"state": "open"},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"state": "closed"},
        )


class YoLocalLockSetButtonSwitch(YoLocalAttributeEntity, SwitchEntity):
    """Whether the lock's physical SET button is enabled."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:gesture-tap-button"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        super().__init__(coordinator, device, "enableSetButton", "set_button", "Set button")

    @property
    def is_on(self) -> bool | None:
        value = self.attribute_value
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_set_attribute(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_attribute(False)
