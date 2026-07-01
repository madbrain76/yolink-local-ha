"""Binary sensor platform for YoLink Local integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import YoLocalCoordinator
from .entity import async_setup_device_entities, YoLocalEntity


DEVICE_TYPE_TO_CLASS = {
    "DoorSensor": BinarySensorDeviceClass.DOOR,
    "LeakSensor": BinarySensorDeviceClass.MOISTURE,
    "MotionSensor": BinarySensorDeviceClass.MOTION,
    "VibrationSensor": BinarySensorDeviceClass.VIBRATION,
}

DEVICE_TYPE_TO_ON_STATE = {
    "DoorSensor": "open",
    "LeakSensor": "alert",
    "MotionSensor": "alert",
    "VibrationSensor": "alert",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink binary sensors from a config entry."""
    def build_entities(
        coordinator: YoLocalCoordinator,
        device,
    ) -> list[BinarySensorEntity]:
        entities: list[BinarySensorEntity] = []
        if device.device_type in DEVICE_TYPE_TO_CLASS:
            entities.append(YoLocalBinarySensor(coordinator, device))
        if device.device_type == "THSensor":
            entities.extend(
                [
                    YoLocalTHAlarmSensor(
                        coordinator, device, "lowTemp", "Low temperature"
                    ),
                    YoLocalTHAlarmSensor(
                        coordinator, device, "highTemp", "High temperature"
                    ),
                    YoLocalTHAlarmSensor(
                        coordinator, device, "lowHumidity", "Low humidity"
                    ),
                    YoLocalTHAlarmSensor(
                        coordinator, device, "highHumidity", "High humidity"
                    ),
                    YoLocalTHAlarmSensor(
                        coordinator, device, "lowBattery", "Low battery"
                    ),
                ]
            )
        elif device.device_type == "LeakSensor":
            entities.extend(
                [
                    YoLocalLeakAlarmSensor(
                        coordinator, device, "detectorError", "Detector error"
                    ),
                    YoLocalLeakAlarmSensor(
                        coordinator, device, "freezeError", "Freeze error"
                    ),
                    YoLocalLeakAlarmSensor(
                        coordinator, device, "stayError", "Stay error"
                    ),
                    YoLocalLeakAlarmSensor(
                        coordinator, device, "reminder", "Reminder"
                    ),
                ]
            )
        elif device.device_type == "MotionSensor":
            entities.append(YoLocalMotionLEDSensor(coordinator, device))
        return entities

    await async_setup_device_entities(hass, entry, async_add_entities, build_entities)


class YoLocalBinarySensor(YoLocalEntity, BinarySensorEntity):
    """Binary sensor for YoLink door/leak/motion sensors."""

    _attr_name = None  # Use device name

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_device_class = DEVICE_TYPE_TO_CLASS.get(device.device_type)
        self._on_state = DEVICE_TYPE_TO_ON_STATE.get(device.device_type, "open")

    @property
    def is_on(self) -> bool | None:
        """Return True if the sensor is triggered."""
        sensor_state = self.state_value("state")
        if sensor_state is None:
            sensor_state = self.device_state.get("state")

        if sensor_state is None:
            return None
        return sensor_state == self._on_state

    @property
    def extra_state_attributes(self) -> dict[str, any]:
        """Return additional state attributes."""
        attrs = {
            "device_id": self._device.device_id,
            "device_model": self._device.device_type,
        }
        state = self.nested_device_state
        battery_level = state.get("battery")
        if battery_level is not None:
            attrs["battery_level"] = min(battery_level * 25, 100)
            attrs["battery_raw"] = battery_level
        if "devTemperature" in state:
            attrs["device_temperature"] = state.get("devTemperature")
        if "version" in state:
            attrs["firmware_version"] = state.get("version")
        return attrs


class YoLocalTHAlarmSensor(YoLocalEntity, BinarySensorEntity):
    """Alarm state binary sensor for YoLink THSensor."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: YoLocalCoordinator,
        device,
        alarm_type: str,
        alarm_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._alarm_type = alarm_type
        self._attr_name = alarm_name
        self._attr_unique_id = f"{device.device_id}_alarm_{alarm_type}"

    @property
    def is_on(self) -> bool | None:
        """Return True if the alarm is triggered."""
        alarm = self.state_value("alarm")
        if isinstance(alarm, dict):
            return alarm.get(self._alarm_type, False)
        return None


class YoLocalLeakAlarmSensor(YoLocalEntity, BinarySensorEntity):
    """Alarm state binary sensor for YoLink LeakSensor."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: YoLocalCoordinator,
        device,
        alarm_type: str,
        alarm_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._alarm_type = alarm_type
        self._attr_name = alarm_name
        self._attr_unique_id = f"{device.device_id}_alarm_{alarm_type}"

    @property
    def is_on(self) -> bool | None:
        """Return True if the alarm is triggered."""
        alarm_state = self.state_value("alarmState")
        if isinstance(alarm_state, dict):
            return alarm_state.get(self._alarm_type, False)
        return None


class YoLocalMotionLEDSensor(YoLocalEntity, BinarySensorEntity):
    """LED alarm status sensor for YoLink MotionSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "LED alarm"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_led_alarm"

    @property
    def is_on(self) -> bool | None:
        """Return True if LED alarm is enabled."""
        return self.state_value("ledAlarm")
