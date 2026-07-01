"""Sensor platform for YoLink Local integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import YoLocalCoordinator
from .entity import async_setup_device_entities, YoLocalEntity


def _state_has_battery(state: dict[str, Any]) -> bool:
    """Return True when a state payload carries a battery field."""
    if "battery" in state:
        return True
    nested_state = state.get("state")
    return isinstance(nested_state, dict) and "battery" in nested_state


def _device_has_battery(coordinator: YoLocalCoordinator, device) -> bool:
    """Return True when the current payload carries a battery field."""
    return _state_has_battery(coordinator.get_state(device.device_id))


def build_sensor_entities(
    coordinator: YoLocalCoordinator,
    device,
) -> list[SensorEntity]:
    """Build all sensor entities for a device."""
    entities: list[SensorEntity] = [
        YoLocalFirmwareSensor(coordinator, device),
        YoLocalLastReportedSensor(coordinator, device),
    ]
    if _device_has_battery(coordinator, device):
        entities.insert(0, YoLocalBatterySensor(coordinator, device))

    if device.device_type == "THSensor":
        has_threshold_sensors = device.model not in {"YS8003-UC", "YS8004-UC"}
        entities.append(YoLocalTemperatureSensor(coordinator, device))
        if device.display_type == "THSensor":
            entities.append(YoLocalTHModeSensor(coordinator, device))
        entities.append(YoLocalTHIntervalSensor(coordinator, device))
        entities.append(
            YoLocalTHCorrectionSensor(coordinator, device, "temperature")
        )
        if has_threshold_sensors:
            entities.append(
                YoLocalTHLimitSensor(coordinator, device, "temperature", "max")
            )
            entities.append(
                YoLocalTHLimitSensor(coordinator, device, "temperature", "min")
            )
        if device.display_type != "TempSensor":
            entities.append(YoLocalHumiditySensor(coordinator, device))
            entities.append(
                YoLocalTHCorrectionSensor(coordinator, device, "humidity")
            )
            if has_threshold_sensors:
                entities.append(
                    YoLocalTHLimitSensor(coordinator, device, "humidity", "max")
                )
                entities.append(
                    YoLocalTHLimitSensor(coordinator, device, "humidity", "min")
                )
    elif device.device_type == "MotionSensor":
        entities.extend(
            [
                YoLocalDeviceTemperatureSensor(coordinator, device),
                YoLocalMotionSensitivitySensor(coordinator, device),
                YoLocalMotionNoMotionDelaySensor(coordinator, device),
                YoLocalMotionAlertIntervalSensor(coordinator, device),
            ]
        )
    elif device.device_type == "LeakSensor":
        entities.extend(
            [
                YoLocalDeviceTemperatureSensor(coordinator, device),
                YoLocalLeakSensorModeSensor(coordinator, device),
                YoLocalLeakIntervalSensor(coordinator, device),
            ]
        )
    elif device.device_type == "DoorSensor":
        entities.extend(
            [
                YoLocalDoorDelaySensor(coordinator, device),
                YoLocalDoorOpenRemindDelaySensor(coordinator, device),
                YoLocalDoorAlertIntervalSensor(coordinator, device),
            ]
        )
    elif device.device_type == "Outlet":
        entities.extend(
            [
                YoLocalOutletPowerSensor(coordinator, device),
            ]
        )

    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up YoLink sensors from a config entry."""
    await async_setup_device_entities(
        hass,
        entry,
        async_add_entities,
        build_sensor_entities,
    )


class YoLocalBatterySensor(YoLocalEntity, SensorEntity):
    """Battery sensor for YoLink devices."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Battery"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_battery"

    @property
    def available(self) -> bool:
        """Battery sensor is always available with the last known value."""
        return True

    @property
    def native_value(self) -> int | None:
        """Return the battery level as percentage."""
        if not super().available:
            return 0

        level = self.state_value("battery", fallback=True)

        if level is None:
            return None
        return min(level * 25, 100)


class YoLocalFirmwareSensor(YoLocalEntity, SensorEntity):
    """Firmware version sensor for YoLink devices."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Firmware"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_firmware"

    @property
    def native_value(self) -> str | None:
        """Return the firmware version."""
        return self.state_value("version", fallback=True)


