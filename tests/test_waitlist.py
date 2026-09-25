"""候补：按原申请时间与可满足条件推进，超时名额由注入时钟释放。"""

import unittest
from datetime import timedelta

from src.continuity import (
    DeliveryFormat,
    EligibilityStatus,
    StateError,
    SupportNeed,
    WaitlistStatus,
)
from tests.factories import at, make_service, publish_offering


def full_house():
    """一个只剩零名额的班级，学员 EN-1 在册。"""
    service, clock = make_service()
    service.register_learner("L-0", "在册学员")
    publish_offering(service, "OFF-1", fmt=DeliveryFormat.OFFLINE, capacity=1)
    service.place_enrollment("EN-1", "L-0", "OFF-1", DeliveryFormat.OFFLINE)
    return service, clock


class WaitlistOrderTest(unittest.TestCase):
    def test_offer_goes_to_earliest_satisfiable_application(self) -> None:
        service, clock = full_house()
        service.register_learner("L-B", "候补乙")
        service.register_learner("L-C", "候补丙")
        clock.set(at(1))
        entry_b = service.apply_waitlist("L-B", "OFF-1", DeliveryFormat.OFFLINE)
        clock.set(at(2))
        entry_c = service.apply_waitlist("L-C", "OFF-1", DeliveryFormat.OFFLINE)
        self.assertEqual(entry_b.status, WaitlistStatus.WAITING)
        clock.set(at(3))
        service.set_eligibility("EN-1", EligibilityStatus.WITHDRAWN, reason="主动退学")
        # 名额先给申请更早的乙
        self.assertEqual(service.waitlist[entry_b.entry_id].status, WaitlistStatus.OFFERED)
        self.assertEqual(service.waitlist[entry_c.entry_id].status, WaitlistStatus.WAITING)

    def test_unsatisfiable_conditions_are_skipped_not_blocking(self) -> None:
        service, clock = make_service()
        service.register_learner("L-0", "在册学员")
        publish_offering(
            service, "OFF-1", fmt=DeliveryFormat.OFFLINE, capacity=1,
            resources={DeliveryFormat.OFFLINE: {SupportNeed.HEARING_ASSIST}},
        )
        service.place_enrollment("EN-1", "L-0", "OFF-1", DeliveryFormat.OFFLINE)
        service.register_learner("L-D", "需要慢速讲解", needs={SupportNeed.SLOW_PACE})
        service.register_learner("L-E", "只需助听", needs={SupportNeed.HEARING_ASSIST})
        clock.set(at(1))
        entry_d = service.apply_waitlist("L-D", "OFF-1", DeliveryFormat.OFFLINE)
        clock.set(at(2))
        entry_e = service.apply_waitlist("L-E", "OFF-1", DeliveryFormat.OFFLINE)
        clock.set(at(3))
        service.set_eligibility("EN-1", EligibilityStatus.WITHDRAWN, reason="迁出")
        # 丁需要慢速讲解，班级资源无法满足，跳过但不阻塞；戊申请更晚但条件可满足
        self.assertEqual(service.waitlist[entry_d.entry_id].status, WaitlistStatus.WAITING)
        self.assertEqual(service.waitlist[entry_e.entry_id].status, WaitlistStatus.OFFERED)


class WaitlistTimeoutTest(unittest.TestCase):
    def test_unconfirmed_offer_released_by_clock(self) -> None:
        service, clock = full_house()
        service.register_learner("L-B", "候补乙")
        service.register_learner("L-C", "候补丙")
        clock.set(at(1))
        entry_b = service.apply_waitlist("L-B", "OFF-1", DeliveryFormat.OFFLINE)
        clock.set(at(2))
        entry_c = service.apply_waitlist("L-C", "OFF-1", DeliveryFormat.OFFLINE)
        clock.set(at(3))
        service.set_eligibility("EN-1", EligibilityStatus.WITHDRAWN, reason="主动退学")
        self.assertEqual(service.waitlist[entry_b.entry_id].status, WaitlistStatus.OFFERED)
        # 乙超时未确认，名额释放并推进给丙
        clock.advance(timedelta(hours=49))  # 超过 offer_ttl=48h
        service.sweep()
        self.assertEqual(service.waitlist[entry_b.entry_id].status, WaitlistStatus.EXPIRED)
        self.assertEqual(service.waitlist[entry_c.entry_id].status, WaitlistStatus.OFFERED)
        with self.assertRaises(StateError):
            service.confirm_waitlist_offer(entry_b.entry_id)
        enrollment_id = service.confirm_waitlist_offer(entry_c.entry_id)
        enrollment = service.enrollments[enrollment_id]
        self.assertTrue(enrollment.is_active_at(service.clock.now()))
        placement = enrollment.placement_at(service.clock.now())
        self.assertEqual(placement.offering_id, "OFF-1")

    def test_confirm_within_ttl_enrolls(self) -> None:
        service, clock = full_house()
        service.register_learner("L-B", "候补乙")
        entry_b = service.apply_waitlist("L-B", "OFF-1", DeliveryFormat.OFFLINE)
        service.set_eligibility("EN-1", EligibilityStatus.WITHDRAWN, reason="主动退学")
        clock.advance(timedelta(hours=10))
        enrollment_id = service.confirm_waitlist_offer(entry_b.entry_id)
        self.assertEqual(service.waitlist[entry_b.entry_id].status, WaitlistStatus.ENROLLED)
        self.assertIn(enrollment_id, service.enrollments)

    def test_cancel_offered_entry_releases_seat_to_next(self) -> None:
        service, clock = full_house()
        service.register_learner("L-B", "候补乙")
        service.register_learner("L-C", "候补丙")
        entry_b = service.apply_waitlist("L-B", "OFF-1", DeliveryFormat.OFFLINE)
        entry_c = service.apply_waitlist("L-C", "OFF-1", DeliveryFormat.OFFLINE)
        service.set_eligibility("EN-1", EligibilityStatus.WITHDRAWN, reason="主动退学")
        self.assertEqual(service.waitlist[entry_b.entry_id].status, WaitlistStatus.OFFERED)
        service.cancel_waitlist(entry_b.entry_id)
        self.assertEqual(service.waitlist[entry_b.entry_id].status, WaitlistStatus.CANCELLED)
        self.assertEqual(service.waitlist[entry_c.entry_id].status, WaitlistStatus.OFFERED)


if __name__ == "__main__":
    unittest.main()
