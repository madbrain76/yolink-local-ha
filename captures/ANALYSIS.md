# YoLink Payload Capture Analysis

Capture directory: [captures](/home/madbrain/projects/yolink-local-ha/captures)

## Scope

- Capture window: 300 seconds
- Hub: `REDACTED_HOST`
- Net id: `REDACTED_NET_ID`
- Devices tracked: 7
- Devices with at least one MQTT event: 7

Event counts from `summary.json`:

- DoorSensor `DoorSensor-1`: 2
- LeakSensor `LeakSensor-1`: 2
- MotionSensor `MotionSensor-1`: 4
- MotionSensor `MotionSensor-2`: 4
- TempSensor `TempSensor-1`: 2
- THSensor `THSensor-1`: 4
- TiltSensor `TiltSensor-1`: 2

## Main finding

MQTT and HTTP `getState` payloads do not use the same shape for most device families.

- HTTP `getState` returns the device state nested under `response.data.state`
- MQTT usually reports device values as flat keys inside `payload.data`
- Several device families also omit some HTTP-only fields from MQTT

This means a simple generic top-level merge is not sufficient if entities read nested state produced by HTTP `getState`.

## Per-device observations

### DoorSensor

Device: `DoorSensor-1` (`DoorSensor-1`)

HTTP nested state example:

```json
{"alertInterval":0,"battery":3,"delay":0,"openRemindDelay":0,"state":"closed","version":"0420"}
```

MQTT payload `data` example:

```json
{"alertType":"normal","battery":3,"loraInfo":{"devNetType":"A","gatewayId":"","gateways":1,"signal":0},"state":"open","version":"0420"}
```

Observed differences:

- MQTT uses flat keys instead of HTTP nested keys
- MQTT omitted `alertInterval`, `delay`, and `openRemindDelay`
- MQTT added `alertType` and `loraInfo`

Functional implication:

- A generic top-level merge can update top-level `state` without refreshing nested `state.state`
- Delay/reminder configuration fields can remain stale unless preserved from HTTP state

### TiltSensor

Device: `TiltSensor-1` (`TiltSensor-1`)

HTTP nested state example:

```json
{"alertInterval":0,"battery":4,"delay":0,"openRemindDelay":0,"state":"closed","version":"060f"}
```

MQTT payload `data` example:

```json
{"alertType":"normal","battery":4,"loraInfo":{"devNetType":"A","gatewayId":"","gateways":1,"signal":0},"state":"open","version":"060f"}
```

Observed differences:

- Same shape mismatch as DoorSensor
- MQTT omitted `alertInterval`, `delay`, and `openRemindDelay`

Functional implication:

- Same as DoorSensor

### LeakSensor

Device: `LeakSensor-1` (`LeakSensor-1`)

HTTP nested state example:

```json
{"alarmState":{"detectorError":false,"freezeError":false,"reminder":false,"stayError":false},"battery":4,"devTemperature":20,"interval":20,"sensorMode":"WaterLeak","state":"normal","supportChangeMode":false,"version":"0313"}
```

MQTT payload `data` example:

```json
{"alarmState":{"detectorError":false,"freezeError":false,"reminder":false,"stayError":false},"battery":4,"devTemperature":21,"loraInfo":{"devNetType":"A","gatewayId":"","gateways":1,"signal":0},"sensorMode":"WaterLeak","state":"alert","version":"0313"}
```

Observed differences:

- MQTT uses flat keys instead of HTTP nested keys
- MQTT omitted `interval` and `supportChangeMode`
- MQTT added `loraInfo`

Functional implication:

- Alarm and state changes are present in MQTT
- Configuration-like fields can remain stale without nested merge preservation

### MotionSensor 7804

Device: `MotionSensor-1` (`MotionSensor-1`)

HTTP nested state example:

```json
{"alertInterval":1,"battery":4,"devTemperature":21,"ledAlarm":true,"nomotionDelay":1,"sensitivity":3,"state":"normal","version":"0474"}
```

MQTT payload `data` example:

```json
{"alertInterval":1,"battery":4,"ledAlarm":true,"loraInfo":{"devNetType":"A","gatewayId":"","gateways":1,"signal":0},"nomotionDelay":1,"sensitivity":3,"state":"alert","version":"0474"}
```

Observed differences:

- MQTT uses flat keys instead of HTTP nested keys
- MQTT omitted `devTemperature`
- MQTT added `loraInfo`

Functional implication:

- Motion state transitions are present in MQTT
- `devTemperature` can remain stale if existing nested state is not preserved

### MotionSensor 7805

Device: `MotionSensor-2` (`MotionSensor-2`)

