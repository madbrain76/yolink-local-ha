"""Siren platform for YoLink Local integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.siren import SirenEntity, SirenEntityFeature
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
    """Set up YoLink sirens from a config entry."""
    def build_entities(
        coordinator: YoLocalCoordinator,
        device,
    ) -> list[YoLocalSiren]:
        if device.device_type != "Siren":
            return []
        return [YoLocalSiren(coordinator, device)]

    await async_setup_device_entities(hass, entry, async_add_entities, build_entities)


class YoLocalSiren(YoLocalEntity, SirenEntity):
    """Siren entity for YoLink siren."""

    _attr_name = None  # Use device name
    _attr_supported_features = SirenEntityFeature.TURN_ON | SirenEntityFeature.TURN_OFF

    @property
    def is_on(self) -> bool | None:
        """Return True if the siren is sounding."""
        state = self.device_state.get("state")
        if state is None:
            return None
        return state == "alert"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the siren."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"state": {"alarm": True}},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the siren."""
        await self.coordinator.async_send_command(
            self._device.device_id,
            {"state": {"alarm": False}},
        )
