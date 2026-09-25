"""换班/换形态：先核验教师资历、名额、支持资源，学员确认后才迁移。"""

import unittest
from datetime import timedelta

from src.continuity import (
    DeliveryFormat,
    EligibilityStatus,
    ProposalStatus,
    StateError,
    SupportNeed,
)
from tests.factories import ALL_NEEDS, at, make_service, publish_offering, session


def base_service():
    service, clock = make_service()
    service.register_learner("L-1", "王阿姨", needs=set(ALL_NEEDS))
    publish_offering(
        service,
        "OFF-LINE",
        fmt=DeliveryFormat.OFFLINE,
        title="书法线下班",
        sessions=[session("S-1", at(10), DeliveryFormat.OFFLINE)],
    )
    service.place_enrollment(
        "EN-1", "L-1", "OFF-LINE", DeliveryFormat.OFFLINE,
        goals=[{"goal_id": "G-1", "title": "楷书入门"}],
    )
    return service, clock


class TransferCheckTest(unittest.TestCase):
    def test_teacher_qualification_blocks_proposal(self) -> None:
        service, _ = base_service()
        publish_offering(
            service, "OFF-PHONE", fmt=DeliveryFormat.PHONE, title="电话辅导班",
            teacher_formats=set(),  # 教师没有电话辅导资历
        )
        proposal = service.propose_transfer("EN-1", "OFF-PHONE", DeliveryFormat.PHONE)
        self.assertEqual(proposal.status, ProposalStatus.REJECTED)
        self.assertTrue(any("资历" in r for r in proposal.reasons))
        # 未确认前授课安排不变
        self.assertEqual(
            service.enrollments["EN-1"].placement_at(service.clock.now()).offering_id,
            "OFF-LINE",
        )

    def test_capacity_and_supports_are_checked(self) -> None:
        service, _ = base_service()
        publish_offering(
            service, "OFF-FULL", fmt=DeliveryFormat.PHONE, capacity=0, title="满员班",
        )
        full = service.propose_transfer("EN-1", "OFF-FULL", DeliveryFormat.PHONE)
        self.assertTrue(any("名额已满" in r for r in full.reasons))
        publish_offering(
            service, "OFF-GAP", fmt=DeliveryFormat.PHONE, title="资源不足班",
            resources={DeliveryFormat.PHONE: {SupportNeed.HEARING_ASSIST}},
        )
        gap = service.propose_transfer("EN-1", "OFF-GAP", DeliveryFormat.PHONE)
        self.assertTrue(any("支持资源不足" in r for r in gap.reasons))
        self.assertIn("SLOW_PACE", gap.checks["unmet_needs"])

    def test_same_target_rejected(self) -> None:
        service, _ = base_service()
        proposal = service.propose_transfer("EN-1", "OFF-LINE", DeliveryFormat.OFFLINE)
        self.assertEqual(proposal.status, ProposalStatus.REJECTED)


