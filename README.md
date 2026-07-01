# YoLink Local

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant integration for YoLink devices using the **Local API** - no cloud required.

This integration communicates directly with your YoLink Local Hub over your LAN using HTTP and MQTT, providing fast, reliable, and private control of your YoLink devices.

## ⚠️ Disclaimer

**USE AT YOUR OWN RISK.** This software controls physical devices in your home, including locks, sirens, and other security-related equipment. The author assumes no responsibility for:

- Unauthorized access to your home
- False alarms or failure to alarm
- Property damage or personal injury
- Any other damages arising from the use of this software

By installing this integration, you acknowledge that you understand these risks and accept full responsibility for the security and safety of your home and its occupants. This software is provided "as is" without warranty of any kind.

**If you are not comfortable with these risks, do not use this software.**

## Why use this instead of Matter or Yolink cloud integrations ?

The YoLink Local Hub supports both Matter and a native Local API, as well as the cloud API. While both Matter and Cloud works, the local API offers:

- **Full device information** - temperature, humidity, battery levels
- **Network state** - periodic check-ins are tracked and devices are marked offline if they stop reporting. The Yolink cloud integration does so, but the Matter integration does not, which was a showstopper for me, and the primary reason I started working on this project.
- **Faster response times** - direct HTTP/MQTT vs Matter's abstraction layer
- **Richer entity types** - sensors, binary sensors, and diagnostic entities

Matter can leave YoLink sensors stale when battery runs out, which can cause incorrect motion/status reporting and disrupt automations. YoLink devices are expected to check in periodically with the hub. This integration marks devices offline after 12 hours without reports, matching YoLink cloud integration behavior.

## Tested devices

| Device Type | Entity Type | Features | Models               |
|-------------|-------------|----------|----------------------|
| DoorSensor | Binary Sensor | Open/closed state, battery | YS7704-UC
| LeakSensor | Binary Sensor | Leak detected, battery | YS7903-UC
| Manipulator | Valve, Sensor | Open/closed control, battery | YS4909-UC
| MotionSensor | Binary Sensor | Motion detected, battery | YS7804-UC
| Outlet | Switch, Sensor | On/off state, power | YS6614-UC
| TempSensor | Sensor Sensor | Temperature, battery | YS8004-UC
| THSensor | Sensor | Temperature, humidity, battery | YS8003-UC, YS8005-UC
| TiltSensor | Binary Sensor | Temperature, battery | YS7706-UC

Additional device types can be added -- contributions welcome.

## Additional exposed entities/diagnostics

This project includes additional entities and diagnostics, not available in the Yolink cloud integration :

- All devices: `Firmware`, `Last reported`
- Battery-powered devices: `Battery`, `Low battery`
- DoorSensor: `Alert interval`, `Delay`, `Open remind delay`
- LeakSensor: `Detector error`, `Device temperature`, `Freeze error`, `Reminder`
- MotionSensor : `Alert interval`, `Device temperature`, `LED alarm`, `No-motion delay`, `Sensitivity`
- Outlet: `Power`
- TempSensor : `High humidity` (bug), `High temperature`, `Low humidity` (bug), `Low temperature`, `Reporting interval`, `Temperature correction`
- THSensor 8003 : `High humidity`, `High temperature`, `Humidity correction`, `LCD temperature unit`, `Low humidity`, `Low temperature`, `Reporting interval`, `Temperature correction`
- THSensor 8005 : `High humidity`, `High temperature`, `Humidity correction`, `Humidity max threshold`, `Humidity min threshold`, `LCD temperature unit` (bug), `Low humidity`, `Low temperature`, `Reporting interval`, `Temperature correction`, `Temperature max threshold`, `Temperature min threshold`
- TiltSensor: `Alert interval`, `Delay`, `Open remind delay`

Diagnostics entities listed as "bug" are being returned from the Yolink local API in JSON responses, but not actually applicable to the specific models. This integration merely exposes them. The values are bogus, and should be ignored.
This bug would be best fixed by Yolink in their local API implementation, rather than manually blacklisted in this project.

Outlet `Power` is derived from the local API `power` field, which reports deciwatts on the tested YS6614-UC smart plug. The local API `watt` field was observed to remain at `0` and is not exposed.

## Prerequisites

Before installing this integration, you need:

1. A **YoLink Local Hub** (model YS1606-UC)
2. The hub connected to your network via Ethernet or Wi-Fi. If the hub is connected to both, you may configure both addresses for better resilience.
3. Devices migrated from YoLink Cloud to the Local Hub
4. HTTP and MQTT protocols enabled on the hub