class YoLocalLastReportedSensor(YoLocalEntity, SensorEntity):
    """Last reported timestamp sensor for YoLink devices."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Last reported"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_last_reported"
        self._last_native_value: datetime | None = None

    @property
    def available(self) -> bool:
        """Keep the diagnostic timestamp available when a value exists."""
        return self.native_value is not None

    @property
    def native_value(self) -> datetime | None:
        """Return the last reported timestamp."""
        for key in ("lastReportedAt", "reportAt"):
            report_at = self.device_state.get(key)
            if not report_at:
                continue
            try:
                parsed = dt_util.parse_datetime(report_at)
            except (TypeError, ValueError):
                continue
            if parsed is not None:
                self._last_native_value = parsed
                return parsed
        return self._last_native_value


class YoLocalTemperatureSensor(YoLocalEntity, SensorEntity):
    """Temperature sensor for YoLink THSensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_name = "Temperature"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_temperature"

    @property
    def native_value(self) -> float | None:
        """Return the temperature."""
        return self.state_value("temperature", fallback=True)


class YoLocalHumiditySensor(YoLocalEntity, SensorEntity):
    """Humidity sensor for YoLink THSensor."""

    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_name = "Humidity"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_humidity"

    @property
    def native_value(self) -> float | None:
        """Return the humidity."""
        return self.state_value("humidity", fallback=True)


class YoLocalTHModeSensor(YoLocalEntity, SensorEntity):
    """Temperature unit mode sensor for YoLink THSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "LCD temperature unit"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_temp_unit"

    @property
    def native_value(self) -> str | None:
        """Return the LCD display temperature unit (C/F)."""
        mode = (
            self.state_value("mode", fallback=True)
            or self.state_value("tempUnit", fallback=True)
            or self.state_value("temperatureUnit", fallback=True)
            or self.state_value("unit", fallback=True)
        )
        if mode is None:
            return None

        normalized = str(mode).strip().lower()
        if normalized in {"c", "0", "celsius", "centigrade", "cel"}:
            return "C"
        if normalized in {"f", "1", "fahrenheit", "fahr"}:
            return "F"
        return str(mode).upper()


class YoLocalTHIntervalSensor(YoLocalEntity, SensorEntity):
    """Reporting interval sensor for YoLink THSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "min"
    _attr_name = "Reporting interval"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_interval"

    @property
    def native_value(self) -> int | None:
        """Return the reporting interval."""
        return self.state_value("interval", fallback=True)


class YoLocalTHCorrectionSensor(YoLocalEntity, SensorEntity):
    """Calibration correction sensor for YoLink THSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: YoLocalCoordinator,
        device,
        measurement_type: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._measurement_type = measurement_type
        if measurement_type == "temperature":
            self._attr_name = "Temperature correction"
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_unique_id = f"{device.device_id}_temp_correction"
        else:
            self._attr_name = "Humidity correction"
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_unique_id = f"{device.device_id}_humidity_correction"

    @property
    def native_value(self) -> int | None:
        """Return the calibration correction value."""
        if self._measurement_type == "temperature":
            return self.state_value("tempCorrection", fallback=True)
        return self.state_value("humidityCorrection", fallback=True)


class YoLocalTHLimitSensor(YoLocalEntity, SensorEntity):
    """Alarm threshold limit sensor for YoLink THSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: YoLocalCoordinator,
        device,
        measurement_type: str,
        limit_type: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._measurement_type = measurement_type
        self._limit_type = limit_type
        limit_name = "max" if limit_type == "max" else "min"
        if measurement_type == "temperature":
            self._attr_name = f"Temperature {limit_name} threshold"
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_unique_id = f"{device.device_id}_temp_{limit_type}"
        else:
            self._attr_name = f"Humidity {limit_name} threshold"
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_unique_id = f"{device.device_id}_humidity_{limit_type}"

    @property
    def native_value(self) -> int | float | None:
        """Return the alarm threshold."""
        state = self.nested_device_state
        if self._measurement_type == "temperature":
            limits = state.get("tempLimit", {})
            if not isinstance(limits, dict):
                limits = self.device_state.get("tempLimit", {})
        else:
            limits = state.get("humidityLimit", {})
            if not isinstance(limits, dict):
                limits = self.device_state.get("humidityLimit", {})

        if isinstance(limits, dict):
            value = limits.get(self._limit_type)
            if value is not None:
                if self._measurement_type == "temperature":
                    if value < -100 or value > 100:
                        return None
                else:
                    if value < 0 or value > 100:
                        return None
            return value
        return None


