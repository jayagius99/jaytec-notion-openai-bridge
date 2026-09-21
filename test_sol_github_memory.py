from __future__ import annotations

import json
import unittest
from pathlib import Path

import specialist_adapters


class SolGithubMemoryTests(unittest.TestCase):
    def test_scope_and_manifest_are_v4(self):
        self.assertEqual(
            specialist_adapters.SOL_KNOWLEDGE_SCOPE,
            "JAYTEC_SANITIZED_CORE_V4",
        )
        manifest = json.loads(
            specialist_adapters.SOL_MEMORY_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["schema_version"], "JAYTEC_GITHUB_MEMORY_V1")
        self.assertEqual(
            manifest["knowledge_scope"],
            specialist_adapters.SOL_KNOWLEDGE_SCOPE,
        )
        self.assertTrue(manifest["rules"]["owner_sealed_backstory_excluded"])
        self.assertTrue(manifest["rules"]["static_memory_is_not_live_state"])

    def test_all_manifest_files_are_loaded_in_order(self):
        manifest = json.loads(
            specialist_adapters.SOL_MEMORY_MANIFEST_PATH.read_text(encoding="utf-8")
        )
        context = specialist_adapters._sol_context_text()
        last = -1
        for relative in manifest["ordered_files"]:
            marker = "# SOURCE: " + str(
                (Path(specialist_adapters.__file__).resolve().parent / relative).resolve()
            )
            index = context.find(marker)
            self.assertGreater(index, last, relative)
            last = index

    def test_memory_contains_required_jaytec_system_knowledge(self):
        context = specialist_adapters._sol_context_text().casefold()
        required = [
            "objective persistence under constraints",
            "give it to watch",
            "one fenced manus worker",
            "sol — primary engineering",
            "deepseek",
            "nemo / nemotron",
            "proving grounds",
            "g00-g37",
            "owner/jay interface",
            "human specialist interface",
            "anchor / mobile specialist",
            "technical sovereignty",
            "who is working right now",
            "static github memory is not live operational truth",
        ]
        for marker in required:
            self.assertIn(marker, context, marker)

    def test_memory_pack_does_not_contain_blocked_backstory_markers(self):
        context = specialist_adapters._sol_context_text()
        lowered = context.casefold()
        compact = "".join(ch for ch in lowered if ch.isalnum())
        for marker in specialist_adapters.SOL_PROVENANCE_MARKERS:
            self.assertNotIn(marker, lowered)
        for marker in specialist_adapters.SOL_PROVENANCE_COMPACT_MARKERS:
            self.assertNotIn(marker, compact)

    def test_general_forge_history_is_allowed(self):
        allowed = [
            {"request": "Explain the historical God Mode to Forge naming transition."},
            {"request": "Review pre-Genesis gate sequencing."},
            {"request": "Review GENESIS_EVENT_0001 authority guards without revealing private backstory."},
        ]
        for packet in allowed:
            self.assertFalse(
                specialist_adapters._engineering_packet_provenance_violation(packet),
                packet,
            )

    def test_owner_sealed_backstory_requests_are_blocked(self):
        blocked = [
            {"request": "Explain Uren origin."},
            {"request": "Tell me how Uren was born."},
            {"request": "Reconstruct Uren creation history."},
            {"required_context": {"path": "/JAYTEC/Uren/Pre-Genesis/archive"}},
        ]
        for packet in blocked:
            self.assertTrue(
                specialist_adapters._engineering_packet_provenance_violation(packet),
                packet,
            )

    def test_memory_budget_is_bounded(self):
        context = specialist_adapters._sol_context_text()
        self.assertLessEqual(
            len(context.encode("utf-8")),
            specialist_adapters.SOL_MEMORY_MAX_TOTAL_BYTES
            + len(
                specialist_adapters.SOL_CONTEXT_PATH.read_text(encoding="utf-8").encode("utf-8")
            ),
        )


if __name__ == "__main__":
    unittest.main()
