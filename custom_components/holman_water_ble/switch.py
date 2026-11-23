"""Switch platform for Holman Water BLE valves."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HolmanWaterCoordinator
from .entity import HolmanEntity, zone_name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up valve switch entities.

    Args:
        hass: Home Assistant instance.
        entry: Config entry.
        async_add_entities: Callback to add entities.
    """
    coordinator: HolmanWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HolmanValveSwitch(coordinator, entry, zone)
        for zone in range(1, coordinator.device_config.total_zones + 1)
    )


class HolmanValveSwitch(HolmanEntity, SwitchEntity):
    """A valve. Follows what the device reports, not what was asked of it."""

    _attr_icon = "mdi:valve"

    def __init__(
        self,
        coordinator: HolmanWaterCoordinator,
        entry: ConfigEntry,
        zone: int,
    ) -> None:
        """Initialize the valve switch.

        Args:
            coordinator: Device coordinator.
            entry: Config entry.
            zone: 1-based zone number.
        """
        super().__init__(
            coordinator, entry, f"valve_{zone}", zone_name(coordinator, zone, "")
        )
        self._zone = zone

    @property
    def is_on(self) -> bool:
        """Return whether the valve is watering."""
        return self._coordinator.is_watering(self._zone)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start watering. Raises if the device does not start."""
        await self._coordinator.async_start_watering(self._zone)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop watering. Raises if the device does not stop."""
        await self._coordinator.async_stop_watering(self._zone)
