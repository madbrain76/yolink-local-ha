"""Register YoLink lock user/PIN/temporary-code actions on lock entities."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform

from .lock_users import (
    CREDENTIAL_TYPES,
    ENROLLABLE_TYPES,
    MAX_USER_ID,
    REPEAT_TYPES,
    TEMP_CODE_TYPES,
)

USER_ID = vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_USER_ID))
CODE = vol.All(cv.string, vol.Match(r"^\d{4,8}$"))


def async_register_lock_services() -> None:
    """Register entity actions for the current lock platform."""
    platform = entity_platform.async_get_current_platform()
    register = platform.async_register_entity_service

    register("get_lock_users", {}, "async_get_users", supports_response=SupportsResponse.ONLY)
    register(
        "get_lock_user_credentials",
        {
            vol.Required("user_id"): USER_ID,
            vol.Optional("include_codes", default=False): cv.boolean,
        },
        "async_get_user_credentials",
        supports_response=SupportsResponse.ONLY,
    )
    register(
        "add_lock_pin",
        {
            vol.Required("user_id"): USER_ID,
            vol.Required("pin"): CODE,
            vol.Optional("uses", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
            vol.Optional("daily_start"): cv.time,
            vol.Optional("daily_end"): cv.time,
        },
        "async_add_pin",
        supports_response=SupportsResponse.OPTIONAL,
    )
    register(
        "enroll_lock_credential",
        {
            vol.Required("user_id"): USER_ID,
            vol.Required("credential_type"): vol.In(sorted(ENROLLABLE_TYPES)),
        },
        "async_enroll_credential",
        supports_response=SupportsResponse.OPTIONAL,
    )
    register(
        "delete_lock_credential",
        {
            vol.Required("user_id"): USER_ID,
            vol.Required("credential_id"): vol.Coerce(int),
            vol.Required("credential_type"): vol.In(sorted(CREDENTIAL_TYPES)),
        },
        "async_delete_credential",
    )
    register("delete_lock_user", {vol.Required("user_id"): USER_ID}, "async_delete_user")
    register(
        "get_lock_temporary_codes",
        {
            vol.Optional("code_type"): vol.In(sorted(TEMP_CODE_TYPES)),
            vol.Optional("include_codes", default=False): cv.boolean,
        },
        "async_get_temporary_codes",
        supports_response=SupportsResponse.ONLY,
    )
    register(
        "add_lock_temporary_code",
        {
            vol.Required("code"): CODE,
            vol.Required("code_type"): vol.In(sorted(TEMP_CODE_TYPES)),
            vol.Optional("start"): cv.datetime,
            vol.Optional("end"): cv.datetime,
            vol.Optional("repeat"): vol.In(sorted(REPEAT_TYPES)),
            vol.Optional("days"): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional("daily_start"): cv.time,
            vol.Optional("daily_end"): cv.time,
        },
        "async_add_temporary_code",
    )
    register(
        "delete_lock_temporary_code",
        {vol.Required("code_id"): vol.Coerce(int)},
        "async_delete_temporary_code",
    )
