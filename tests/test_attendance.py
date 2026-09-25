import unittest

from tests.helpers import DAY, HOUR, T0, build_service, publish_calligraphy, register


class CheckInTest(unittest.TestCase):
    """电话或线下签到重复上传只记一次，内容冲突交由教务核对。"""

    def setUp(self):
        self.svc, self.clock, self.store = build_service()
        publish_calligraphy(self.svc)
        register(self.svc, "learner-1")
        self.svc.place_enrollment("enr-1", "learner-1", "course-calligraphy", mode="offline")
        self.svc.schedule_session("s-1", "course-calligraphy", mode="offline", start=T0 + DAY, end=T0 + DAY + HOUR)

    def test_duplicate_upload_recorded_once(self):
        first = self.svc.upload_checkin("up-1", "s-1", "learner-1", source="phone", payload={"status": "attended", "minutes": 60})
        second = self.svc.upload_checkin("up-2", "s-1", "learner-1", source="offline", payload={"status": "attended", "minutes": 60})
        self.assertTrue(first.created)
        self.assertTrue(second.duplicate)
        self.assertEqual(len(self.svc.records), 1)

    def test_same_upload_id_is_idempotent(self):
        self.svc.upload_checkin("up-1", "s-1", "learner-1", source="phone", payload={"status": "attended"})
        events_before = len(self.store.events)
        again = self.svc.upload_checkin("up-1", "s-1", "learner-1", source="phone", payload={"status": "attended"})
        self.assertTrue(again.duplicate)
        self.assertEqual(len(self.store.events), events_before)  # 不产生新事件

    def test_conflicting_content_goes_to_staff_review(self):
        self.svc.upload_checkin("up-1", "s-1", "learner-1", source="phone", payload={"status": "attended", "minutes": 60})
        conflicted = self.svc.upload_checkin("up-2", "s-1", "learner-1", source="offline", payload={"status": "attended", "minutes": 20})
        self.assertIsNotNone(conflicted.conflict_case_id)
        record = self.svc.records[conflicted.record_id]
        self.assertTrue(record.under_review)
        self.assertEqual(len(record.payloads), 2)  # 两路上传都保留
        # 核对期间不能登记成果
        with self.assertRaises(ValueError):
            self.svc.record_outcome(record.record_id, outcome={"summary": "过早登记"})
        self.svc.resolve_conflict(
            conflicted.conflict_case_id, chosen_status="attended",
            note="以电话记录为准", resolved_by="教务员小李",
        )
        self.assertFalse(record.under_review)
        case = self.svc.conflicts[conflicted.conflict_case_id]
        self.assertEqual(case.status, "resolved")
        self.assertEqual(case.resolved_by, "教务员小李")
        # 核对完成后可以登记成果
        self.svc.record_outcome(record.record_id, outcome={"summary": "完成练习"})


class AbsenceAttributionTest(unittest.TestCase):
    """缺席按原因归因：场地调整、支持未落实等不计入主动退学。"""

    def setUp(self):
        self.svc, self.clock, self.store = build_service()
        publish_calligraphy(self.svc)
        register(self.svc, "learner-1")
        register(self.svc, "learner-2")
        self.svc.place_enrollment("enr-1", "learner-1", "course-calligraphy", mode="offline")
        self.svc.place_enrollment("enr-2", "learner-2", "course-calligraphy", mode="offline")

    def test_venue_change_absence_is_excused_not_dropout(self):
        self.svc.schedule_session(
            "s-2", "course-calligraphy", mode="offline",
            start=T0 + 2 * DAY, end=T0 + 2 * DAY + HOUR,
            venue_changed=True, affected_learners=("learner-1",),
        )
        created = self.svc.finalize_absences("s-2")
        self.assertEqual(len(created), 2)
        by_learner = {self.svc.records[r].learner_id: self.svc.records[r] for r in created}
        self.assertEqual(by_learner["learner-1"].attribution, "venue_change")
        self.assertEqual(by_learner["learner-2"].attribution, "unexcused")
        summary = self.svc.absence_summary("learner-1")
        self.assertEqual(summary.unexcused, 0)
        self.assertEqual(summary.excused.get("venue_change"), 1)
        self.assertFalse(summary.dropout_risk)
        self.assertEqual(self.svc.absence_summary("learner-2").unexcused, 1)

    def test_missing_support_excuses_absence(self):
        self.svc.schedule_session(
            "s-3", "course-calligraphy", mode="offline",
            start=T0 + 3 * DAY, end=T0 + 3 * DAY + HOUR, supports_available=(),
        )
        created = self.svc.finalize_absences("s-3")
        for record_id in created:
            self.assertEqual(self.svc.records[record_id].attribution, "support_unavailable")
        self.assertEqual(self.svc.absence_summary("learner-1").unexcused, 0)

    def test_repeated_unexcused_absence_flags_dropout_risk(self):
        for i in range(3):
            self.svc.schedule_session(
                f"s-x-{i}", "course-calligraphy", mode="offline",
                start=T0 + (i + 5) * DAY, end=T0 + (i + 5) * DAY + HOUR,
            )
            self.svc.finalize_absences(f"s-x-{i}")
        summary = self.svc.absence_summary("learner-1")
        self.assertEqual(summary.unexcused, 3)
        self.assertEqual(summary.unexcused_streak, 3)
        self.assertTrue(summary.dropout_risk)


if __name__ == "__main__":
    unittest.main()
