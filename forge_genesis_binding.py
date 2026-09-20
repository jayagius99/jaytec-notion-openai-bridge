"""Fail-closed verifier for Forge Genesis strategic-policy bindings."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from forge_strategic_drives import RootOwnerBoundary, StrategicDriveConfig

POLICY_PATH=Path(__file__).resolve().parent/"forge_genesis_strategic_policy_v1.json"


class GenesisBindingError(RuntimeError):
    pass


def load_policy(path: str | Path=POLICY_PATH) -> dict[str,Any]:
    try:
        value=json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise GenesisBindingError("GENESIS_POLICY_INVALID") from exc
    if not isinstance(value,Mapping):
        raise GenesisBindingError("GENESIS_POLICY_INVALID")
    return dict(value)


def verify_genesis_bindings(
    policy: Mapping[str,Any],
    *,
    current_root_head: str,
    current_root_registry_blob: str,
) -> dict[str,Any]:
    if policy.get("status") != "PRE_GENESIS":
        raise GenesisBindingError("GENESIS_POLICY_STATUS_INVALID")
    binding=policy.get("root_binding")
    if not isinstance(binding,Mapping):
        raise GenesisBindingError("ROOT_BINDING_INVALID")
    if str(binding.get("head") or "") != str(current_root_head or ""):
        raise GenesisBindingError("ROOT_HEAD_DRIFT")
    if str(binding.get("role_registry_blob") or "") != str(current_root_registry_blob or ""):
        raise GenesisBindingError("ROOT_REGISTRY_DRIFT")
    RootOwnerBoundary.parse(policy.get("root_owner_continuity") or {})
    StrategicDriveConfig.parse(policy.get("strategic_drives") or {})
    expected_digest="gitblob:"+str(current_root_registry_blob)
    actual_digest=str((policy.get("root_owner_continuity") or {}).get("root_registry_digest") or "")
    if actual_digest != expected_digest:
        raise GenesisBindingError("ROOT_DIGEST_BINDING_MISMATCH")
    return {
        "schema_version":"FORGE_GENESIS_BINDING_PROOF_V1",
        "status":"PASS",
        "root_head":current_root_head,
        "root_registry_blob":current_root_registry_blob,
        "strategic_policy_bound":True,
        "forge_activated":False,
    }


def main() -> int:
    head=os.environ.get("JAYTEC_ROOT_OWNER_HEAD","").strip()
    blob=os.environ.get("JAYTEC_ROOT_ROLE_REGISTRY_BLOB","").strip()
    if not head or not blob:
        raise SystemExit("ROOT_BINDING_ENV_REQUIRED")
    result=verify_genesis_bindings(load_policy(),current_root_head=head,current_root_registry_blob=blob)
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
