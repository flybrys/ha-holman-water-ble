"""Number platform for Holman Water BLE settings."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MAX_POLL_INTERVAL_HOURS,
    MAX_WATERING_DURATION_MINUTES,
    MIN_POLL_INTERVAL_HOURS,
    MIN_WATERING_DURATION_MINUTES,
)
from .coordinator import HolmanWaterCoordinator
from .entity import HolmanEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities.

    Args:
        hass: Home Assistant instance.
        entry: Config entry.
        async_add_entities: Callback to add entities.
    """
    coordinator: HolmanWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[HolmanSetting] = [
        HolmanWateringDuration(coordinator, entry, zone)
        for zone in range(1, coordinator.device_config.total_zones + 1)
    ]
    entities.append(HolmanPollInterval(coordinator, entry))
    async_add_entities(entities)


class HolmanSetting(HolmanEntity, RestoreNumber):
    """A setting kept by Home Assistant and restored after a restart."""

    _attr_native_step = 1.0
    _attr_mode = NumberMode.SLIDER

    @property
    def available(self) -> bool:
        """Settings live in Home Assistant, so the device is not needed."""
        return True

    async def async_added_to_hass(self) -> None:
        """Restore the last value."""
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is None or last.native_value is None:
            return
        if self.native_min_value <= last.native_value <= self.native_max_value:
            self._apply(int(last.native_value))

    async def async_set_native_value(self, value: float) -> None:
        """Change the setting."""
        self._apply(int(value))
        self.async_write_ha_state()

    def _apply(self, value: int) -> None:
        """Hand a value to the coordinator."""
        raise NotImplementedError


class HolmanWateringDuration(HolmanSetting):
    """Run time used the next time a valve is turned on."""

    _attr_native_min_value = MIN_WATERING_DURATION_MINUTES
    _attr_native_max_value = MAX_WATERING_DURATION_MINUTES
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-cog-outline"

    def __init__(
        self,
        coordinator: HolmanWaterCoordinator,
        entry: ConfigEntry,
        zone: int,
    ) -> None:
        """Initialize the watering duration number.

        Args:
            coordinator: Device coordinator.
            entry: Config entry.
            zone: 1-based zone number.
        """
        super().__init__(
            coordinator, entry, f"duration_{zone}", f"Valve {zone} Duration"
        )
        self._zone = zone

    @property
    def native_value(self) -> float:
        """Return the run time in minutes."""
        return float(self._coordinator.get_watering_duration(self._zone))

    def _apply(self, value: int) -> None:
        """Set the run time."""
        self._coordinator.set_watering_duration(self._zone, value)


class HolmanPollInterval(HolmanSetting):
    """How often the status is read while the device is idle."""

    _attr_native_min_value = MIN_POLL_INTERVAL_HOURS
    _attr_native_max_value = MAX_POLL_INTERVAL_HOURS
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_icon = "mdi:calendar-sync"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: HolmanWaterCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the poll interval number.

        Args:
            coordinator: Device coordinator.
            entry: Config entry.
        """
        super().__init__(coordinator, entry, "poll_interval", "Poll Interval")

    @property
    def native_value(self) -> float:
        """Return the poll interval in hours."""
        return float(self._coordinator.poll_interval_hours)

    def _apply(self, value: int) -> None:
        """Set the poll interval."""
        self._coordinator.set_poll_interval_hours(value)
