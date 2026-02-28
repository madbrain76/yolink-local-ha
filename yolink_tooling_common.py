"""Shared helpers for standalone YoLink tooling scripts."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

HUB_HTTP_PORT = 1080
HUB_MQTT_PORT = 18080


def normalize_host(host_or_url: str) -> str:
    """Accept host or URL and return hostname."""
    if "://" in host_or_url:
        parsed = urlparse(host_or_url)
        if parsed.hostname:
            return parsed.hostname
    return host_or_url


def model_num_from_app_eui(app_eui: str | None) -> str | None:
    """Extract 4-digit model number from appEui."""
    if app_eui and len(app_eui) >= 10:
        return app_eui[6:10]
    return None


def normalize_display_type(raw_type: str, app_eui: str | None) -> str:
    """Match integration display naming overrides."""
    model_num = model_num_from_app_eui(app_eui)
    if model_num == "7706":
        return "TiltSensor"
    if model_num == "8004":
        return "TempSensor"
    return raw_type


def normalize_event_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract device id + event data from varying MQTT payload formats."""
    device_id = payload.get("deviceId", "")

    raw_data = payload.get("data")
    if raw_data is None and isinstance(payload.get("params"), dict):
        params = payload["params"]
        raw_data = params.get("data", params)
    if raw_data is None and "state" in payload:
        raw_data = {"state": payload.get("state")}

    if isinstance(raw_data, dict):
        event_data = dict(raw_data)
    elif raw_data is not None:
        event_data = {"state": raw_data}
    else:
        event_data = {}

    if "reportAt" not in event_data and payload.get("time"):
        event_data["reportAt"] = payload.get("time")
    if "online" not in event_data and "online" in payload:
        event_data["online"] = payload.get("online")

    return device_id, event_data


def merge_state(existing_state: dict[str, Any], event_data: dict[str, Any]) -> dict[str, Any]:
    """Match integration merge behavior for nested/flat MQTT payloads."""
    new_state = {**existing_state, **event_data}

    existing_state_obj = existing_state.get("state")
    event_state_obj = event_data.get("state")

    merged_state_obj: dict[str, Any] | None = None
    if isinstance(existing_state_obj, dict):
        merged_state_obj = dict(existing_state_obj)
        if isinstance(event_state_obj, dict):
            merged_state_obj.update(event_state_obj)
        elif event_state_obj is not None:
            merged_state_obj["state"] = event_state_obj

        for key, value in event_data.items():
            if key in {"online", "reportAt", "state"}:
                continue
            merged_state_obj[key] = value
            new_state.pop(key, None)
    elif isinstance(event_state_obj, dict):
        merged_state_obj = dict(event_state_obj)

    if merged_state_obj is not None:
        new_state["state"] = merged_state_obj

    return new_state