class TransferConfirmTest(unittest.TestCase):
    def test_migration_only_after_learner_confirms(self) -> None:
        service, clock = base_service()
        publish_offering(service, "OFF-PHONE", fmt=DeliveryFormat.PHONE, title="电话辅导班")
        proposal = service.propose_transfer("EN-1", "OFF-PHONE", DeliveryFormat.PHONE)
        self.assertEqual(proposal.status, ProposalStatus.PENDING)
        enrollment = service.enrollments["EN-1"]
        self.assertEqual(len(enrollment.placements), 1)  # 确认前不迁移
        clock.advance(timedelta(hours=5))
        service.confirm_transfer(proposal.proposal_id)
        self.assertEqual(service.proposals[proposal.proposal_id].status, ProposalStatus.CONFIRMED)
        old, new = enrollment.placements
        self.assertEqual(old.valid_to, at(5))
        self.assertEqual(new.offering_id, "OFF-PHONE")
        self.assertEqual(new.format, DeliveryFormat.PHONE)
        self.assertEqual(new.proposal_id, proposal.proposal_id)
        # 支持需求跟随学员，不随换形态丢失
        self.assertEqual(service.learners["L-1"].needs_at(at(6)), set(ALL_NEEDS))

    def test_confirm_rechecks_capacity(self) -> None:
        service, clock = base_service()
        publish_offering(
            service, "OFF-PHONE", fmt=DeliveryFormat.PHONE, capacity=1, title="电话辅导班",
        )
        proposal = service.propose_transfer("EN-1", "OFF-PHONE", DeliveryFormat.PHONE)
        # 方案生成后名额被他人占去
        service.register_learner("L-2", "李叔叔")
        service.place_enrollment("EN-2", "L-2", "OFF-PHONE", DeliveryFormat.PHONE)
        confirmed = service.confirm_transfer(proposal.proposal_id)
        self.assertEqual(confirmed.status, ProposalStatus.REJECTED)
        self.assertTrue(any("名额已满" in r for r in confirmed.reasons))

    def test_pending_proposal_expires_by_clock(self) -> None:
        service, clock = base_service()
        publish_offering(service, "OFF-PHONE", fmt=DeliveryFormat.PHONE, title="电话辅导班")
        proposal = service.propose_transfer("EN-1", "OFF-PHONE", DeliveryFormat.PHONE)
        clock.advance(timedelta(hours=73))  # 超过 proposal_ttl
        service.sweep()
        self.assertEqual(service.proposals[proposal.proposal_id].status, ProposalStatus.EXPIRED)
        with self.assertRaises(StateError):
            service.confirm_transfer(proposal.proposal_id)

    def test_suspended_enrollment_cannot_transfer(self) -> None:
        service, _ = base_service()
        publish_offering(service, "OFF-PHONE", fmt=DeliveryFormat.PHONE, title="电话辅导班")
        service.set_eligibility("EN-1", EligibilityStatus.SUSPENDED, reason="住院")
        proposal = service.propose_transfer("EN-1", "OFF-PHONE", DeliveryFormat.PHONE)
        self.assertEqual(proposal.status, ProposalStatus.REJECTED)
        self.assertTrue(any("报名资格" in r for r in proposal.reasons))

    def test_cancel_pending_proposal(self) -> None:
        service, _ = base_service()
        publish_offering(service, "OFF-PHONE", fmt=DeliveryFormat.PHONE, title="电话辅导班")
        proposal = service.propose_transfer("EN-1", "OFF-PHONE", DeliveryFormat.PHONE)
        service.cancel_transfer(proposal.proposal_id)
        self.assertEqual(service.proposals[proposal.proposal_id].status, ProposalStatus.CANCELLED)
        with self.assertRaises(StateError):
            service.confirm_transfer(proposal.proposal_id)


class OutcomePreservationTest(unittest.TestCase):
    def test_completed_outcomes_survive_transfer(self) -> None:
        service, clock = base_service()
        clock.set(at(11))
        service.record_session("EN-1", "S-1", attended=True, outcomes=["G-1"])
        publish_offering(service, "OFF-PHONE", fmt=DeliveryFormat.PHONE, title="电话辅导班")
        proposal = service.propose_transfer("EN-1", "OFF-PHONE", DeliveryFormat.PHONE)
        service.confirm_transfer(proposal.proposal_id)
        goal = service.enrollments["EN-1"].goals["G-1"]
        self.assertEqual(goal.status.value, "COMPLETED")
        self.assertEqual(goal.completed_in_offering, "OFF-LINE")
        # 改班后成果记录不被覆盖
        record = service.records["LR-EN-1-S-1"]
        self.assertIsNone(record.valid_to)
        self.assertEqual(record.outcomes, ("G-1",))

    def test_correction_cannot_drop_completed_outcomes(self) -> None:
        service, clock = base_service()
        clock.set(at(11))
        record_id = service.record_session("EN-1", "S-1", attended=True, outcomes=["G-1"])
        with self.assertRaises(StateError):
            service.correct_record(record_id, outcomes=[], reason="试图清除成果")


if __name__ == "__main__":
    unittest.main()
