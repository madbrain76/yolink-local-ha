#!/usr/bin/env python3
"""Monitor YoLink MQTT events from primary and secondary hub addresses."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
import paho.mqtt.client as mqtt

from yolink_tooling_common import HUB_HTTP_PORT, HUB_MQTT_PORT, normalize_host


@dataclass
class HostMonitor:
    """Runtime state for one monitored hub address."""

    host: str
    token: str | None = None
    client: mqtt.Client | None = None
    connected: bool = False
    events: int = 0
    auth_failures: int = 0
    mqtt_connect_attempts: int = 0
    last_error: str | None = None


def now_label() -> str:
    """Return a compact UTC timestamp for monitor output."""
    return datetime.now(UTC).isoformat(timespec="seconds")


async def get_token(
    session: aiohttp.ClientSession,
    host: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Get OAuth token from one hub address."""
    url = f"http://{host}:{HUB_HTTP_PORT}/open/yolink/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    async with session.post(url, data=data) as resp:
        resp.raise_for_status()
        result = await resp.json()
    token = result.get("access_token")
    if not token:
        raise RuntimeError(f"Auth failed for {host}: {result}")
    return token


def event_key(payload: dict[str, Any]) -> str:
    """Return a stable key for comparing events across interfaces."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


async def run(args: argparse.Namespace) -> int:
    """Monitor MQTT events on all configured hub addresses."""
    hosts = [normalize_host(args.host)]
    if args.secondary_host:
        secondary_host = normalize_host(args.secondary_host)
        if secondary_host not in hosts:
            hosts.append(secondary_host)

    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    event_counts_by_key: Counter[str] = Counter()
    event_hosts_by_key: dict[str, set[str]] = {}
    monitors: dict[str, HostMonitor] = {
        host: HostMonitor(host=host) for host in hosts
    }
    tasks: list[asyncio.Task[None]] = []

    def make_on_connect(host: str):
        def on_connect(
            client: mqtt.Client,
            _userdata: Any,
            _flags: Any,
            rc: Any,
            _properties: Any = None,
        ) -> None:
            if rc == 0 or str(rc) == "Success":
                monitors[host].connected = True
                client.subscribe(f"ylsubnet/{args.net_id}/+/report")
                print(f"{now_label()} [{host}] MQTT connected and subscribed")
            else:
                monitors[host].last_error = f"MQTT connect failed: {rc}"
                print(f"{now_label()} [{host}] MQTT connect failed: {rc}")

        return on_connect

    def make_on_disconnect(host: str):
        def on_disconnect(
            _client: mqtt.Client,
            _userdata: Any,
            _disconnect_flags: Any,
            rc: Any,
            _properties: Any = None,
        ) -> None:
            monitors[host].connected = False
            monitors[host].last_error = f"MQTT disconnected: {rc}"
            print(f"{now_label()} [{host}] MQTT disconnected: {rc}")

        return on_disconnect

    def make_on_message(host: str):
        def on_message(_client: mqtt.Client, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
            try:
                payload = json.loads(msg.payload.decode())
            except Exception as err:
                print(f"{now_label()} [{host}] MQTT decode failed: {err}")
                return

            key = event_key(payload)
            monitors[host].events += 1
            event_counts_by_key[key] += 1
            event_hosts_by_key.setdefault(key, set()).add(host)
            device_id = payload.get("deviceId", "")
            event = payload.get("event", "")
            timestamp = payload.get("time", "")
            duplicate = " duplicate" if event_counts_by_key[key] > 1 else ""
            print(
                f"{now_label()} [{host}] event#{monitors[host].events}{duplicate}: "
                f"device={device_id} event={event} time={timestamp}"
            )
            if args.stop_after_event:
                loop.call_soon_threadsafe(done.set)

        return on_message

    async def monitor_host(
        session: aiohttp.ClientSession,
        monitor: HostMonitor,
    ) -> None:
        """Keep one host monitored through transient auth/connect failures."""
        while not done.is_set():
            if monitor.client is not None:
                await asyncio.sleep(args.retry_interval)
                continue

            try:
                monitor.token = await get_token(
                    session=session,
                    host=monitor.host,
                    client_id=args.client_id,
                    client_secret=args.client_secret,
                )
            except Exception as err:
                monitor.auth_failures += 1
                monitor.last_error = f"HTTP auth failed: {err}"
                print(f"{now_label()} [{monitor.host}] HTTP auth failed: {err}")
                await asyncio.sleep(args.retry_interval)
                continue

            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.username_pw_set(args.client_id, monitor.token)
            client.on_connect = make_on_connect(monitor.host)
            client.on_disconnect = make_on_disconnect(monitor.host)
            client.on_message = make_on_message(monitor.host)
            monitor.client = client
            monitor.mqtt_connect_attempts += 1
            client.reconnect_delay_set(min_delay=1, max_delay=args.retry_interval)
            client.connect_async(monitor.host, HUB_MQTT_PORT, keepalive=60)
            client.loop_start()
            print(f"{now_label()} [{monitor.host}] MQTT connect attempt started")

        if monitor.client is not None:
            monitor.client.disconnect()
            monitor.client.loop_stop()
            monitor.client = None

    async with aiohttp.ClientSession() as session:
        for monitor in monitors.values():
            tasks.append(asyncio.create_task(monitor_host(session, monitor)))

        print("Monitoring MQTT events. Trigger a YoLink device now.")
        print("Hosts:", ", ".join(monitors))
        try:
            await asyncio.wait_for(done.wait(), timeout=args.timeout)
        except asyncio.TimeoutError:
            print(f"Timed out after {args.timeout}s")
        finally:
            done.set()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

    print("Summary:")
    for host, monitor in monitors.items():
        print(
            f"  {host}: connected={monitor.connected} events={monitor.events} "
            f"auth_failures={monitor.auth_failures} "
            f"mqtt_connect_attempts={monitor.mqtt_connect_attempts} "
            f"last_error={monitor.last_error}"
        )
    duplicate_events = sum(
        1 for hosts_for_event in event_hosts_by_key.values() if len(hosts_for_event) > 1
    )
    print(f"  unique_events={len(event_hosts_by_key)} duplicate_events={duplicate_events}")
    return 0


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Monitor YoLink MQTT events on primary and secondary hub addresses."
    )
    parser.add_argument(
        "--host",
        default=os.getenv("YOLINK_HOST"),
        required=os.getenv("YOLINK_HOST") is None,
        help="Primary hub host or URL (default from YOLINK_HOST)",
    )
    parser.add_argument(
        "--secondary-host",
        default=os.getenv("YOLINK_HOST_SECONDARY") or os.getenv("YOLINK_SECONDARY_HOST"),
        help="Secondary hub host or URL (default from YOLINK_HOST_SECONDARY)",
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("YOLINK_CLIENT_ID"),
        required=os.getenv("YOLINK_CLIENT_ID") is None,
        help="Local API client ID (default from YOLINK_CLIENT_ID)",
    )
    parser.add_argument(
        "--client-secret",
        default=os.getenv("YOLINK_CLIENT_SECRET"),
        required=os.getenv("YOLINK_CLIENT_SECRET") is None,
        help="Local API client secret (default from YOLINK_CLIENT_SECRET)",
    )
    parser.add_argument(
        "--net-id",
        default=os.getenv("YOLINK_NET_ID") or os.getenv("YOLINK_NET"),
        required=os.getenv("YOLINK_NET_ID") is None and os.getenv("YOLINK_NET") is None,
        help="YoLink net ID (default from YOLINK_NET_ID or YOLINK_NET)",
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retry-interval", type=int, default=5)
    parser.add_argument(
        "--stop-after-event",
        action="store_true",
        help="Exit after the first event on any monitored host.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the monitor."""
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
