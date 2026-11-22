"""Load the integration through Home Assistant's own config entry setup."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from homeassistant import config_entries, loader
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry,
    category_registry,
    device_registry,
    entity,
    entity_registry,
    floor_registry,
    frame,
    issue_registry,
    label_registry,
    restore_state,
    translation,
)

from custom_components.holman_water_ble import coordinator as coordinator_module
from custom_components.holman_water_ble.const import DOMAIN, PASSCODE_FILENAME
from custom_components.holman_water_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .fake_tap import MAC, FakeTap

VALVE = "switch.bx1_bluetooth_tap_timer_valve_1"
DURATION = "number.bx1_bluetooth_tap_timer_valve_1_duration"
POLL = "number.bx1_bluetooth_tap_timer_poll_interval"
BATTERY = "sensor.bx1_bluetooth_tap_timer_battery_voltage"
WATERING = "sensor.bx1_bluetooth_tap_timer_watering_status"
READ = "button.bx1_bluetooth_tap_timer_read_diagnostics"


@pytest.fixture
def tap():
    """Create the simulated tap."""
    return FakeTap()


@pytest_asyncio.fixture
async def hass(tmp_path, tap):
    """Create a Home Assistant able to set up config entries."""
    source = Path(__file__).parent.parent / "custom_components"
    shutil.copytree(
        source,
        tmp_path / "custom_components",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tmp_path / PASSCODE_FILENAME).write_text(json.dumps({MAC: 12345}))

    instance = HomeAssistant(str(tmp_path))
    instance.config.skip_pip = True
    instance.config.components.update({"bluetooth", "bluetooth_adapters"})
    instance.config_entries = config_entries.ConfigEntries(instance, {})
    loader.async_setup(instance)
    entity.async_setup(instance)
    frame.async_setup(instance)
    translation.async_setup(instance)
    if hasattr(device_registry, "async_setup"):
        device_registry.async_setup(instance)
    # Together, as Home Assistant does: the registries wait for each other.
    await asyncio.gather(
        *(
            registry.async_load(instance)
            for registry in (
                area_registry,
                category_registry,
                device_registry,
                entity_registry,
                floor_registry,
                issue_registry,
                label_registry,
                restore_state,
            )
        ),
        instance.config_entries.async_initialize(),
    )
    instance.set_state(CoreState.running)

    with (
        patch.object(coordinator_module, "bluetooth") as bluetooth,
        patch.object(coordinator_module, "VERIFY_DELAY_SECONDS", 0),
        patch(
            "custom_components.holman_water_ble.holman_ble.establish_connection",
            tap.connect,
        ),
    ):
        bluetooth.async_ble_device_from_address = lambda *a, **k: MagicMock(address=MAC)
        bluetooth.async_last_service_info = lambda *a, **k: SimpleNamespace(
            source="kiosk", rssi=-97
        )
        bluetooth.async_register_callback = lambda *a: lambda: None
        yield instance
        await instance.async_stop(force=True)


def _make_entry(**kwargs) -> ConfigEntry:
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="BX1",
        data={"mac": MAC, "device_name": "BX1", "device_type": 100, **kwargs},
        source="bluetooth",
        unique_id=MAC,
        options={},
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )


async def _set_up(hass: HomeAssistant, **kwargs) -> ConfigEntry:
    entry = _make_entry(**kwargs)
    await hass.config_entries.async_add(entry)
    await _settle(hass)
    return entry


async def _settle(hass: HomeAssistant) -> None:
    """Let timers that are due, and the work they start, finish."""
    for _ in range(3):
        await hass.async_block_till_done(wait_background_tasks=True)


async def _call(hass: HomeAssistant, domain: str, service: str, entity_id, **data):
    await hass.services.async_call(
        domain, service, {"entity_id": entity_id, **data}, blocking=True
    )


@pytest.mark.asyncio
async def test_setup_reads_status(hass, tap):
    """Setting up reads the status and leaves the tap as it was."""
    entry = await _set_up(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(VALVE).state == "off"
    assert hass.states.get(BATTERY).state == "6.1"
    assert hass.states.get(WATERING).state == "Off"
    assert tap.writes == ["passcode"]


@pytest.mark.asyncio
async def test_unique_ids_are_unchanged(hass):
    """Existing installations keep their entities."""
    entry = await _set_up(hass)
    registry = entity_registry.async_get(hass)
    ids = {
        e.unique_id.removeprefix(entry.entry_id)
        for e in entity_registry.async_entries_for_config_entry(
            registry, entry.entry_id
        )
    }
    assert ids == {
        "_valve_1",
        "_duration_1",
        "_poll_interval",
        "_read_diagnostics",
        "_clear_pairing",
        "_pair",
        "_battery_voltage",
        "_voltage_dc",
        "_voltage_ac",
        "_voltage_valve",
        "_dial_position",
        "_watering_status",
        "_rain_sensor",
        "_active_station",
        "_connected_valves",
        "_firmware_version",
        "_protocol_version",
        "_diagnostic_byte",
    }


@pytest.mark.asyncio
async def test_unreachable_at_startup(hass, tap):
    """The entry loads, says unavailable, and recovers without a reload."""
    tap.reachable = False
    entry = await _set_up(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(VALVE).state == "unavailable"
    assert hass.states.get(DURATION).state == "10.0"
    tap.reachable = True
    await _call(hass, "button", "press", READ)
    assert hass.states.get(VALVE).state == "off"


@pytest.mark.asyncio
async def test_settings_are_restored(hass, tap):
    """Run time and poll interval survive a reload, and the run time is used."""
    entry = await _set_up(hass)
    await _call(hass, "number", "set_value", DURATION, value=75)
    await _call(hass, "number", "set_value", POLL, value=6)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    assert hass.states.get(DURATION).state == "75.0"
    assert hass.states.get(POLL).state == "6.0"
    await _call(hass, "switch", "turn_on", VALVE)
    assert tap.minutes == 75


@pytest.mark.asyncio
async def test_run_from_services(hass, tap):
    """Set a run time over an hour, turn on, turn off."""
    await _set_up(hass)
    await _call(hass, "number", "set_value", DURATION, value=61)
    await _call(hass, "switch", "turn_on", VALVE)
    assert tap.minutes == 61
    assert hass.states.get(VALVE).state == "on"
    assert hass.states.get(WATERING).state == "On"
    await _call(hass, "switch", "turn_off", VALVE)
    assert not tap.watering
    assert hass.states.get(VALVE).state == "off"


@pytest.mark.asyncio
async def test_failed_command_reaches_the_caller(hass, tap):
    """An automation finds out that the valve did not open."""
    await _set_up(hass)
    tap.obeys = False
    with pytest.raises(HomeAssistantError, match="is not watering"):
        await _call(hass, "switch", "turn_on", VALVE)
    assert hass.states.get(VALVE).state == "off"


@pytest.mark.asyncio
async def test_unload_and_reload(hass, tap):
    """Reloading during a run keeps the run, and shows it."""
    entry = await _set_up(hass)
    await _call(hass, "switch", "turn_on", VALVE)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not hass.services.has_service(DOMAIN, "unpair")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    assert tap.watering
    assert hass.states.get(VALVE).state == "on"


@pytest.mark.asyncio
async def test_unknown_type_is_corrected(hass):
    """An entry created without a type takes it from the device."""
    entry = await _set_up(hass, device_type=0)
    assert entry.data["device_type"] == 100
    assert entry.state is ConfigEntryState.LOADED
    device = device_registry.async_entries_for_config_entry(
        device_registry.async_get(hass), entry.entry_id
    )[0]
    assert device.model == "BX1"


@pytest.mark.asyncio
async def test_diagnostics_and_unpair_service(hass):
    """Diagnostics work, and the service finds the device by address."""
    entry = await _set_up(hass)
    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert dump["device"]["available"] is True
    assert dump["device_registry"]["model"] == "BX1"
    assert MAC not in json.dumps(dump)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "unpair", {"mac_address": "00:00"}, blocking=True
        )
    await hass.services.async_call(
        DOMAIN, "unpair", {"mac_address": MAC.lower()}, blocking=True
    )
    assert hass.states.get(VALVE).state == "unavailable"
