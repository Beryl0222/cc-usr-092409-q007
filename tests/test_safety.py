"""安全限制：暂停对应形态的活动，其他形态照常；缺席自动归因为安全暂停。"""

import unittest
from datetime import timedelta

from src.continuity import AbsenceAttribution, DeliveryFormat
from tests.factories import at, make_service, publish_offering, session


class SafetyRestrictionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        svc = self.service
        svc.register_learner("L-1", "吴奶奶")
        publish_offering(
            svc, "OFF-LINE", fmt=DeliveryFormat.OFFLINE, title="线下班",
            sessions=[
                session("S-OFF", at(10), DeliveryFormat.OFFLINE),
                session("S-OFF2", at(30), DeliveryFormat.OFFLINE),
            ],
        )
        publish_offering(
            svc, "OFF-PHONE", fmt=DeliveryFormat.PHONE, title="电话班",
            sessions=[session("S-PH", at(12), DeliveryFormat.PHONE)],
        )
        svc.place_enrollment("EN-1", "L-1", "OFF-LINE", DeliveryFormat.OFFLINE)

    def test_restriction_pauses_only_matching_format(self) -> None:
        svc = self.service
        svc.set_safety_restriction(
            "L-1", {DeliveryFormat.OFFLINE},
            valid_from=at(0), valid_to=at(24), reason="术后不宜出行",
        )
        learner = svc.learners["L-1"]
        self.assertEqual(learner.paused_formats_at(at(10)), {DeliveryFormat.OFFLINE})
        self.assertEqual(learner.paused_formats_at(at(30)), set())
        # 电话形态不在限制内，仍可参加
        self.assertNotIn(DeliveryFormat.PHONE, learner.paused_formats_at(at(12)))

    def test_absence_during_pause_auto_attributed(self) -> None:
        svc = self.service
        svc.set_safety_restriction(
            "L-1", {DeliveryFormat.OFFLINE}, valid_from=at(0), valid_to=at(24),
            reason="术后不宜出行",
        )
        self.clock.set(at(11))
        record_id = svc.record_session("EN-1", "S-OFF", attended=False)
        record = svc.records[record_id]
        self.assertEqual(record.attribution, AbsenceAttribution.SAFETY_PAUSE)
        self.assertEqual(svc.withdrawal_risk("EN-1")["counts_toward_withdrawal"], 0)

    def test_paused_sessions_do_not_remind(self) -> None:
        svc = self.service
        svc.set_safety_restriction(
            "L-1", {DeliveryFormat.OFFLINE}, valid_from=at(0), valid_to=at(24),
            reason="术后不宜出行",
        )
        svc.sweep(at(5))  # S-OFF 在提醒窗口内但被暂停；S-OFF2 未到提醒点
        reminders = [e for e in svc.events if e.event_type == "REMINDER_SENT"]
        self.assertEqual(reminders, [])
        # 限制窗口结束后，后续场次恢复提醒
        svc.sweep(at(25))
        reminders = [e for e in svc.events if e.event_type == "REMINDER_SENT"]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].payload["session_id"], "S-OFF2")


if __name__ == "__main__":
    unittest.main()
