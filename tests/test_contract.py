import json
import unittest
from pathlib import Path

from src.validator import validate_event


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((Path(__file__).parents[1] / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_new_event_types_follow_contract(self) -> None:
        record = {
            "event_id": "transfer_proposal:TP-EN-1-1:v1",
            "event_type": "TRANSFER_PROPOSED",
            "aggregate_type": "transfer_proposal",
            "aggregate_id": "TP-EN-1-1",
            "occurred_at": "2026-09-21T10:00:00+08:00",
            "version": 1,
            "summary": "建议迁移至电话辅导，待学员确认",
            "payload": {"enrollment_id": "EN-1"},
        }
        self.assertEqual(validate_event(record), [])

    def test_unknown_names_are_rejected(self) -> None:
        record = {
            "event_id": "e-1",
            "event_type": "NOT_A_EVENT",
            "aggregate_type": "not_an_aggregate",
            "aggregate_id": "a-1",
            "occurred_at": "2026-09-21T10:00:00+08:00",
            "version": 1,
            "summary": "非法名称应被拒绝",
        }
        errors = validate_event(record)
        self.assertTrue(any("事件类型" in e for e in errors))
        self.assertTrue(any("聚合类型" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
