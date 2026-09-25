"""端到端：学员从线下班临时转电话辅导的完整教务故事。

对应需求：支持需求随课程接续、场地调整缺席不误算主动退学、
已完成成果不被改班覆盖、教务员能解释来龙去脉。
"""

import unittest
from datetime import timedelta

from src.continuity import (
    AbsenceAttribution,
    DeliveryFormat,
    ProposalStatus,
    StateError,
    SupportNeed,
)
from tests.factories import ALL_NEEDS, at, make_service, publish_offering, session


class ContinuityStoryTest(unittest.TestCase):
    def test_offline_to_phone_continuity(self) -> None:
        service, clock = make_service()
        svc = service
        # 王阿姨报名线下书法班，需要助听、慢速讲解、照护陪同
        svc.register_learner("L-1", "王阿姨", needs=set(ALL_NEEDS))
        publish_offering(
            svc, "CALLI-OFF", fmt=DeliveryFormat.OFFLINE, title="书法线下班",
            sessions=[
                session("S-1", at(10), DeliveryFormat.OFFLINE),
                session("S-2", at(30), DeliveryFormat.OFFLINE),
            ],
        )
        svc.place_enrollment(
            "EN-1", "L-1", "CALLI-OFF", DeliveryFormat.OFFLINE,
            goals=[{"goal_id": "G-1", "title": "楷书入门"}],
        )
        # 第一场出席并完成阶段目标
        clock.set(at(11))
        svc.record_session("EN-1", "S-1", attended=True, outcomes=["G-1"])

        # 场地临时调整：发布新版本换教室，王阿姨未能到场
        publish_offering(
            svc, "CALLI-OFF", fmt=DeliveryFormat.OFFLINE, title="书法线下班",
            sessions=[session("S-2", at(30), DeliveryFormat.OFFLINE, location="临时教室")],
            effective_from=at(20),
        )
        clock.set(at(31))
        svc.record_session("EN-1", "S-2", attended=False)
        # 缺席默认是待归因，不会直接计入退学评估
        self.assertEqual(svc.withdrawal_risk("EN-1")["counts_toward_withdrawal"], 0)
        # 教务核实为场地调整所致
        svc.attribute_absence(
            "LR-EN-1-S-2", AbsenceAttribution.VENUE_ADJUSTMENT, reason="教室临时调换未通知到"
        )
        self.assertEqual(svc.withdrawal_risk("EN-1")["counts_toward_withdrawal"], 0)

        # 临时转电话辅导：先核验教师资历、名额与支持资源
        publish_offering(
            svc, "CALLI-PHONE", fmt=DeliveryFormat.PHONE, title="书法电话辅导班",
            sessions=[session("S-3", at(50), DeliveryFormat.PHONE)],
        )
        clock.set(at(40))
        proposal = svc.propose_transfer("EN-1", "CALLI-PHONE", DeliveryFormat.PHONE)
        self.assertEqual(proposal.status, ProposalStatus.PENDING)
        # 学员确认前不迁移
        self.assertEqual(
            svc.enrollments["EN-1"].placement_at(at(40)).offering_id, "CALLI-OFF"
        )
        svc.confirm_transfer(proposal.proposal_id)
        # 支持需求随学员接续到电话辅导
        self.assertEqual(svc.learners["L-1"].needs_at(at(50)), set(ALL_NEEDS))
        clock.set(at(51))
        svc.record_session("EN-1", "S-3", attended=True)

        # 教务员解释这段时间的安排
        report = svc.explain("EN-1", at(0), at(60))
        self.assertEqual([p["format"] for p in report["placements"]], ["OFFLINE", "PHONE"])
        self.assertEqual(report["absence_summary"], {"VENUE_ADJUSTMENT": 1})
        phone_session = next(s for s in report["sessions"] if s["session_id"] == "S-3")
        self.assertEqual(phone_session["support_gaps"], [])
        goal = next(g for g in report["goals"] if g["goal_id"] == "G-1")
        self.assertEqual(goal["status"], "COMPLETED")
        self.assertEqual(goal["completed_in_offering"], "CALLI-OFF")
        narrative = report["narrative"]
        self.assertIn("经学员确认迁移", narrative)
        self.assertIn("场地调整", narrative)

        # 已完成成果不被后续改班覆盖：更正记录也不能移除成果
        with self.assertRaises(StateError):
            svc.correct_record("LR-EN-1-S-1", outcomes=[], reason="误操作")


if __name__ == "__main__":
    unittest.main()