class YoLocalDeviceTemperatureSensor(YoLocalEntity, SensorEntity):
    """Device internal temperature sensor for YoLink sensors."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Device temperature"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_dev_temperature"

    @property
    def native_value(self) -> int | None:
        """Return the device temperature."""
        return self.state_value("devTemperature")


class YoLocalOutletPowerSensor(YoLocalEntity, SensorEntity):
    """Active power sensor for YoLink outlets."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 1
    _attr_name = "Power"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_power"

    @property
    def native_value(self) -> float | None:
        """Return active power in watts from the Outlet deciwatt field."""
        power = self.state_value("power", fallback=True)
        if power is None:
            return None
        return float(power) / 10


class YoLocalMotionSensitivitySensor(YoLocalEntity, SensorEntity):
    """Sensitivity sensor for YoLink MotionSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Sensitivity"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_sensitivity"

    @property
    def native_value(self) -> int | None:
        """Return the sensitivity level (1-5)."""
        return self.state_value("sensitivity")


class YoLocalMotionNoMotionDelaySensor(YoLocalEntity, SensorEntity):
    """No-motion delay sensor for YoLink MotionSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "min"
    _attr_name = "No-motion delay"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_nomotion_delay"

    @property
    def native_value(self) -> int | None:
        """Return the no-motion delay in minutes."""
        return self.state_value("nomotionDelay")


class YoLocalMotionAlertIntervalSensor(YoLocalEntity, SensorEntity):
    """Alert interval sensor for YoLink MotionSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "min"
    _attr_name = "Alert interval"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_alert_interval"

    @property
    def native_value(self) -> int | None:
        """Return the alert interval."""
        return self.state_value("alertInterval")


class YoLocalLeakSensorModeSensor(YoLocalEntity, SensorEntity):
    """Sensor mode sensor for YoLink LeakSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Sensor mode"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_sensor_mode"

    @property
    def native_value(self) -> str | None:
        """Return the sensor mode."""
        return self.state_value("sensorMode")


class YoLocalLeakIntervalSensor(YoLocalEntity, SensorEntity):
    """Reporting interval sensor for YoLink LeakSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "min"
    _attr_name = "Reporting interval"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_interval"

    @property
    def native_value(self) -> int | None:
        """Return the reporting interval."""
        return self.state_value("interval")


class YoLocalDoorDelaySensor(YoLocalEntity, SensorEntity):
    """Delay sensor for YoLink DoorSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "s"
    _attr_name = "Delay"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_delay"

    @property
    def native_value(self) -> int | None:
        """Return the delay in seconds."""
        return self.state_value("delay")


class YoLocalDoorOpenRemindDelaySensor(YoLocalEntity, SensorEntity):
    """Open remind delay sensor for YoLink DoorSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "min"
    _attr_name = "Open remind delay"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_open_remind_delay"

    @property
    def native_value(self) -> int | None:
        """Return the open remind delay in minutes."""
        return self.state_value("openRemindDelay")


class YoLocalDoorAlertIntervalSensor(YoLocalEntity, SensorEntity):
    """Alert interval sensor for YoLink DoorSensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "min"
    _attr_name = "Alert interval"

    def __init__(self, coordinator: YoLocalCoordinator, device) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_alert_interval"

    @property
    def native_value(self) -> int | None:
        """Return the alert interval."""
        return self.state_value("alertInterval")
