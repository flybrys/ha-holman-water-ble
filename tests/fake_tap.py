"""A simulated BX1, used in place of a BleakClient.

It decodes the packets it is sent, so tests cover the real client and packet
builders. Like the real tap, it acknowledges a start it then ignores.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.holman_water_ble.const import (
    CHAR_UUID_COMMAND,
    CHAR_UUID_DEVICE_INFO,
    CHAR_UUID_MAC,
    CHAR_UUID_PASSCODE,
    CHAR_UUID_TIME,
    CHAR_UUID_WATERING,
    PROFILE_UUID_PREFIX,
    PROFILE_UUID_SUFFIX,
)
from custom_components.holman_water_ble.parser import build_current_time

MAC = "AA:BB:CC:DD:EE:FF"
SERIAL = "00002A25-0000-1000-8000-00805F9B34FB"
BROKEN = "0000F0FF-0000-1000-8000-00805F9B34FB"


def _characteristic(uuid: str, *properties: str) -> SimpleNamespace:
    """Describe a characteristic the way bleak does, in lower case."""
    return SimpleNamespace(uuid=uuid.lower(), properties=list(properties))


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
        # In step with the default run time of 10 minutes.
        self.profiles = {
            f"{PROFILE_UUID_PREFIX}{zone}{PROFILE_UUID_SUFFIX}": bytearray(
                [0] * 16 + [10, 0, 0, 0]
            )
            for zone in (1, 2)
        }
        self.accepts_profile = True
        self.has_dial = False
        self.device_type = 100
        self.passcode = 0
        self.clock: datetime = dt_util.now()
        self.writes: list[str] = []
        self.connections = 0
        self.disconnections = 0
        self.services = [
            SimpleNamespace(
                uuid="c521f000-0d70-4d4f-8e43-40d84c50ab38",
                characteristics=[
                    _characteristic(CHAR_UUID_MAC, "read"),
                    _characteristic(CHAR_UUID_DEVICE_INFO, "read", "notify"),
                    _characteristic(CHAR_UUID_WATERING, "read", "write"),
                    _characteristic(CHAR_UUID_COMMAND, "write"),
                    _characteristic(CHAR_UUID_PASSCODE, "read", "write"),
                    _characteristic(SERIAL, "read"),
                    _characteristic(BROKEN, "read"),
                ],
            )
        ]
        self.reads: list[str] = []
        self.is_connected = False

    @property
    def button_minutes(self) -> int:
        """The run time the button of zone 1 would use."""
        profile = self.profiles[f"{PROFILE_UUID_PREFIX}1{PROFILE_UUID_SUFFIX}"]
        return profile[15] * 60 + profile[16]

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

    async def read_gatt_char(self, char: str | SimpleNamespace) -> bytes:
        """Answer a read, by uuid or by characteristic."""
        uuid = char if isinstance(char, str) else char.uuid.upper()
        self.reads.append(uuid)
        if not self.responds:
            raise TimeoutError("no answer")
        if uuid == CHAR_UUID_WATERING:
            return bytes([0, 0, 0, 12, 0, 0, 0, 0, 0, 0])
        if uuid in self.profiles:
            return bytes(self.profiles[uuid])
        if uuid == SERIAL:
            return b"SN" + bytes.fromhex(MAC.replace(":", ""))[::-1]
        if uuid == BROKEN:
            raise OSError("not permitted")
        if uuid == CHAR_UUID_PASSCODE:
            return bytes([self.passcode >> 8, self.passcode & 0xFF])
        if uuid == CHAR_UUID_TIME:
            return build_current_time(self.clock)
        assert uuid == CHAR_UUID_DEVICE_INFO
        data = bytearray(17)
        data[1] = 3
        data[2] = 2
        data[3] = self.device_type
        data[5] = 61
        data[8] = (1 if self.watering else 0) | (0x20 if self.has_dial else 0)
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
            # Suspected of the real tap: a profile write ends a run.
            self.watering = False
            if self.accepts_profile:
                self.profiles[uuid] = bytearray(data)
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
