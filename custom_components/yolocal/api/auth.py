"""Authentication and token management."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

import aiohttp


class AuthenticationError(Exception):
    """Raised when authentication fails."""


class TokenManager:
    """Handles OAuth token acquisition and refresh."""

    # Refresh token 5 minutes before expiry
    REFRESH_BUFFER_SECONDS = 300

    def __init__(
        self,
        host: str,
        client_id: str,
        client_secret: str,
        session: aiohttp.ClientSession,
        port: int = 1080,
        hosts: Sequence[str] | None = None,
    ) -> None:
        """Initialize the token manager."""
        self._hosts = tuple(dict.fromkeys(hosts or [host]))
        self._host_index = 0
        self._port = port
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session
        self._token: str | None = None
        self._expires_at: float = 0
        self._host_tokens: dict[str, tuple[str, float]] = {}
        self._refresh_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        """Return the base URL for the hub."""
        return f"http://{self.host}:{self._port}"

    @property
    def host(self) -> str:
        """Return the active hub host."""
        return self._hosts[self._host_index]

    @property
    def hosts(self) -> tuple[str, ...]:
        """Return all configured hub hosts."""
        return self._hosts

    @property
    def client_id(self) -> str:
        """Return the client ID (needed for MQTT auth)."""
        return self._client_id

    async def get_token(self) -> str:
        """Return a valid token, refreshing if needed."""
        cached_token = self._cached_token(self.host)
        if cached_token is not None:
            return cached_token

        async with self._refresh_lock:
            cached_token = self._cached_token(self.host)
            if cached_token is not None:
                return cached_token
            await self._refresh()

            cached_token = self._cached_token(self.host)
            if cached_token is None:
                raise AuthenticationError("No token available")
            return cached_token

    async def get_token_for_host(self, host: str) -> str:
        """Return a valid token obtained from a specific hub host."""
        cached_token = self._cached_token(host)
        if cached_token is not None:
            return cached_token

        async with self._refresh_lock:
            cached_token = self._cached_token(host)
            if cached_token is not None:
                return cached_token
            return await self._refresh_host(host)

    def _is_expired(self) -> bool:
        """Check if the token is expired or about to expire."""
        if self._token is None:
            return True
        return time.time() >= (self._expires_at - self.REFRESH_BUFFER_SECONDS)

    def _cached_token(self, host: str) -> str | None:
        """Return the cached token for a host when it is still valid."""
        cached = self._host_tokens.get(host)
        if cached is None:
            return None
        token, expires_at = cached
        if time.time() >= (expires_at - self.REFRESH_BUFFER_SECONDS):
            self._host_tokens.pop(host, None)
            return None
        return token

    def _store_token(self, host: str, token: str, expires_in: int | float) -> None:
        """Store a token for a hub host."""
        expires_at = time.time() + expires_in
        self._host_tokens[host] = (token, expires_at)
        if host == self.host:
            self._token = token
            self._expires_at = expires_at

    def switch_host(self) -> bool:
        """Switch to the next configured host and invalidate the cached token."""
        if len(self._hosts) <= 1:
            return False
        self._host_index = (self._host_index + 1) % len(self._hosts)
        self._token = None
        self._expires_at = 0
        return True

    async def _refresh(self) -> None:
        """Obtain a new token from the hub."""
        if self._cached_token(self.host) is not None:
            return

        last_error: Exception | None = None
        for _ in self._hosts:
            try:
                await self._refresh_host(self.host)
            except (aiohttp.ClientConnectionError, TimeoutError, OSError) as err:
                last_error = err
                if not self.switch_host():
                    raise
                continue
            return

        if last_error is not None:
            raise last_error
        raise AuthenticationError("No hub hosts configured")

    async def _refresh_host(self, host: str) -> str:
        """Obtain a new token from one specific hub host."""
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        url = f"http://{host}:{self._port}/open/yolink/token"
        async with self._session.post(url, data=data) as resp:
            resp.raise_for_status()
            result = await resp.json()

        if "access_token" not in result:
            raise AuthenticationError(f"Auth failed: {result}")

        token = result["access_token"]
        expires_in = result.get("expires_in", 7200)
        self._store_token(host, token, expires_in)
        return token
