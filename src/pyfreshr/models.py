from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeviceType(str, Enum):
    """Categorised device type derived from the raw ``type`` string returned by the API."""

    FRESH_R = "fresh-r"
    FORWARD = "forward"
    MONITOR = "monitor"


@dataclass
class DeviceSummary:
    id: str | None = None
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
        if data is None:
            return cls()
        extras = {k: v for k, v in data.items() if k not in ("id", "type", "active_from")}
        return cls(
            id=data.get("id"),
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
    hum: float | None = None
    dp: float | None = None
    temp: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceReadings:
        if data is None:
            return cls()
        known = {"t1", "t2", "t3", "t4", "flow", "co2", "hum", "dp", "temp"}
        extras = {k: v for k, v in data.items() if k not in known}
        # do not coerce types here; callers may have already converted flow
        flow_val = data.get("flow")
        try:
            if flow_val is not None and not isinstance(flow_val, float):
                flow_val = float(flow_val)
        except (TypeError, ValueError):
            flow_val = None

        return cls(
            t1=data.get("t1"),
            t2=data.get("t2"),
            t3=data.get("t3"),
            t4=data.get("t4"),
            flow=flow_val,
            co2=data.get("co2"),
            hum=data.get("hum"),
            dp=data.get("dp"),
            temp=data.get("temp"),
            extras=extras,
        )
