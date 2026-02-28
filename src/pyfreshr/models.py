from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List


@dataclass
class Device:
    id: Optional[str] = None
    active_from: Optional[str] = None
    # preserve any extra fields received
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Device:
        if data is None:
            return cls()
        extras = {k: v for k, v in data.items() if k not in ("id", "active_from")}
        return cls(id=data.get("id"), active_from=data.get("active_from"), extras=extras)


@dataclass
class DeviceCurrent:
    t1: Optional[Any] = None
    t2: Optional[Any] = None
    t3: Optional[Any] = None
    t4: Optional[Any] = None
    flow: Optional[float] = None
    co2: Optional[Any] = None
    hum: Optional[Any] = None
    dp: Optional[Any] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DeviceCurrent:
        if data is None:
            return cls()
        extras = {k: v for k, v in data.items() if k not in ("t1", "t2", "t3", "t4", "flow", "co2", "hum", "dp")}
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
            extras=extras,
        )
