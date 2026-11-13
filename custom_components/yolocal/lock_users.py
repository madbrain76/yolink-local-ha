"""User, PIN and temporary-code management for YoLink LockV2 locks.

Mixed into the lock entity and exposed as Home Assistant entity actions
(see lock_services.py). Secrets are base64-encoded on the wire, never logged,
and masked in action responses unless explicitly requested.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .api import ApiError
from .const import LOCK_V2_DEVICE_TYPE

MAX_USER_ID = 11  # YoLink LockV2 supports user slots 0-11
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 8
ENROLLABLE_TYPES = {"fingerprint": "Fingerprint", "card": "Card", "fob": "Fob"}
CREDENTIAL_TYPES = {**ENROLLABLE_TYPES, "pin": "Code"}
TEMP_CODE_TYPES = {"one_time": "OneTime", "time_range": "RangeTime", "recurring": "PeriodTime"}
TEMP_CODE_TYPES_REVERSE = {v: k for k, v in TEMP_CODE_TYPES.items()}
REPEAT_TYPES = {"weekly", "monthly"}


def _encode_secret(code: str) -> str:
    return base64.b64encode(code.encode("ascii")).decode("ascii")


def _decode_secret(secret: Any) -> str | None:
    if not isinstance(secret, str):
        return None
    try:
        return base64.b64decode(secret, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError):
        return None


def _mask(code: str | None) -> str | None:
    if not code:
        return None
    return "•" * max(len(code) - 2, 0) + code[-2:]


def _validate_code(code: str, label: str) -> str:
    code = str(code).strip()
    if not code.isdigit() or not PIN_MIN_LENGTH <= len(code) <= PIN_MAX_LENGTH:
        raise ServiceValidationError(
            f"{label} must be {PIN_MIN_LENGTH}-{PIN_MAX_LENGTH} digits"
        )
    return code


def _validate_user_id(user_id: int) -> int:
    user_id = int(user_id)
    if not 0 <= user_id <= MAX_USER_ID:
        raise ServiceValidationError(f"user_id must be between 0 and {MAX_USER_ID}")
    return user_id


def _to_ms(value: datetime) -> int:
    return int(dt_util.as_utc(value).timestamp() * 1000)


def _from_ms(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    return dt_util.utc_from_timestamp(value / 1000).isoformat()


def _hm(value: Any) -> str:
    """Format a time (or 'HH:MM[:SS]' string) as YoLink's 'H:m'."""
    if hasattr(value, "hour"):
        return f"{value.hour}:{value.minute}"
    parts = str(value).split(":")
    try:
        return f"{int(parts[0])}:{int(parts[1])}"
    except (IndexError, ValueError) as err:
        raise ServiceValidationError(f"Invalid time of day: {value}") from err


def _describe_credential(credential: dict[str, Any], include_codes: bool) -> dict[str, Any]:
    cipher = credential.get("cipher") if isinstance(credential.get("cipher"), dict) else {}
    described: dict[str, Any] = {"id": credential.get("id")}
    cipher_type = cipher.get("type")
    if cipher_type:
        described["type"] = "pin" if str(cipher_type).lower() in ("code", "password") else str(cipher_type).lower()
    code = _decode_secret(cipher.get("secret"))
    if code is not None:
        described["code"] = code if include_codes else _mask(code)
    if start := _from_ms(credential.get("startAt")):
        described["start"] = start
    if end := _from_ms(credential.get("endAt")):
        described["end"] = end
    period = credential.get("effectivePeriod")
    if isinstance(period, dict) and period.get("type") not in (None, "disable"):
        described["repeat"] = period.get("type")
        if period.get("value"):
            described["days"] = period.get("value")
    for key, name in (
        ("effectiveStartTime", "daily_start"),
        ("effectiveEndTime", "daily_end"),
        ("effectiveTimes", "uses"),
    ):
        if credential.get(key) not in (None, ""):
            described[name] = credential[key]
    return described


