"""Coordinator for Holman Water BLE device operations.

Connects on demand, runs one operation at a time, and keeps the state Home
Assistant shows tied to what the device itself reports.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Optional

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    CLOCK_DRIFT_LIMIT_SECONDS,
    DEFAULT_POLL_INTERVAL_HOURS,
    DEFAULT_WATERING_DURATION_MINUTES,
    PATH_RETRY_MIN_SECONDS,
    PATH_RETRY_RSSI_GAIN,
    RETRY_DELAYS_SECONDS,
    UNKNOWN_RUN_RECHECK_SECONDS,
    VERIFY_ATTEMPTS,
    VERIFY_DELAY_SECONDS,
    WATERING_COMPLETION_MARGIN_SECONDS,
)
from .errors import HolmanCommandError, HolmanConnectionError, HolmanError
from .holman_ble import HolmanBLE
from .models import DeviceConfig, DeviceInfo
from .passcodes import PasscodeManager

_LOGGER = logging.getLogger(__name__)


class HolmanWaterCoordinator:
    """Coordinator for a single Holman Water BLE device."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        mac_address: str,
        device_config: DeviceConfig,
        passcodes: PasscodeManager,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance.
            entry: Config entry for this device.
            mac_address: Address of the device.
            device_config: Device configuration (model, zones, etc.).
            passcodes: Passcode storage shared by all devices.
        """
        self._hass = hass
        self._entry = entry
        self._mac_address = mac_address
        self._device_config = device_config
        self._passcodes = passcodes

        self._lock = asyncio.Lock()
        self._shutdown = False
        self._device_info: Optional[DeviceInfo] = None
        # Nothing is known until the device has answered once.
        self._reachable = False
        self._poll_interval_hours: float = DEFAULT_POLL_INTERVAL_HOURS
        self._watering_durations = {
            zone: DEFAULT_WATERING_DURATION_MINUTES
            for zone in range(1, device_config.total_zones + 1)
        }
        # Zones the device is watering -> expected end. None when the run was
        # not started from Home Assistant, so its end is not known.
        self._watering: dict[int, Optional[datetime]] = {}

        self._failures = 0
        self._last_attempt: Optional[datetime] = None
        self._last_path: Optional[tuple[str, int]] = None
        self._cancel_refresh: Optional[CALLBACK_TYPE] = None
        self._cancel_advertisements: Optional[CALLBACK_TYPE] = None
        self._state_update_callbacks: list[Callable[[], None]] = []

    # ---------------------------------------------------------------- state

    @property
    def device_config(self) -> DeviceConfig:
        """Get the device configuration."""
        return self._device_config

    @property
    def device_info(self) -> Optional[DeviceInfo]:
        """Get the last status read from the device."""
        return self._device_info

    @property
    def mac_address(self) -> str:
        """Get the device MAC address."""
        return self._mac_address

    @property
    def available(self) -> bool:
        """Whether the device is paired and answered the last attempt."""
        return self._reachable and self.paired

    @property
    def paired(self) -> bool:
        """Whether a passcode is stored for this device."""
        return self._passcodes.get(self._mac_address) is not None

    @property
    def poll_interval_hours(self) -> float:
        """Get the periodic poll interval in hours."""
        return self._poll_interval_hours

    def set_poll_interval_hours(self, hours: float) -> None:
        """Set the poll interval. Takes effect from now."""
        self._poll_interval_hours = hours
        # Leave an initial read, a retry or the end of a run as scheduled.
        if self._reachable and not self._watering and self._cancel_refresh:
            self._schedule_refresh(hours * 3600)

    def is_watering(self, zone: int) -> bool:
        """Whether the device is watering a zone."""
        return zone in self._watering

    def get_watering_end_time(self, zone: int) -> Optional[datetime]:
        """Get the expected end of a run, or None if not known."""
        return self._watering.get(zone)

    def get_watering_duration(self, zone: int) -> int:
        """Get the run time for a zone in minutes."""
        return self._watering_durations.get(zone, DEFAULT_WATERING_DURATION_MINUTES)

    def set_watering_duration(self, zone: int, duration: int) -> None:
        """Set the run time for a zone in minutes. Used from the next start."""
        self._watering_durations[zone] = duration

    def register_state_update_callback(self, update: Callable[[], None]) -> None:
        """Register a callback for state changes."""
        self._state_update_callbacks.append(update)

    def unregister_state_update_callback(self, update: Callable[[], None]) -> None:
        """Unregister a callback for state changes."""
        if update in self._state_update_callbacks:
            self._state_update_callbacks.remove(update)

    def _notify_state_update(self) -> None:
        """Tell the entities that something changed."""
        for update in list(self._state_update_callbacks):
            try:
                update()
            except Exception:  # noqa: BLE001 - one entity must not stop the rest
                _LOGGER.exception("State update failed for %s", self._mac_address)

    # ------------------------------------------------------------ lifecycle

    @callback
    def async_start(self) -> None:
        """Start watching the device and read its status."""
        self._cancel_advertisements = bluetooth.async_register_callback(
            self._hass,
            self._on_advertisement,
            bluetooth.BluetoothCallbackMatcher(
                address=self._mac_address, connectable=True
            ),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
        if self.paired:
            self._schedule_refresh(0)

    @callback
    def async_shutdown(self) -> None:
        """Stop all scheduled work. No new connection is made afterwards."""
        self._shutdown = True
        if self._cancel_refresh:
            self._cancel_refresh()
            self._cancel_refresh = None
        if self._cancel_advertisements:
            self._cancel_advertisements()
            self._cancel_advertisements = None

    @callback
    def _schedule_refresh(self, delay: float) -> None:
        """Schedule the next status read, replacing any earlier plan."""
        if self._cancel_refresh:
            self._cancel_refresh()
            self._cancel_refresh = None
        if self._shutdown:
            return

        @callback
        def _due(_now: datetime) -> None:
            self._cancel_refresh = None
            self._entry.async_create_background_task(
                self._hass,
                self._async_scheduled_refresh(),
                f"holman_water_ble refresh {self._mac_address}",
            )

        self._cancel_refresh = async_call_later(self._hass, delay, _due)

    @callback
    def _schedule_next(self) -> None:
        """Schedule the status read that should follow the current state."""
        if not self.paired:
            return
        if not self._reachable:
            index = min(max(self._failures, 1), len(RETRY_DELAYS_SECONDS)) - 1
            self._schedule_refresh(RETRY_DELAYS_SECONDS[index])
            return
        if not self._watering:
            self._schedule_refresh(self._poll_interval_hours * 3600)
            return
        ends = [end for end in self._watering.values() if end is not None]
        if len(ends) < len(self._watering):
            self._schedule_refresh(UNKNOWN_RUN_RECHECK_SECONDS)
            return
        # Leave a known run alone until it should be over.
        remaining = (min(ends) - dt_util.utcnow()).total_seconds()
        self._schedule_refresh(max(remaining, 0) + WATERING_COMPLETION_MARGIN_SECONDS)

    async def _async_scheduled_refresh(self) -> None:
        """Read the status; end a run that has outlived its time."""
        try:
            await self.async_refresh()
            now = dt_util.utcnow()
            overdue = [
                zone
                for zone, end in self._watering.items()
                if end is not None and end <= now
            ]
            for zone in overdue:
                _LOGGER.warning(
                    "%s is still watering after its run time, stopping zone %d",
                    self._mac_address,
                    zone,
                )
                # Forget the passed end, so a failed stop is not retried in a
                # tight loop.
                self._watering[zone] = None
                await self.async_stop_watering(zone)
        except HolmanError as exc:
            _LOGGER.debug("Status read of %s failed: %s", self._mac_address, exc)

    @callback
    def _on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        """Retry at once when a new or clearly better path to the device appears."""
        if self._reachable or self._shutdown or not self.paired:
            return
        if self._lock.locked():
            return
        if self._last_attempt is not None:
            waited = (dt_util.utcnow() - self._last_attempt).total_seconds()
            if waited < PATH_RETRY_MIN_SECONDS:
                return
            if self._last_path is not None:
                source, rssi = self._last_path
                if (
                    service_info.source == source
                    and service_info.rssi < rssi + PATH_RETRY_RSSI_GAIN
                ):
                    return
        _LOGGER.debug(
            "New path to %s via %s (%d dBm), retrying",
            self._mac_address,
            service_info.source,
            service_info.rssi,
        )
        self._schedule_refresh(0)

    # ----------------------------------------------------------- operations

    async def async_refresh(self) -> DeviceInfo:
        """Read the device status. Does not change anything on the device,
        other than correcting its clock when it is idle and clearly wrong.
        """

        async def _read(client: HolmanBLE) -> DeviceInfo:
            info = await client.read_device_info()
            if info is None:
                raise HolmanConnectionError(
                    f"{self._mac_address} connected but did not return its status"
                )
            self._apply_status(info)
            if not info.is_watering and self._clock_is_wrong(info):
                await client.set_current_time()
            return info

        return await self._run(_read)

    async def async_start_watering(self, zone: int) -> None:
        """Start a zone and confirm with the device that it is watering.

        Args:
            zone: 1-based zone number.

        Raises:
            HolmanError: If the device was not reached or did not start.
        """
        duration = self.get_watering_duration(zone)

        async def _start(client: HolmanBLE) -> None:
            if not await client.start_watering(zone, duration):
                raise HolmanCommandError(
                    f"{self._mac_address} did not accept the start command"
                )
            # Assumed until the status says otherwise.
            self._watering[zone] = dt_util.utcnow() + timedelta(minutes=duration)
            info = await self._read_until(client, watering=True)
            if info is None:
                raise HolmanConnectionError(
                    f"{self._mac_address} accepted the start command but its "
                    "status could not be read, so it may or may not be watering"
                )
            self._apply_status(info)
            if not info.is_watering:
                raise HolmanCommandError(
                    f"{self._mac_address} accepted the start command "
                    f"({duration} min) but is not watering"
                )

        await self._run(_start)

    async def async_stop_watering(self, zone: int) -> None:
        """Stop a zone and confirm with the device that it stopped.

        Args:
            zone: 1-based zone number.

        Raises:
            HolmanError: If the device was not reached or did not stop.
        """

        async def _stop(client: HolmanBLE) -> None:
            if not await client.stop_watering(zone):
                raise HolmanCommandError(
                    f"{self._mac_address} did not accept the stop command"
                )
            # The device only reports whether any zone is watering.
            others = any(other != zone for other in self._watering)
            info = await self._read_until(client, watering=others)
            if info is None:
                raise HolmanConnectionError(
                    f"{self._mac_address} accepted the stop command but its "
                    "status could not be read, so it may still be watering"
                )
            if not info.is_watering or others:
                self._watering.pop(zone, None)
            self._apply_status(info)
            if info.is_watering and not others:
                raise HolmanCommandError(
                    f"{self._mac_address} accepted the stop command but is "
                    "still watering"
                )

        await self._run(_stop)

    async def async_pair(self) -> None:
        """Pair with the device using a new passcode.

        Also clears the schedules stored on the device and sets its clock, so
        that only Home Assistant decides when it waters.
        """

        async def _pair(client: HolmanBLE) -> None:
            passcode = HolmanBLE.generate_passcode()
            if not await client.pair(passcode):
                raise HolmanCommandError(
                    f"{self._mac_address} did not accept the new passcode"
                )
            await self._passcodes.async_set(self._mac_address, passcode)
            await client.clear_schedules(self._device_config.total_zones)
            await client.set_current_time()
            info = await client.read_device_info()
            if info is not None:
                self._apply_status(info)

        await self._run(_pair, authenticate=False)

    async def async_unpair(self) -> None:
        """Clear the passcode on the device and forget it."""

        async def _unpair(client: HolmanBLE) -> None:
            if not await client.unpair():
                raise HolmanCommandError(
                    f"{self._mac_address} did not accept clearing the passcode"
                )
            await self._passcodes.async_delete(self._mac_address)
            self._watering.clear()

        await self._run(_unpair)

    # -------------------------------------------------------------- helpers

    async def _run(
        self,
        operation: Callable[[HolmanBLE], Awaitable[Any]],
        authenticate: bool = True,
    ) -> Any:
        """Connect, authenticate, run one operation and disconnect."""
        async with self._lock:
            if self._shutdown:
                raise HolmanError(f"{self._mac_address} is being unloaded")
            passcode = self._passcodes.get(self._mac_address)
            if authenticate and passcode is None:
                raise HolmanCommandError(f"{self._mac_address} is not paired")

            try:
                client = await self._connect()
                try:
                    if authenticate and not await client.authenticate(passcode):
                        raise HolmanConnectionError(
                            f"{self._mac_address} connected but did not accept "
                            "the passcode"
                        )
                    result = await operation(client)
                finally:
                    await client.disconnect()
            except HolmanConnectionError as exc:
                self._mark_unreachable(exc)
                raise
            except HolmanCommandError:
                # It answered; it just did not do what was asked.
                self._mark_reachable()
                raise
            else:
                self._mark_reachable()
                return result
            finally:
                self._schedule_next()
                self._notify_state_update()

    async def _connect(self) -> HolmanBLE:
        """Connect over whichever path Home Assistant has right now."""
        self._last_attempt = dt_util.utcnow()
        self._last_path = None
        device = self._current_ble_device()
        if device is None:
            heard = bluetooth.async_address_present(
                self._hass, self._mac_address, connectable=False
            )
            raise HolmanConnectionError(
                f"{self._mac_address} is only heard by Bluetooth scanners that "
                "cannot make connections (for example Shelly devices)"
                if heard
                else f"{self._mac_address} is not heard by any Bluetooth adapter "
                "or proxy"
            )
        path = bluetooth.async_last_service_info(
            self._hass, self._mac_address, connectable=True
        )
        if path is not None:
            self._last_path = (path.source, path.rssi)

        client = HolmanBLE(device, self._current_ble_device)
        if not await client.connect():
            raise HolmanConnectionError(
                f"Could not connect to {self._mac_address}: {client.last_error}"
            )
        return client

    def _current_ble_device(self) -> Optional[BLEDevice]:
        """Return the device via the best connectable path, if there is one."""
        return bluetooth.async_ble_device_from_address(
            self._hass, self._mac_address, connectable=True
        )

    async def _read_until(
        self, client: HolmanBLE, watering: bool
    ) -> Optional[DeviceInfo]:
        """Read the status until it matches, or the attempts run out.

        Returns:
            The last status read, or None if it could not be read at all.
        """
        info: Optional[DeviceInfo] = None
        for _ in range(VERIFY_ATTEMPTS):
            await asyncio.sleep(VERIFY_DELAY_SECONDS)
            latest = await client.read_device_info()
            if latest is not None:
                info = latest
                if info.is_watering == watering:
                    break
        return info

    def _apply_status(self, info: DeviceInfo) -> None:
        """Make our view of the zones match what the device reports."""
        self._device_info = info
        if not info.is_watering:
            self._watering.clear()
        elif not self._watering and self._device_config.total_zones == 1:
            # Started at the device, or before Home Assistant restarted.
            self._watering[1] = None

    @staticmethod
    def _clock_is_wrong(info: DeviceInfo) -> bool:
        """Whether the device clock was read and is clearly wrong."""
        if info.year == 0:
            return False
        now = dt_util.now().replace(tzinfo=None)
        try:
            device_time = datetime(
                info.year, info.month, info.day, info.hour, info.minute, info.second
            )
        except ValueError:
            return True
        return abs((device_time - now).total_seconds()) > CLOCK_DRIFT_LIMIT_SECONDS

    def _mark_reachable(self) -> None:
        """Record a successful contact."""
        if not self._reachable:
            _LOGGER.info("%s is reachable", self._mac_address)
        self._reachable = True
        self._failures = 0

    def _mark_unreachable(self, error: HolmanError) -> None:
        """Record a failed contact, logging only the first in a row."""
        if self._reachable or self._failures == 0:
            _LOGGER.warning("%s; will keep retrying", error)
        self._reachable = False
        self._failures += 1
