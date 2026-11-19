"""Button platform for YoLink Local integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ApiError
from .const import GARAGE_DOOR_DEVICE_TYPES
from .coordinator import YoLocalCoordinator
from .entity import YoLocalEntity, async_setup_device_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink buttons from a config entry."""
    await async_setup_device_entities(
        hass, entry, async_add_entities, build_button_entities
    )


def build_button_entities(
    coordinator: YoLocalCoordinator, device
) -> list[YoLocalGarageDoorToggle]:
    """Build a toggle button for garage door controllers."""
    if device.device_type not in GARAGE_DOOR_DEVICE_TYPES:
        return []
    return [YoLocalGarageDoorToggle(coordinator, device)]


class YoLocalGarageDoorToggle(YoLocalEntity, ButtonEntity):
    """Pulse a YoLink garage door controller's relay.

    The controller has no position feedback and the opener decides the
    direction, so this is a plain button. Pair it with a door sensor in Home
    Assistant (for example a template cover) to get open/close.
    """

    _attr_name = "Toggle"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the toggle button."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_toggle"
        self._pressing = False

    async def async_press(self) -> None:
        """Send one toggle.

        A press while the previous toggle is still being sent is ignored: a
        second pulse could stop or reverse the door. The toggle is never
        retried, since a lost reply doesn't mean the pulse didn't happen.
        """
        if self._pressing:
            return
        self._pressing = True
        try:
            await self.coordinator.async_toggle(self._device.device_id)
        except ApiError as err:
            raise HomeAssistantError(
                f"YoLink hub rejected toggle for {self._device.name}: {err}"
            ) from err
        finally:
            self._pressing = False
