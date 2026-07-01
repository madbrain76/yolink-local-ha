"""Minimal Home Assistant test stubs for local unit tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_homeassistant_stubs() -> None:
    homeassistant = ModuleType("homeassistant")

    core = ModuleType("homeassistant.core")

    class HomeAssistant:
        """Minimal HomeAssistant stub."""

        def __init__(self) -> None:
            self.data: dict[str, dict[str, object]] = {}

        def async_create_task(self, coro):
            return SimpleNamespace(coro=coro, done=lambda: False, cancel=lambda: None)

        def async_create_background_task(self, coro, _name):
            return SimpleNamespace(coro=coro, done=lambda: False, cancel=lambda: None)

    def callback(func):
        return func

    core.HomeAssistant = HomeAssistant
    core.callback = callback

    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigFlowResult = dict

    class ConfigEntry:
        """Minimal ConfigEntry stub."""

        def __init__(self) -> None:
            self.entry_id = "entry"
            self.data = {}
            self._unload_callbacks = []

        def async_on_unload(self, callback):
            self._unload_callbacks.append(callback)
            return callback

    config_entries.ConfigEntry = ConfigEntry

    class ConfigFlow:
        """Minimal ConfigFlow stub."""

        def __init_subclass__(cls, **_kwargs):
            return super().__init_subclass__()

        def async_create_entry(self, title="", data=None):
            return {"type": "create_entry", "title": title, "data": data or {}}

        def async_show_form(
            self,
            step_id=None,
            data_schema=None,
            errors=None,
            description_placeholders=None,
        ):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
                "description_placeholders": description_placeholders or {},
            }

        def _get_reconfigure_entry(self):
            return self._reconfigure_entry

        def add_suggested_values_to_schema(self, data_schema, _suggested_values):
            return data_schema

        def async_update_reload_and_abort(
            self,
            entry,
            title=None,
            data=None,
            data_updates=None,
            options=None,
        ):
            new_data = dict(entry.data)
            if data is not None:
                new_data = data
            if data_updates is not None:
                new_data.update(data_updates)
            return {
                "type": "abort",
                "reason": "reconfigure_successful",
                "title": title,
                "data": new_data,
                "options": options,
            }

    config_entries.ConfigFlow = ConfigFlow

    exceptions = ModuleType("homeassistant.exceptions")

    class ConfigEntryAuthFailed(Exception):
        """Minimal auth failure stub."""

    class ConfigEntryNotReady(Exception):
        """Minimal not-ready stub."""

    exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    exceptions.ConfigEntryNotReady = ConfigEntryNotReady

    helpers = ModuleType("homeassistant.helpers")

    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        """Minimal DataUpdateCoordinator stub."""

        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, hass, logger, name=None, update_interval=None):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None
            self.last_update_success = True
            self.last_exception = None

        def async_set_updated_data(self, data):
            self.data = data
            self.last_update_success = True
            self.last_exception = None

        def async_set_update_error(self, err):
            self.last_exception = err
            self.last_update_success = False

    class CoordinatorEntity:
        """Minimal CoordinatorEntity stub."""

        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return True

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.CoordinatorEntity = CoordinatorEntity

    entity_platform = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    entity = ModuleType("homeassistant.helpers.entity")

    class Entity:
        """Minimal entity stub."""

    class EntityCategory:
        DIAGNOSTIC = "diagnostic"

    entity.Entity = Entity
    entity.EntityCategory = EntityCategory

    device_registry = ModuleType("homeassistant.helpers.device_registry")

    @dataclass
    class DeviceInfo:
        identifiers: set[tuple[str, str]]
        name: str
        manufacturer: str
        model: str
        serial_number: str

    class DeviceRegistry:
        def __init__(self) -> None:
            self.devices: dict[frozenset[tuple[str, str]], SimpleNamespace] = {}

        def async_get_device(self, identifiers=None, **_kwargs):
            return self.devices.get(frozenset(identifiers or set()))

        def async_remove_device(self, device_id):
            for identifiers, entry in list(self.devices.items()):
                if entry.id == device_id:
                    self.devices.pop(identifiers, None)

    _device_registry = DeviceRegistry()

    def async_get(_hass):
        return _device_registry

    def async_entries_for_config_entry(_registry, config_entry_id):
        return [
            entry
            for entry in _device_registry.devices.values()
            if getattr(entry, "config_entry_id", None) == config_entry_id
        ]

    device_registry.DeviceInfo = DeviceInfo
    device_registry.async_get = async_get
    device_registry.async_entries_for_config_entry = async_entries_for_config_entry

    entity_registry = ModuleType("homeassistant.helpers.entity_registry")

    class EntityRegistry:
        def __init__(self) -> None:
            self.entities: dict[str, SimpleNamespace] = {}

        def async_get(self, entity_id):
            return self.entities.get(entity_id)

        def async_remove(self, entity_id):
            self.entities.pop(entity_id, None)

    _entity_registry = EntityRegistry()

    def async_get_entity_registry(_hass):
        return _entity_registry

    def async_entries_for_config_entry(_registry, config_entry_id):
        return [
            entry
            for entry in _entity_registry.entities.values()
            if getattr(entry, "config_entry_id", None) == config_entry_id
        ]

    entity_registry.async_get = async_get_entity_registry
    entity_registry.async_entries_for_config_entry = async_entries_for_config_entry

    util = ModuleType("homeassistant.util")
    dt = ModuleType("homeassistant.util.dt")

    def parse_datetime(value: str):
        return datetime.fromisoformat(value)

    def utcnow():
        return datetime.now(UTC)

    dt.parse_datetime = parse_datetime
    dt.utcnow = utcnow
    util.dt = dt

    components = ModuleType("homeassistant.components")

    sensor = ModuleType("homeassistant.components.sensor")

    class SensorEntity:
        async def async_remove(self) -> None:
            return None

    class SensorDeviceClass:
        BATTERY = "battery"
        POWER = "power"
        TIMESTAMP = "timestamp"
        TEMPERATURE = "temperature"
        HUMIDITY = "humidity"

    class SensorStateClass:
        MEASUREMENT = "measurement"

    sensor.SensorEntity = SensorEntity
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass

    binary_sensor = ModuleType("homeassistant.components.binary_sensor")

    class BinarySensorEntity:
        async def async_remove(self) -> None:
            return None

    class BinarySensorDeviceClass:
        DOOR = "door"
        MOISTURE = "moisture"
        MOTION = "motion"
        VIBRATION = "vibration"
        PROBLEM = "problem"

    binary_sensor.BinarySensorEntity = BinarySensorEntity
    binary_sensor.BinarySensorDeviceClass = BinarySensorDeviceClass

    switch = ModuleType("homeassistant.components.switch")

    class SwitchEntity:
        async def async_remove(self) -> None:
            return None

    class SwitchDeviceClass:
        OUTLET = "outlet"

    switch.SwitchEntity = SwitchEntity
    switch.SwitchDeviceClass = SwitchDeviceClass


    valve = ModuleType("homeassistant.components.valve")

    class ValveEntity:
        async def async_remove(self) -> None:
            return None

    class ValveDeviceClass:
        WATER = "water"
        GAS = "gas"

    class ValveEntityFeature:
        OPEN = 1
        CLOSE = 2
        SET_POSITION = 4

    def __or__(self, other: int) -> int:
        return int(self) | int(other)

    valve.ValveEntity = ValveEntity
    valve.ValveDeviceClass = ValveDeviceClass
    valve.ValveEntityFeature = ValveEntityFeature

    const = ModuleType("homeassistant.const")
    const.PERCENTAGE = "%"

    class UnitOfTemperature:
        CELSIUS = "C"

    class UnitOfPower:
        WATT = "W"

    const.UnitOfTemperature = UnitOfTemperature
    const.UnitOfPower = UnitOfPower

    homeassistant.core = core
    homeassistant.config_entries = config_entries
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers
    homeassistant.util = util
    homeassistant.components = components
    homeassistant.const = const

    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.exceptions"] = exceptions
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator
    sys.modules["homeassistant.helpers.entity_platform"] = entity_platform
    sys.modules["homeassistant.helpers.entity"] = entity
    sys.modules["homeassistant.helpers.device_registry"] = device_registry
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
    sys.modules["homeassistant.util"] = util
    sys.modules["homeassistant.util.dt"] = dt
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.sensor"] = sensor
    sys.modules["homeassistant.components.binary_sensor"] = binary_sensor
    sys.modules["homeassistant.components.switch"] = switch
    sys.modules["homeassistant.components.valve"] = valve
    sys.modules["homeassistant.const"] = const


def _install_runtime_dependency_stubs() -> None:
    voluptuous = ModuleType("voluptuous")

    class _SchemaKey:
        def __init__(self, key, default=None) -> None:
            self.key = key
            self.default = default

        def __hash__(self) -> int:
            return hash((self.key, self.default))

        def __eq__(self, other) -> bool:
            return (
                isinstance(other, _SchemaKey)
                and self.key == other.key
                and self.default == other.default
            )

    class Schema:
        def __init__(self, schema) -> None:
            self.schema = schema

    voluptuous.Schema = Schema
    voluptuous.Required = _SchemaKey
    voluptuous.Optional = _SchemaKey
    sys.modules["voluptuous"] = voluptuous

    aiohttp = ModuleType("aiohttp")

    class ClientSession:
        async def close(self) -> None:
            return None

    class ClientConnectionError(Exception):
        """Minimal aiohttp connection error stub."""

    aiohttp.ClientSession = ClientSession
    aiohttp.ClientConnectionError = ClientConnectionError
    sys.modules["aiohttp"] = aiohttp

    paho = ModuleType("paho")
    mqtt_pkg = ModuleType("paho.mqtt")
    mqtt_client = ModuleType("paho.mqtt.client")

    class MQTTMessage:
        payload = b""

    class CallbackAPIVersion:
        VERSION2 = object()

    class Client:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def username_pw_set(self, *_args, **_kwargs) -> None:
            pass

        def connect_async(self, *_args, **_kwargs) -> None:
            pass

        def loop_start(self) -> None:
            pass

        def loop_stop(self) -> None:
            pass

        def disconnect(self) -> None:
            pass

        def subscribe(self, *_args, **_kwargs) -> None:
            pass

    mqtt_client.MQTTMessage = MQTTMessage
    mqtt_client.CallbackAPIVersion = CallbackAPIVersion
    mqtt_client.Client = Client

    paho.mqtt = mqtt_pkg
    mqtt_pkg.client = mqtt_client

    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = mqtt_pkg
    sys.modules["paho.mqtt.client"] = mqtt_client


_install_homeassistant_stubs()
_install_runtime_dependency_stubs()