HTTP nested state example:

```json
{"alertInterval":1,"battery":4,"batteryType":"Li","devTemperature":28,"ledAlarm":true,"nomotionDelay":1,"sensitivity":3,"state":"normal","version":"0512"}
```

MQTT payload `data` example:

```json
{"alertInterval":1,"battery":4,"ledAlarm":true,"loraInfo":{"devNetType":"A","gatewayId":"","gateways":1,"signal":0},"nomotionDelay":1,"sensitivity":3,"state":"alert","version":"0512"}
```

Observed differences:

- MQTT uses flat keys instead of HTTP nested keys
- MQTT omitted `batteryType` and `devTemperature`
- MQTT added `loraInfo`

Functional implication:

- Same as MotionSensor 7804, with the additional note that `batteryType` appears in HTTP state but not MQTT
- This also confirms that `batteryType` is an actual HTTP field on at least one motion device

### THSensor

Device: `THSensor-1` (`THSensor-1`)

HTTP nested state example:

```json
{"alarm":{"code":0,"highHumidity":false,"highTemp":false,"lowBattery":false,"lowHumidity":false,"lowTemp":false,"period":false},"battery":4,"humidity":46.4,"humidityCorrection":0,"humidityLimit":{"max":100,"min":0},"interval":0,"mode":"f","state":"normal","tempCorrection":0,"tempLimit":{"max":1000,"min":-1000},"temperature":22.5,"version":"03bb"}
```

MQTT payload `data` example:

```json
{"alarm":{"code":0,"highHumidity":false,"highTemp":false,"lowBattery":false,"lowHumidity":false,"lowTemp":false,"period":false},"battery":4,"humidity":47.4,"humidityCorrection":0,"humidityLimit":{"max":100,"min":0},"interval":0,"loraInfo":{"devNetType":"A","gatewayId":"","gateways":1,"signal":0},"mode":"c","state":"normal","tempCorrection":0,"tempLimit":{"max":1000,"min":-1000},"temperature":22.5,"version":"03bb"}
```

Observed differences:

- MQTT uses flat keys instead of HTTP nested keys
- MQTT included all key sensor values in this run
- MQTT added `loraInfo`
- No HTTP-only state fields were observed in this capture

Functional implication:

- TH MQTT payloads were not sparse in this run
- The main issue is still shape mismatch, not missing values

### TempSensor

Device: `TempSensor-1` (`TempSensor-1`)

HTTP nested state example:

```json
{"alarm":{"code":0,"highHumidity":false,"highTemp":false,"lowBattery":false,"lowHumidity":false,"lowTemp":false,"period":false},"battery":2,"batteryType":"Li","humidity":0,"humidityCorrection":0,"humidityLimit":{"max":0,"min":0},"interval":0,"mode":"f","state":"normal","tempCorrection":0,"tempLimit":{"max":1000,"min":-1000},"temperature":-20.3,"version":"0460"}
```

MQTT payload `data` example:

```json
{"alarm":{"code":0,"highHumidity":false,"highTemp":false,"lowBattery":false,"lowHumidity":false,"lowTemp":false,"period":false},"battery":2,"batteryType":"Li","humidity":0,"humidityCorrection":0,"humidityLimit":{"max":0,"min":0},"interval":0,"loraInfo":{"devNetType":"A","gatewayId":"","gateways":1,"signal":0},"mode":"f","state":"normal","tempCorrection":0,"tempLimit":{"max":1000,"min":-1000},"temperature":-20,"version":"0460"}
```

Observed differences:

- MQTT uses flat keys instead of HTTP nested keys
- MQTT included all key sensor values in this run
- MQTT added `loraInfo`
- No HTTP-only state fields were observed in this capture

Functional implication:

- Same conclusion as THSensor for this run

## Overall conclusion

The captured data shows that MQTT-vs-HTTP mismatch is real and common.

Observed across all tested device families:

- HTTP state is nested
- MQTT state is usually flat

Observed on several device families:

- MQTT omitted fields that HTTP `getState` includes

Because of this, a generic top-level merge would be risky. It can leave nested HTTP state stale after MQTT updates, especially for:

- DoorSensor
- TiltSensor
- MotionSensor
- LeakSensor

For TH/Temp devices in this capture, the main risk is shape mismatch rather than missing values, but that is still enough to justify merge logic that folds flat MQTT fields into nested state.

## Recommendation

Do not replace the current device-type-aware/nested merge behavior with a simple generic top-level merge unless entity readers are also rewritten to consume the flat MQTT shape directly.

Safer options:

- keep the current nested merge behavior
- or normalize MQTT payloads into HTTP-like nested shape before storing them
