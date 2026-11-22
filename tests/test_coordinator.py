"""Tests for the Holman Water BLE coordinator, against a simulated tap."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from custom_components.holman_water_ble import coordinator as coordinator_module
from custom_components.holman_water_ble.const import (
    CHAR_UUID_COMMAND,
    CHAR_UUID_MAC,
    CHAR_UUID_PASSCODE,
    CHAR_UUID_WATERING,
    PASSCODE_FILENAME,
)
from custom_components.holman_water_ble.coordinator import HolmanWaterCoordinator
from custom_components.holman_water_ble.errors import (
    HolmanCommandError,
    HolmanConnectionError,
)
from custom_components.holman_water_ble.models import DeviceConfig
from custom_components.holman_water_ble.passcodes import (
    PasscodeManager,
    async_get_passcodes,
)

from .fake_tap import BROKEN, MAC, SERIAL, FakeTap

BX1 = DeviceConfig(model="BX1", name="BX1 Bluetooth Tap Timer", total_zones=1)
BX2 = DeviceConfig(model="BX2", name="BX2 Bluetooth Tap Timer", total_zones=2)


class Timers:
    """Captures what is scheduled instead of waiting for it."""

    def __init__(self) -> None:
        """Start with nothing scheduled."""
        self.delay: float | None = None
        self.action = None

    def call_later(self, _hass, delay, action):
        """Stand in for async_call_later."""
        self.delay = delay
        self.action = action

        def _cancel() -> None:
            if self.action is action:
                self.delay = None
                self.action = None

        return _cancel

    async def fire(self) -> None:
        """Run what is scheduled, and let it finish."""
        action = self.action
        self.delay = None
        self.action = None
        action(dt_util.utcnow())
        await asyncio.sleep(0)
        for task in [t for t in asyncio.all_tasks() if t.get_name() == "refresh"]:
            await task


@pytest_asyncio.fixture
async def hass(tmp_path):
    """Create a Home Assistant core object."""
    instance = HomeAssistant(str(tmp_path))
    yield instance
    await instance.async_stop(force=True)


@pytest.fixture
def tap():
    """Create the simulated tap."""
    return FakeTap()


@pytest.fixture
def timers():
    """Create the captured scheduler."""
    return Timers()


@pytest.fixture
def bt(tap, timers):
    """Replace Bluetooth, the scheduler and the read-back delay."""
    state = SimpleNamespace(device=MagicMock(address=MAC), heard=True, callback=None)

    def _register(_hass, callback, _matcher, _mode):
        state.callback = callback
        return lambda: None

    with (
        patch.object(coordinator_module, "bluetooth") as bluetooth,
        patch.object(coordinator_module, "async_call_later", timers.call_later),
        patch.object(coordinator_module, "VERIFY_DELAY_SECONDS", 0),
        patch(
            "custom_components.holman_water_ble.holman_ble.establish_connection",
            tap.connect,
        ),
    ):
        bluetooth.async_ble_device_from_address = lambda *a, **k: state.device
        bluetooth.async_address_present = lambda *a, **k: state.heard
        bluetooth.async_last_service_info = lambda *a, **k: SimpleNamespace(
            source="kiosk", rssi=-97
        )
        bluetooth.async_register_callback = _register
        yield state


def _entry():
    entry = MagicMock()
    entry.async_create_background_task = lambda _hass, coro, _name: (
        asyncio.get_running_loop().create_task(coro, name="refresh")
    )
    return entry


@pytest_asyncio.fixture
async def coordinator(hass, bt):
    """Create a paired BX1 coordinator."""
    passcodes = await async_get_passcodes(hass)
    await passcodes.async_set(MAC, 12345)
    return HolmanWaterCoordinator(hass, _entry(), MAC, BX1, passcodes)


@pytest.mark.asyncio
class TestRunTime:
    """Issue: runs of an hour or more were ignored by the tap."""

    @pytest.mark.parametrize("minutes", [1, 40, 59, 60, 61, 90, 120, 240])
    async def test_starts_for_the_whole_run_time(self, coordinator, tap, minutes):
        """The tap waters for exactly the requested time."""
        coordinator.set_watering_duration(1, minutes)
        await coordinator.async_start_watering(1)
        assert tap.watering
        assert tap.minutes == minutes
        assert coordinator.is_watering(1)


@pytest.mark.asyncio
class TestStatus:
    """State follows the device."""

    async def test_unknown_until_the_device_answers(self, coordinator):
        """A fresh coordinator does not claim the tap is available."""
        assert coordinator.paired
        assert not coordinator.available

    async def test_reading_status_changes_nothing(self, coordinator, tap):
        """A status read must not write schedules or the clock."""
        tap.watering = True
        await coordinator.async_refresh()
        assert tap.writes == ["passcode"]
        assert tap.watering
        assert coordinator.available

    async def test_clock_set_only_when_idle_and_wrong(self, coordinator, tap):
        """The clock is corrected when clearly wrong, never during a run."""
        tap.clock = dt_util.now() - timedelta(hours=1)
        tap.watering = True
        await coordinator.async_refresh()
        assert "time" not in tap.writes
        tap.watering = False
        await coordinator.async_refresh()
        assert "time" in tap.writes
        assert "schedule" not in tap.writes

    async def test_run_started_at_the_tap_is_shown(self, coordinator, tap, timers):
        """Watering started elsewhere shows, and is checked again."""
        tap.watering = True
        await coordinator.async_refresh()
        assert coordinator.is_watering(1)
        assert coordinator.get_watering_end_time(1) is None
        assert timers.delay == 15 * 60

    async def test_run_stopped_at_the_tap_is_shown(self, coordinator, tap):
        """A run ended elsewhere no longer shows."""
        await coordinator.async_start_watering(1)
        tap.watering = False
        await coordinator.async_refresh()
        assert not coordinator.is_watering(1)

    async def test_unreadable_status_is_a_failure(self, coordinator, tap):
        """Connecting without getting a status is not success."""
        tap.responds = False
        with pytest.raises(HolmanConnectionError):
            await coordinator.async_refresh()
        assert not coordinator.available


@pytest.mark.asyncio
class TestCommands:
    """Commands are confirmed, and fail loudly."""

    async def test_start_ignored_by_the_tap_raises(self, coordinator, tap):
        """An acknowledged but ignored start is an error, not on."""
        tap.obeys = False
        with pytest.raises(HolmanCommandError, match="is not watering"):
            await coordinator.async_start_watering(1)
        assert not coordinator.is_watering(1)
        assert coordinator.available

    async def test_start_unreachable_raises(self, coordinator, tap):
        """A start that cannot connect is an error."""
        tap.reachable = False
        with pytest.raises(HomeAssistantError):
            await coordinator.async_start_watering(1)
        assert not coordinator.is_watering(1)
        assert not coordinator.available

    async def test_failed_start_keeps_an_earlier_run(self, coordinator, tap):
        """A start that cannot connect does not hide a run in progress."""
        await coordinator.async_start_watering(1)
        tap.reachable = False
        with pytest.raises(HolmanConnectionError):
            await coordinator.async_start_watering(1)
        assert coordinator.is_watering(1)

    async def test_stop(self, coordinator, tap):
        """A stop is confirmed."""
        await coordinator.async_start_watering(1)
        await coordinator.async_stop_watering(1)
        assert not tap.watering
        assert not coordinator.is_watering(1)

    async def test_stop_ignored_by_the_tap_raises(self, coordinator, tap):
        """A valve that stays open is still shown as open."""
        await coordinator.async_start_watering(1)
        tap.obeys = False
        with pytest.raises(HolmanCommandError, match="still watering"):
            await coordinator.async_stop_watering(1)
        assert coordinator.is_watering(1)

    async def test_stop_unreachable_stays_on(self, coordinator, tap):
        """A stop that cannot connect does not claim the valve is off."""
        await coordinator.async_start_watering(1)
        tap.reachable = False
        with pytest.raises(HolmanConnectionError):
            await coordinator.async_stop_watering(1)
        assert coordinator.is_watering(1)
        assert not coordinator.available

    async def test_rejected_passcode_raises(self, coordinator, tap):
        """A failed passcode write is not treated as success."""
        tap.accepts_passcode = False
        with pytest.raises(HolmanConnectionError, match="passcode"):
            await coordinator.async_start_watering(1)
        assert "start" not in tap.writes

    async def test_every_connection_is_closed(self, coordinator, tap):
        """No connection slot is left in use, whatever happens."""
        await coordinator.async_start_watering(1)
        tap.obeys = False
        with pytest.raises(HolmanCommandError):
            await coordinator.async_stop_watering(1)
        assert tap.connections == tap.disconnections
        assert not tap.is_connected

    async def test_two_zones_stop_independently(self, hass, bt, tap):
        """Stopping one of two zones leaves the other on."""
        passcodes = await async_get_passcodes(hass)
        await passcodes.async_set(MAC, 1)
        coordinator = HolmanWaterCoordinator(hass, _entry(), MAC, BX2, passcodes)
        await coordinator.async_start_watering(1)
        await coordinator.async_start_watering(2)
        with patch.object(FakeTap, "write_gatt_char", _ignore_stop):
            await coordinator.async_stop_watering(1)
        assert not coordinator.is_watering(1)
        assert coordinator.is_watering(2)


async def _ignore_stop(self, uuid, data, response=False):
    """Keep reporting watering, as a tap with another zone on does."""


@pytest.mark.asyncio
class TestSchedule:
    """When the tap is contacted."""

    async def test_run_is_left_alone_until_it_should_be_over(
        self, coordinator, timers
    ):
        """A known run is not polled while it lasts."""
        coordinator.set_watering_duration(1, 60)
        coordinator.set_poll_interval_hours(1)
        await coordinator.async_start_watering(1)
        assert 3600 < timers.delay <= 3631

    async def test_end_of_run(self, coordinator, tap, timers):
        """When the run is over the switch turns off, from the status."""
        await coordinator.async_start_watering(1)
        tap.watering = False
        await timers.fire()
        assert not coordinator.is_watering(1)
        assert timers.delay == 4 * 3600
        assert "stop" not in tap.writes

    async def test_overdue_run_is_stopped(self, coordinator, tap, timers):
        """A run that outlives its time is stopped."""
        await coordinator.async_start_watering(1)
        coordinator._watering[1] = dt_util.utcnow() - timedelta(minutes=1)
        await timers.fire()
        assert not tap.watering
        assert not coordinator.is_watering(1)

    async def test_overdue_run_that_will_not_stop(self, coordinator, tap, timers):
        """A tap that will not stop is not contacted in a tight loop."""
        await coordinator.async_start_watering(1)
        coordinator._watering[1] = dt_util.utcnow() - timedelta(minutes=1)
        tap.obeys = False
        await timers.fire()
        assert coordinator.is_watering(1)
        assert timers.delay == 15 * 60

    async def test_end_of_run_unreachable(self, coordinator, tap, timers):
        """An unreachable tap is not assumed to have stopped."""
        await coordinator.async_start_watering(1)
        tap.reachable = False
        await timers.fire()
        assert coordinator.is_watering(1)
        assert not coordinator.available
        assert timers.delay == 60

    async def test_retries_back_off(self, coordinator, tap, timers):
        """An unreachable tap is retried, less and less often."""
        tap.reachable = False
        coordinator.async_start()
        delays = []
        for _ in range(6):
            await timers.fire()
            delays.append(timers.delay)
        assert delays == [60, 120, 300, 600, 900, 900]
        tap.reachable = True
        await timers.fire()
        assert coordinator.available
        assert timers.delay == 4 * 3600

    async def test_poll_interval_change_does_not_stop_contact(
        self, coordinator, timers
    ):
        """Changing the poll interval must not shut the coordinator down."""
        await coordinator.async_refresh()
        coordinator.set_poll_interval_hours(18)
        assert timers.delay == 18 * 3600
        await coordinator.async_start_watering(1)
        assert coordinator.is_watering(1)

    async def test_unload_stops_everything(self, coordinator, tap, timers):
        """After unload nothing is scheduled and nothing connects."""
        await coordinator.async_refresh()
        coordinator.async_shutdown()
        assert timers.action is None
        with pytest.raises(HomeAssistantError):
            await coordinator.async_refresh()
        assert tap.connections == 1


@pytest.mark.asyncio
class TestPaths:
    """Finding a path to the tap."""

    async def test_no_connectable_path_is_explained(self, coordinator, bt, tap):
        """Heard only by scanners that cannot connect."""
        bt.device = None
        with pytest.raises(HolmanConnectionError, match="cannot make connections"):
            await coordinator.async_refresh()
        assert tap.connections == 0
        assert not coordinator.available

    async def test_not_heard_is_explained(self, coordinator, bt):
        """Not heard at all."""
        bt.device = None
        bt.heard = False
        with pytest.raises(HolmanConnectionError, match="not heard by any"):
            await coordinator.async_refresh()

    async def _fail_once(self, coordinator, tap):
        coordinator.async_start()
        tap.reachable = False
        with pytest.raises(HolmanConnectionError):
            await coordinator.async_refresh()
        coordinator._last_attempt -= timedelta(seconds=61)

    @pytest.mark.parametrize(
        ("source", "rssi", "retried"),
        [
            ("kiosk", -96, False),
            ("kiosk", -80, True),
            ("hallway", -87, True),
        ],
    )
    async def test_new_path_triggers_a_retry(
        self, coordinator, bt, tap, timers, source, rssi, retried
    ):
        """A new scanner, or a much better signal, is tried at once."""
        await self._fail_once(coordinator, tap)
        bt.callback(SimpleNamespace(source=source, rssi=rssi), None)
        assert (timers.delay == 0) is retried

    async def test_retry_is_rate_limited(self, coordinator, bt, tap, timers):
        """Advertisements cannot cause constant connection attempts."""
        await self._fail_once(coordinator, tap)
        coordinator._last_attempt = dt_util.utcnow()
        bt.callback(SimpleNamespace(source="hallway", rssi=-60), None)
        assert timers.delay == 60

    async def test_advertisements_ignored_when_reachable(
        self, coordinator, bt, tap, timers
    ):
        """A reachable tap keeps its normal schedule."""
        coordinator.async_start()
        await coordinator.async_refresh()
        bt.callback(SimpleNamespace(source="hallway", rssi=-60), None)
        assert timers.delay == 4 * 3600


@pytest.mark.asyncio
class TestPairing:
    """Pairing and passcodes."""

    async def test_pair(self, hass, bt, tap, tmp_path):
        """Pairing stores a passcode, and clears the tap's schedules once."""
        coordinator = HolmanWaterCoordinator(
            hass, _entry(), MAC, BX1, await async_get_passcodes(hass)
        )
        assert not coordinator.paired
        await coordinator.async_pair()
        assert coordinator.available
        assert tap.writes == ["passcode", "schedule", "time"]
        saved = json.loads((tmp_path / PASSCODE_FILENAME).read_text())
        assert saved == {MAC: tap.passcode}

    async def test_pair_unreachable(self, hass, bt, tap):
        """A failed pairing stores nothing."""
        tap.reachable = False
        coordinator = HolmanWaterCoordinator(
            hass, _entry(), MAC, BX1, await async_get_passcodes(hass)
        )
        with pytest.raises(HolmanConnectionError):
            await coordinator.async_pair()
        assert not coordinator.paired

    async def test_unpair(self, coordinator, tap):
        """Unpairing clears both sides."""
        await coordinator.async_unpair()
        assert tap.passcode == 0
        assert not coordinator.paired
        assert not coordinator.available

    async def test_unpair_unreachable_keeps_the_passcode(self, coordinator, tap):
        """A failed unpair leaves the pairing usable."""
        tap.reachable = False
        with pytest.raises(HolmanConnectionError):
            await coordinator.async_unpair()
        assert coordinator.paired

    async def test_unpaired_device_is_not_contacted(self, hass, bt, tap, timers):
        """Nothing is scheduled or sent before pairing."""
        coordinator = HolmanWaterCoordinator(
            hass, _entry(), MAC, BX1, await async_get_passcodes(hass)
        )
        coordinator.async_start()
        assert timers.action is None
        with pytest.raises(HolmanCommandError, match="not paired"):
            await coordinator.async_start_watering(1)
        assert tap.connections == 0

    async def test_devices_share_one_store(self, hass, tmp_path):
        """Saving one device's passcode keeps the other's newer one."""
        first = await async_get_passcodes(hass)
        second = await async_get_passcodes(hass)
        assert first is second
        await first.async_set("A", 1)
        await second.async_set("B", 2)
        await first.async_set("A", 3)
        saved = json.loads((tmp_path / PASSCODE_FILENAME).read_text())
        assert saved == {"A": 3, "B": 2}

    async def test_existing_file_is_used(self, hass, tmp_path):
        """Passcodes saved by earlier versions still work."""
        (tmp_path / PASSCODE_FILENAME).write_text(json.dumps({MAC: 777}))
        manager = PasscodeManager(hass)
        await manager.async_load()
        assert manager.get(MAC) == 777


