import json
import unittest
from pathlib import Path

from src.validator import EVENT_TYPES, validate_event
from tests.helpers import build_service, publish_calligraphy, register

DATA_DIR = Path(__file__).parents[1] / "data"


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((DATA_DIR / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_all_samples_match_envelope(self) -> None:
        for path in sorted(DATA_DIR.glob("*.json")):
            with self.subTest(path=path.name):
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(validate_event(record), [])

    def test_unknown_event_type_rejected(self) -> None:
        sample = json.loads((DATA_DIR / "sample.json").read_text(encoding="utf-8"))
        sample["event_type"] = "NOT_A_TYPE"
        self.assertIn("未知事件类型：NOT_A_TYPE", validate_event(sample))

    def test_unknown_aggregate_rejected(self) -> None:
        sample = json.loads((DATA_DIR / "sample.json").read_text(encoding="utf-8"))
        sample["aggregate_type"] = "nobody"
        self.assertIn("未知聚合类型：nobody", validate_event(sample))

    def test_bad_version_rejected(self) -> None:
        sample = json.loads((DATA_DIR / "sample.json").read_text(encoding="utf-8"))
        sample["version"] = 0
        self.assertIn("version 必须是正整数", validate_event(sample))


class ServiceContractTest(unittest.TestCase):
    """服务发出的事件类型与契约保持一致。"""

    def test_handled_event_types_match_contract(self) -> None:
        svc, _, _ = build_service()
        self.assertEqual(svc.event_types, EVENT_TYPES)

    def test_emitted_events_pass_validation(self) -> None:
        svc, _, store = build_service()
        publish_calligraphy(svc)
        register(svc, "learner-1")
        svc.place_enrollment("enr-1", "learner-1", "course-calligraphy", mode="offline")
        for event in store.read_all():
            self.assertEqual(validate_event(event), [], event["event_id"])


if __name__ == "__main__":
    unittest.main()
