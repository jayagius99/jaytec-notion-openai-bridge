from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from dispatch_authority_receipt import (
    DispatchAuthorityReceiptError,
    build_receipt,
    receipt_digest,
    verify_and_claim_dispatch_authority_receipt,
)
from orchestration import packet_hash


def packet() -> dict:
    return {
        "task_id": "FORGE-GENESIS-ACTIVATION-001",
        "subtask_id": "G03-AUTHORITY-TEST",
        "workflow_id": "JAYTEC-G03",
        "idempotency_key": "g03-authority-test-001",
        "allowed_operations": ["read", "validate"],
        "specialist_plan": ["codex"],
    }


def times() -> tuple[str, str, datetime]:
    now = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
    issued = (now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    expires = (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    return issued, expires, now


def make_receipt(p: dict | None = None, **overrides) -> dict:
    p = p or packet()
    issued, expires, _ = times()
    args = dict(
        authority_id="authority-001",
        packet=p,
        protected_action=False,
        approval_actor="JAYTEC_CONTROL_PLANE",
        estimated_cost_units=0,
        budget_limit_units=0,
        allowed_operations=["read", "validate"],
        allowed_specialists=["codex"],
        issued_at=issued,
        expires_at=expires,
    )
    args.update(overrides)
    return build_receipt(**args)


class TrustedIssuer:
    def __init__(self):
        self.allowed: set[tuple[str, str, str]] = set()
        self.consumed: set[tuple[str, str, str]] = set()

    def issue(self, p: dict, receipt: dict) -> None:
        self.allowed.add(
            (
                receipt["authority_id"],
                receipt["receipt_sha256"],
                packet_hash(p),
            )
        )

    def claim(self, authority_id: str, receipt_sha256: str, packet_sha256: str) -> bool:
        key = (authority_id, receipt_sha256, packet_sha256)
        if key not in self.allowed or key in self.consumed:
            return False
        self.consumed.add(key)
        return True


class DispatchAuthorityReceiptTests(unittest.TestCase):
    def test_valid_exact_receipt_claims_once(self):
        p = packet()
        receipt = make_receipt(p)
        issuer = TrustedIssuer()
        issuer.issue(p, receipt)
        _, _, now = times()

        observed = verify_and_claim_dispatch_authority_receipt(
            packet=p,
            receipt=receipt,
            trusted_claim=issuer.claim,
            now=now,
        )
        self.assertEqual(observed["receipt_sha256"], receipt["receipt_sha256"])

        with self.assertRaisesRegex(
            DispatchAuthorityReceiptError,
            "TRUSTED_AUTHORITY_CLAIM_REJECTED",
        ):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=receipt,
                trusted_claim=issuer.claim,
                now=now,
            )

    def test_self_consistent_forgery_is_not_authority(self):
        p = packet()
        receipt = make_receipt(p)
        forged = copy.deepcopy(receipt)
        forged["authority_id"] = "attacker-minted"
        forged["receipt_sha256"] = receipt_digest(forged)
        issuer = TrustedIssuer()
        _, _, now = times()

        with self.assertRaisesRegex(
            DispatchAuthorityReceiptError,
            "TRUSTED_AUTHORITY_CLAIM_REJECTED",
        ):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=forged,
                trusted_claim=issuer.claim,
                now=now,
            )

    def test_packet_mutation_breaks_subject_binding(self):
        p = packet()
        receipt = make_receipt(p)
        issuer = TrustedIssuer()
        issuer.issue(p, receipt)
        mutated = copy.deepcopy(p)
        mutated["allowed_operations"].append("research")
        _, _, now = times()

        with self.assertRaisesRegex(
            DispatchAuthorityReceiptError,
            "PACKET_DIGEST_MISMATCH",
        ):
            verify_and_claim_dispatch_authority_receipt(
                packet=mutated,
                receipt=receipt,
                trusted_claim=issuer.claim,
                now=now,
            )

    def test_task_and_idempotency_binding_are_exact(self):
        p = packet()
        _, _, now = times()
        for field in ("task_id", "subtask_id", "workflow_id", "idempotency_key"):
            receipt = make_receipt(p)
            receipt[field] = "wrong"
            receipt["receipt_sha256"] = receipt_digest(receipt)
            issuer = TrustedIssuer()
            issuer.issue(p, receipt)
            with self.subTest(field=field), self.assertRaisesRegex(
                DispatchAuthorityReceiptError,
                "PACKET_BINDING_MISMATCH:" + field,
            ):
                verify_and_claim_dispatch_authority_receipt(
                    packet=p,
                    receipt=receipt,
                    trusted_claim=issuer.claim,
                    now=now,
                )

    def test_protected_action_requires_jay(self):
        p = packet()
        receipt = make_receipt(
            p,
            protected_action=True,
            approval_actor="JAYTEC_CONTROL_PLANE",
        )
        issuer = TrustedIssuer()
        issuer.issue(p, receipt)
        _, _, now = times()
        with self.assertRaisesRegex(
            DispatchAuthorityReceiptError,
            "PROTECTED_ACTION_REQUIRES_JAY",
        ):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=receipt,
                trusted_claim=issuer.claim,
                now=now,
            )

        approved = make_receipt(
            p,
            authority_id="authority-jay",
            protected_action=True,
            approval_actor="JAY",
        )
        issuer.issue(p, approved)
        verify_and_claim_dispatch_authority_receipt(
            packet=p,
            receipt=approved,
            trusted_claim=issuer.claim,
            now=now,
        )

    def test_budget_and_scope_fail_closed(self):
        p = packet()
        _, _, now = times()

        over_budget = make_receipt(
            p,
            authority_id="authority-budget",
            estimated_cost_units=2,
            budget_limit_units=1,
        )
        issuer = TrustedIssuer()
        issuer.issue(p, over_budget)
        with self.assertRaisesRegex(DispatchAuthorityReceiptError, "COST_EXCEEDS_BUDGET"):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=over_budget,
                trusted_claim=issuer.claim,
                now=now,
            )

        op_scope = make_receipt(
            p,
            authority_id="authority-op",
            allowed_operations=["read"],
        )
        issuer.issue(p, op_scope)
        with self.assertRaisesRegex(DispatchAuthorityReceiptError, "OPERATION_SCOPE_EXCEEDED"):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=op_scope,
                trusted_claim=issuer.claim,
                now=now,
            )

        specialist_scope = make_receipt(
            p,
            authority_id="authority-specialist",
            allowed_specialists=["gemini"],
        )
        issuer.issue(p, specialist_scope)
        with self.assertRaisesRegex(DispatchAuthorityReceiptError, "SPECIALIST_SCOPE_EXCEEDED"):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=specialist_scope,
                trusted_claim=issuer.claim,
                now=now,
            )

    def test_expired_future_and_invalid_window_fail(self):
        p = packet()
        _, _, now = times()
        issuer = TrustedIssuer()

        expired = make_receipt(
            p,
            authority_id="authority-expired",
            issued_at=(now - timedelta(minutes=10)).isoformat(),
            expires_at=(now - timedelta(minutes=1)).isoformat(),
        )
        issuer.issue(p, expired)
        with self.assertRaisesRegex(DispatchAuthorityReceiptError, "AUTHORITY_EXPIRED"):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=expired,
                trusted_claim=issuer.claim,
                now=now,
            )

        future = make_receipt(
            p,
            authority_id="authority-future",
            issued_at=(now + timedelta(minutes=2)).isoformat(),
            expires_at=(now + timedelta(minutes=7)).isoformat(),
        )
        issuer.issue(p, future)
        with self.assertRaisesRegex(DispatchAuthorityReceiptError, "AUTHORITY_NOT_YET_VALID"):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=future,
                trusted_claim=issuer.claim,
                now=now,
            )

        bad_window = make_receipt(
            p,
            authority_id="authority-window",
            issued_at=(now + timedelta(minutes=1)).isoformat(),
            expires_at=(now + timedelta(minutes=1)).isoformat(),
        )
        issuer.issue(p, bad_window)
        with self.assertRaisesRegex(DispatchAuthorityReceiptError, "INVALID_AUTHORITY_WINDOW"):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=bad_window,
                trusted_claim=issuer.claim,
                now=now,
            )

    def test_integrity_unknown_fields_and_single_use_are_strict(self):
        p = packet()
        _, _, now = times()

        tampered = make_receipt(p, authority_id="authority-tamper")
        tampered["budget_limit_units"] = 999
        issuer = TrustedIssuer()
        issuer.issue(p, tampered)
        with self.assertRaisesRegex(DispatchAuthorityReceiptError, "RECEIPT_DIGEST_MISMATCH"):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=tampered,
                trusted_claim=issuer.claim,
                now=now,
            )

        unknown = make_receipt(p, authority_id="authority-unknown")
        unknown["surprise_authority"] = True
        unknown["receipt_sha256"] = receipt_digest(unknown)
        issuer.issue(p, unknown)
        with self.assertRaisesRegex(DispatchAuthorityReceiptError, "UNKNOWN_FIELDS"):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=unknown,
                trusted_claim=issuer.claim,
                now=now,
            )

        reusable = make_receipt(p, authority_id="authority-reusable")
        reusable["single_use"] = False
        reusable["receipt_sha256"] = receipt_digest(reusable)
        issuer.issue(p, reusable)
        with self.assertRaisesRegex(DispatchAuthorityReceiptError, "SINGLE_USE_REQUIRED"):
            verify_and_claim_dispatch_authority_receipt(
                packet=p,
                receipt=reusable,
                trusted_claim=issuer.claim,
                now=now,
            )


if __name__ == "__main__":
    unittest.main()