@pytest.mark.asyncio
class TestReadRaw:
    """Reading what the tap stores."""

    async def test_reads_without_writing(self, coordinator, tap):
        """Only the login is written, and every entry is reported."""
        data = await coordinator.async_read_raw()
        assert tap.writes == ["passcode"]
        by_uuid = {entry["uuid"].upper(): entry for entry in data}
        assert len(by_uuid) == 7
        assert by_uuid[CHAR_UUID_WATERING]["value"] == "00 00 00 0c 00 00 00 00 00 00"
        assert by_uuid[CHAR_UUID_COMMAND]["note"] == "not readable"
        assert by_uuid[BROKEN]["note"] == "read failed: not permitted"
        assert coordinator.available

    async def test_private_data_is_left_out(self, coordinator, tap):
        """Neither the passcode nor the MAC address is returned."""
        data = await coordinator.async_read_raw()
        by_uuid = {entry["uuid"].upper(): entry for entry in data}
        for uuid in (CHAR_UUID_PASSCODE, CHAR_UUID_MAC, SERIAL):
            assert "value" not in by_uuid[uuid]
        assert CHAR_UUID_MAC not in tap.reads
        # The login reads the passcode once; the dump must not.
        assert tap.reads.count(CHAR_UUID_PASSCODE) == 1

    async def test_refused_while_watering(self, coordinator, tap):
        """A run in progress is left alone."""
        await coordinator.async_start_watering(1)
        connections = tap.connections
        with pytest.raises(HolmanCommandError, match="is watering"):
            await coordinator.async_read_raw()
        assert tap.connections == connections
        assert tap.watering


@pytest.mark.asyncio
async def test_update_callbacks(coordinator):
    """Entities are told about changes until they unregister."""
    calls = []

    def _update() -> None:
        calls.append(1)

    coordinator.register_state_update_callback(_update)
    await coordinator.async_refresh()
    assert calls
    coordinator.unregister_state_update_callback(_update)
    calls.clear()
    await coordinator.async_refresh()
    assert not calls
