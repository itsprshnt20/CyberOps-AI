
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cases import CaseError, Store, suggestions

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class CaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "case.sqlite3"
        self.store = Store(self.path)
        for file in ("identity.json", "endpoint.json"):
            self.store.import_alerts((SAMPLES / file).read_bytes())
        self.alerts = self.store.alerts()
        self.case = self.store.create("Demo investigation", "High", "")
        self.store.link(self.case, [self.alerts[0]["id"]])

    def test_dedup_and_conflict(self):
        raw = (SAMPLES / "identity.json").read_bytes()
        self.assertEqual(self.store.import_alerts(raw)[0],
                         {"new": 0, "duplicate": 2, "rejected": 0, "conflicting": 0})
        self.assertEqual(sum(row["external_id"] == "demo-01" for row in self.alerts), 2)
        records = json.loads(raw)
        records[0]["title"] = "Changed"
        counts, issues = self.store.import_alerts(json.dumps(records).encode())
        self.assertEqual(counts["conflicting"], 1)
        self.assertIn("Original retained", issues[0])
        self.assertEqual(self.store.alerts()[0]["title"], self.alerts[0]["title"])

    def test_invalid_alerts(self):
        counts, issues = self.store.import_alerts(b'[{"external_id":"bad"},null]')
        self.assertEqual(counts["rejected"], 2)
        self.assertTrue(issues[1].startswith("Record 2"))
        with self.assertRaisesRegex(CaseError, "Malformed JSON"):
            self.store.import_alerts(b"[")

    def test_suggestions_require_linked_entity_and_time(self):
        _, linked, _ = self.store.detail(self.case)
        self.assertEqual([row["external_id"] for row in suggestions(linked, self.alerts, 6)], ["demo-01"])
        self.assertEqual(suggestions(linked, self.alerts, 5), [])
        self.assertEqual(len(linked), 1)

    def test_close_requires_resolution(self):
        with self.assertRaisesRegex(CaseError, "resolution"):
            self.store.change(self.case, "Closed", "Reviewer", "")
        self.store.change(self.case, "Closed", "Reviewer", "Evidence checked", "Analyst")
        row, _, events = self.store.detail(self.case)
        self.assertEqual(row["status"], "Closed")
        self.assertIn("Evidence checked", events[-1]["detail"])

    def test_only_linked_facts_and_immutable_export(self):
        self.store.note(self.case, "Analyst note", "Riya", "Original interpretation.")
        self.store.note(self.case, "Open question", "Riya", "Was this expected?")
        version, original = self.store.preview(self.case, "Lee", "Review sources")
        self.assertIn(self.alerts[0]["id"], original)
        for row in self.alerts[1:]:
            self.assertNotIn(row["id"], original)
        self.assertIn("Riya", original)
        self.store.finalize(self.case, version, "Lee", "Review sources", original)
        saved = self.store.revisions(self.case)[0]["markdown"]
        self.store.link(self.case, [self.alerts[2]["id"]])
        self.store.note(self.case, "Analyst note", "Lee", "Later observation.")
        with self.assertRaisesRegex(CaseError, "fresh preview"):
            self.store.finalize(self.case, version, "Lee", "Review sources", original)
        version, updated = self.store.preview(self.case, "Lee", "Review sources")
        self.store.finalize(self.case, version, "Lee", "Review sources", updated)
        latest, first = Store(self.path).revisions(self.case)
        self.assertEqual((latest["revision"], first["revision"]), (2, 1))
        self.assertEqual(first["markdown"], saved)
        self.assertNotIn("Later observation", saved)
        self.assertIn(self.alerts[2]["id"], latest["markdown"])
