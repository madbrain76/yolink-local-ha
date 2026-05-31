"""YoLink Local integration for Home Assistant."""

from __future__ import annotations

import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.core import HomeAssistant

from .api import AuthenticationError
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HUB_IP,
    CONF_NET_ID,
    DEFAULT_HTTP_PORT,
    DEFAULT_MQTT_PORT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import YoLocalCoordinator, create_coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up YoLink Local from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    host = entry.data[CONF_HUB_IP]
    coordinator: YoLocalCoordinator | None = None

    try:
        coordinator = await create_coordinator(
            hass=hass,
            host=host,
            client_id=entry.data[CONF_CLIENT_ID],
            client_secret=entry.data[CONF_CLIENT_SECRET],
            config_entry_id=entry.entry_id,
            net_id=entry.data[CONF_NET_ID],
            http_port=DEFAULT_HTTP_PORT,
            mqtt_port=DEFAULT_MQTT_PORT,
        )
        # Perform the first data refresh so the coordinator (and therefore
        # all entities) have valid state before platforms are set up.
        await coordinator.async_config_entry_first_refresh()
    except AuthenticationError as err:
        if coordinator is not None:
            await coordinator.async_shutdown()
        raise ConfigEntryAuthFailed("YoLink Local authentication failed") from err
    except (aiohttp.ClientError, OSError, TimeoutError) as err:
        if coordinator is not None:
            await coordinator.async_shutdown()
        raise ConfigEntryNotReady(
            f"Cannot connect to YoLink hub at {host}"
        ) from err
    except Exception:
        if coordinator is not None:
            await coordinator.async_shutdown()
        _LOGGER.exception("Failed to set up YoLink Local")
        return False

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: YoLocalCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok
