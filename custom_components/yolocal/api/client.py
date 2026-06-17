"""HTTP client for YoLink Local Hub API."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .auth import TokenManager
from .device import Device

READ_REQUEST_RETRY_ATTEMPTS = 3
TRANSPORT_RETRY_DELAY = 1.0


class ApiError(Exception):
    """Raised when an API call fails."""

    def __init__(self, code: str, desc: str, method: str | None = None) -> None:
        """Initialize the API error."""
        self.code = code
        self.desc = desc
        self.method = method
        prefix = f"{method} failed" if method else "API error"
        super().__init__(f"{prefix}: {code} {desc}".strip())


class YoLinkClient:
    """HTTP client for YoLink Local Hub."""

    def __init__(
        self,
        host: str,
        token_manager: TokenManager,
        session: aiohttp.ClientSession,
        port: int = 1080,
    ) -> None:
        """Initialize the client."""
        self._host = host
        self._port = port
        self._token_manager = token_manager
        self._session = session

    @property
    def host(self) -> str:
        """Return the hub host."""
        return self._host

    @property
    def base_url(self) -> str:
        """Return the base URL for the hub."""
        return f"http://{self._host}:{self._port}"

    async def get_devices(self) -> list[Device]:
        """Fetch the list of devices from the hub."""
        result = await self._request(
            {"method": "Home.getDeviceList"},
            retry_transport=True,
        )
        return [Device.from_api(d) for d in result.get("devices", [])]

    async def get_state(self, device: Device) -> dict[str, Any]:
        """Get the current state of a device."""
        return await self._request({
            "method": f"{device.device_type}.getState",
            "targetDevice": device.device_id,
            "token": device.token,
        }, retry_transport=True)

    async def set_state(
        self, device: Device, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Set the state of a device."""
        return await self._request({
            "method": f"{device.device_type}.setState",
            "targetDevice": device.device_id,
            "token": device.token,
            "params": params,
        })

    async def _request(
        self,
        payload: dict[str, Any],
        retry_transport: bool = False,
    ) -> dict[str, Any]:
        """Make an authenticated API request."""
        attempts = READ_REQUEST_RETRY_ATTEMPTS if retry_transport else 1
        url = f"{self.base_url}/open/yolink/v2/api"

        for attempt in range(1, attempts + 1):
            token = await self._token_manager.get_token()
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            }

            try:
                async with self._session.post(
                    url,
                    json=payload,
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    result = await resp.json()
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                if attempt >= attempts:
                    raise
                await asyncio.sleep(TRANSPORT_RETRY_DELAY)
                continue

            if result.get("code") != "000000":
                raise ApiError(
                    code=str(result.get("code", "")),
                    desc=str(result.get("desc", "")),
                    method=str(result.get("method") or payload.get("method") or ""),
                )

            return result.get("data", {})

        raise RuntimeError("unreachable request retry state")


async def create_client(
    host: str,
    client_id: str,
    client_secret: str,
    port: int = 1080,
) -> tuple["YoLinkClient", TokenManager, aiohttp.ClientSession]:
    """Create an authenticated client.

    Returns the client, token manager, and session. Caller is responsible
    for closing the session when done.

    Raises:
        AuthenticationError: If credentials are invalid.
    """
    session = aiohttp.ClientSession()
    try:
        token_manager = TokenManager(host, client_id, client_secret, session, port)
        await token_manager.get_token()  # Validates credentials
        client = YoLinkClient(host, token_manager, session, port)
        return client, token_manager, session
    except Exception:
        await session.close()
        raise
