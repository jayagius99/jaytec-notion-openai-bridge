import unittest

from five_seat_authority_bootstrap import (
    ProductionAuthorityBootstrapRefused,
    validate_bootstrap_evidence,
)


class AuthorityBootstrapValidationTests(unittest.TestCase):
    def _evidence(self, **changes):
        value = {
            "current_shared_state_version": 0,
            "durable_max_nonfabric_source_version": 91,
            "durable_nonfabric_version_rows": 2,
            "active_fabric_jobs": 0,
            "seats_total": 5,
            "seats_free": 5,
        }
        value.update(changes)
        return value

    def test_accepts_zero_authority_when_durable_evidence_matches(self):
        validate_bootstrap_evidence(self._evidence(), 91)

    def test_is_idempotent_when_already_initialized_to_expected_version(self):
        validate_bootstrap_evidence(
            self._evidence(current_shared_state_version=91),
            91,
        )

    def test_rejects_guessed_or_mismatched_version(self):
        with self.assertRaisesRegex(
            ProductionAuthorityBootstrapRefused,
            "DURABLE_SOURCE_VERSION_MISMATCH",
        ):
            validate_bootstrap_evidence(self._evidence(), 90)

    def test_rejects_live_fabric_work(self):
        with self.assertRaisesRegex(
            ProductionAuthorityBootstrapRefused,
            "ACTIVE_FABRIC_JOBS_PRESENT",
        ):
            validate_bootstrap_evidence(
                self._evidence(active_fabric_jobs=1),
                91,
            )

    def test_requires_exactly_five_free_seats(self):
        with self.assertRaisesRegex(
            ProductionAuthorityBootstrapRefused,
            "ALL_FIVE_SEATS_MUST_BE_FREE",
        ):
            validate_bootstrap_evidence(
                self._evidence(seats_free=4),
                91,
            )

    def test_rejects_existing_different_authority_version(self):
        with self.assertRaisesRegex(
            ProductionAuthorityBootstrapRefused,
            "AUTHORITY_ALREADY_INITIALIZED_DIFFERENT_VERSION",
        ):
            validate_bootstrap_evidence(
                self._evidence(current_shared_state_version=90),
                91,
            )


if __name__ == "__main__":
    unittest.main()
