import unittest

from relationship_policy import (
    Actor,
    Purpose,
    RelationshipPolicyError,
    authorize_relationship,
    manus_outbound_destinations,
    notion_outbound_destinations,
    specialist_outbound_destinations,
)


class RelationshipPolicyTests(unittest.TestCase):
    def test_manus_has_no_hidden_specialist_or_notion_path(self):
        self.assertEqual(
            manus_outbound_destinations(),
            frozenset({Actor.JAYTEC, Actor.GITHUB, Actor.NEON, Actor.RENDER}),
        )

    def test_notion_can_only_return_transfer_data_to_jaytec(self):
        self.assertEqual(notion_outbound_destinations(), frozenset({Actor.JAYTEC}))
        with self.assertRaises(RelationshipPolicyError):
            authorize_relationship(
                source=Actor.NOTION,
                destination=Actor.JAYTEC,
                purpose=Purpose.SPECIALIST_TASK,
            )

    def test_specialists_can_only_return_to_jaytec(self):
        destinations = specialist_outbound_destinations()
        self.assertEqual(destinations[Actor.GEMINI], frozenset({Actor.JAYTEC}))
        self.assertEqual(destinations[Actor.ENGINEERING], frozenset({Actor.JAYTEC}))

    def test_manus_cannot_call_gemini_engineering_notion_or_chatgpt_directly(self):
        for destination in (
            Actor.GEMINI,
            Actor.ENGINEERING,
            Actor.NOTION,
            Actor.CHATGPT,
        ):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(
                    RelationshipPolicyError, "RELATIONSHIP_EDGE_BLOCKED"
                ):
                    authorize_relationship(
                        source=Actor.MANUS,
                        destination=destination,
                        purpose=Purpose.SPECIALIST_REQUEST,
                        current_task_authorized=True,
                        jay_authorized_notion_via_chatgpt=True,
                    )

    def test_jaytec_to_manus_requires_current_delegation(self):
        with self.assertRaisesRegex(
            RelationshipPolicyError, "RELATIONSHIP_CURRENT_AUTH_REQUIRED"
        ):
            authorize_relationship(
                source=Actor.JAYTEC,
                destination=Actor.MANUS,
                purpose=Purpose.TASK_PACKET,
            )
        authorize_relationship(
            source=Actor.JAYTEC,
            destination=Actor.MANUS,
            purpose=Purpose.TASK_PACKET,
            current_task_authorized=True,
        )

    def test_notion_transfer_requires_current_jay_via_chatgpt_authority(self):
        with self.assertRaisesRegex(
            RelationshipPolicyError, "RELATIONSHIP_NOTION_AUTH_REQUIRED"
        ):
            authorize_relationship(
                source=Actor.JAYTEC,
                destination=Actor.NOTION,
                purpose=Purpose.TRANSFER_REQUEST,
                current_task_authorized=True,
            )
        authorize_relationship(
            source=Actor.JAYTEC,
            destination=Actor.NOTION,
            purpose=Purpose.TRANSFER_REQUEST,
            current_task_authorized=True,
            jay_authorized_notion_via_chatgpt=True,
        )

    def test_connector_read_is_allowed_but_mutation_requires_separate_authority(self):
        for destination in (Actor.GITHUB, Actor.NEON, Actor.RENDER):
            authorize_relationship(
                source=Actor.MANUS,
                destination=destination,
                purpose=Purpose.INSPECT,
            )
            with self.assertRaisesRegex(
                RelationshipPolicyError,
                "RELATIONSHIP_CONNECTOR_MUTATION_AUTH_REQUIRED",
            ):
                authorize_relationship(
                    source=Actor.MANUS,
                    destination=destination,
                    purpose=Purpose.WRITE,
                    current_task_authorized=True,
                )
            authorize_relationship(
                source=Actor.MANUS,
                destination=destination,
                purpose=Purpose.WRITE,
                current_task_authorized=True,
                connector_mutation_authorized=True,
            )

    def test_mutation_authority_flag_is_strict_boolean(self):
        with self.assertRaisesRegex(
            RelationshipPolicyError, "RELATIONSHIP_MUTATION_AUTH_INVALID"
        ):
            authorize_relationship(
                source=Actor.MANUS,
                destination=Actor.GITHUB,
                purpose=Purpose.WRITE,
                current_task_authorized=True,
                connector_mutation_authorized=1,
            )

    def test_no_connector_to_connector_edges(self):
        connectors = (Actor.GITHUB, Actor.NEON, Actor.RENDER)
        for src in connectors:
            for dst in connectors:
                if src == dst:
                    continue
                with self.assertRaises(RelationshipPolicyError):
                    authorize_relationship(
                        source=src,
                        destination=dst,
                        purpose=Purpose.RESULT,
                    )

    def test_gemini_and_engineering_cannot_delegate_to_manus(self):
        for src in (Actor.GEMINI, Actor.ENGINEERING):
            with self.subTest(src=src):
                with self.assertRaises(RelationshipPolicyError):
                    authorize_relationship(
                        source=src,
                        destination=Actor.MANUS,
                        purpose=Purpose.DELEGATE,
                        current_task_authorized=True,
                    )


if __name__ == "__main__":
    unittest.main()
