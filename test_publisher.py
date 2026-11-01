import argparse
import gzip
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import publisher


class PublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keydir = tempfile.TemporaryDirectory()
        cls.private = Path(cls.keydir.name) / "private.pem"
        cls.public = Path(cls.keydir.name) / "public.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(cls.private)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["openssl", "pkey", "-in", str(cls.private), "-pubout", "-out", str(cls.public)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        cls.keydir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = publisher.ROOT
        publisher.ROOT = Path(self.temp.name)
        (publisher.ROOT / "data").mkdir()
        self.row = {"id":"fixture-1", "title":"Fixture", "category":"test", "summary":"Test summary", "content":"Not liable under the fixture.", "citation":"Test section 1", "source_url":"https://example.gov.in/act", "jurisdiction":"IN", "verified_at":"2024-01-01", "status":"verified"}
        (publisher.ROOT / "data/entries.jsonl").write_text(json.dumps(self.row) + "\n", encoding="utf-8")
        (publisher.ROOT / "data/approvals.json").write_text("{}", encoding="utf-8")
        self.output = publisher.ROOT / "dist"
        self.args = argparse.Namespace(version="v1", key=str(self.private), output=str(self.output))

    def tearDown(self):
        publisher.ROOT = self.old_root
        self.temp.cleanup()

    def approve(self):
        value = {self.row["id"]: {"sha256": publisher.sha(publisher.canonical_entry(self.row)), "reviewer":"Test reviewer", "approved_at":"2024-01-01"}}
        (publisher.ROOT / "data/approvals.json").write_text(json.dumps(value), encoding="utf-8")

    def test_unapproved_or_changed_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            publisher.build(self.args)
        self.approve()
        self.row["content"] = "Changed after review."
        (publisher.ROOT / "data/entries.jsonl").write_text(json.dumps(self.row), encoding="utf-8")
        with self.assertRaises(ValueError):
            publisher.build(self.args)

    def test_signed_pack_preserves_text_and_detects_tampering(self):
        self.approve()
        publisher.build(self.args)
        manifest = self.output / "manifest.json"
        signature = self.output / "manifest.sig"
        subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.public), "-signature", str(signature), str(manifest)], check=True, stdout=subprocess.DEVNULL)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        pack = self.output / "india-all.sqlite.gz"
        self.assertEqual(data["packs"][0]["sha256"], publisher.file_sha(pack))
        with gzip.open(pack, "rb") as zipped:
            unpacked = self.output / "unpacked.sqlite"
            unpacked.write_bytes(zipped.read())
        db = sqlite3.connect(unpacked)
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT content FROM rights_entries").fetchone()[0], self.row["content"])
            self.assertEqual(db.execute("SELECT entry_id FROM rights_trigrams WHERE gram = ?", ("not",)).fetchone()[0], self.row["id"])
        finally:
            db.close()
        manifest.write_bytes(manifest.read_bytes() + b" ")
        result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(self.public), "-signature", str(signature), str(manifest)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertNotEqual(result.returncode, 0)

    def test_official_url_guard(self):
        self.assertTrue(publisher.official("https://morth.nic.in/source"))
        self.assertFalse(publisher.official("https://morth.nic.in.example.com/source"))
        self.assertFalse(publisher.official("http://morth.nic.in/source"))

    def test_source_registry_has_each_state_and_ut_without_claiming_completion(self):
        registry = json.loads((Path(__file__).parent / "catalog/sources.json").read_text(encoding="utf-8"))
        expected = {"AN", "AP", "AR", "AS", "BR", "CG", "CH", "DH", "DL", "GA", "GJ", "HR", "HP", "JH", "JK", "KA", "KL", "LA", "LD", "MH", "ML", "MN", "MP", "MZ", "NL", "OD", "PB", "PY", "RJ", "SK", "TN", "TR", "TS", "UK", "UP", "WB"}
        sources = registry["sources"]
        self.assertEqual({s["jurisdiction"][3:] for s in sources if s["jurisdiction"].startswith("IN-")}, expected)
        self.assertEqual(len({s["id"] for s in sources}), len(sources))
        self.assertTrue(all(publisher.official(s["url"]) for s in sources))
        self.assertEqual(len(registry["jurisdictions_pending_source_audit"]), 36)


if __name__ == "__main__":
    unittest.main()