class LockUserManagementMixin:
    """Entity actions for managing users, PINs and temporary codes."""

    async def _user_management(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._device.device_type != LOCK_V2_DEVICE_TYPE:
            raise ServiceValidationError(
                f"{self._device.name} does not support user management"
            )
        try:
            result = await self.coordinator.async_user_management(
                self._device.device_id, params
            )
        except ApiError as err:
            # err never includes the request params, so no secret leaks here.
            raise HomeAssistantError(
                f"{self._device.name} rejected {params.get('command')}: {err.code} {err.desc}"
            ) from err
        return result if isinstance(result, dict) else {}

    # --- users and their credentials --------------------------------------

    async def async_get_users(self) -> dict[str, Any]:
        result = await self._user_management({"command": "getUserList"})
        users = result.get("users") if isinstance(result.get("users"), list) else []
        return {"users": [u.get("id") for u in users if isinstance(u, dict)]}

    async def async_get_user_credentials(
        self, user_id: int, include_codes: bool = False
    ) -> dict[str, Any]:
        user_id = _validate_user_id(user_id)
        result = await self._user_management(
            {"command": "getUserCredentials", "userId": user_id}
        )
        credentials = result.get("credentials")
        return {
            "user_id": user_id,
            "credentials": [
                _describe_credential(c, include_codes)
                for c in (credentials if isinstance(credentials, list) else [])
                if isinstance(c, dict)
            ],
        }

    async def async_add_pin(
        self,
        user_id: int,
        pin: str,
        uses: int = 0,
        daily_start: Any = None,
        daily_end: Any = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "command": "addUserCredential",
            "userId": _validate_user_id(user_id),
            "cipher": {"type": "Code", "secret": _encode_secret(_validate_code(pin, "PIN"))},
            "effectivePeriod": {"type": "disable"},
            "effectiveTimes": int(uses),
        }
        params.update(_daily_window(daily_start, daily_end))
        result = await self._user_management(params)
        return {"credentials": _credential_ids(result)}

    async def async_enroll_credential(
        self, user_id: int, credential_type: str
    ) -> dict[str, Any]:
        cipher_type = ENROLLABLE_TYPES.get(str(credential_type).lower())
        if cipher_type is None:
            raise ServiceValidationError(
                f"credential_type must be one of {sorted(ENROLLABLE_TYPES)}"
            )
        result = await self._user_management(
            {
                "command": "addUserCredential",
                "userId": _validate_user_id(user_id),
                "cipher": {"type": cipher_type},
                "effectivePeriod": {"type": "disable"},
                "effectiveTimes": 0,
            }
        )
        return {"credentials": _credential_ids(result)}

    async def async_delete_credential(
        self, user_id: int, credential_id: int, credential_type: str
    ) -> None:
        cipher_type = CREDENTIAL_TYPES.get(str(credential_type).lower())
        if cipher_type is None:
            raise ServiceValidationError(
                f"credential_type must be one of {sorted(CREDENTIAL_TYPES)}"
            )
        await self._user_management(
            {
                "command": "delUserCredential",
                "userId": _validate_user_id(user_id),
                "credentialId": int(credential_id),
                "cipherType": cipher_type,
            }
        )

    async def async_delete_user(self, user_id: int) -> None:
        await self._user_management(
            {"command": "delUser", "userId": _validate_user_id(user_id)}
        )

    # --- temporary codes ---------------------------------------------------

    async def async_get_temporary_codes(
        self, code_type: str | None = None, include_codes: bool = False
    ) -> dict[str, Any]:
        if code_type is None:
            wanted = list(TEMP_CODE_TYPES.values())
        else:
            if code_type not in TEMP_CODE_TYPES:
                raise ServiceValidationError(
                    f"code_type must be one of {sorted(TEMP_CODE_TYPES)}"
                )
            wanted = [TEMP_CODE_TYPES[code_type]]
        codes: list[dict[str, Any]] = []
        for yolink_type in wanted:
            result = await self._user_management(
                {"command": "getTemporaryCredentials", "type": yolink_type}
            )
            credentials = result.get("credentials")
            for credential in credentials if isinstance(credentials, list) else []:
                if isinstance(credential, dict):
                    codes.append(
                        {
                            "code_type": TEMP_CODE_TYPES_REVERSE[yolink_type],
                            **_describe_credential(credential, include_codes),
                        }
                    )
        return {"codes": codes}

    async def async_add_temporary_code(
        self,
        code: str,
        code_type: str,
        start: datetime | None = None,
        end: datetime | None = None,
        repeat: str | None = None,
        days: list[str] | None = None,
        daily_start: Any = None,
        daily_end: Any = None,
    ) -> None:
        yolink_type = TEMP_CODE_TYPES.get(code_type)
        if yolink_type is None:
            raise ServiceValidationError(
                f"code_type must be one of {sorted(TEMP_CODE_TYPES)}"
            )
        params: dict[str, Any] = {
            "command": "addTemporaryCredential",
            "type": yolink_type,
            "cipher": {"type": "Password", "secret": _encode_secret(_validate_code(code, "Code"))},
        }

        if yolink_type in ("RangeTime", "PeriodTime"):
            if start is None or end is None:
                raise ServiceValidationError(f"{code_type} codes need a start and an end")
            # Compare as UTC so a naive and an offset-aware value cannot clash.
            if dt_util.as_utc(end) <= dt_util.as_utc(start):
                raise ServiceValidationError("end must be after start")
        if start is not None:
            params["startAt"] = _to_ms(start)
        if end is not None:
            params["endAt"] = _to_ms(end)

        if yolink_type == "PeriodTime":
            if repeat not in REPEAT_TYPES:
                raise ServiceValidationError("recurring codes need repeat: weekly or monthly")
            if not days:
                raise ServiceValidationError("recurring codes need at least one day")
            params["effectivePeriod"] = {"type": repeat, "value": [str(d) for d in days]}
            params.update(_daily_window(daily_start, daily_end))
        else:
            params["effectivePeriod"] = {"type": "disable"}

        await self._user_management(params)

    async def async_delete_temporary_code(self, code_id: int) -> None:
        await self._user_management(
            {"command": "delTemporaryCredential", "credentialId": int(code_id)}
        )


def _daily_window(daily_start: Any, daily_end: Any) -> dict[str, str]:
    if (daily_start is None) != (daily_end is None):
        raise ServiceValidationError("daily_start and daily_end must be set together")
    if daily_start is None:
        return {}
    return {"effectiveStartTime": _hm(daily_start), "effectiveEndTime": _hm(daily_end)}


def _credential_ids(result: dict[str, Any]) -> list[dict[str, Any]]:
    credentials = result.get("credentials")
    return [
        _describe_credential(c, include_codes=False)
        for c in (credentials if isinstance(credentials, list) else [])
        if isinstance(c, dict)
    ]
