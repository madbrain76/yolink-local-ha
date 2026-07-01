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
    CONF_SECONDARY_HUB_IP,
    DEFAULT_HTTP_PORT,
    DEFAULT_MQTT_PORT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import YoLocalCoordinator, create_coordinator

_LOGGER = logging.getLogger(__name__)


def _configured_hosts(entry: ConfigEntry) -> list[str]:
    """Return configured primary and optional secondary hosts."""
    hosts = [entry.data[CONF_HUB_IP]]
    secondary_host = entry.data.get(CONF_SECONDARY_HUB_IP)
    if secondary_host:
        hosts.append(secondary_host)
    return hosts


def _entry_title(entry: ConfigEntry) -> str:
    """Return a config entry title showing configured hub hosts."""
    return f"YoLink Hub ({', '.join(_configured_hosts(entry))})"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up YoLink Local from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    host = entry.data[CONF_HUB_IP]
    hosts = _configured_hosts(entry)
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
            hosts=hosts,
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

    expected_title = _entry_title(entry)
    update_kwargs = {}
    if getattr(entry, "title", None) != expected_title:
        update_kwargs["title"] = expected_title
    if getattr(entry, "options", {}):
        update_kwargs["options"] = {}
    if update_kwargs:
        hass.config_entries.async_update_entry(entry, **update_kwargs)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: YoLocalCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok
