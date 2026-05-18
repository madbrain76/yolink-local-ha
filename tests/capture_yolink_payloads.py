#!/usr/bin/env python3
"""Capture raw YoLink HTTP and MQTT payloads for configured devices."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt

from yolink_tooling_common import (
    HUB_HTTP_PORT,
    HUB_MQTT_PORT,
    normalize_display_type,
    normalize_host,
)


SERIAL_SUFFIX = "_SERIAL"
REDACTED = "REDACTED"
SENSITIVE_KEYS = {
    "accesstoken",
    "authorization",
    "clientsecret",
    "refreshtoken",
    "token",
    "secret",
}


@dataclass(slots=True)
class TrackedDevice:
    """Device selected for capture."""

    env_name: str
    device_id: str
    name: str
    raw_type: str
    display_type: str
    token: str
    alias: str

    @property
    def http_method(self) -> str:
        """Return the getState method for this device."""
        return f"{self.raw_type}.getState"


def iso_now() -> str:
    """Return current UTC timestamp."""
    return datetime.now(UTC).isoformat()


def json_default(value: Any) -> str:
    """Serialize fallback objects."""
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unsupported type: {type(value)!r}")


def write_json(path: Path, payload: Any) -> None:
    """Write structured JSON with stable formatting."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=json_default) + "\n")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSON record to a jsonl file."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=json_default) + "\n")


def alias_for_device(display_type: str, index: int) -> str:
    """Return a stable, non-secret alias for captured device artifacts."""
    safe_type = "".join(char for char in display_type if char.isalnum()) or "Device"
    return f"{safe_type}-{index}"


def sanitize_value(
    value: Any,
    *,
    device_aliases: dict[str, str],
    host: str,
    net_id: str,
) -> Any:
    """Remove credentials and local identifiers from capture output."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = key.replace("_", "").replace("-", "").lower()
            if normalized_key in SENSITIVE_KEYS:
                sanitized[key] = REDACTED
            elif key in {"host", "hub", "hub_host"}:
                sanitized[key] = "REDACTED_HOST"
            elif key in {"net_id", "netId", "net"}:
                sanitized[key] = "REDACTED_NET_ID"
            elif key in {"deviceId", "targetDevice", "device_id"} and isinstance(item, str):
                sanitized[key] = device_aliases.get(item, REDACTED)
            elif key in {"name", "device_name"} and isinstance(item, str):
                sanitized[key] = device_aliases.get(item, REDACTED)
            elif key in {"appEui"}:
                sanitized[key] = REDACTED
            else:
                sanitized[key] = sanitize_value(
                    item,
                    device_aliases=device_aliases,
                    host=host,
                    net_id=net_id,
                )
        return sanitized
    if isinstance(value, list):
        return [
            sanitize_value(item, device_aliases=device_aliases, host=host, net_id=net_id)
            for item in value
        ]
    if isinstance(value, str):
        sanitized = value
        for raw_value, alias in device_aliases.items():
            sanitized = sanitized.replace(raw_value, alias)
        if host:
            sanitized = sanitized.replace(host, "REDACTED_HOST")
        if net_id:
            sanitized = sanitized.replace(net_id, "REDACTED_NET_ID")
        return sanitized
    return value


def configured_serials() -> dict[str, str]:
    """Return all configured *_SERIAL environment variables."""
    found: dict[str, str] = {}
    for key, value in os.environ.items():
        if not key.endswith(SERIAL_SUFFIX):
            continue
        if not value:
            continue
        found[key] = value
    return dict(sorted(found.items()))


async def get_token(
    session: aiohttp.ClientSession,
    host: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Get OAuth token from the hub."""
    url = f"http://{host}:{HUB_HTTP_PORT}/open/yolink/token"
    form = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    async with session.post(url, data=form) as response:
        response.raise_for_status()
        payload = await response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Auth failed: {payload}")
    return token


