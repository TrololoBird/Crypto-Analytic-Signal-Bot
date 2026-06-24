"""Signal object — unified lifecycle for Deep + Scanner."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SignalModule = Literal[1, 2]
SignalState = Literal["forming", "signal", "activated", "tracking", "closed"]


@dataclass(slots=True)
class Signal:
    symbol: str
    module: SignalModule
    direction: str
    setup_id: str
    thesis: str
    plan: dict[str, Any]
    state: SignalState
    created_at: str
    activated_at: str = ""
    as_of: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "module": self.module,
            "direction": self.direction,
            "setup_id": self.setup_id,
            "thesis": self.thesis,
            "plan": self.plan,
            "state": self.state,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "as_of": self.as_of,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Signal:
        return cls(
            symbol=str(raw.get("symbol") or "").upper(),
            module=int(raw.get("module") or 1),  # type: ignore[arg-type]
            direction=str(raw.get("direction") or "").lower(),
            setup_id=str(raw.get("setup_id") or ""),
            thesis=str(raw.get("thesis") or ""),
            plan=dict(raw.get("plan") or {}),
            state=str(raw.get("state") or "forming"),  # type: ignore[arg-type]
            created_at=str(raw.get("created_at") or ""),
            activated_at=str(raw.get("activated_at") or ""),
            as_of=str(raw.get("as_of") or ""),
            provenance=dict(raw.get("provenance") or {}),
        )
