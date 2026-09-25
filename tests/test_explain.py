import unittest

from tests.helpers import DAY, HOUR, T0, build_service, publish_calligraphy, register


class ExplainTest(unittest.TestCase):
    """教务员可解释：为何安排某种形态、哪些支持已落实、缺席如何归因、目标怎样接续。"""

    def test_staff_can_explain_placement_supports_absences_and_goals(self):
        svc, clock, store = build_service()
        publish_calligraphy(svc, "course-a", capacity=1)
        publish_calligraphy(svc, "course-b", capacity=1, modes=("phone",))
        register(svc, "learner-1", needs=("hearing_assist", "slow_paced"))
        svc.set_goal("learner-1", "goal-1", title="楷书入门", offering_id="course-a")
        svc.place_enrollment("enr-1", "learner-1", "course-a", mode="offline")
        # 第一场线下到课并登记成果
        svc.schedule_session("s-1", "course-a", mode="offline", start=T0 + DAY, end=T0 + DAY + HOUR)
        r1 = svc.upload_checkin("up-1", "s-1", "learner-1", source="offline", payload={"status": "attended"})
        svc.record_outcome(r1.record_id, outcome={"summary": "掌握基本笔画"}, goal_ids=("goal-1",))
        # 第二场因场地调整未能参加
        svc.schedule_session(
            "s-2", "course-a", mode="offline",
            start=T0 + 2 * DAY, end=T0 + 2 * DAY + HOUR,
            venue_changed=True, affected_learners=("learner-1",),
        )
        svc.finalize_absences("s-2")
        # 临时转到电话辅导：先评估，学员确认后迁移
        clock.advance(3 * DAY)
        svc.propose_transfer("prop-1", "enr-1", target_offering_id="course-b", target_mode="phone", reason="学员临时不便到校")
        svc.confirm_transfer("prop-1")
        # 电话场次到课，成果继续累积到同一学习目标
        svc.schedule_session("s-3", "course-b", mode="phone", start=T0 + 4 * DAY, end=T0 + 4 * DAY + HOUR)
        r3 = svc.upload_checkin("up-3", "s-3", "learner-1", source="phone", payload={"status": "attended"})
        svc.record_outcome(r3.record_id, outcome={"summary": "完成偏旁练习"}, goal_ids=("goal-1",))

        explanation = svc.explain_period("learner-1", T0, T0 + 5 * DAY)
        # 授课形态：先线下后电话，迁移带来评估记录
        self.assertEqual([s.mode for s in explanation.mode_segments], ["offline", "phone"])
        self.assertEqual(explanation.mode_segments[1].decision_ref, "prop-1")
        self.assertTrue(explanation.mode_segments[1].checks["supports_satisfiable"])
        # 支持落实：助听与慢速讲解随课程接续，三场全部落实
        self.assertEqual(len(explanation.supports), 3)
        self.assertTrue(all(s.fulfilled for s in explanation.supports))
        # 缺席归因：场地调整，不计入主动退学
        self.assertEqual(len(explanation.absences), 1)
        self.assertEqual(explanation.absences[0].attribution, "venue_change")
        self.assertFalse(explanation.absences[0].counts_toward_dropout)
        # 学习目标跨班接续：目标从 course-a 接到 course-b，两场成果都在
        goal = explanation.goals[0]
        self.assertEqual(goal.path, ["course-a", "course-b"])
        self.assertEqual(goal.outcomes, ["完成偏旁练习", "掌握基本笔画"])
        # 教务可直接使用的中文说明
        text = explanation.render()
        for keyword in ("线下班", "电话辅导", "场地调整", "不计入主动退学", "助听", "楷书入门"):
            self.assertIn(keyword, text)


if __name__ == "__main__":
    unittest.main()