For detailed setup instructions, see the excellent [YoLink Local Hub Setup Guide](https://community.home-assistant.io/t/yolink-local-hub-matter-integration-guide/911359) on the Home Assistant Community forums.

## Finding your credentials

You'll need four pieces of information from the YoLink app:

### Hub IP Address

1. Open the YoLink app
2. Tap on your **YoLink Local Hub** device
3. Tap the **⋮** menu (top right)
4. Find the **IP Address** under the Ethernet or Wi-fi section. Make sure you have a DHCP reservation for the hub with this IP address in your router/DHCP server.
5. If your router has local DNS for DHCP reservations, you may also use the FQDN, which is what I do.

If your hub is connected by both Ethernet and Wi-Fi, note both IP addresses or hostnames. The integration can use one as the primary address and the other as an optional secondary address. Both addresses must point to the same YoLink Local Hub.

![Hub main screen](images/IMG_2021.PNG)

![Hub details showing IP address](images/IMG_2022.PNG)

### Client ID, Client Secret, and Net ID

1. From the hub screen, tap **Local Network**
2. Go to the **Integrations** tab for Client ID and Client Secret
3. Go to the **General** tab for Net ID

![Integrations tab with API credentials](images/IMG_2023.PNG)

![General tab with Net ID](images/IMG_2024.PNG)

## Installation

### HACS Installation (Recommended)

1. Open **HACS** in your Home Assistant instance
2. Click on the three dots in the top right corner and select **Custom repositories**
3. Paste `https://github.com/madbrain76/yolink-local-ha` into the **Repository** field
4. Select **Integration** as the **Category**
5. Click **Add**
6. Once added, search for **YoLink Local** and click **Download**
7. Restart Home Assistant

### Manual Installation

1. Download or clone this repository
2. Copy the `custom_components/yolocal` folder to your Home Assistant's `custom_components` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings -> Devices & Services**
2. Click **Add Integration**
3. Search for **YoLink Local**
4. Enter your credentials:
   - **Primary hub IP/hostname**: Your hub's normal IP address or hostname, typically the Ethernet address
   - **Secondary hub IP/hostname (optional)**: The same hub's other address, typically Wi-Fi when the primary address is Ethernet
   - **Client ID**: From the Integrations tab
   - **Client Secret**: From the Integrations tab
   - **Net ID**: From the General tab

The secondary address is optional. Configure it only when the same hub is reachable on two network interfaces, such as Ethernet and Wi-Fi.

To add, remove, or change the secondary address later, use **Settings -> Devices & Services -> YoLink Local -> Reconfigure**. The integration entry title lists both configured hostnames when two are set.

## How It Works

- **Device Discovery**: On startup, the integration queries the hub for all connected devices
- **Initial State**: Each device's current state is fetched via HTTP
- **Real-time Updates**: MQTT subscription receives instant state changes (door opens, temperature changes, etc.)
- **Dual-host resilience**: When both hub addresses are configured, the integration keeps independent MQTT subscriptions to both interfaces and deduplicates identical events. HTTP reads can fail over between configured addresses.
- **Commands**: Lock/unlock, on/off, and other commands are sent via HTTP

## Test Scripts

The `tests/` folder includes standalone test and diagnostics scripts that are not part of the Home Assistant runtime integration:

- `tests/wait_for_yolink_change.py` and wrappers:
  - `tests/wait_for_th.sh`
  - `tests/wait_for_temp.sh`
  - `tests/wait_for_motion.sh`
  - `tests/wait_for_door.sh`
  - `tests/wait_for_tilt.sh`
  - `tests/wait_for_leak.sh`
  - `tests/wait_for_lock.sh`
  - `tests/wait_for_any.sh` (generic, device-type agnostic)
  - `tests/list_yolink_devices.py`
  - `tests/list_yolink_device_types.py`
  - `tests/capture_yolink_payloads.py`

### Test Environment Variables

Set these before running tests:

Required connection/auth:

- `YOLINK_HOST` (hub host or URL)
- `YOLINK_HOST_SECONDARY` (optional secondary hub host or URL)
- `YOLINK_CLIENT_ID`
- `YOLINK_CLIENT_SECRET`
- `YOLINK_NET` or `YOLINK_NET_ID`

Required device serials for full live type coverage:

- `YOLINK_MOTION_7804_SERIAL` or `YOLINK_MOTION_7805_SERIAL`
- `YOLINK_TH_8003_SERIAL`
- `YOLINK_DOOR_7704_SERIAL`
- `YOLINK_LEAK_7903_SERIAL`
- `YOLINK_TEMP_8004_SERIAL`
- `YOLINK_TILT_7706_SERIAL`
- `YOLINK_LOCK_MODEL_NUMBER_SERIAL`

Optional:

- `YOLINK_TIMEOUT` (wait script timeout, default `900`)
- `YOLINK_FIELD` (for TH waits: `temperature`, `humidity`, `unit`, `both`)

### Example Setup

```bash
export YOLINK_HOST=
export YOLINK_NET=
export YOLINK_CLIENT_ID="..."
export YOLINK_CLIENT_SECRET="..."

export YOLINK_MOTION_7805_SERIAL=
export YOLINK_TH_8003_SERIAL=
export YOLINK_DOOR_7704_SERIAL=
export YOLINK_LEAK_7903_SERIAL=
export YOLINK_TEMP_8004_SERIAL=
export YOLINK_TILT_7706_SERIAL=
export YOLINK_LOCK_MODEL_NUMBER_SERIAL=
```

### Run Test Scripts

List detected type/model pairs:

```bash
uv run python tests/list_yolink_device_types.py
```

List device ids and names:

```bash
uv run python tests/list_yolink_devices.py
```

Wait for TH unit change:

```bash
./tests/wait_for_th.sh --field unit --timeout 120
```

Capture payloads for configured devices:

```bash
uv run python tests/capture_yolink_payloads.py --duration 300
```

Monitor primary and secondary MQTT paths:

```bash
uv run python tests/monitor_yolink_mqtt_hosts.py --timeout 300
```

This script keeps retrying both configured hosts for transient startup or network failures. It reports which interface receives each event and marks exact duplicate YoLink payloads received from both interfaces.

## Adding New Device Types

To add support for a new YoLink device type, follow this procedure:

### Step 1: List available device types

```bash
uv run python tests/list_yolink_device_types.py
```

Identify the device type and model number of the new device.

### Step 2: Capture payloads

Set an environment variable with the device's serial (device ID) and run the capture script:

```bash
export YOLINK_MYNEWDEV_SERIAL=<device-id-from-step-1>
uv run python tests/capture_yolink_payloads.py --duration 300
```

This captures both HTTP state responses and MQTT events. By default, output is saved to a timestamped `captures/yolink-payloads-*` directory.

### Step 3: Validate device behavior

Use the generic wait script to confirm state changes are detected:

```bash
./tests/wait_for_any.sh --device-name "<device-name>" --timeout 120
```

Manually trigger the device (e.g., open/close a door, detect motion) and verify the script reports the change.

### Step 4: Analyze and implement

Study the captured payloads in `captures/yolink-payloads-*/` to understand the state format, field names, and MQTT event structure. Then add the device type to the appropriate platform file (`binary_sensor.py`, `sensor.py`, `switch.py`, etc.) in `custom_components/yolocal/`. Re-run the capture and wait scripts after implementation to confirm the new entity maps the same fields reported by the hub.

## Troubleshooting

### Integration doesn't appear after restart

Check the Home Assistant logs for import errors. The integration requires `paho-mqtt>=2.0.0`.

### Devices show as unavailable

- Verify the hub IP address is correct and reachable
- Check that HTTP (port 1080) and MQTT (port 18080) are enabled on the hub
- Ensure devices have been migrated to the Local Network in the YoLink app
- If using both Ethernet and Wi-Fi, verify both configured addresses belong to the same hub and both ports are reachable on each address

### State updates are delayed

Real-time updates require MQTT. If updates only happen on HA restart, check that:
- MQTT is enabled on the hub (port 18080)
- Your firewall allows the connection
- If a secondary hub address is configured, verify both primary and secondary MQTT connections are reachable. The integration should continue receiving events through either interface while the other is unavailable.

## Contributing

Contributions are welcome. To add support for additional device types:

1. Check the [YoLink Local API documentation](https://doc.yosmart.com/docs/protocol/local_hub/localHubMethods)
2. Follow the [Adding New Device Types](#adding-new-device-types) capture procedure above
3. Add the device type to the appropriate platform file
4. Submit a pull request

## License

GNU General Public License v3.0 -- see [LICENSE](LICENSE) for details.

## Credits

- [David Bruce Borenstein](https://github.com/borenstein) for the [original integration](https://github.com/borenstein/yolink-local-ha)
- [YoLink](https://www.yosmart.com/) for the Local Hub and API
- The Home Assistant community for testing and feedback
