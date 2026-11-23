# Holman Water BLE (Unofficial) — Home Assistant Integration

Unofficial Home Assistant integration for Holman Bluetooth irrigation devices (BX1, BX2, BX4, BTX1A, BTX1B, BTX6, BTX8, and more).

> **⚠️ Disclaimer**: This is a personal hobby project. I am not affiliated with, endorsed by, or in any way connected to Holman Industries. The BLE protocol was reverse-engineered from the official iGardener mobile app and may break with firmware updates. **Use at your own risk — absolutely no warranty of any kind is provided.**

> A fork of [daweizhangau/ha-holman-water-ble](https://github.com/daweizhangau/ha-holman-water-ble) by Dawei Zhang, with reliability fixes. Bluetooth proxies must support active connections: Shelly devices only forward advertisements and cannot be used to control a tap.

---

## Features

- **Valve Control**: Turn individual valves/zones on and off via switch entities
- **Configurable Duration**: Set watering duration per zone (1–240 minutes, default 10)
- **Auto Stop**: Watering stops automatically after the configured duration. The switch returns to OFF once the device confirms the watering is complete
- **Manual Override**: Turn a valve off at any time
- **Confirmed commands**: After a start or stop the device's own status is read back. If it did not act, the action fails with an error
- **Schedule Clearing**: Schedules stored on the device are cleared when it is paired
- **Diagnostics**: Read device information on demand (voltages, firmware, dial position, watering status, etc.)
- **Health Check**: The integration periodically connects to the device to verify it is still reachable and refresh sensor values
- **Unpairing**: Clear the device passcode so it can be re-paired with the official app
- **Multi-Device**: Support for devices with 1–8 valves/zones
- **On-Demand Connection**: BLE connects only when needed, entities remain available in HA

## Supported Devices

| Model | Description | Zones | Type |
|---|---|---|---|
| BX1 | Bluetooth Tap Timer | 1 | Battery |
| BX2 | Bluetooth Tap Timer | 2 | Battery |
| BX4 | Bluetooth Tap Timer | 4 | Battery |
| CO3314 | BX4 4 Outlet Tap timer | 4 | Battery |
| CO3312 | BX2 (newer version) | 2 | Battery |
| BTX1A | Single Tap Timer H-Bridge | 1 | Battery |
| BTX1B | Single Tap Timer DRV8837 | 1 | Battery |
| BTX6 | AC Controller | 6 | AC |
| BTX8 | AC Controller | 8 | AC |
| BTXM4 | LD Controller (Motor) | 4 | Battery |
| BTS1 | Water Cell | 1 | Battery |
| BTV1 | Latching Valve Controller | 1 | Battery |
| CO3112 | Manifold Solar (2 Zones) | 2 | Battery |
| CO3114 | Manifold Solar (4 Zones) | 4 | Battery |
| CO3012 | Valve Box (2 Zones) | 2 | Battery |

## Requirements

- Home Assistant 2026.2 or later
- A Bluetooth adapter accessible to HA (built-in, USB dongle, or [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy/))
- A supported Holman Bluetooth device

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**.
2. Add this repository URL and select category **Integration**.
3. Search for **Holman Water BLE** and click **Download**.
4. Restart Home Assistant.

### Manual

Copy the `custom_components/holman_water_ble/` folder into your HA `config/custom_components/` directory and restart Home Assistant.

## Configuration

1. Power on your Holman device and ensure Bluetooth is enabled.
2. Go to **Settings → Devices & Services** in Home Assistant.
3. Click **+ Add Integration**, search for **Holman Water BLE**.
4. Select your device from the discovered list, or enter the MAC address manually.
5. Confirm the device details.

The integration will automatically pair with the device by generating and storing a random passcode.

## Entities

### Switch (per zone)
- **`{device_name} Valve`** (`Valve {N}` with several zones) — Turn watering on/off for each zone

### Number
- **`{device_name} Run Time`** — Run time when started from Home Assistant, in minutes (1–240, default 10)
- **`{device_name} Button Run Time`** — Run time when started with the button on the device, in minutes (1–240, default 10)
- **`{device_name} Poll Interval`** — Set how often the integration checks the device is reachable (1–24 hours, default 4). Sensors are refreshed and device availability is updated on each check

### Button
- **`{device_name} Read Diagnostics`** — Trigger a BLE connection to read current device status
- **`{device_name} Clear Pairing`** — Clear the device passcode so it can be re-paired with another app. All entities become unavailable until re-paired
- **`{device_name} Pair`** — Re-pair an unpaired device. Only visible when the device is unpaired. Generates a new passcode and restores all entities

### Sensor (diagnostic)
- Battery Voltage
- Voltage DC / AC / Valve
- Dial Position (Valve 1–8, LOCK, OFF, RUN, SYSTEST)
- Watering Status (On/Off)
- Rain Sensor (Off/Wet)
- Active Station
- Connected Valves
- Firmware Version
- Protocol Version
- Diagnostic Byte
- RSSI

## Services

### `holman_water_ble.unpair`
Clear the pairing passcode from a device.

| Field | Type | Description |
|---|---|---|
| `mac_address` | string | MAC address of the device to unpair |

## Passcode Storage

Passcodes are stored in `{config_dir}/holman_water_ble_passcodes.json`, keyed by MAC address. This allows multiple devices to be managed independently.

## How It Works

1. **On-demand connection**: When you toggle a switch or press a button, the integration connects to the device via BLE.
2. **Authentication**: The stored passcode is written to the device.
3. **Operation**: The requested action (start/stop watering, read diagnostics, unpair) is performed. Start and stop are confirmed by reading the status.
4. **Pairing only**: All zone schedules are disabled (mode=0) to prevent automatic watering, and the clock is set.
5. **Time sync**: The clock is corrected when the device is idle and more than two minutes out. A status read changes nothing else on the device, apart from the button's run time below.
   **Button**: The device's own button waters for a run time stored with its schedule. It is set with the Button Run Time entity, and sent while the device is idle. Runs started with the button show in Home Assistant at the next status read.
6. **Disconnect**: The BLE connection is closed.
7. **Runs**: A run started from Home Assistant is left alone until it should be over, plus a 30-second margin. Then the status is read and the switch follows it. A run started at the device also shows, and is checked every 15 minutes.
8. **Manual override**: Turning a valve off sends a stop command. If the device cannot be reached, the valve is not shown as off.
9. **Periodic health check**: Every N hours (configurable via the Poll Interval entity, default 4) while idle, the status is read.
   **Unreachable devices**: entities become unavailable at the first failed connection. The device is retried after 1, 2, 5, 10 and then every 15 minutes, and at once when a different or clearly stronger Bluetooth path appears.
10. **Pairing lifecycle**: After unpairing, all entities become unavailable and only the Pair button is visible. Pressing Pair re-generates a passcode, re-pairs the device, and restores all entities.

This means the device is only connected briefly when needed, preserving battery life.

## Tested Devices

| Model | Description | Year of Purchase |
|---|---|---|
| BX1 | Single valve, Firmware version 3, Protocol version 2 | 2026 |

## Diagnostics

To help troubleshoot issues, open the device page and click **Download diagnostics**. The downloaded JSON contains connection state, last reading, signal strength, and other debug info.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---
