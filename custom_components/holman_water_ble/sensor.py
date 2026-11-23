"""Sensor platform for Holman Water BLE diagnostics.

Only sensors the device can use are created: mains voltage on mains-powered
models, station and valve counts on multi-valve models, and the dial position
once the device has reported a dial.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_HAS_DIAL, DIAL_POSITION_MAP, DOMAIN
from .coordinator import HolmanWaterCoordinator
from .entity import HolmanEntity
from .models import DeviceConfig, DeviceInfo

# Created by earlier versions, no longer created by this one.
RETIRED_KEYS = ("voltage_dc", "watering_status", "firmware_version", "protocol_version")


@dataclass(frozen=True, kw_only=True)
class HolmanSensorDescription:
    """What a sensor shows, and on which devices."""

    key: str
    name: str
    value_fn: Callable[[DeviceInfo], Any]
    icon: str
    unit: Optional[str] = None
    numeric: bool = False
    enabled_by_default: bool = True
    supported: Callable[[DeviceConfig, dict[str, Any]], bool] = lambda _c, _d: True


def _rain(info: DeviceInfo) -> str:
    if info.rain_sensor_wet:
        return "Wet"
    return "On" if info.rain_sensor_on else "Off"


SENSORS: tuple[HolmanSensorDescription, ...] = (
    HolmanSensorDescription(
        key="battery_voltage",
        name="Battery Voltage",
        value_fn=lambda info: round(info.voltage_dc, 1),
        icon="mdi:battery-50",
        unit=UnitOfElectricPotential.VOLT,
        numeric=True,
    ),
    HolmanSensorDescription(
        key="voltage_ac",
        name="Voltage AC",
        value_fn=lambda info: round(info.voltage_ac, 1),
        icon="mdi:power-plug",
        unit=UnitOfElectricPotential.VOLT,
        numeric=True,
        supported=lambda config, _data: config.is_ac_device,
    ),
    HolmanSensorDescription(
        key="voltage_valve",
        name="Voltage Valve",
        value_fn=lambda info: round(info.voltage_valve, 1),
        icon="mdi:valve",
        unit=UnitOfElectricPotential.VOLT,
        numeric=True,
        enabled_by_default=False,
    ),
    HolmanSensorDescription(
        key="dial_position",
        name="Dial Position",
        value_fn=lambda info: DIAL_POSITION_MAP.get(
            info.dial_position, f"Unknown ({info.dial_position})"
        ),
        icon="mdi:knob",
        supported=lambda _config, data: data.get(CONF_HAS_DIAL) is True,
    ),
    HolmanSensorDescription(
        key="rain_sensor",
        name="Rain Sensor",
        value_fn=_rain,
        icon="mdi:weather-rainy",
        enabled_by_default=False,
    ),
    HolmanSensorDescription(
        key="active_station",
        name="Active Station",
        value_fn=lambda info: info.active_station,
        icon="mdi:pipe-valve",
        numeric=True,
        supported=lambda config, _data: config.total_zones > 1,
    ),
    HolmanSensorDescription(
        key="connected_valves",
        name="Connected Valves",
        value_fn=lambda info: info.connected_valves,
        icon="mdi:pipe-valve",
        numeric=True,
        supported=lambda config, _data: config.total_zones > 1,
    ),
    HolmanSensorDescription(
        key="diagnostic_byte",
        name="Diagnostic Byte",
        value_fn=lambda info: info.diagnostic,
        icon="mdi:bug",
        numeric=True,
        enabled_by_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors this device can use, and remove any others.

    Args:
        hass: Home Assistant instance.
        entry: Config entry.
        async_add_entities: Callback to add entities.
    """
    coordinator: HolmanWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    wanted = [
        description
        for description in SENSORS
        if description.supported(coordinator.device_config, dict(entry.data))
    ]
    wanted_keys = {description.key for description in wanted}

    registry = er.async_get(hass)
    for key in (*RETIRED_KEYS, *(d.key for d in SENSORS)):
        if key in wanted_keys:
            continue
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)

    async_add_entities(
        HolmanDiagnosticSensor(coordinator, entry, description)
        for description in wanted
    )


class HolmanDiagnosticSensor(HolmanEntity, SensorEntity):
    """A value read from the device."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: HolmanWaterCoordinator,
        entry: ConfigEntry,
        description: HolmanSensorDescription,
    ) -> None:
        """Initialize the sensor.

        Args:
            coordinator: Device coordinator.
            entry: Config entry.
            description: What the sensor shows.
        """
        super().__init__(coordinator, entry, description.key, description.name)
        self._value_fn = description.value_fn
        self._attr_icon = description.icon
        self._attr_native_unit_of_measurement = description.unit
        self._attr_entity_registry_enabled_default = description.enabled_by_default
        if description.unit == UnitOfElectricPotential.VOLT:
            self._attr_device_class = SensorDeviceClass.VOLTAGE
        if description.numeric:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> Any:
        """Return the value, or None before the device has answered."""
        info = self._coordinator.device_info
        return None if info is None else self._value_fn(info)
