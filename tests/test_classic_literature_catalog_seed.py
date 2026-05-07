import json
import unittest
from pathlib import Path


CATALOG_PATH = Path("runtime/reports/classic_literature_catalog_seed.json")


class ClassicLiteratureCatalogSeedTests(unittest.TestCase):
    def test_catalog_matches_project_15_acceptance_contract(self) -> None:
        payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        entries = payload.get("catalog_entries")

        self.assertEqual(payload.get("catalog_status"), "source_candidate_catalog_ready")
        self.assertEqual(payload.get("catalog_entry_count"), 200)
        self.assertEqual(payload.get("validated_catalog_entry_count"), 200)
        self.assertEqual(payload.get("priority_seed_entry_count"), 22)
        self.assertEqual(payload.get("validated_priority_seed_count"), 22)
        self.assertEqual(payload.get("remaining_entries_needed"), 0)
        self.assertEqual(payload.get("canonical_link_enrichment_status"), "optional_future_enrichment")
        self.assertIsInstance(entries, list)
        self.assertEqual(len(entries), 200)

        seen_title_author = set()
        priority_seed_count = 0
        for entry in entries:
            self.assertIsInstance(entry, dict)
            self.assertTrue(str(entry.get("title") or "").strip())
            self.assertTrue(str(entry.get("author") or "").strip())
            self.assertTrue(str(entry.get("category") or "").strip())
            self.assertTrue(str(entry.get("availability_status") or "").strip())

            candidates = entry.get("source_library_candidates")
            self.assertIsInstance(candidates, list)
            self.assertTrue(any(str(candidate or "").strip() for candidate in candidates))

            title_author = (
                str(entry.get("title") or "").strip().lower(),
                str(entry.get("author") or "").strip().lower(),
            )
            self.assertNotIn(title_author, seen_title_author)
            seen_title_author.add(title_author)

            if entry.get("priority_seed") is True:
                priority_seed_count += 1

        self.assertEqual(priority_seed_count, payload.get("priority_seed_entry_count"))


if __name__ == "__main__":
    unittest.main()