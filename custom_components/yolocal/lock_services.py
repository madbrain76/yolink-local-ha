"""Register YoLink lock user/PIN/temporary-code actions on lock entities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, ENTITY_MATCH_ALL
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers import target as target_helpers
from homeassistant.helpers.service import async_register_platform_entity_service

from .const import DOMAIN
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


async def require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Refuse non-admin users, as Home Assistant's admin_only services do.

    Calls without a user (automations, scripts) are allowed, like there.
    """
    if not call.context.user_id:
        return
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None:
        raise UnknownUser(context=call.context)
    if not user.is_admin:
        raise Unauthorized(context=call.context)


def resolve_lockv2_targets(
    hass: HomeAssistant, call: ServiceCall, *, single: bool
) -> list[Any]:
    """Resolve a write action's target to available yolocal LockV2 locks.

    Home Assistant's entity-action helpers skip unavailable or unmatched
    targets and still report success, which would make a revocation that
    reached no lock look done. So targets are resolved here, and the call
    fails unless it reaches at least one available LockV2 (exactly one for
    slot actions, since user and credential slots are local to each lock).
    """
    service = call.service
    registered = {
        entity_id: entity
        for platform in entity_platform.async_get_platforms(hass, DOMAIN)
        if platform.domain == LOCK_DOMAIN
        for entity_id, entity in platform.entities.items()
    }
    if call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
        wanted = set(registered)
    else:
        selected = target_helpers.async_extract_referenced_entity_ids(
            hass, target_helpers.TargetSelection(call.data)
        )
        wanted = selected.referenced | selected.indirectly_referenced
    locks = [registered[entity_id] for entity_id in sorted(wanted) if entity_id in registered]

    if unavailable := [lock.entity_id for lock in locks if not lock.available]:
        raise ServiceValidationError(
            f"{service}: {', '.join(unavailable)} is unavailable; nothing was sent"
        )
    if not_v2 := [lock.entity_id for lock in locks if not getattr(lock, "_is_v2", False)]:
        raise ServiceValidationError(
            f"{service} is only supported on YoLink LockV2 locks, not {', '.join(not_v2)}"
        )
    if not locks:
        raise ServiceValidationError(f"{service} matched no YoLink lock; nothing was sent")
    if single and len(locks) != 1:
        raise ServiceValidationError(
            f"{service} uses lock-specific user and credential slots, so it must target "
            f"exactly one lock (matched {len(locks)}); nothing was sent"
        )
    return locks


def _write_action(
    hass: HomeAssistant, method: str, *, single: bool
) -> Callable[[ServiceCall], Awaitable[Any]]:
    async def handle(call: ServiceCall) -> Any:
        await require_admin(hass, call)
        locks = resolve_lockv2_targets(hass, call, single=single)
        fields = {key: value for key, value in call.data.items() if key not in ENTITY_FIELDS}
        responses = {}
        for lock in locks:
            lock.async_set_context(call.context)
            responses[lock.entity_id] = await getattr(lock, method)(**fields)
        return responses if call.return_response else None

    return handle


def _read_action(
    hass: HomeAssistant, method: str
) -> Callable[[Any, ServiceCall], Awaitable[Any]]:
    async def handle(entity: Any, call: ServiceCall) -> Any:
        # Masked metadata keeps normal entity permissions; full codes need admin.
        if call.data.get("include_codes"):
            await require_admin(hass, call)
        fields = {key: value for key, value in call.data.items() if key not in ENTITY_FIELDS}
        return await getattr(entity, method)(**fields)

    return handle


ENTITY_FIELDS = ("entity_id", "area_id", "device_id", "floor_id", "label_id")


@callback
def async_register_lock_services(hass: HomeAssistant) -> None:
    """Register the lock actions once, for every yolocal lock entity."""
    read = partial(
        async_register_platform_entity_service, hass, DOMAIN, entity_domain=LOCK_DOMAIN
    )

    def write(service: str, schema: dict, method: str, *, single: bool, response: bool) -> None:
        hass.services.async_register(
            DOMAIN,
            service,
            _write_action(hass, method, single=single),
            schema=cv.make_entity_service_schema(schema),
            supports_response=SupportsResponse.OPTIONAL if response else SupportsResponse.NONE,
        )

    read("get_lock_users", schema={}, func="async_get_users", supports_response=SupportsResponse.ONLY)
    read(
        "get_lock_user_credentials",
        schema={
            vol.Required("user_id"): USER_ID,
            vol.Optional("include_codes", default=False): cv.boolean,
        },
        func=_read_action(hass, "async_get_user_credentials"),
        supports_response=SupportsResponse.ONLY,
    )
    read(
        "get_lock_temporary_codes",
        schema={
            vol.Optional("code_type"): vol.In(sorted(TEMP_CODE_TYPES)),
            vol.Optional("include_codes", default=False): cv.boolean,
        },
        func=_read_action(hass, "async_get_temporary_codes"),
        supports_response=SupportsResponse.ONLY,
    )
    write(
        "add_lock_pin",
        {
            vol.Required("user_id"): USER_ID,
            vol.Required("pin"): CODE,
            vol.Optional("uses", default=0): vol.All(strict_int, vol.Range(min=0, max=255)),
            vol.Optional("daily_start"): DAILY_TIME,
            vol.Optional("daily_end"): DAILY_TIME,
        },
        "async_add_pin",
        single=True,
        response=True,
    )
    write(
        "enroll_lock_credential",
        {
            vol.Required("user_id"): USER_ID,
            vol.Required("credential_type"): vol.In(sorted(ENROLLABLE_TYPES)),
        },
        "async_enroll_credential",
        single=True,
        response=True,
    )
    write(
        "delete_lock_credential",
        {
            vol.Required("user_id"): USER_ID,
            vol.Required("credential_id"): SLOT_ID,
            vol.Required("credential_type"): vol.In(sorted(CREDENTIAL_TYPES)),
        },
        "async_delete_credential",
        single=True,
        response=False,
    )
    write(
        "delete_lock_user",
        {vol.Required("user_id"): USER_ID},
        "async_delete_user",
        single=True,
        response=False,
    )
    # A temporary code has no slot, so it may go to several locks.
    write(
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
        single=False,
        response=False,
    )
    write(
        "delete_lock_temporary_code",
        {vol.Required("code_id"): SLOT_ID},
        "async_delete_temporary_code",
        single=True,
        response=False,
    )
