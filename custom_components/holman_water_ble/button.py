"""Button platform for Holman Water BLE actions."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HolmanWaterCoordinator
from .entity import HolmanEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up action button entities.

    Args:
        hass: Home Assistant instance.
        entry: Config entry.
        async_add_entities: Callback to add entities.
    """
    coordinator: HolmanWaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HolmanReadDiagnosticsButton(coordinator, entry),
            HolmanClearPairingButton(coordinator, entry),
            HolmanPairButton(coordinator, entry),
        ]
    )


class HolmanReadDiagnosticsButton(HolmanEntity, ButtonEntity):
    """Reads the device status now."""

    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: HolmanWaterCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry, "read_diagnostics", "Read Diagnostics")

    @property
    def available(self) -> bool:
        """Usable while unreachable too, to retry by hand."""
        return self._coordinator.paired

    async def async_press(self) -> None:
        """Read the status."""
        await self._coordinator.async_refresh()


class HolmanClearPairingButton(HolmanEntity, ButtonEntity):
    """Clears the device passcode."""

    _attr_icon = "mdi:link-off"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: HolmanWaterCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry, "clear_pairing", "Clear Pairing")

    @property
    def available(self) -> bool:
        """Only available when the device is paired."""
        return self._coordinator.paired

    async def async_press(self) -> None:
        """Clear the pairing."""
        await self._coordinator.async_unpair()


class HolmanPairButton(HolmanEntity, ButtonEntity):
    """Pairs an unpaired device."""

    _attr_icon = "mdi:link-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: HolmanWaterCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry, "pair", "Pair")

    @property
    def available(self) -> bool:
        """Only available when the device is not paired."""
        return not self._coordinator.paired

    async def async_press(self) -> None:
        """Pair the device."""
        await self._coordinator.async_pair()