async def api_request(
    session: aiohttp.ClientSession,
    host: str,
    token: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Call the hub API and return the full JSON response."""
    url = f"http://{host}:{HUB_HTTP_PORT}/open/yolink/v2/api"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with session.post(url, json=payload, headers=headers) as response:
        response.raise_for_status()
        return await response.json()


def resolve_devices(
    devices: list[dict[str, Any]],
    serial_map: dict[str, str],
) -> list[TrackedDevice]:
    """Match configured serial env vars to hub devices."""
    by_id = {device.get("deviceId"): device for device in devices}
    tracked: list[TrackedDevice] = []
    missing: list[dict[str, str]] = []
    type_counts: dict[str, int] = {}

    for env_name, device_id in serial_map.items():
        device = by_id.get(device_id)
        if device is None:
            missing.append({"env_name": env_name, "device_id": device_id})
            continue
        display_type = normalize_display_type(
            device.get("type", ""),
            device.get("appEui"),
        )
        type_counts[display_type] = type_counts.get(display_type, 0) + 1
        tracked.append(
            TrackedDevice(
                env_name=env_name,
                device_id=device_id,
                name=device.get("name", ""),
                raw_type=device.get("type", ""),
                display_type=display_type,
                token=device.get("token", ""),
                alias=alias_for_device(display_type, type_counts[display_type]),
            )
        )

    if missing:
        raise RuntimeError(f"Configured device ids not found on hub: {missing}")

    return tracked


async def fetch_http_state(
    *,
    session: aiohttp.ClientSession,
    host: str,
    output_host: str,
    net_id: str,
    oauth_token: str,
    tracked: TrackedDevice,
    device_aliases: dict[str, str],
) -> dict[str, Any]:
    """Fetch one device state and return a capture record."""
    api_payload = {
        "method": tracked.http_method,
        "targetDevice": tracked.device_id,
        "token": tracked.token,
    }
    response = await api_request(session, host, oauth_token, api_payload)
    return sanitize_value(
        {
            "captured_at": iso_now(),
            "device_id": tracked.device_id,
            "device_name": tracked.name,
            "env_name": tracked.env_name,
            "display_type": tracked.display_type,
            "raw_type": tracked.raw_type,
            "request": api_payload,
            "response": response,
        },
        device_aliases=device_aliases,
        host=output_host,
        net_id=net_id,
    )


async def capture_http_baseline(
    *,
    session: aiohttp.ClientSession,
    host: str,
    output_host: str,
    net_id: str,
    oauth_token: str,
    tracked_devices: list[TrackedDevice],
    device_aliases: dict[str, str],
    out_dir: Path,
) -> list[dict[str, Any]]:
    """Capture one initial HTTP state snapshot per device."""
    baselines: list[dict[str, Any]] = []
    for tracked in tracked_devices:
        record = await fetch_http_state(
            session=session,
            host=host,
            output_host=output_host,
            net_id=net_id,
            oauth_token=oauth_token,
            tracked=tracked,
            device_aliases=device_aliases,
        )
        baselines.append(record)
        device_dir = out_dir / "devices" / tracked.alias
        device_dir.mkdir(parents=True, exist_ok=True)
        write_json(device_dir / "http_initial.json", record)
        append_jsonl(out_dir / "http_events.jsonl", {**record, "capture_kind": "initial"})
    return baselines


async def run_capture(args: argparse.Namespace) -> int:
    """Run the capture session."""
    host = normalize_host(args.host)
    serial_map = configured_serials()
    if not serial_map:
        raise RuntimeError("No *_SERIAL environment variables are set.")

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "devices").mkdir(exist_ok=True)

    session = aiohttp.ClientSession()
    mqtt_client: mqtt.Client | None = None
    pending_http: set[asyncio.Task[None]] = set()
    event_counts: dict[str, int] = {}
    event_index = 0
    loop = asyncio.get_running_loop()

    try:
        oauth_token = await get_token(
            session=session,
            host=host,
            client_id=args.client_id,
            client_secret=args.client_secret,
        )
        device_list_response = await api_request(
            session=session,
            host=host,
            token=oauth_token,
            payload={"method": "Home.getDeviceList"},
        )
        devices = device_list_response.get("data", {}).get("devices", [])
        tracked_devices = resolve_devices(devices, serial_map)
        tracked_by_id = {device.device_id: device for device in tracked_devices}
        device_aliases = {
            device.device_id: device.alias for device in tracked_devices
        } | {
            device.name: device.alias
            for device in tracked_devices
            if device.name
        }

        metadata = sanitize_value(
            {
                "captured_at": iso_now(),
                "host": host,
                "net_id": args.net_id,
                "duration_seconds": args.duration,
                "serial_env": serial_map,
                "tracked_devices": [
                    {
                        "env_name": device.env_name,
                        "device_id": device.device_id,
                        "name": device.name,
                        "alias": device.alias,
                        "raw_type": device.raw_type,
                        "display_type": device.display_type,
                    }
                    for device in tracked_devices
                ],
                "device_list_response": device_list_response,
            },
            device_aliases=device_aliases,
            host=host,
            net_id=args.net_id,
        )
        write_json(out_dir / "metadata.json", metadata)

        await capture_http_baseline(
            session=session,
            host=host,
            output_host=host,
            net_id=args.net_id,
            oauth_token=oauth_token,
            tracked_devices=tracked_devices,
            device_aliases=device_aliases,
            out_dir=out_dir,
        )

        def schedule_follow_up_http(
            tracked: TrackedDevice,
            raw_payload: dict[str, Any],
            sequence: int,
        ) -> None:
            async def runner() -> None:
                try:
                    record = await fetch_http_state(
                        session=session,
                        host=host,
                        output_host=host,
                        net_id=args.net_id,
                        oauth_token=oauth_token,
                        tracked=tracked,
                        device_aliases=device_aliases,
                    )
                except Exception as exc:
                    error_record = sanitize_value(
                        {
                            "captured_at": iso_now(),
                            "capture_kind": "post_mqtt_error",
                            "sequence": sequence,
                            "device_id": tracked.device_id,
                            "device_name": tracked.name,
                            "env_name": tracked.env_name,
                            "display_type": tracked.display_type,
                            "raw_type": tracked.raw_type,
                            "mqtt_payload": raw_payload,
                            "error": repr(exc),
                        },
                        device_aliases=device_aliases,
                        host=host,
                        net_id=args.net_id,
                    )
                    device_dir = out_dir / "devices" / tracked.alias
                    device_dir.mkdir(parents=True, exist_ok=True)
                    write_json(device_dir / f"http_after_mqtt_{sequence:04d}.json", error_record)
                    append_jsonl(out_dir / "http_events.jsonl", error_record)
                    return

                full_record = {
                    **record,
                    "capture_kind": "post_mqtt",
                    "sequence": sequence,
                    "mqtt_payload": sanitize_value(
                        raw_payload,
                        device_aliases=device_aliases,
                        host=host,
                        net_id=args.net_id,
                    ),
                }
                append_jsonl(out_dir / "http_events.jsonl", full_record)
                device_dir = out_dir / "devices" / tracked.alias
                write_json(
                    device_dir / f"http_after_mqtt_{sequence:04d}.json",
                    full_record,
                )

            task = asyncio.create_task(runner())
            pending_http.add(task)
            task.add_done_callback(pending_http.discard)

        def on_message(
            client: mqtt.Client,
            userdata: Any,
            msg: mqtt.MQTTMessage,
        ) -> None:
            nonlocal event_index
            del client, userdata

            try:
                payload = json.loads(msg.payload.decode())
            except Exception as exc:
                append_jsonl(
                    out_dir / "mqtt_events.jsonl",
                    {
                        "captured_at": iso_now(),
                        "topic": msg.topic,
                        "decode_error": repr(exc),
                        "payload_text": msg.payload.decode(errors="replace"),
                    },
                )
                return

            device_id = payload.get("deviceId")
            tracked = tracked_by_id.get(device_id)
            if tracked is None:
                return

            event_index += 1
            event_counts[device_id] = event_counts.get(device_id, 0) + 1
            record = {
                "captured_at": iso_now(),
                "sequence": event_index,
                "topic": msg.topic,
                "device_id": tracked.device_id,
                "device_name": tracked.name,
                "env_name": tracked.env_name,
                "display_type": tracked.display_type,
                "raw_type": tracked.raw_type,
                "mqtt_payload": payload,
            }
            record = sanitize_value(
                record,
                device_aliases=device_aliases,
                host=host,
                net_id=args.net_id,
            )
            append_jsonl(out_dir / "mqtt_events.jsonl", record)
            device_dir = out_dir / "devices" / tracked.alias
            write_json(device_dir / f"mqtt_{event_index:04d}.json", record)
            loop.call_soon_threadsafe(schedule_follow_up_http, tracked, payload, event_index)

        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        mqtt_client.username_pw_set(args.client_id, oauth_token)
        mqtt_client.on_message = on_message
        mqtt_client.connect_async(host, HUB_MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
        await asyncio.sleep(1)
        mqtt_client.subscribe(f"ylsubnet/{args.net_id}/+/report")

        print(f"Capturing {len(tracked_devices)} devices for {args.duration}s")
        print(f"Output directory: {out_dir}")
        for tracked in tracked_devices:
            print(
                f"- {tracked.alias} {tracked.display_type} "
                f"({tracked.env_name})"
            )

        await asyncio.sleep(args.duration)

        if pending_http:
            await asyncio.gather(*pending_http, return_exceptions=True)

        aliased_event_counts = {
            tracked_by_id[device_id].alias: count
            for device_id, count in event_counts.items()
            if device_id in tracked_by_id
        }
        summary = sanitize_value(
            {
                "finished_at": iso_now(),
                "host": host,
                "net_id": args.net_id,
                "duration_seconds": args.duration,
                "output_dir": str(out_dir),
                "event_counts": aliased_event_counts,
                "devices_without_mqtt_events": [
                    tracked.device_id
                    for tracked in tracked_devices
                    if event_counts.get(tracked.device_id, 0) == 0
                ],
            },
            device_aliases=device_aliases,
            host=host,
            net_id=args.net_id,
        )
        write_json(out_dir / "summary.json", summary)
        return 0
    finally:
        if mqtt_client:
            mqtt_client.disconnect()
            time.sleep(0.2)
            mqtt_client.loop_stop()
        for task in list(pending_http):
            task.cancel()
            with suppress(Exception):
                await task
        await session.close()


def parse_args() -> argparse.Namespace:
    """Parse CLI args."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    parser = argparse.ArgumentParser(
        description="Capture raw YoLink HTTP and MQTT payloads for configured devices.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("YOLINK_HOST"),
        required=os.getenv("YOLINK_HOST") is None,
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("YOLINK_CLIENT_ID"),
        required=os.getenv("YOLINK_CLIENT_ID") is None,
    )
    parser.add_argument(
        "--client-secret",
        default=os.getenv("YOLINK_CLIENT_SECRET"),
        required=os.getenv("YOLINK_CLIENT_SECRET") is None,
    )
    parser.add_argument(
        "--net-id",
        default=os.getenv("YOLINK_NET_ID") or os.getenv("YOLINK_NET"),
        required=os.getenv("YOLINK_NET_ID") is None and os.getenv("YOLINK_NET") is None,
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="How long to listen for MQTT updates.",
    )
    parser.add_argument(
        "--output-dir",
        default=f"captures/yolink-payloads-{timestamp}",
        help="Directory for capture artifacts.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    return asyncio.run(run_capture(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
