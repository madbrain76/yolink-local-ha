"""Register YoLink lock user/PIN/temporary-code actions on lock entities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import voluptuous as vol
from homeassistant.core import ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform

from .lock_users import (
    CREDENTIAL_TYPES,
    ENROLLABLE_TYPES,
    MAX_USER_ID,
    REPEAT_TYPES,
    TEMP_CODE_TYPES,
    strict_int,
    time_of_day,
)

# strict_int, not vol.Coerce(int): coercion would turn 2.7 or True into a
# different slot.
USER_ID = vol.All(strict_int, vol.Range(min=0, max=MAX_USER_ID))
SLOT_ID = vol.All(strict_int, vol.Range(min=0))
# [0-9], not \d: \d also matches non-ASCII digits that cannot be encoded.
CODE = vol.All(cv.string, vol.Match(r"^[0-9]{4,8}\Z"))
# time_of_day, not cv.time: cv.time can drop extra fields (or seconds) before
# a stricter check sees the value, turning a malformed window into 0:0-0:0.
DAILY_TIME = time_of_day


INDIRECT_TARGETS = ("area_id", "device_id", "floor_id", "label_id")


def require_single_lock_target(entity: Any, call: ServiceCall) -> None:
    """Refuse a delete unless it names exactly this one lock entity.

    User, credential and code ids are slots local to each lock, so the same
    id on another lock is a different credential. Area, device, floor and
    label targets, "all", or several entities could send it to more than
    one lock, so only a single entity_id is accepted.
    """
    data = call.data
    if any(data.get(key) for key in INDIRECT_TARGETS):
        raise ServiceValidationError(
            "Delete actions must target one lock entity, not an area, device, floor or label"
        )
    raw = data.get("entity_id")
    entity_ids = raw.split(",") if isinstance(raw, str) else list(raw or [])
    entity_ids = [str(entity_id).strip() for entity_id in entity_ids if str(entity_id).strip()]
    if entity_ids != [entity.entity_id]:
        raise ServiceValidationError("Delete actions must target exactly one lock entity")


def _single_lock_action(
    method: str, fields: tuple[str, ...]
) -> Callable[[Any, ServiceCall], Awaitable[None]]:
    async def handle(entity: Any, call: ServiceCall) -> None:
        require_single_lock_target(entity, call)
        await getattr(entity, method)(
            **{field: call.data[field] for field in fields if field in call.data}
        )

    return handle


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
            vol.Optional("uses", default=0): vol.All(strict_int, vol.Range(min=0, max=255)),
            vol.Optional("daily_start"): DAILY_TIME,
            vol.Optional("daily_end"): DAILY_TIME,
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
            vol.Required("credential_id"): SLOT_ID,
            vol.Required("credential_type"): vol.In(sorted(CREDENTIAL_TYPES)),
        },
        _single_lock_action(
            "async_delete_credential", ("user_id", "credential_id", "credential_type")
        ),
    )
    register(
        "delete_lock_user",
        {vol.Required("user_id"): USER_ID},
        _single_lock_action("async_delete_user", ("user_id",)),
    )
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
            vol.Optional("daily_start"): DAILY_TIME,
            vol.Optional("daily_end"): DAILY_TIME,
        },
        "async_add_temporary_code",
    )
    register(
        "delete_lock_temporary_code",
        {vol.Required("code_id"): SLOT_ID},
        _single_lock_action("async_delete_temporary_code", ("code_id",)),
    )
