"""Base entity for YoLink Local integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import ApiError, Device
from .const import DOMAIN
from .coordinator import STALE_REPORT_AGE, YoLocalCoordinator, device_contact_timestamp

EntityBuilder = Callable[[YoLocalCoordinator, Device], Iterable[Entity]]
EntityRefreshNeeded = Callable[[YoLocalCoordinator, Device, list[Entity]], bool]


async def async_setup_device_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build_entities: EntityBuilder,
    refresh_needed: EntityRefreshNeeded | None = None,
    retirable_suffixes: tuple[str, ...] = (),
) -> None:
    """Set up entities that follow the YoLink dynamic device registry."""
    coordinator: YoLocalCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities_by_device_id: dict[str, list[Entity]] = {}

    async def _remove_entities(entities: list[Entity]) -> None:
        removed: list[str] = []
        for entity in entities:
            if isinstance(entity, YoLocalEntity):
                entity_id = getattr(entity, "entity_id", None)
                await entity.async_remove_from_hass()
                if entity_id:
                    removed.append(entity_id)
        coordinator.async_clear_entity_statistics(removed)

    def add_devices(devices: Iterable[Device]) -> None:
        new_entities: list[Entity] = []
        for device in devices:
            existing = entities_by_device_id.get(device.device_id)
            if existing is not None and (
                refresh_needed is None
                or not refresh_needed(coordinator, device, existing)
            ):
                continue
            built = list(build_entities(coordinator, device))
            if existing is None:
                entities_by_device_id[device.device_id] = built
                new_entities.extend(built)
                continue
            known_ids = {entity.unique_id for entity in existing}
            wanted_ids = {entity.unique_id for entity in built}
            stale = [
                entity for entity in existing if entity.unique_id not in wanted_ids
            ]
            additions = [
                entity for entity in built if entity.unique_id not in known_ids
            ]
            if stale:
                entities_by_device_id[device.device_id] = [
                    entity for entity in existing if entity.unique_id in wanted_ids
                ]
                hass.async_create_task(_remove_entities(stale))
            if additions:
                entities_by_device_id[device.device_id].extend(additions)
                new_entities.extend(additions)
        if new_entities:
            async_add_entities(new_entities)

    async def remove_devices(device_ids: list[str]) -> None:
        removed: list[str] = []
        for device_id in device_ids:
            for entity in entities_by_device_id.pop(device_id, []):
                if isinstance(entity, YoLocalEntity):
                    entity_id = getattr(entity, "entity_id", None)
                    await entity.async_remove_from_hass()
                    if entity_id:
                        removed.append(entity_id)
        coordinator.async_clear_entity_statistics(removed)

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
    _retire_orphan_entities(hass, entry, coordinator, build_entities, retirable_suffixes)
    if refresh_needed is not None:
        entry.async_on_unload(
            coordinator.async_add_listener(
                lambda: add_devices(coordinator.devices.values())
            )
        )


def _retire_orphan_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: YoLocalCoordinator,
    build_entities: EntityBuilder,
    retirable_suffixes: tuple[str, ...],
) -> None:
    """Delete registry rows for diagnostics the hub does not support.

    In-session pruning cannot reach an entity that was left over from an earlier
    release, because Home Assistant restores it without the platform ever
    creating it again. Only suffixes with durable payload backing belong here,
    so a value that is merely not cached yet is not mistaken for unsupported.
    """
    if not retirable_suffixes:
        return
    registry = er.async_get(hass)
    if hasattr(er, "async_entries_for_config_entry"):
        rows = list(er.async_entries_for_config_entry(registry, entry.entry_id))
    else:
        rows = list(getattr(registry, "entities", {}).values())
    for device in coordinator.devices.values():
        wanted = {entity.unique_id for entity in build_entities(coordinator, device)}
        removed: list[str] = []
        for row in rows:
            unique_id = getattr(row, "unique_id", None)
            if not unique_id or unique_id in wanted:
                continue
            if unique_id not in {
                f"{device.device_id}{suffix}" for suffix in retirable_suffixes
            }:
                continue
            registry.async_remove(row.entity_id)
            removed.append(row.entity_id)
    coordinator.async_clear_entity_statistics(removed)


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

        # Contact is a device report timestamp or a newly received MQTT device
        # event. A cached getState reply and its envelope timestamp cannot
        # extend battery-device liveness.
        last_contact = device_contact_timestamp(state)
        if last_contact is not None:
            if dt_util.utcnow() - last_contact > STALE_REPORT_AGE:
                return False

        return True


def refresh_when_missing(
    build_entities: EntityBuilder, device_types: frozenset[str]
) -> EntityRefreshNeeded:
    """Rebuild a device's entities once its payload supports one not yet created.

    Optional setting entities depend on keys the hub only returns once the
    device has answered a poll, which can be after setup (a sleeping lock at
    startup, or a lock paired while Home Assistant is running). Only the given
    device types are rechecked, so other devices keep their one-time build.
    """

    def refresh_needed(
        coordinator: YoLocalCoordinator, device: Device, existing: list[Entity]
    ) -> bool:
        if device.device_type not in device_types:
            return False
        known = {entity.unique_id for entity in existing}
        return any(
            entity.unique_id not in known
            for entity in build_entities(coordinator, device)
        )

    return refresh_needed


def device_attributes(coordinator: YoLocalCoordinator, device: Device) -> dict[str, Any]:
    """Return a device's `attributes` settings block from cached state."""
    state = coordinator.get_state(device.device_id)
    nested = state.get("state")
    attributes = nested.get("attributes") if isinstance(nested, dict) else None
    if not isinstance(attributes, dict):
        attributes = state.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


class YoLocalAttributeEntity(YoLocalEntity):
    """Base for config entities backed by one key of a device's `attributes`."""

    def __init__(
        self,
        coordinator: YoLocalCoordinator,
        device: Device,
        attribute: str,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._attribute = attribute
        self._attr_name = name
        self._attr_unique_id = f"{device.device_id}_{key}"

    @property
    def attribute_value(self) -> Any:
        return device_attributes(self.coordinator, self._device).get(self._attribute)

    async def async_set_attribute(self, value: Any) -> None:
        try:
            await self.coordinator.async_set_attributes(
                self._device.device_id, {self._attribute: value}
            )
        except ApiError as err:
            raise HomeAssistantError(
                f"YoLink hub rejected {self._attribute} change for {self._device.name}: {err}"
            ) from err
