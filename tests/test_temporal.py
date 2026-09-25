"""生效区间：课程版本、报名资格、支持需求、授课形态、学习记录。"""

import unittest

from src.continuity import (
    AbsenceAttribution,
    DeliveryFormat,
    EligibilityStatus,
    StateError,
    SupportNeed,
)
from tests.factories import T0, ALL_NEEDS, at, make_service, publish_offering, session


class TemporalTest(unittest.TestCase):
    def test_course_versions_keep_effective_intervals(self) -> None:
        service, clock = make_service()
        publish_offering(
            service,
            sessions=[session("S-1", at(10), DeliveryFormat.OFFLINE),
                      session("S-OLD", at(100), DeliveryFormat.OFFLINE)],
        )
        clock.set(at(50))
        publish_offering(
            service,
            title="书法中级",
            sessions=[session("S-2", at(60), DeliveryFormat.OFFLINE)],
            effective_from=at(50),
        )
        offering = service.offerings["OFF-1"]
        v1, v2 = offering.versions
        self.assertEqual(v1.valid_to, at(50))
        self.assertIsNone(v2.valid_to)
        live = {s.session_id for s, _ in offering.live_sessions()}
        # 旧版本未开始的场次随换版失效，历史场次仍可追溯
        self.assertEqual(live, {"S-1", "S-2"})
        self.assertEqual(offering.version_at(at(10)).title, "书法初级")
        self.assertEqual(offering.version_at(at(60)).title, "书法中级")

    def test_course_version_must_move_forward(self) -> None:
        service, _ = make_service()
        publish_offering(service, effective_from=at(10))
        with self.assertRaises(StateError):
            publish_offering(service, effective_from=at(5))

    def test_support_needs_only_affect_future_sessions(self) -> None:
        service, _ = make_service()
        service.register_learner("L-1", "张三", needs={SupportNeed.HEARING_ASSIST})
        service.update_support_needs(
            "L-1",
            {SupportNeed.HEARING_ASSIST, SupportNeed.SLOW_PACE},
            effective_from=at(24),
            reason="评估后增加慢速讲解",
        )
        learner = service.learners["L-1"]
        self.assertEqual(learner.needs_at(at(12)), {SupportNeed.HEARING_ASSIST})
        self.assertEqual(
            learner.needs_at(at(36)),
            {SupportNeed.HEARING_ASSIST, SupportNeed.SLOW_PACE},
        )
        # 生效区间各自保留
        first, second = learner.support_needs
        self.assertEqual(first.valid_to, at(24))
        self.assertIsNone(second.valid_to)

    def test_support_needs_cannot_backdate_before_current(self) -> None:
        service, _ = make_service()
        service.register_learner("L-1", "张三")
        service.update_support_needs("L-1", set(ALL_NEEDS), effective_from=at(24))
        with self.assertRaises(StateError):
            service.update_support_needs("L-1", set(), effective_from=at(12))

    def test_eligibility_keeps_intervals(self) -> None:
        service, _ = make_service()
        service.register_learner("L-1", "张三")
        publish_offering(service)
        service.place_enrollment("EN-1", "L-1", "OFF-1", DeliveryFormat.OFFLINE)
        service.set_eligibility(
            "EN-1", EligibilityStatus.SUSPENDED, reason="休假", effective_from=at(48)
        )
        enrollment = service.enrollments["EN-1"]
        self.assertEqual(enrollment.eligibility_at(at(24)).status, EligibilityStatus.ELIGIBLE)
        self.assertEqual(enrollment.eligibility_at(at(72)).status, EligibilityStatus.SUSPENDED)
        self.assertFalse(enrollment.is_active_at(at(72)))

    def test_record_correction_preserves_intervals(self) -> None:
        service, clock = make_service()
        service.register_learner("L-1", "张三")
        publish_offering(service, sessions=[session("S-1", at(10), DeliveryFormat.OFFLINE)])
        service.place_enrollment("EN-1", "L-1", "OFF-1", DeliveryFormat.OFFLINE)
        clock.set(at(11))
        record_id = service.record_session("EN-1", "S-1", attended=False)
        clock.set(at(12))
        new_id = service.attribute_absence(
            record_id, AbsenceAttribution.VENUE_ADJUSTMENT, reason="场地临时调整"
        )
        old = service.records[record_id]
        new = service.records[new_id]
        self.assertEqual(old.valid_to, at(12))
        self.assertEqual(new.supersedes, record_id)
        self.assertIsNone(new.valid_to)
        self.assertEqual(new.attribution, AbsenceAttribution.VENUE_ADJUSTMENT)
        # 已被取代的记录不能再作为更正对象
        clock.set(at(13))
        with self.assertRaises(StateError):
            service.correct_record(record_id, reason="对已关闭记录操作")


if __name__ == "__main__":
    unittest.main()
