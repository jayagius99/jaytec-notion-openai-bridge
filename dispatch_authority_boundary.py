"""Cross-system dispatch boundary between the bridge and V2 authority.

Production provider dispatch stays fail-closed until the V2 work engine can
supply and the bridge can verify an exact dispatch authorization contract.
There is intentionally no environment-variable override for this gate.
"""
from __future__ import annotations

from dataclasses import dataclass


PRODUCTION_DISPATCH_AUTHORITY_INTEGRATED = False
BLOCK_REASON = "V2_DISPATCH_AUTHORITY_NOT_INTEGRATED"


@dataclass(frozen=True)
class DispatchBoundaryDecision:
    allowed: bool
    reason: str


def production_dispatch_authority_integrated() -> bool:
    return PRODUCTION_DISPATCH_AUTHORITY_INTEGRATED


def evaluate_dispatch_boundary(*, runtime_mode: str) -> DispatchBoundaryDecision:
    if runtime_mode == "production" and not production_dispatch_authority_integrated():
        return DispatchBoundaryDecision(False, BLOCK_REASON)
    return DispatchBoundaryDecision(True, "DISPATCH_BOUNDARY_ALLOWED_NONPRODUCTION")
