# THSensor restart-state recovery

Initially assessed 2026-10-12; integration behavior updated 2026-10-25. Scope: a sleeping `THSensor` after Home Assistant or this integration restarts.

## Conclusion

**No YoSmart-documented Local Hub mechanism other than `THSensor.getState` is guaranteed to return a complete last-known THSensor state after restart.** Home Assistant state restoration could preserve the last value Home Assistant already had, but that is local restoration, not a new value from the hub or device. The integration persists device-contact and replay-detection records, but does not restore measurement values or a complete device-state snapshot.

## Verified findings

| Candidate | Finding |
| --- | --- |
| Local `getState` | YoSmart's Local API quick-start uses `DeviceType.getState` to read device state. The THSensor contract declares the complete state and `reportAt`. [Local API quick-start](https://doc.yosmart.com/docs/overall/qsg_local/), [THSensor API](https://doc.yosmart.com/docs/yolinkapi/THSensor/) |
| `fetchState` | `THSensor.fetchState` is not listed in the THSensor API. `Lock.fetchState` is explicitly a lock-only cached-status operation, distinct from `Lock.getState`; it cannot be assumed for TH sensors. [THSensor API](https://doc.yosmart.com/docs/yolinkapi/THSensor/), [Lock API](https://doc.yosmart.com/docs/yolinkapi/Lock/) |
| Activity and metrics logs | `THSensor.getActivityLogs` and `THSensor.getMetricsLogs` are documented, but require `DEVICE.HISTORICAL_DATA.READ`. Activity records have an untyped `data` object; metric records contain timestamped numeric fields. Neither contract promises a latest complete state snapshot. [THSensor API](https://doc.yosmart.com/docs/yolinkapi/THSensor/) |
| MQTT restart recovery | The Local Hub documentation defines `ylsubnet/<net-id>/+/report` as a device-event topic. It does not document retained messages, QoS, persistent-session/offline backlog delivery, or a replay API. [Local Hub MQTT API](https://doc.yosmart.com/docs/protocol/local_hub/openAPILocal/) |
| Home Assistant restoration | Home Assistant supports restoring a sensor's prior stored value through `RestoreSensor` and `async_get_last_sensor_data`. That can preserve the last HA observation, not query YoLink. [Home Assistant sensor restoration](https://developers.home-assistant.io/docs/core/entity/sensor/#restoring-sensor-states) |

## `data.online` semantics (assessed 2026-10-13)

### Documented facts

- The `THSensor.getState` and `DoorSensor.getState` contracts define `data.online` only as "Is device online" and define `data.reportAt` separately as "Time of reported." Neither contract defines how `online` is calculated, nor says it is evaluated at request time, stored with the reported state, or derived from `reportAt`. [THSensor API](https://doc.yosmart.com/docs/yolinkapi/THSensor/), [DoorSensor API](https://doc.yosmart.com/docs/yolinkapi/DoorSensor/)
- Both device pages state that callback and MQTT reports use the same response schema as `getState`. That establishes that a report can carry both fields, but does not specify their temporal relationship. [THSensor callbacks](https://doc.yosmart.com/docs/yolinkapi/THSensor/), [DoorSensor callbacks](https://doc.yosmart.com/docs/yolinkapi/DoorSensor/)
- YoSmart uses more explicit wording for some other device types: `SmartRemoter.getState` is described as "Get last reported state" and still includes `data.online` plus `data.reportAt`. It does not explain whether that `online` value is live or historical. This wording cannot be generalized to THSensor. [SmartRemoter API](https://doc.yosmart.com/docs/yolinkapi/SmartRemoter/)
- The Local Hub quick-start shows a successful `DoorSensor.getState` response containing both `online: false` and `reportAt`, but provides no semantics for either field beyond the device contract. [Local API quick-start](https://doc.yosmart.com/docs/overall/qsg_local/)
- The Local Hub protocol documents HTTP and MQTT transport but adds no `online` or cache-timing semantics. Its error-code table defines `000201` only as "Cannot connect to the device." [Local Hub protocol](https://doc.yosmart.com/docs/protocol/local_hub/openAPILocal/), [error codes](https://doc.yosmart.com/docs/protocol/Code/)

### Inference and consequence

The documentation does **not** support treating `online` from an older successful `getState` response as either a live reachability result or a property of the older cached report. Both interpretations remain possible. A policy of applying `online` while rejecting an older state payload would be an integration choice, not a documented YoSmart API guarantee. The integration rejects stale snapshots entirely; definitive API semantics require confirmation from YoSmart or controlled hub testing.

## Current integration behavior

- Startup restores the contact ledger, performs Home Assistant's first refresh, and the generic client issues `<device type>.getState`; there is no alternate THSensor query. Measurement values initially come from the HTTP response, not the ledger. [setup](../../custom_components/yolocal/__init__.py), [coordinator](../../custom_components/yolocal/coordinator.py), [client](../../custom_components/yolocal/api/client.py)
- MQTT subscribes to the report topic after connecting. The implementation does not explicitly configure a durable MQTT session, QoS, or a request for historical message replay. Persisted event identities detect duplicates received after reconnect or restart. [MQTT client](../../custom_components/yolocal/api/mqtt.py), [coordinator](../../custom_components/yolocal/coordinator.py)
- Entities read measurement values from the coordinator's in-memory state. No integration entity subclasses a Home Assistant restore class or calls a restore API. [base entity](../../custom_components/yolocal/entity.py), [TH sensors](../../custom_components/yolocal/sensor.py)
- `THSensor.DataRecord` batches can restore temperature and humidity from timestamped samples after subscribing, but do not contain a complete device state. This observed local behavior is described in [issue #12](https://github.com/madbrain76/yolink-local-ha/issues/12); it is not documented by the THSensor API page. Newer samples update readings without replacing newer full reports or changing full-report timestamps.

## Unknowns and limits

- YoSmart does not say whether the two history endpoints are available through the Local Hub endpoint, despite sharing the BDDP method format. This was not live-tested.
- YoSmart does not document which fields its THSensor history records contain in practice, retention length, ordering, or whether the newest record is a complete state. They therefore cannot be relied on to reconstruct the current integration state without an observed contract.
- YoSmart does not document MQTT retention, session persistence, QoS, or offline replay for the Local Hub. A subscriber must not rely on those behaviours after restart without vendor confirmation or a controlled test.

## Practical implication

For the current hub/API contract, the only reliable source of a newly obtained complete THSensor state is a successful `THSensor.getState` or a new report received after subscribing. To avoid losing an already-known value across a restart, Home Assistant entity restoration or explicit persistence of the integration's canonical state would be required; both require an implementation change.
