"""学习记录的错误路径与成果保护。"""

import unittest

from src.continuity import (
    AbsenceAttribution,
    DeliveryFormat,
    NotFoundError,
    StateError,
)
from tests.factories import at, make_service, publish_offering, session


class RecordGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        svc = self.service
        svc.register_learner("L-1", "郑爷爷")
        publish_offering(
            svc, "OFF-1", fmt=DeliveryFormat.OFFLINE,
            sessions=[session("S-1", at(10), DeliveryFormat.OFFLINE)],
        )
        publish_offering(svc, "OFF-2", fmt=DeliveryFormat.PHONE, title="别的班")
        svc.place_enrollment(
            "EN-1", "L-1", "OFF-1", DeliveryFormat.OFFLINE,
            goals=[{"goal_id": "G-1", "title": "太极拳入门"}],
        )
        self.clock.set(at(11))

    def test_unknown_session(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.record_session("EN-1", "S-404", attended=True)

    def test_session_of_other_offering_rejected(self) -> None:
        publish_offering(
            self.service, "OFF-2", fmt=DeliveryFormat.PHONE, title="别的班",
            sessions=[session("S-X", at(10), DeliveryFormat.PHONE)],
            effective_from=at(1),
        )
        with self.assertRaises(StateError):
            self.service.record_session("EN-1", "S-X", attended=True)

    def test_duplicate_record_rejected(self) -> None:
        self.service.record_session("EN-1", "S-1", attended=True)
        with self.assertRaises(StateError):
            self.service.record_session("EN-1", "S-1", attended=True)

    def test_unknown_goal_rejected(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.record_session("EN-1", "S-1", attended=True, outcomes=["G-404"])

    def test_goal_cannot_complete_twice(self) -> None:
        svc = self.service
        publish_offering(
            svc, "OFF-1", fmt=DeliveryFormat.OFFLINE,
            sessions=[session("S-2", at(20), DeliveryFormat.OFFLINE)],
            effective_from=at(12),
        )
        svc.record_session("EN-1", "S-1", attended=True, outcomes=["G-1"])
        self.clock.set(at(21))
        with self.assertRaises(StateError):
            svc.record_session("EN-1", "S-2", attended=True, outcomes=["G-1"])

    def test_attended_record_needs_no_attribution(self) -> None:
        record_id = self.service.record_session("EN-1", "S-1", attended=True)
        with self.assertRaises(StateError):
            self.service.attribute_absence(record_id, AbsenceAttribution.VOLUNTARY)


if __name__ == "__main__":
    unittest.main()
