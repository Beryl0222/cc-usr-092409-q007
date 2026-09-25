import unittest
from datetime import timedelta

from tests.helpers import DAY, HOUR, T0, build_service, publish_calligraphy, register


class TransferTest(unittest.TestCase):
    """换班/换形态：先评估教师资历、名额与支持资源，学员确认后才迁移。"""

    def setUp(self):
        self.svc, self.clock, self.store = build_service()
        publish_calligraphy(self.svc)
        register(self.svc, "learner-1")
        self.svc.place_enrollment("enr-1", "learner-1", "course-calligraphy", mode="offline")

    def test_mode_switch_requires_learner_confirmation(self):
        proposal = self.svc.propose_transfer(
            "prop-1", "enr-1", target_offering_id="course-calligraphy", target_mode="phone"
        )
        self.assertEqual(proposal.status, "pending")
        self.assertTrue(all(proposal.checks.values()))
        # 学员确认前授课形态不变
        self.assertEqual(self.svc.enrollments["enr-1"].modes.at(self.clock.now()).value.mode, "offline")
        self.clock.advance(2 * HOUR)
        self.svc.confirm_transfer("prop-1")
        assignment = self.svc.enrollments["enr-1"].modes.at(self.clock.now()).value
        self.assertEqual(assignment.mode, "phone")
        self.assertEqual(assignment.decision_ref, "prop-1")
        # 原线下区间保留在历史中，可解释"那段时间为什么这样安排"
        segments = self.svc.enrollments["enr-1"].modes.between(T0, T0 + 2 * DAY)
        self.assertEqual([s.value.mode for s in segments], ["offline", "phone"])

    def test_cross_offering_hold_and_expiry_releases_seat(self):
        publish_calligraphy(self.svc, "course-phone-a", capacity=1, modes=("phone",))
        proposal = self.svc.propose_transfer("prop-2", "enr-1", target_offering_id="course-phone-a", target_mode="phone")
        self.assertEqual(proposal.status, "pending")
        self.assertIsNotNone(proposal.hold_id)
        # 名额被预留，其他学员的迁移评估不通过
        register(self.svc, "learner-2")
        self.svc.place_enrollment("enr-2", "learner-2", "course-calligraphy", mode="offline")
        other = self.svc.propose_transfer("prop-3", "enr-2", target_offering_id="course-phone-a", target_mode="phone")
        self.assertEqual(other.status, "rejected")
        self.assertFalse(other.checks["seat_available"])
        # 超时未确认：注入时钟前进，名额释放
        self.clock.advance(timedelta(hours=25))
        released = self.svc.release_expired_holds()
        self.assertEqual(released, ["prop-2"])
        self.assertEqual(self.svc.proposals["prop-2"].status, "expired")
        retry = self.svc.propose_transfer("prop-4", "enr-2", target_offering_id="course-phone-a", target_mode="phone")
        self.assertEqual(retry.status, "pending")

    def test_confirm_after_expiry_raises(self):
        publish_calligraphy(self.svc, "course-phone-b", capacity=1, modes=("phone",))
        self.svc.propose_transfer("prop-5", "enr-1", target_offering_id="course-phone-b", target_mode="phone")
        self.clock.advance(timedelta(hours=25))
        with self.assertRaises(ValueError):
            self.svc.confirm_transfer("prop-5")
        self.assertEqual(self.svc.proposals["prop-5"].status, "expired")

    def test_unsatisfiable_support_blocks_proposal(self):
        publish_calligraphy(self.svc, "course-online", resources=(), modes=("online",))
        proposal = self.svc.propose_transfer("prop-6", "enr-1", target_offering_id="course-online", target_mode="online")
        self.assertEqual(proposal.status, "rejected")
        self.assertFalse(proposal.checks["supports_satisfiable"])
        self.assertIn("助听", proposal.check_details["supports_satisfiable"])
        with self.assertRaises(ValueError):
            self.svc.confirm_transfer("prop-6")

    def test_unqualified_teacher_blocks_proposal(self):
        publish_calligraphy(self.svc, "course-advanced", quals=("junior_art",), required=("senior_art",), modes=("phone",))
        proposal = self.svc.propose_transfer("prop-7", "enr-1", target_offering_id="course-advanced", target_mode="phone")
        self.assertEqual(proposal.status, "rejected")
        self.assertFalse(proposal.checks["teacher_qualified"])

    def test_course_version_update_can_make_transfer_satisfiable(self):
        publish_calligraphy(self.svc, "course-online-2", resources=(), modes=("online",))
        first = self.svc.propose_transfer("prop-11", "enr-1", target_offering_id="course-online-2", target_mode="online")
        self.assertEqual(first.status, "rejected")
        # 课程版本升级后重新评估；旧版本保留在自己的生效区间
        self.clock.advance(HOUR)
        self.svc.version_course("course-online-2", effective_from=self.clock.now(), support_resources=("hearing_assist",))
        second = self.svc.propose_transfer("prop-12", "enr-1", target_offering_id="course-online-2", target_mode="online")
        self.assertEqual(second.status, "pending")
        versions = self.svc.offerings["course-online-2"].versions.records()
        self.assertEqual(len(versions), 2)

    def test_completed_outcome_survives_transfer(self):
        self.svc.schedule_session("s-1", "course-calligraphy", mode="offline", start=T0 + HOUR, end=T0 + 2 * HOUR)
        result = self.svc.upload_checkin("up-1", "s-1", "learner-1", source="offline", payload={"status": "attended"})
        self.svc.record_outcome(result.record_id, outcome={"summary": "完成楷书基本笔画"})
        self.clock.advance(3 * HOUR)
        self.svc.propose_transfer("prop-8", "enr-1", target_offering_id="course-calligraphy", target_mode="phone")
        self.svc.confirm_transfer("prop-8")
        record = self.svc.records[result.record_id]
        self.assertEqual(record.outcome, {"summary": "完成楷书基本笔画"})
        self.assertEqual(record.offering_id, "course-calligraphy")
        with self.assertRaises(ValueError):
            self.svc.record_outcome(result.record_id, outcome={"summary": "篡改成果"})

    def test_goal_continues_across_offerings(self):
        publish_calligraphy(self.svc, "course-phone-c", capacity=1, modes=("phone",))
        self.svc.set_goal("learner-1", "goal-1", title="楷书入门", offering_id="course-calligraphy")
        self.clock.advance(HOUR)
        self.svc.propose_transfer("prop-9", "enr-1", target_offering_id="course-phone-c", target_mode="phone")
        self.svc.confirm_transfer("prop-9")
        goal = self.svc.profiles["learner-1"].goals["goal-1"]
        self.assertEqual(len(goal.continuations), 1)
        self.assertEqual(goal.continuations[0].from_offering_id, "course-calligraphy")
        self.assertEqual(goal.continuations[0].to_offering_id, "course-phone-c")

    def test_decline_releases_hold(self):
        publish_calligraphy(self.svc, "course-phone-d", capacity=1, modes=("phone",))
        self.svc.propose_transfer("prop-10", "enr-1", target_offering_id="course-phone-d", target_mode="phone")
        self.svc.decline_transfer("prop-10")
        self.assertEqual(self.svc.proposals["prop-10"].status, "declined")
        self.assertEqual(self.svc._seats_available("course-phone-d", self.clock.now()), 1)

    def test_support_change_between_propose_and_confirm_blocks_migration(self):
        publish_calligraphy(self.svc, "course-phone-e", capacity=1, modes=("phone",), resources=("hearing_assist",))
        self.svc.propose_transfer("prop-13", "enr-1", target_offering_id="course-phone-e", target_mode="phone")
        # 等待期内支持需求变化，确认时复评不通过
        self.clock.advance(HOUR)
        self.svc.update_support_needs("learner-1", ("hearing_assist", "caregiver_escort"), reason="新增照护陪同")
        with self.assertRaises(ValueError):
            self.svc.confirm_transfer("prop-13")
        self.assertEqual(self.svc.proposals["prop-13"].status, "pending")


if __name__ == "__main__":
    unittest.main()
