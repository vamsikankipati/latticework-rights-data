import json
import tempfile
import unittest
from pathlib import Path

import evidence_index as evidence


class EvidenceIndexTests(unittest.TestCase):
    def test_literal_search_preserves_negation_and_page_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = "First page.\fNot liable unless permitted.\f".encode()
            (root / "fixture.txt").write_bytes(content)
            report = {"schema_version": 1, "documents": [{"id": "fixture", "title": "Official fixture", "url": "https://example.gov.in/fixture.pdf", "sha256": "source-hash", "status": "collected", "text": {"path": "fixture.txt", "sha256": evidence.digest(content), "status": "extracted_unreviewed", "pages": 2}}]}
            report_path = root / "documents.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            database = root / "evidence.sqlite"
            self.assertEqual(evidence.build(report_path, database), 2)
            result = evidence.search(database, "t LiAb")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["page_number"], 2)
            self.assertEqual(result[0]["review_status"], "unreviewed")
            self.assertEqual(evidence.search(database, "NOT LIABLE")[0]["document_id"], "fixture")
            (root / "fixture.txt").write_text("tampered", encoding="utf-8")
            with self.assertRaises(ValueError):
                evidence.build(report_path, database)
            self.assertEqual(len(evidence.search(database, "liable")), 1)


if __name__ == "__main__":
    unittest.main()
