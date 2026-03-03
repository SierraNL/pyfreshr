from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DeviceType(StrEnum):
    """Categorised device type derived from the raw ``type`` string returned by the API."""

    FRESH_R = "fresh-r"
    FORWARD = "forward"
    MONITOR = "monitor"


@dataclass
class DeviceSummary:
    id: str
    type: str = "unknown"
    active_from: str | None = None
    # preserve any extra fields received
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def device_type(self) -> DeviceType:
        """Return the categorised :class:`DeviceType` for this device.

        Mirrors the JS logic: ``deviceType.includes("forward")`` etc.
        Defaults to :attr:`DeviceType.FRESH_R` for unrecognised type strings.
        """
        t = (self.type or "").lower()
        if "forward" in t:
            return DeviceType.FORWARD
        if "monitor" in t:
            return DeviceType.MONITOR
        return DeviceType.FRESH_R

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceSummary:
        if not data:
            raise ValueError("DeviceSummary.from_dict requires a non-empty dict with an 'id' key")
        extras = {k: v for k, v in data.items() if k not in ("id", "type", "active_from")}
        return cls(
            id=data["id"],
            type=data.get("type", "unknown"),
            active_from=data.get("active_from"),
            extras=extras,
        )


@dataclass
class DeviceReadings:
    t1: float | None = None
    t2: float | None = None
    t3: float | None = None
    t4: float | None = None
    flow: float | None = None
    co2: int | None = None
    hum: int | None = None
    dp: float | None = None
    temp: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceReadings:
        if data is None:
            return cls()
        known = {"t1", "t2", "t3", "t4", "flow", "co2", "hum", "dp", "temp"}
        extras = {k: v for k, v in data.items() if k not in known}

        def _float(key: str) -> float | None:
            v = data.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _int(key: str) -> int | None:
            v = data.get(key)
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return cls(
            t1=_float("t1"),
            t2=_float("t2"),
            t3=_float("t3"),
            t4=_float("t4"),
            flow=_float("flow"),
            co2=_int("co2"),
            hum=_float("hum"),
            dp=_float("dp"),
            temp=_float("temp"),
            extras=extras,
        )
