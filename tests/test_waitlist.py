import unittest
from datetime import timedelta

from tests.helpers import HOUR, T0, build_service, publish_calligraphy, register


class WaitlistTest(unittest.TestCase):
    """候补按原申请时间与可满足条件推进，超时未确认的名额由时钟释放。"""

    def setUp(self):
        self.svc, self.clock, self.store = build_service()
        publish_calligraphy(self.svc, capacity=1)
        register(self.svc, "learner-1")
        self.svc.place_enrollment("enr-1", "learner-1", "course-calligraphy", mode="offline")

    def test_advances_by_application_time_and_satisfiability(self):
        register(self.svc, "learner-2", needs=("caregiver_escort",))  # 课程无法落实照护陪同
        register(self.svc, "learner-3", needs=("hearing_assist",))
        # learner-2 申请更早但条件不满足；learner-3 申请更晚但可满足
        self.svc.join_waitlist("wl-1", "learner-2", "course-calligraphy", applied_at=T0 + HOUR)
        self.svc.join_waitlist("wl-2", "learner-3", "course-calligraphy", applied_at=T0 + 2 * HOUR)
        self.svc.set_eligibility("enr-1", "ended", reason="结课")  # 腾出名额后自动推进
        offers = [o for o in self.svc.offers.values() if o.status == "pending"]
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].learner_id, "learner-3")
        self.assertEqual(self.svc.waitlist_entries["wl-1"].status, "waiting")
        status = {row["entry_id"]: row for row in self.svc.waitlist_status("course-calligraphy")}
        self.assertFalse(status["wl-1"]["satisfiable"])
        self.assertIn("照护陪同", status["wl-1"]["note"])

    def test_unconfirmed_offer_expires_and_seat_released(self):
        register(self.svc, "learner-2")
        self.svc.join_waitlist("wl-1", "learner-2", "course-calligraphy")
        self.svc.set_eligibility("enr-1", "ended")
        first_offer = next(iter(self.svc.offers.values()))
        # 名额被候补出价占用，直接报名失败
        register(self.svc, "learner-3")
        with self.assertRaises(ValueError):
            self.svc.place_enrollment("enr-3", "learner-3", "course-calligraphy", mode="offline")
        # 超时未确认：注入时钟前进，名额释放并按原申请时间重新出价
        self.clock.advance(timedelta(hours=13))
        expired = self.svc.expire_waitlist_offers()
        self.assertEqual(expired, [first_offer.offer_id])
        self.assertEqual(self.svc.offers[first_offer.offer_id].status, "expired")
        entry = self.svc.waitlist_entries["wl-1"]
        self.assertEqual(entry.status, "offered")  # 重新出价，仍保留原申请时间
        self.assertEqual(entry.applied_at, T0)
        new_offer = self.svc.offers[entry.current_offer_id]
        self.assertEqual(new_offer.status, "pending")
        self.assertNotEqual(new_offer.offer_id, first_offer.offer_id)

    def test_confirm_offer_places_enrollment(self):
        register(self.svc, "learner-2")
        self.svc.join_waitlist("wl-1", "learner-2", "course-calligraphy")
        self.svc.set_eligibility("enr-1", "ended")
        offer = next(iter(self.svc.offers.values()))
        self.svc.confirm_waitlist_offer(offer.offer_id, enrollment_id="enr-2", mode="offline")
        self.assertEqual(self.svc.waitlist_entries["wl-1"].status, "promoted")
        assignment = self.svc.enrollments["enr-2"].modes.at(self.clock.now()).value
        self.assertEqual(assignment.mode, "offline")
        self.assertEqual(assignment.decision_ref, offer.offer_id)

    def test_confirm_after_offer_expiry_raises(self):
        register(self.svc, "learner-2")
        self.svc.join_waitlist("wl-1", "learner-2", "course-calligraphy")
        self.svc.set_eligibility("enr-1", "ended")
        offer = next(iter(self.svc.offers.values()))
        self.clock.advance(timedelta(hours=13))
        with self.assertRaises(ValueError):
            self.svc.confirm_waitlist_offer(offer.offer_id, enrollment_id="enr-2", mode="offline")
        self.assertEqual(self.svc.offers[offer.offer_id].status, "expired")


if __name__ == "__main__":
    unittest.main()
