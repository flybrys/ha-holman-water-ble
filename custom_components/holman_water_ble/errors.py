"""Errors raised by the Holman Water BLE integration.

They derive from HomeAssistantError so that a failed action is shown to the
user and fails the calling script or automation, instead of passing silently.
"""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class HolmanError(HomeAssistantError):
    """Base error for Holman Water BLE."""


class HolmanConnectionError(HolmanError):
    """The device could not be reached, or stopped answering."""


class HolmanCommandError(HolmanError):
    """The device was reached but did not do what was asked."""
