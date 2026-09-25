"""教务解释视图：授课形态安排原因、支持落实、缺席归因、目标接续。"""

import unittest
from datetime import timedelta

from src.continuity import (
    AbsenceAttribution,
    DeliveryFormat,
    SupportNeed,
    explain_enrollment,
)
from tests.factories import ALL_NEEDS, at, make_service, publish_offering, session


class ExplainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        svc = self.service
        svc.register_learner("L-1", "陈奶奶", needs=set(ALL_NEEDS))
        publish_offering(
            svc, "OFF-LINE", fmt=DeliveryFormat.OFFLINE, title="书法线下班",
            sessions=[
                session("S-1", at(10), DeliveryFormat.OFFLINE),
                session("S-2", at(20), DeliveryFormat.OFFLINE),
            ],
        )
        svc.place_enrollment(
            "EN-1", "L-1", "OFF-LINE", DeliveryFormat.OFFLINE,
            goals=[{"goal_id": "G-1", "title": "楷书入门"}, {"goal_id": "G-2", "title": "行书进阶"}],
        )

    def test_explain_covers_placement_supports_absence_and_goals(self) -> None:
        svc, clock = self.service, self.clock
        clock.set(at(11))
        svc.record_session("EN-1", "S-1", attended=True, outcomes=["G-1"])
        clock.set(at(21))
        svc.record_session(
            "EN-1", "S-2", attended=False,
            attribution=AbsenceAttribution.VENUE_ADJUSTMENT, note="教室临时调换",
        )
        # 换到电话辅导
        publish_offering(
            svc, "OFF-PHONE", fmt=DeliveryFormat.PHONE, title="电话辅导班",
            sessions=[session("S-3", at(30), DeliveryFormat.PHONE)],
        )
        clock.set(at(25))
        proposal = svc.propose_transfer("EN-1", "OFF-PHONE", DeliveryFormat.PHONE)
        svc.confirm_transfer(proposal.proposal_id)
        clock.set(at(31))
        svc.record_session("EN-1", "S-3", attended=True)

        report = explain_enrollment(svc, "EN-1", at(0), at(40))
        # 授课形态安排：两段，各带原因
        self.assertEqual(len(report["placements"]), 2)
        first, second = report["placements"]
        self.assertEqual(first["format"], "OFFLINE")
        self.assertEqual(second["format"], "PHONE")
        self.assertIn("经学员确认迁移", second["reason"])
        self.assertTrue(second["proposal_checks"]["teacher_ok"])
        # 支持落实：电话班资源齐全，无缺口
        s3 = next(s for s in report["sessions"] if s["session_id"] == "S-3")
        self.assertEqual(s3["support_gaps"], [])
        self.assertEqual(len(s3["supports_delivered"]), 3)
        # 缺席归因：场地调整不计入退学评估
        self.assertEqual(report["absence_summary"], {"VENUE_ADJUSTMENT": 1})
        self.assertEqual(report["withdrawal_risk"]["counts_toward_withdrawal"], 0)
        s2 = next(s for s in report["sessions"] if s["session_id"] == "S-2")
        self.assertFalse(s2["counts_toward_withdrawal"])
        # 目标：G-1 已完成且留在原班级
        g1 = next(g for g in report["goals"] if g["goal_id"] == "G-1")
        self.assertEqual(g1["status"], "COMPLETED")
        self.assertEqual(g1["completed_in_offering"], "OFF-LINE")
        # 中文叙述可直接向家属说明
        narrative = report["narrative"]
        for keyword in ("授课形态安排", "支持需求落实", "缺席归因", "学习目标接续", "场地调整"):
            self.assertIn(keyword, narrative)

    def test_explain_shows_carry_chain_across_enrollments(self) -> None:
        svc, clock = self.service, self.clock
        svc.register_learner("L-2", "周爷爷")
        publish_offering(svc, "OFF-2", fmt=DeliveryFormat.PHONE, title="国画电话班")
        svc.place_enrollment("EN-9", "L-2", "OFF-2", DeliveryFormat.PHONE)
        carried = svc.carry_goals("EN-1", "EN-9", ["G-2"])
        report = svc.explain("EN-9", at(0), at(40))
        goal = next(g for g in report["goals"] if g["goal_id"] == carried[0])
        self.assertEqual(goal["carried_from"], "G-2")
        self.assertEqual(goal["carry_chain"][0]["enrollment_id"], "EN-1")
        self.assertIn("跨班接续", report["narrative"])
        # 原报名中的目标标记为已接续，成果归属不变
        self.assertEqual(svc.enrollments["EN-1"].goals["G-2"].status.value, "CARRIED")

    def test_support_gap_visible_in_report(self) -> None:
        svc, clock = self.service, self.clock
        publish_offering(
            svc, "OFF-PHONE", fmt=DeliveryFormat.PHONE, title="资源不足电话班",
            resources={DeliveryFormat.PHONE: {SupportNeed.HEARING_ASSIST}},
            sessions=[session("S-9", at(30), DeliveryFormat.PHONE)],
        )
        # 资源不足时直接报名会被拒绝；改为先减少需求再迁入，再补回需求制造缺口场景
        svc.update_support_needs("L-1", {SupportNeed.HEARING_ASSIST}, effective_from=at(5))
        clock.set(at(6))
        svc.place_enrollment(
            "EN-2", "L-1", "OFF-PHONE", DeliveryFormat.PHONE,
        )
        svc.update_support_needs("L-1", set(ALL_NEEDS), effective_from=at(25))
        clock.set(at(31))
        report = svc.explain("EN-2", at(0), at(40))
        s9 = next(s for s in report["sessions"] if s["session_id"] == "S-9")
        self.assertIn("SLOW_PACE", s9["support_gaps"])
        self.assertIn("CARE_COMPANION", s9["support_gaps"])


if __name__ == "__main__":
    unittest.main()
