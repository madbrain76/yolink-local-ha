"""Device data model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Device:
    """Represents a YoLink device."""

    device_id: str
    name: str
    token: str
    device_type: str
    display_type: str
    model: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Device:
        """Create a Device from API response data."""
        # Extract model number from appEui (e.g., "d88b4c7804000000" -> "YS7804-UC")
        model = None
        display_type = data["type"]
        if app_eui := data.get("appEui"):
            if len(app_eui) >= 10:
                model_num = app_eui[6:10]
                model = f"YS{model_num}-UC"
                # Normalize user-facing type names for known models.
                if model_num == "7706":
                    display_type = "TiltSensor"
                elif model_num == "8004":
                    display_type = "TempSensor"

        return cls(
            device_id=data["deviceId"],
            name=data["name"],
            token=data["token"],
            device_type=data["type"],
            display_type=display_type,
            model=model,
        )
