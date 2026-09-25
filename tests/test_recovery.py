"""重启恢复：回放事件后，提醒、候补推进与阶段回顾继续，且不重复。"""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.continuity import (
    ContinuityService,
    DeliveryFormat,
    EligibilityStatus,
    ManualClock,
    WaitlistStatus,
)
from tests.factories import T0, at, publish_offering, session


class RecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.clock = ManualClock(T0)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reopen(self) -> ContinuityService:
        return ContinuityService.open(self.path, self.clock)

    def test_reminders_continue_after_restart_without_duplicates(self) -> None:
        service = self.reopen()
        service.register_learner("L-1", "孙爷爷")
        publish_offering(
            service, sessions=[session("S-1", at(20), DeliveryFormat.OFFLINE)]
        )
        service.place_enrollment("EN-1", "L-1", "OFF-1", DeliveryFormat.OFFLINE)
        # 场次在提醒窗口内（24h 前），重启后 recover 补发提醒
        reopened = self.reopen()
        reminders = [e for e in reopened.events if e.event_type == "REMINDER_SENT"]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].payload["session_id"], "S-1")
        # 再次重启不重复提醒
        again = self.reopen()
        reminders = [e for e in again.events if e.event_type == "REMINDER_SENT"]
        self.assertEqual(len(reminders), 1)

    def test_waitlist_offer_expires_while_down(self) -> None:
        service = self.reopen()
        service.register_learner("L-0", "在册学员")
        service.register_learner("L-B", "候补乙")
        service.register_learner("L-C", "候补丙")
        publish_offering(service, capacity=1)
        service.place_enrollment("EN-1", "L-0", "OFF-1", DeliveryFormat.OFFLINE)
        self.clock.set(at(1))
        entry_b = service.apply_waitlist("L-B", "OFF-1", DeliveryFormat.OFFLINE)
        entry_c = service.apply_waitlist("L-C", "OFF-1", DeliveryFormat.OFFLINE)
        self.clock.set(at(2))
        service.set_eligibility("EN-1", EligibilityStatus.WITHDRAWN, reason="主动退学")
        self.assertEqual(service.waitlist[entry_b.entry_id].status, WaitlistStatus.OFFERED)
        # 系统停机期间名额超时；重启后释放并推进给下一位
        self.clock.set(at(2) + timedelta(hours=49))
        reopened = self.reopen()
        self.assertEqual(reopened.waitlist[entry_b.entry_id].status, WaitlistStatus.EXPIRED)
        self.assertEqual(reopened.waitlist[entry_c.entry_id].status, WaitlistStatus.OFFERED)
        # 再重启一次不会重复释放
        again = self.reopen()
        expired = [e for e in again.events if e.event_type == "WAITLIST_EXPIRED"]
        self.assertEqual(len(expired), 1)

    def test_stage_reviews_generated_after_restart(self) -> None:
        service = self.reopen()
        service.register_learner("L-1", "孙爷爷")
        publish_offering(service)
        service.place_enrollment("EN-1", "L-1", "OFF-1", DeliveryFormat.OFFLINE)
        self.clock.set(at(days=31))
        reopened = self.reopen()
        reviews = [e for e in reopened.events if e.event_type == "OUTCOME_REVIEWED"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].payload["period_start"], T0.isoformat())
        # 再次重启不重复生成同一期
        again = self.reopen()
        reviews = [e for e in again.events if e.event_type == "OUTCOME_REVIEWED"]
        self.assertEqual(len(reviews), 1)
        # 跨过第二个周期后生成下一期
        self.clock.set(at(days=61))
        third = self.reopen()
        reviews = [e for e in third.events if e.event_type == "OUTCOME_REVIEWED"]
        self.assertEqual(len(reviews), 2)


if __name__ == "__main__":
    unittest.main()
