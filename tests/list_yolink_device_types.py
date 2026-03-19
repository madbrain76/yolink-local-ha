#!/usr/bin/env python3
"""List unique YoLink device type/model pairs from a local hub."""

from __future__ import annotations

import argparse
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from yolink_tooling_common import (
    HUB_HTTP_PORT,
    model_num_from_app_eui,
    normalize_display_type,
    normalize_host,
)


def get_token(base_url: str, client_id: str, client_secret: str) -> str:
    """Fetch OAuth token."""
    req = Request(
        f"{base_url}/open/yolink/token",
        data=urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode(),
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Auth failed: {data}")
    return token


def get_devices(base_url: str, token: str) -> list[dict]:
    """Fetch device list."""
    req = Request(
        f"{base_url}/open/yolink/v2/api",
        data=json.dumps({"method": "Home.getDeviceList"}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    if data.get("code") != "000000":
        raise RuntimeError(f"API error: {data}")
    return data.get("data", {}).get("devices", [])


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="List unique YoLink device type/model pairs",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("YOLINK_HOST"),
        required=os.getenv("YOLINK_HOST") is None,
        help="Hub host or URL (default from YOLINK_HOST)",
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
    return parser.parse_args()


def main() -> int:
    """Entrypoint."""
    args = parse_args()
    host = normalize_host(args.host)
    base_url = f"http://{host}:{HUB_HTTP_PORT}"
    token = get_token(base_url, args.client_id, args.client_secret)
    devices = get_devices(base_url, token)

    rows: set[tuple[str, str]] = set()
    for device in devices:
        raw_type = device.get("type") or "Unknown"
        app_eui = device.get("appEui") or ""
        model_num = model_num_from_app_eui(app_eui)
        model = f"YS{model_num}-UC" if model_num else "Unknown"
        display_type = normalize_display_type(raw_type, app_eui)
        rows.add((display_type, model))

    col1 = "Device type"
    col2 = "Model"
    width1 = max(len(col1), *(len(row[0]) for row in rows))
    width2 = max(len(col2), *(len(row[1]) for row in rows))
    sep = f"+-{'-' * width1}-+-{'-' * width2}-+"
    print(sep)
    print(f"| {col1:<{width1}} | {col2:<{width2}} |")
    print(sep)
    for device_type, model in sorted(rows):
        print(f"| {device_type:<{width1}} | {model:<{width2}} |")
    print(sep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
