import json
import unittest
from datetime import datetime
from pathlib import Path


class SourceRecordsTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "data" / "source_records.json"
        self.data = json.loads(path.read_text(encoding="utf-8"))

    def instant(self, value):
        instant = datetime.fromisoformat(value)
        self.assertIsNotNone(instant.utcoffset())
        return instant

    def test_source_documents_agree(self):
        before, after, _ = self.data["tenure"]
        self.assertEqual((before["orchard"], before["to"]), (after["orchard"], after["from"]))
        self.assertNotEqual(before["member"], after["member"])
        single = self.data["compensation"][0]
        self.assertGreater(single["cents"], sum(x.get("part_cents", 0) for x in single["parts"]))
        self.assertEqual({x["state"] for x in self.data["pool_transfers"]}, {"paid", "pending"})
        trips = [x for x in self.data["trips"] if x["shipment"] == "TR-51"]
        self.assertEqual({x["party_ref"] for x in trips}, {"P01", "P02"})

