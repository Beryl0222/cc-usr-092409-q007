import unittest

from tests.helpers import DAY, HOUR, T0, build_service, publish_calligraphy, register


class SupportChangeTest(unittest.TestCase):
    """支持需求变化只影响未来场次，既往场次的评估保持当时口径。"""

    def setUp(self):
        self.svc, self.clock, self.store = build_service()
        publish_calligraphy(self.svc)
        register(self.svc, "learner-1", needs=("hearing_assist",))
        self.svc.place_enrollment("enr-1", "learner-1", "course-calligraphy", mode="offline")

    def test_support_change_only_affects_future_sessions(self):
        self.svc.schedule_session(
            "s-1", "course-calligraphy", mode="offline",
            start=T0 + DAY, end=T0 + DAY + HOUR, supports_available=("hearing_assist",),
        )
        # 需求在第二场之前变化：新增慢速讲解
        self.svc.update_support_needs(
            "learner-1", ("hearing_assist", "slow_paced"),
            effective_from=T0 + 2 * DAY, reason="家属反馈语速偏快",
        )
        self.svc.schedule_session(
            "s-2", "course-calligraphy", mode="offline",
            start=T0 + 3 * DAY, end=T0 + 3 * DAY + HOUR, supports_available=("hearing_assist",),
        )
        explanation = self.svc.explain_period("learner-1", T0, T0 + 4 * DAY)
        by_session = {s.session_id: s for s in explanation.supports}
        # 第一场仍按当时需求评估：只需要助听，已落实
        self.assertEqual(by_session["s-1"].needed, ["hearing_assist"])
        self.assertTrue(by_session["s-1"].fulfilled)
        # 第二场按新需求评估：慢速讲解未落实
        self.assertEqual(by_session["s-2"].missing, ["slow_paced"])
        self.assertFalse(by_session["s-2"].fulfilled)


class SafetyRestrictionTest(unittest.TestCase):
    """安全限制暂停相应活动，但保留其他可参加内容。"""

    def setUp(self):
        self.svc, self.clock, self.store = build_service()
        publish_calligraphy(self.svc)

    def test_safety_pause_blocks_scoped_mode_and_excuses_absence(self):
        register(self.svc, "learner-2")
        self.svc.place_enrollment("enr-2", "learner-2", "course-calligraphy", mode="phone")
        self.svc.apply_safety_restriction(
            "learner-2", "rst-1", modes=("phone",),
            start=T0 + DAY, end=T0 + 3 * DAY, note="听力复查期间暂停电话辅导",
        )
        self.svc.schedule_session("s-phone", "course-calligraphy", mode="phone", start=T0 + 2 * DAY, end=T0 + 2 * DAY + HOUR)
        with self.assertRaises(ValueError):
            self.svc.upload_checkin("up-x", "s-phone", "learner-2", source="phone", payload={"status": "attended"})
        created = self.svc.finalize_absences("s-phone")
        record = self.svc.records[created[0]]
        self.assertEqual(record.attribution, "safety_pause")
        summary = self.svc.absence_summary("learner-2")
        self.assertEqual(summary.unexcused, 0)
        self.assertFalse(summary.dropout_risk)

    def test_safety_pause_keeps_other_modes_attendable(self):
        register(self.svc, "learner-1")
        self.svc.place_enrollment("enr-1", "learner-1", "course-calligraphy", mode="offline")
        # 限制只针对电话辅导，线下班不受影响
        self.svc.apply_safety_restriction("learner-1", "rst-2", modes=("phone",), start=T0, note="暂停电话辅导")
        self.svc.schedule_session("s-off", "course-calligraphy", mode="offline", start=T0 + DAY, end=T0 + DAY + HOUR)
        result = self.svc.upload_checkin("up-1", "s-off", "learner-1", source="offline", payload={"status": "attended"})
        self.assertTrue(result.created)

    def test_lift_restriction_restores_participation(self):
        register(self.svc, "learner-3")
        self.svc.place_enrollment("enr-3", "learner-3", "course-calligraphy", mode="phone")
        self.svc.apply_safety_restriction("learner-3", "rst-3", modes=("phone",), start=T0, note="暂停电话辅导")
        self.svc.lift_safety_restriction("learner-3", "rst-3", at=T0 + DAY)
        self.svc.schedule_session("s-phone-2", "course-calligraphy", mode="phone", start=T0 + 2 * DAY, end=T0 + 2 * DAY + HOUR)
        result = self.svc.upload_checkin("up-2", "s-phone-2", "learner-3", source="phone", payload={"status": "attended"})
        self.assertTrue(result.created)


if __name__ == "__main__":
    unittest.main()
