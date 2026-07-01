"""Config flow for YoLink Local integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .api import AuthenticationError, create_client
from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_HUB_IP,
    CONF_NET_ID,
    CONF_SECONDARY_HUB_IP,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HUB_IP): str,
        vol.Optional(CONF_SECONDARY_HUB_IP): str,
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
        vol.Required(CONF_NET_ID): str,
    }
)


def _clean_config_data(data: dict[str, Any]) -> dict[str, Any]:
    """Return config data with empty optional values removed."""
    cleaned = {
        CONF_HUB_IP: data[CONF_HUB_IP].strip(),
        CONF_CLIENT_ID: data[CONF_CLIENT_ID].strip(),
        CONF_CLIENT_SECRET: data[CONF_CLIENT_SECRET].strip(),
        CONF_NET_ID: data[CONF_NET_ID].strip(),
    }
    secondary_host = data.get(CONF_SECONDARY_HUB_IP, "").strip()
    if secondary_host:
        cleaned[CONF_SECONDARY_HUB_IP] = secondary_host
    return cleaned


def _configured_hosts(data: dict[str, Any]) -> list[str]:
    """Return configured hub hosts, excluding empty optional values."""
    hosts = [data[CONF_HUB_IP]]
    secondary_host = data.get(CONF_SECONDARY_HUB_IP)
    if secondary_host:
        hosts.append(secondary_host)
    return hosts


def _entry_title(data: dict[str, Any]) -> str:
    """Return a config entry title showing configured hub hosts."""
    return f"YoLink Hub ({', '.join(_configured_hosts(data))})"


async def _validate_config_hosts(config_data: dict[str, Any]) -> None:
    """Validate credentials against each configured hub host."""
    for host in _configured_hosts(config_data):
        _client, _token_manager, session = await create_client(
            host=host,
            client_id=config_data[CONF_CLIENT_ID],
            client_secret=config_data[CONF_CLIENT_SECRET],
            hosts=[host],
        )
        await session.close()


def _reconfigure_schema() -> vol.Schema:
    """Return the reconfigure form schema."""
    return vol.Schema(
        {
            vol.Required(CONF_HUB_IP): str,
            vol.Optional(CONF_SECONDARY_HUB_IP): str,
            vol.Required(CONF_CLIENT_ID): str,
            vol.Required(CONF_CLIENT_SECRET): str,
            vol.Required(CONF_NET_ID): str,
        }
    )


class YoLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for YoLink Local."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            config_data = _clean_config_data(user_input)
            try:
                client, _, session = await create_client(
                    host=config_data[CONF_HUB_IP],
                    client_id=config_data[CONF_CLIENT_ID],
                    client_secret=config_data[CONF_CLIENT_SECRET],
                    hosts=_configured_hosts(config_data),
                )
                await session.close()
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during setup")
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=_entry_title(config_data),
                    data=config_data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration of an existing YoLink Local hub."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        suggested_values = entry.data

        if user_input is not None:
            new_data = _clean_config_data(user_input)
            suggested_values = new_data

            try:
                await _validate_config_hosts(new_data)
            except AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during reconfiguration")
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    title=_entry_title(new_data),
                    data=new_data,
                    options={},
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _reconfigure_schema(),
                suggested_values,
            ),
            errors=errors,
        )
