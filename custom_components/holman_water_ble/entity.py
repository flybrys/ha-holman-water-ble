"""Shared base for Holman Water BLE entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HolmanWaterCoordinator


class HolmanEntity(Entity):
    """An entity whose state is pushed by the coordinator."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: HolmanWaterCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
    ) -> None:
        """Initialize the entity.

        Args:
            coordinator: Device coordinator.
            entry: Config entry.
            key: Unique key suffix.
            name: Display name, without the device name.
        """
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = f"{coordinator.device_config.name} {name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.mac_address)},
            name=coordinator.device_config.name,
            manufacturer=MANUFACTURER,
            model=coordinator.device_config.model or None,
        )

    async def async_added_to_hass(self) -> None:
        """Follow the coordinator for as long as the entity exists."""
        await super().async_added_to_hass()
        self._coordinator.register_state_update_callback(self._on_update)
        self.async_on_remove(
            lambda: self._coordinator.unregister_state_update_callback(
                self._on_update
            )
        )

    @property
    def available(self) -> bool:
        """Return whether the device is paired and reachable."""
        return self._coordinator.available

    def _on_update(self) -> None:
        """Write the new state after a coordinator update."""
        self.async_write_ha_state()
