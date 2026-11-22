"""A simulated BX1, used in place of a BleakClient.

It decodes the packets it is sent, so tests cover the real client and packet
builders. Like the real tap, it acknowledges a start it then ignores.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.holman_water_ble.const import (
    CHAR_UUID_DEVICE_INFO,
    CHAR_UUID_PASSCODE,
    CHAR_UUID_TIME,
    CHAR_UUID_WATERING,
    PROFILE_UUID_PREFIX,
)
from custom_components.holman_water_ble.parser import build_current_time

MAC = "AA:BB:CC:DD:EE:FF"


class FakeTap:
    """State of the simulated tap across connections."""

    def __init__(self) -> None:
        """Initialize an idle, reachable tap."""
        self.address = MAC
        self.reachable = True
        self.responds = True
        self.obeys = True
        self.accepts_passcode = True
        self.watering = False
        self.minutes: int | None = None
        self.passcode = 0
        self.clock: datetime = dt_util.now()
        self.writes: list[str] = []
        self.connections = 0
        self.disconnections = 0
        self.services: list[SimpleNamespace] = []
        self.is_connected = False

    async def connect(self, **kwargs: object) -> FakeTap:
        """Stand in for establish_connection."""
        self.connections += 1
        if not self.reachable:
            raise TimeoutError("no answer")
        self.is_connected = True
        return self

    async def disconnect(self) -> None:
        """Disconnect."""
        self.is_connected = False
        self.disconnections += 1

    async def pair(self) -> None:
        """Bond."""

    async def read_gatt_char(self, uuid: str) -> bytes:
        """Answer a read."""
        if not self.responds:
            raise TimeoutError("no answer")
        if uuid == CHAR_UUID_PASSCODE:
            return bytes([self.passcode >> 8, self.passcode & 0xFF])
        if uuid == CHAR_UUID_TIME:
            return build_current_time(self.clock)
        assert uuid == CHAR_UUID_DEVICE_INFO
        data = bytearray(17)
        data[1] = 3
        data[2] = 2
        data[3] = 100
        data[5] = 61
        data[8] = 1 if self.watering else 0
        return bytes(data)

    async def write_gatt_char(
        self, uuid: str, data: bytes, response: bool = False
    ) -> None:
        """Act on a write."""
        if uuid == CHAR_UUID_PASSCODE:
            self.writes.append("passcode")
            if not self.accepts_passcode:
                raise OSError("write rejected")
            self.passcode = (data[0] << 8) | data[1]
        elif uuid == CHAR_UUID_TIME:
            self.writes.append("time")
        elif uuid.startswith(PROFILE_UUID_PREFIX):
            self.writes.append("schedule")
        elif uuid == CHAR_UUID_WATERING:
            start = bool(data[0] & 1)
            self.writes.append("start" if start else "stop")
            if not self.obeys:
                return
            if not start:
                self.watering = False
            elif data[3] <= 59:
                self.watering = True
                self.minutes = data[2] * 60 + data[3]
        else:
            raise AssertionError(uuid)
