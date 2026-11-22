"""Passcode storage shared by every configured Holman device.

All devices share one JSON file, so they must also share one in-memory store:
with a copy per device, saving one device's passcode wrote back a stale
passcode for the others.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from homeassistant.core import HomeAssistant

from .const import DOMAIN, PASSCODE_FILENAME
from .models import PasscodeStore

_LOGGER = logging.getLogger(__name__)

_DATA_KEY = f"{DOMAIN}_passcodes"


class PasscodeManager:
    """Loads and saves passcodes without blocking the event loop."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the manager."""
        self._hass = hass
        self._path = hass.config.path(PASSCODE_FILENAME)
        self._store = PasscodeStore()
        self._lock = asyncio.Lock()
        self._loaded = False

    async def async_load(self) -> None:
        """Load passcodes once. A missing file means nothing is paired yet."""
        async with self._lock:
            if self._loaded:
                return
            data = await self._hass.async_add_executor_job(self._read)
            for mac, passcode in data.items():
                self._store.set(mac, passcode)
            self._loaded = True

    def get(self, mac: str) -> Optional[int]:
        """Return the passcode for a device, or None if it is not paired."""
        return self._store.get(mac)

    async def async_set(self, mac: str, passcode: int) -> None:
        """Store a passcode."""
        async with self._lock:
            self._store.set(mac, passcode)
            await self._async_save()

    async def async_delete(self, mac: str) -> None:
        """Forget a passcode."""
        async with self._lock:
            self._store.delete(mac)
            await self._async_save()

    async def _async_save(self) -> None:
        """Write the current passcodes to disk."""
        data = dict(self._store.passcodes)
        await self._hass.async_add_executor_job(self._write, data)

    def _read(self) -> dict[str, int]:
        """Read the passcode file. Runs in the executor."""
        try:
            with open(self._path, encoding="utf-8") as file:
                return json.load(file)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            _LOGGER.error("Failed to load passcodes: %s", exc)
            return {}

    def _write(self, data: dict[str, int]) -> None:
        """Replace the passcode file in one step. Runs in the executor."""
        temp_path = f"{self._path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
        os.replace(temp_path, self._path)


async def async_get_passcodes(hass: HomeAssistant) -> PasscodeManager:
    """Return the shared passcode manager, loading it on first use."""
    if _DATA_KEY not in hass.data:
        hass.data[_DATA_KEY] = PasscodeManager(hass)
    manager: PasscodeManager = hass.data[_DATA_KEY]
    await manager.async_load()
    return manager
