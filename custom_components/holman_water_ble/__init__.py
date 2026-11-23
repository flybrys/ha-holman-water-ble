"""Init for Holman Water BLE integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_HAS_DIAL,
    DEFAULT_DEVICE_NAME,
    DEVICE_TYPE_MAP,
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
    SERVICE_READ_RAW,
    SERVICE_UNPAIR,
)
from .coordinator import HolmanWaterCoordinator
from .models import DeviceConfig
from .passcodes import async_get_passcodes

_LOGGER = logging.getLogger(__name__)


def _device_config_from_type(device_type: int) -> DeviceConfig:
    """Build a DeviceConfig from a device type.

    An unknown type gets a generic one-zone config: no model is assumed until
    the device has reported its type.

    Args:
        device_type: A device type id, 0 if unknown.

    Returns:
        The DeviceConfig for that device type.
    """
    if device_type not in DEVICE_TYPE_MAP:
        return DeviceConfig(model="", name=DEFAULT_DEVICE_NAME, total_zones=1)
    model, name, zones, is_ac = DEVICE_TYPE_MAP[device_type]
    return DeviceConfig(
        model=model, name=name, total_zones=zones, is_ac_device=is_ac
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Holman Water BLE from a config entry.

    Succeeds whether or not the device can be reached: the coordinator keeps
    trying, and the entities are unavailable until it answers.

    Args:
        hass: Home Assistant instance.
        entry: Config entry for this device.

    Returns:
        True.
    """
    mac_address: str = entry.data[CONF_MAC]
    device_type: int = entry.data.get("device_type", 0)
    device_config = _device_config_from_type(device_type)

    coordinator = HolmanWaterCoordinator(
        hass,
        entry,
        mac_address,
        device_config,
        await async_get_passcodes(hass),
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    _register_device(hass, entry, mac_address, device_config, device_type)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def _check_device() -> None:
        _update_device_version(hass, entry, coordinator)
        _self_heal(hass, entry, coordinator)

    coordinator.register_state_update_callback(_check_device)
    _register_services(hass)
    coordinator.async_start()
    return True


def _register_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    mac_address: str,
    device_config: DeviceConfig,
    device_type: int,
) -> None:
    """Create the device registry entry, or update it to the known type.

    Args:
        hass: Home Assistant instance.
        entry: Config entry owning the device.
        mac_address: MAC address of the device.
        device_config: Device configuration (generic when type unknown).
        device_type: Device type stored in the config entry (0 if unknown).
    """
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, mac_address)},
        identifiers={(DOMAIN, mac_address)},
        manufacturer=MANUFACTURER,
        name=entry.data.get("device_name", DEFAULT_DEVICE_NAME),
    )
    if device_type not in DEVICE_TYPE_MAP:
        return
    model_id = str(device_type)
    if device.model != device_config.model or device.model_id != model_id:
        registry.async_update_device(
            device.id,
            name=device_config.name,
            model=device_config.model,
            model_id=model_id,
        )


@callback
def _update_device_version(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: HolmanWaterCoordinator,
) -> None:
    """Show the firmware the device reports on its device page."""
    info = coordinator.device_info
    if info is None or info.firmware_version == 0:
        return
    sw_version = f"{info.firmware_version} (protocol {info.protocol_version})"
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier(
        (DOMAIN, coordinator.mac_address), entry.entry_id
    )
    if device is not None and device.sw_version != sw_version:
        registry.async_update_device(device.id, sw_version=sw_version)


@callback
def _self_heal(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: HolmanWaterCoordinator,
) -> None:
    """Store what the device reports about itself; reload if entities change.

    Covers its device type and whether it has a dial.

    Args:
        hass: Home Assistant instance.
        entry: Config entry for this device.
        coordinator: The coordinator for this device.
    """
    info = coordinator.device_info
    if info is None:
        return
    has_dial = entry.data.get(CONF_HAS_DIAL)
    if has_dial is not info.is_dial_presented:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HAS_DIAL: info.is_dial_presented}
        )
        # Unknown and absent both create no dial sensor.
        if bool(has_dial) != info.is_dial_presented:
            hass.config_entries.async_schedule_reload(entry.entry_id)
            return
    stored_type = entry.data.get("device_type", 0)
    if info.device_type in (0, stored_type):
        return
    if info.device_type not in DEVICE_TYPE_MAP:
        _LOGGER.debug(
            "%s reports unknown device type %d, keeping %d",
            coordinator.mac_address,
            info.device_type,
            stored_type,
        )
        return
    _LOGGER.info(
        "%s corrected from type %d to %d; reloading",
        coordinator.mac_address,
        stored_type,
        info.device_type,
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "device_type": info.device_type}
    )
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry to unload.

    Returns:
        True if unload was successful.
    """
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    coordinator: HolmanWaterCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    coordinator.async_shutdown()
    if not hass.data[DOMAIN]:
        for service in (SERVICE_UNPAIR, SERVICE_READ_RAW):
            hass.services.async_remove(DOMAIN, service)
    return True


@callback
def _register_services(hass: HomeAssistant) -> None:
    """Register the integration's services once."""
    if hass.services.has_service(DOMAIN, SERVICE_UNPAIR):
        return

    def find(call: ServiceCall) -> HolmanWaterCoordinator:
        """Return the coordinator of the device a call names."""
        mac_address = str(call.data.get("mac_address", "")).upper()
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if coordinator.mac_address.upper() == mac_address:
                return coordinator
        raise ServiceValidationError(f"No Holman device {mac_address or '(none)'}")

    async def handle_unpair(call: ServiceCall) -> None:
        """Handle the unpair service call."""
        await find(call).async_unpair()

    async def handle_read_raw(call: ServiceCall) -> ServiceResponse:
        """Handle the read_raw service call."""
        return {"characteristics": await find(call).async_read_raw()}

    hass.services.async_register(DOMAIN, SERVICE_UNPAIR, handle_unpair)
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_RAW,
        handle_read_raw,
        supports_response=SupportsResponse.ONLY,
    )
