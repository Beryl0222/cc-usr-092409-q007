"""银龄连续学习档案服务。

课程版本、报名资格、支持需求、授课形态与每次学习记录都保存在生效区间上；
换班或换形态先评估教师资历、名额与支持资源，建议经学员确认后才迁移，
已经完成的学习成果不被后续改班覆盖；支持需求变化只影响未来场次；
安全限制暂停相应活动但保留其他可参加内容；候补按原申请时间与可满足
条件推进，超时未确认的名额由可注入时钟释放；电话或线下签到重复上传只
记一次，内容冲突交教务核对；所有状态变化写入事件日志，重启后重放恢复，
继续提醒、候补与阶段回顾。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from .clock import Clock, SystemClock
from .domain import (
    ATTRIBUTION_LABELS,
    CHECK_LABELS,
    ELIGIBILITY_ACTIVE,
    ELIGIBILITY_ENDED,
    ELIGIBILITY_SUSPENDED,
    MODE_LABELS,
    NEED_LABELS,
    AbsenceAttribution,
    AbsenceItem,
    AbsenceSummary,
    CheckInResult,
    ConflictCase,
    ContinuousProfile,
    CourseOffering,
    CourseSession,
    CourseVersion,
    Enrollment,
    Followups,
    GoalContinuation,
    GoalLine,
    LearningGoal,
    LearningRecord,
    ModeAssignment,
    ModeSegment,
    PeriodExplanation,
    SeatHold,
    SafetyRestriction,
    SupportFulfillment,
    TransferProposal,
    WaitlistEntry,
    WaitlistOffer,
    parse_ts,
)
from .intervals import Interval
from .store import EventStore, NullEventStore


def _event_seq(event_id: str) -> int:
    return int(event_id.rsplit("-", 1)[1])


def _outcome_text(outcome: Any) -> str:
    if isinstance(outcome, dict):
        return str(outcome.get("summary", outcome))
    return str(outcome)


class LearningContinuityService:
    """连续学习档案外观服务：命令先校验再写事件，状态由事件重放得到。"""

    def __init__(
        self,
        clock: Clock | None = None,
        store: EventStore | None = None,
        *,
        hold_ttl: timedelta = timedelta(hours=48),
        offer_ttl: timedelta = timedelta(hours=48),
        stage_size: int = 4,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self.store: EventStore = store or NullEventStore()
        self.hold_ttl = hold_ttl
        self.offer_ttl = offer_ttl
        self.stage_size = stage_size
        self.offerings: dict[str, CourseOffering] = {}
        self.profiles: dict[str, ContinuousProfile] = {}
        self.enrollments: dict[str, Enrollment] = {}
        self.sessions: dict[str, CourseSession] = {}
        self.records: dict[str, LearningRecord] = {}
        self.proposals: dict[str, TransferProposal] = {}
        self.holds: dict[str, SeatHold] = {}
        self.waitlist_entries: dict[str, WaitlistEntry] = {}
        self.offers: dict[str, WaitlistOffer] = {}
        self.conflicts: dict[str, ConflictCase] = {}
        self._upload_index: dict[str, str] = {}
        self._record_by_session_learner: dict[tuple[str, str], str] = {}
        self._versions: dict[str, int] = {}
        self._seq = 0
        self._handlers = {
            "COURSE_PUBLISHED": self._on_course_published,
            "COURSE_VERSIONED": self._on_course_versioned,
            "LEARNER_REGISTERED": self._on_learner_registered,
            "SUPPORT_UPDATED": self._on_support_updated,
            "SAFETY_RESTRICTION_APPLIED": self._on_safety_applied,
            "SAFETY_RESTRICTION_LIFTED": self._on_safety_lifted,
            "ENROLLMENT_PLACED": self._on_enrollment_placed,
            "ELIGIBILITY_CHANGED": self._on_eligibility_changed,
            "TRANSFER_PROPOSED": self._on_transfer_proposed,
            "TRANSFER_CONFIRMED": self._on_transfer_confirmed,
            "TRANSFER_DECLINED": self._on_transfer_declined,
            "TRANSFER_EXPIRED": self._on_transfer_expired,
            "GOAL_SET": self._on_goal_set,
            "GOAL_CONTINUED": self._on_goal_continued,
            "SESSION_RECORDED": self._on_session_recorded,
            "CHECKIN_UPLOADED": self._on_checkin_uploaded,
            "CHECKIN_CONFLICT_RAISED": self._on_conflict_raised,
            "CHECKIN_CONFLICT_RESOLVED": self._on_conflict_resolved,
            "ABSENCE_ATTRIBUTED": self._on_absence_attributed,
            "OUTCOME_REVIEWED": self._on_outcome_reviewed,
            "WAITLIST_JOINED": self._on_waitlist_joined,
            "WAITLIST_OFFERED": self._on_waitlist_offered,
            "WAITLIST_PROMOTED": self._on_waitlist_promoted,
            "WAITLIST_OFFER_EXPIRED": self._on_waitlist_offer_expired,
            "STAGE_REVIEW_RECORDED": self._on_stage_review,
        }

    @property
    def event_types(self) -> frozenset[str]:
        return frozenset(self._handlers)

    # ------------------------------------------------------------------
    # 事件写入与重放
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str, payload: dict, summary: str) -> dict:
        self._seq += 1
        event = {
            "event_id": f"evt-{self._seq:06d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self.clock.now().isoformat(),
            "version": self._versions.get(aggregate_id, 0) + 1,
            "summary": summary,
            "payload": payload,
        }
        self.store.append(event)
        self._apply(event)
        return event

    def _apply(self, event: dict) -> None:
        handler = self._handlers.get(event["event_type"])
        if handler is None:
            raise ValueError(f"未知事件类型：{event['event_type']}")
        handler(event)
        aggregate_id = event["aggregate_id"]
        self._versions[aggregate_id] = max(self._versions.get(aggregate_id, 0), event["version"])
        self._seq = max(self._seq, _event_seq(event["event_id"]))

    @classmethod
    def recover(cls, clock: Clock | None = None, store: EventStore | None = None, **kwargs: Any) -> "LearningContinuityService":
        """从事件存储重放恢复，重启后继续提醒、候补与阶段回顾。"""
        if store is None:
            raise ValueError("恢复服务需要事件存储")
        service = cls(clock=clock, store=store, **kwargs)
        for event in store.read_all():
            service._apply(event)
        return service

    # ------------------------------------------------------------------
    # 内部查询
    # ------------------------------------------------------------------

    def _offering(self, offering_id: str) -> CourseOffering:
        try:
            return self.offerings[offering_id]
        except KeyError:
            raise ValueError(f"未知课程：{offering_id}") from None

    def _profile(self, learner_id: str) -> ContinuousProfile:
        try:
            return self.profiles[learner_id]
        except KeyError:
            raise ValueError(f"未知学员：{learner_id}") from None

    def _enrollment(self, enrollment_id: str) -> Enrollment:
        try:
            return self.enrollments[enrollment_id]
        except KeyError:
            raise ValueError(f"未知报名：{enrollment_id}") from None

    def _session(self, session_id: str) -> CourseSession:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise ValueError(f"未知场次：{session_id}") from None

    def _record(self, record_id: str) -> LearningRecord:
        try:
            return self.records[record_id]
        except KeyError:
            raise ValueError(f"未知学习记录：{record_id}") from None

    def _proposal(self, proposal_id: str) -> TransferProposal:
        try:
            return self.proposals[proposal_id]
        except KeyError:
            raise ValueError(f"未知迁移提案：{proposal_id}") from None

    def _offer(self, offer_id: str) -> WaitlistOffer:
        try:
            return self.offers[offer_id]
        except KeyError:
            raise ValueError(f"未知候补名额：{offer_id}") from None

    def _conflict(self, case_id: str) -> ConflictCase:
        try:
            return self.conflicts[case_id]
        except KeyError:
            raise ValueError(f"未知核对事项：{case_id}") from None

    def _version_at(self, offering_id: str, moment: datetime) -> CourseVersion | None:
        offering = self.offerings.get(offering_id)
        if offering is None:
            return None
        effective = offering.versions.at(moment)
        return effective.value if effective else None

    def _needs_at(self, learner_id: str, moment: datetime) -> frozenset[str]:
        profile = self.profiles.get(learner_id)
        if profile is None:
            return frozenset()
        effective = profile.support_needs.at(moment)
        return effective.value if effective else frozenset()

    def _enrollments_of(self, learner_id: str) -> list[Enrollment]:
        return [e for e in self.enrollments.values() if e.learner_id == learner_id]

    def _enrollment_at(self, learner_id: str, offering_id: str, moment: datetime) -> Enrollment | None:
        for enrollment in self._enrollments_of(learner_id):
            assignment = enrollment.modes.at(moment)
            if assignment and assignment.value.offering_id == offering_id:
                return enrollment
        return None

    def _seats_available(self, offering_id: str, moment: datetime, *, ignore_offer_id: str | None = None) -> int:
        version = self._version_at(offering_id, moment)
        if version is None:
            return 0
        taken = 0
        for enrollment in self.enrollments.values():
            if enrollment.offering_id != offering_id:
                continue
            elig = enrollment.eligibility.at(moment)
            if elig and elig.value == ELIGIBILITY_ACTIVE:
                taken += 1
        for hold in self.holds.values():
            if hold.offering_id == offering_id and hold.status == "active" and hold.expires_at > moment:
                taken += 1
        for offer in self.offers.values():
            if offer.offer_id == ignore_offer_id:
                continue
            if offer.offering_id == offering_id and offer.status == "pending" and offer.expires_at > moment:
                taken += 1
        return version.capacity - taken

    def _is_paused(self, learner_id: str, session: CourseSession) -> bool:
        profile = self.profiles.get(learner_id)
        if profile is None:
            return False
        return any(
            session.mode in restriction.modes and restriction.interval.contains(session.interval.start)
            for restriction in profile.safety_restrictions.values()
        )

    def _session_supports(self, session: CourseSession) -> frozenset[str]:
        if session.supports_available is not None:
            return session.supports_available
        version = self._version_at(session.offering_id, session.interval.start)
        return version.support_resources if version else frozenset()

    def _expected_enrollments(self, session: CourseSession) -> list[Enrollment]:
        """按场次开始时间的生效区间判断谁应当参加，而不是按当前状态。"""
        expected = []
        for enrollment in self.enrollments.values():
            assignment = enrollment.modes.at(session.interval.start)
            if assignment is None or assignment.value.offering_id != session.offering_id:
                continue
            if assignment.value.mode != session.mode:
                continue
            elig = enrollment.eligibility.at(session.interval.start)
            if not elig or elig.value != ELIGIBILITY_ACTIVE:
                continue
            expected.append(enrollment)
        return expected

    def _expected_sessions(self, learner_id: str, start: datetime, end: datetime) -> list[CourseSession]:
        result = []
        for session in self.sessions.values():
            if session.interval.end is not None and session.interval.end <= start:
                continue
            if session.interval.start >= end:
                continue
            enrollment = self._enrollment_at(learner_id, session.offering_id, session.interval.start)
            if enrollment is None:
                continue
            assignment = enrollment.modes.at(session.interval.start)
            if assignment is None or assignment.value.mode != session.mode:
                continue
            elig = enrollment.eligibility.at(session.interval.start)
            if not elig or elig.value != ELIGIBILITY_ACTIVE:
                continue
            result.append(session)
        return sorted(result, key=lambda s: s.interval.start)

    # ------------------------------------------------------------------
    # 课程与学员登记
    # ------------------------------------------------------------------

    def publish_course(
        self,
        offering_id: str,
        *,
        title: str,
        subject: str = "",
        teacher_id: str,
        teacher_qualifications,
        required_qualifications=(),
        capacity: int,
        support_resources=(),
        modes=("offline",),
        effective_from: datetime,
    ) -> None:
        if offering_id in self.offerings:
            raise ValueError(f"课程已存在：{offering_id}")
        version = {
            "version_no": 1,
            "effective_from": effective_from.isoformat(),
            "teacher_id": teacher_id,
            "teacher_qualifications": sorted(teacher_qualifications),
            "required_qualifications": sorted(required_qualifications),
            "capacity": capacity,
            "support_resources": sorted(support_resources),
            "modes": sorted(modes),
        }
        self._emit(
            "COURSE_PUBLISHED", "course_offering", offering_id,
            {"offering_id": offering_id, "title": title, "subject": subject, "version": version},
            f"发布课程《{title}》",
        )

    def version_course(self, offering_id: str, *, effective_from: datetime, **changes: Any) -> None:
        """发布新课程版本；旧版本保留在各自的生效区间里。"""
        offering = self._offering(offering_id)
        base = offering.versions.latest()
        if base is None:
            raise ValueError("课程尚未发布")
        current = base.value
        version = {
            "version_no": current.version_no + 1,
            "effective_from": effective_from.isoformat(),
            "teacher_id": changes.get("teacher_id", current.teacher_id),
            "teacher_qualifications": sorted(changes.get("teacher_qualifications", current.teacher_qualifications)),
            "required_qualifications": sorted(changes.get("required_qualifications", current.required_qualifications)),
            "capacity": changes.get("capacity", current.capacity),
            "support_resources": sorted(changes.get("support_resources", current.support_resources)),
            "modes": sorted(changes.get("modes", current.modes)),
        }
        self._emit(
            "COURSE_VERSIONED", "course_offering", offering_id,
            {"offering_id": offering_id, "version": version},
            f"课程版本更新到 v{version['version_no']}",
        )

    def register_learner(self, learner_id: str, *, name: str = "", support_needs=(), effective_from: datetime | None = None, reason: str = "初始登记") -> None:
        if learner_id in self.profiles:
            raise ValueError(f"学员已登记：{learner_id}")
        effective_from = effective_from or self.clock.now()
        self._emit(
            "LEARNER_REGISTERED", "learner_profile", learner_id,
            {
                "learner_id": learner_id,
                "name": name,
                "support_needs": sorted(support_needs),
                "effective_from": effective_from.isoformat(),
                "reason": reason,
            },
            "登记学员连续档案",
        )

    def update_support_needs(self, learner_id: str, support_needs, *, effective_from: datetime | None = None, reason: str = "支持需求变化") -> None:
        """更新支持需求：自 effective_from 起生效，只影响未来场次。"""
        self._profile(learner_id)
        effective_from = effective_from or self.clock.now()
        self._emit(
            "SUPPORT_UPDATED", "learner_profile", learner_id,
            {
                "learner_id": learner_id,
                "support_needs": sorted(support_needs),
                "effective_from": effective_from.isoformat(),
                "reason": reason,
            },
            "更新支持需求（仅影响未来场次）",
        )

    def apply_safety_restriction(self, learner_id: str, restriction_id: str, *, modes, start: datetime | None = None, end: datetime | None = None, note: str = "") -> None:
        """安全限制：暂停指定授课形态，其他形态仍可参加。"""
        self._profile(learner_id)
        start = start or self.clock.now()
        self._emit(
            "SAFETY_RESTRICTION_APPLIED", "learner_profile", learner_id,
            {
                "learner_id": learner_id,
                "restriction_id": restriction_id,
                "modes": sorted(modes),
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
                "note": note,
            },
            "应用安全限制",
        )

    def lift_safety_restriction(self, learner_id: str, restriction_id: str, *, at: datetime | None = None) -> None:
        profile = self._profile(learner_id)
        if restriction_id not in profile.safety_restrictions:
            raise ValueError(f"未知安全限制：{restriction_id}")
        at = at or self.clock.now()
        self._emit(
            "SAFETY_RESTRICTION_LIFTED", "learner_profile", learner_id,
            {"learner_id": learner_id, "restriction_id": restriction_id, "lifted_at": at.isoformat()},
            "解除安全限制",
        )

    def set_goal(self, learner_id: str, goal_id: str, *, title: str, offering_id: str) -> None:
        self._profile(learner_id)
        self._emit(
            "GOAL_SET", "learner_profile", learner_id,
            {"learner_id": learner_id, "goal_id": goal_id, "title": title, "offering_id": offering_id},
            f"设定学习目标《{title}》",
        )

    # ------------------------------------------------------------------
    # 报名与资格
    # ------------------------------------------------------------------

    def place_enrollment(
        self,
        enrollment_id: str,
        learner_id: str,
        offering_id: str,
        *,
        mode: str,
        effective_from: datetime | None = None,
        reason: str = "初始报名",
        decision_ref: str | None = None,
        ignore_offer_id: str | None = None,
    ) -> None:
        effective_from = effective_from or self.clock.now()
        self._profile(learner_id)
        if enrollment_id in self.enrollments:
            raise ValueError(f"报名标识已存在：{enrollment_id}")
        version = self._version_at(offering_id, effective_from)
        if version is None:
            raise ValueError("该时刻课程没有生效版本")
        if mode not in version.modes:
            raise ValueError(f"课程版本不支持授课形态：{MODE_LABELS.get(mode, mode)}")
        if self._enrollment_at(learner_id, offering_id, effective_from) is not None:
            raise ValueError("学员已报名该课程，请勿重复报名")
        needs = self._needs_at(learner_id, effective_from)
        missing = needs - version.support_resources
        if missing:
            raise ValueError("课程无法落实支持需求：" + "、".join(NEED_LABELS.get(n, n) for n in sorted(missing)))
        if self._seats_available(offering_id, effective_from, ignore_offer_id=ignore_offer_id) < 1:
            raise ValueError("课程名额不足")
        self._emit(
            "ENROLLMENT_PLACED", "enrollment", enrollment_id,
            {
                "enrollment_id": enrollment_id,
                "learner_id": learner_id,
                "offering_id": offering_id,
                "mode": mode,
                "effective_from": effective_from.isoformat(),
                "reason": reason,
                "decision_ref": decision_ref,
            },
            f"报名成功（{MODE_LABELS.get(mode, mode)}）",
        )

    def set_eligibility(self, enrollment_id: str, status: str, *, effective_from: datetime | None = None, reason: str = "") -> None:
        """调整报名资格：资格变化同样保留生效区间。"""
        if status not in (ELIGIBILITY_ACTIVE, ELIGIBILITY_SUSPENDED, ELIGIBILITY_ENDED):
            raise ValueError(f"未知报名资格状态：{status}")
        enrollment = self._enrollment(enrollment_id)
        effective_from = effective_from or self.clock.now()
        self._emit(
            "ELIGIBILITY_CHANGED", "enrollment", enrollment_id,
            {
                "enrollment_id": enrollment_id,
                "status": status,
                "effective_from": effective_from.isoformat(),
                "reason": reason,
            },
            f"报名资格调整为 {status}",
        )
        if status == ELIGIBILITY_ENDED:
            self.advance_waitlist(enrollment.offering_id)

    # ------------------------------------------------------------------
    # 换班 / 换形态：先评估，学员确认后才迁移
    # ------------------------------------------------------------------

    def _evaluate_transfer(self, enrollment: Enrollment, target_offering_id: str, target_mode: str, moment: datetime) -> tuple[dict[str, bool], dict[str, str]]:
        checks: dict[str, bool] = {}
        details: dict[str, str] = {}
        elig = enrollment.eligibility.at(moment)
        checks["eligibility_active"] = bool(elig and elig.value == ELIGIBILITY_ACTIVE)
        if not checks["eligibility_active"]:
            details["eligibility_active"] = "报名资格当前不可用"
        version = self._version_at(target_offering_id, moment)
        if version is None:
            checks["mode_supported"] = False
            checks["teacher_qualified"] = False
            checks["seat_available"] = False
            checks["supports_satisfiable"] = False
            details["mode_supported"] = "目标课程在该时刻没有生效版本"
            return checks, details
        checks["mode_supported"] = target_mode in version.modes
        if not checks["mode_supported"]:
            details["mode_supported"] = f"目标课程不支持{MODE_LABELS.get(target_mode, target_mode)}"
        missing_quals = version.required_qualifications - version.teacher_qualifications
        checks["teacher_qualified"] = not missing_quals
        if missing_quals:
            details["teacher_qualified"] = "教师缺少资历：" + "、".join(sorted(missing_quals))
        if target_offering_id == enrollment.offering_id:
            checks["seat_available"] = True  # 同班换形态不占用新名额
        else:
            checks["seat_available"] = self._seats_available(target_offering_id, moment) >= 1
            if not checks["seat_available"]:
                details["seat_available"] = "目标课程名额不足"
        needs = self._needs_at(enrollment.learner_id, moment)
        missing_needs = needs - version.support_resources
        checks["supports_satisfiable"] = not missing_needs
        if missing_needs:
            details["supports_satisfiable"] = "无法落实：" + "、".join(NEED_LABELS.get(n, n) for n in sorted(missing_needs))
        return checks, details

    def propose_transfer(self, proposal_id: str, enrollment_id: str, *, target_offering_id: str, target_mode: str, reason: str = "") -> TransferProposal:
        """生成迁移建议：先判断教师资历、名额与支持资源，学员确认前不迁移。"""
        now = self.clock.now()
        enrollment = self._enrollment(enrollment_id)
        current = enrollment.modes.at(now)
        if current and current.value.offering_id == target_offering_id and current.value.mode == target_mode:
            raise ValueError("目标授课形态与当前一致，无需迁移")
        checks, details = self._evaluate_transfer(enrollment, target_offering_id, target_mode, now)
        passed = all(checks.values())
        hold_id = None
        expires_at = None
        if passed:
            expires_at = now + self.hold_ttl
            if target_offering_id != enrollment.offering_id:
                hold_id = f"hold-{proposal_id}"
            status = "pending"
            summary = "迁移建议已生成，待学员确认"
        else:
            status = "rejected"
            failed = "、".join(CHECK_LABELS[k] for k, v in checks.items() if not v)
            summary = f"迁移建议未通过评估：{failed}"
        self._emit(
            "TRANSFER_PROPOSED", "transfer_proposal", proposal_id,
            {
                "proposal_id": proposal_id,
                "enrollment_id": enrollment_id,
                "learner_id": enrollment.learner_id,
                "target_offering_id": target_offering_id,
                "target_mode": target_mode,
                "checks": checks,
                "check_details": details,
                "status": status,
                "reason": reason,
                "proposed_at": now.isoformat(),
                "expires_at": expires_at.isoformat() if expires_at else None,
                "hold_id": hold_id,
            },
            summary,
        )
        return self.proposals[proposal_id]

    def confirm_transfer(self, proposal_id: str, *, confirmed_by: str = "学员") -> TransferProposal:
        """学员确认后才迁移；已完成的学习成果保留在原记录中不被覆盖。"""
        now = self.clock.now()
        proposal = self._proposal(proposal_id)
        if proposal.status != "pending":
            raise ValueError(f"提案当前状态不可确认：{proposal.status}")
        if proposal.expires_at is not None and now >= proposal.expires_at:
            self._expire_proposal(proposal, now)
            raise ValueError("确认超时，名额已释放")
        enrollment = self._enrollment(proposal.enrollment_id)
        # 确认时复评：等待期内教师资历与支持资源可能变化；名额由预留保障
        checks, _ = self._evaluate_transfer(enrollment, proposal.target_offering_id, proposal.target_mode, now)
        blocking = [k for k in ("eligibility_active", "mode_supported", "teacher_qualified", "supports_satisfiable") if not checks.get(k)]
        if blocking:
            raise ValueError("确认时复评未通过：" + "、".join(CHECK_LABELS[k] for k in blocking))
        from_offering = enrollment.offering_id
        to_offering = proposal.target_offering_id
        reason = f"{confirmed_by}确认迁移"
        self._emit(
            "TRANSFER_CONFIRMED", "transfer_proposal", proposal_id,
            {
                "proposal_id": proposal_id,
                "enrollment_id": proposal.enrollment_id,
                "learner_id": proposal.learner_id,
                "from_offering_id": from_offering,
                "to_offering_id": to_offering,
                "mode": proposal.target_mode,
                "switched_at": now.isoformat(),
                "hold_id": proposal.hold_id,
                "reason": reason,
            },
            f"{reason}：{MODE_LABELS.get(proposal.target_mode, proposal.target_mode)}",
        )
        if to_offering != from_offering:
            self._continue_goals(proposal.learner_id, from_offering, to_offering, now)
            self.advance_waitlist(from_offering)  # 腾出名额后推进候补
        return self.proposals[proposal_id]

    def decline_transfer(self, proposal_id: str, *, decided_by: str = "学员") -> None:
        proposal = self._proposal(proposal_id)
        if proposal.status != "pending":
            raise ValueError(f"提案当前状态不可拒绝：{proposal.status}")
        self._emit(
            "TRANSFER_DECLINED", "transfer_proposal", proposal_id,
            {
                "proposal_id": proposal_id,
                "hold_id": proposal.hold_id,
                "decided_by": decided_by,
                "decided_at": self.clock.now().isoformat(),
            },
            f"{decided_by}拒绝迁移建议",
        )

    def release_expired_holds(self) -> list[str]:
        """释放超时未确认的迁移预留名额；时间来自注入的时钟。"""
        now = self.clock.now()
        released = []
        touched = set()
        for proposal in list(self.proposals.values()):
            if proposal.status == "pending" and proposal.expires_at is not None and now >= proposal.expires_at:
                self._expire_proposal(proposal, now)
                released.append(proposal.proposal_id)
                touched.add(proposal.target_offering_id)
        for offering_id in touched:
            self.advance_waitlist(offering_id)
        return released

    def _expire_proposal(self, proposal: TransferProposal, at: datetime) -> None:
        self._emit(
            "TRANSFER_EXPIRED", "transfer_proposal", proposal.proposal_id,
            {"proposal_id": proposal.proposal_id, "hold_id": proposal.hold_id, "expired_at": at.isoformat()},
            "确认超时，名额已释放",
        )

    def _continue_goals(self, learner_id: str, from_offering: str, to_offering: str, at: datetime) -> None:
        profile = self._profile(learner_id)
        for goal in profile.goals.values():
            current = goal.continuations[-1].to_offering_id if goal.continuations else goal.origin_offering_id
            if current != from_offering:
                continue
            self._emit(
                "GOAL_CONTINUED", "learner_profile", learner_id,
                {
                    "learner_id": learner_id,
                    "goal_id": goal.goal_id,
                    "from_offering_id": from_offering,
                    "to_offering_id": to_offering,
                    "at": at.isoformat(),
                    "note": "随迁移接续学习目标",
                },
                f"学习目标《{goal.title}》接续到 {to_offering}",
            )

    # ------------------------------------------------------------------
    # 场次、签到与缺席归因
    # ------------------------------------------------------------------

    def schedule_session(
        self,
        session_id: str,
        offering_id: str,
        *,
        mode: str,
        start: datetime,
        end: datetime | None = None,
        venue_changed: bool = False,
        affected_learners=(),
        supports_available=None,
    ) -> None:
        self._offering(offering_id)
        self._emit(
            "SESSION_RECORDED", "course_session", session_id,
            {
                "session_id": session_id,
                "offering_id": offering_id,
                "mode": mode,
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
                "venue_changed": venue_changed,
                "affected_learners": sorted(affected_learners),
                "supports_available": sorted(supports_available) if supports_available is not None else None,
            },
            "记录课程场次",
        )

    def upload_checkin(self, upload_id: str, session_id: str, learner_id: str, *, source: str, payload: dict) -> CheckInResult:
        """签到上传：按上传标识幂等；内容一致只记一次，内容冲突交教务核对。"""
        if upload_id in self._upload_index:
            return CheckInResult(record_id=self._upload_index[upload_id], duplicate=True)
        session = self._session(session_id)
        if self._is_paused(learner_id, session):
            raise ValueError("该场次因安全限制已暂停，不能签到")
        enrollment = self._enrollment_at(learner_id, session.offering_id, session.interval.start)
        if enrollment is None:
            raise ValueError("学员在该场次时间未报名此课程")
        assignment = enrollment.modes.at(session.interval.start)
        if assignment is None or assignment.value.mode != session.mode:
            raise ValueError("学员在该场次时间的授课形态与本场次不符")
        key = (session_id, learner_id)
        existing_id = self._record_by_session_learner.get(key)
        if existing_id is None:
            record_id = f"rec-{upload_id}"
            self._emit(
                "CHECKIN_UPLOADED", "learning_record", record_id,
                {
                    "upload_id": upload_id,
                    "session_id": session_id,
                    "learner_id": learner_id,
                    "enrollment_id": enrollment.enrollment_id,
                    "source": source,
                    "payload": payload,
                    "record_id": record_id,
                    "duplicate_of": None,
                },
                f"签到成功（{source}）",
            )
            return CheckInResult(record_id=record_id, created=True)
        existing = self.records[existing_id]
        if self._same_content(existing, payload):
            self._emit(
                "CHECKIN_UPLOADED", "learning_record", existing_id,
                {
                    "upload_id": upload_id,
                    "session_id": session_id,
                    "learner_id": learner_id,
                    "enrollment_id": enrollment.enrollment_id,
                    "source": source,
                    "payload": payload,
                    "record_id": existing_id,
                    "duplicate_of": existing_id,
                },
                "重复签到，已忽略",
            )
            return CheckInResult(record_id=existing_id, duplicate=True)
        case_id = f"conf-{upload_id}"
        self._emit(
            "CHECKIN_CONFLICT_RAISED", "learning_record", existing_id,
            {
                "case_id": case_id,
                "session_id": session_id,
                "learner_id": learner_id,
                "record_id": existing_id,
                "upload_id": upload_id,
                "source": source,
                "payload": payload,
            },
            "签到内容冲突，转教务核对",
        )
        return CheckInResult(record_id=existing_id, conflict_case_id=case_id)

    @staticmethod
    def _same_content(record: LearningRecord, payload: dict) -> bool:
        normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return any(json.dumps(p, sort_keys=True, ensure_ascii=False) == normalized for p in record.payloads)

    def resolve_conflict(self, case_id: str, *, chosen_status: str, attribution: str | None = None, note: str = "", resolved_by: str = "教务员") -> None:
        case = self._conflict(case_id)
        if case.status != "pending":
            raise ValueError("该冲突已核对完毕")
        record = self._record(case.record_id)
        if record.completed:
            raise ValueError("学习成果已完成，签到记录不可再改写")
        if chosen_status == "absent" and attribution is None:
            attribution = AbsenceAttribution.UNEXCUSED.value
        self._emit(
            "CHECKIN_CONFLICT_RESOLVED", "learning_record", case.record_id,
            {
                "case_id": case_id,
                "record_id": case.record_id,
                "chosen_status": chosen_status,
                "attribution": attribution,
                "note": note,
                "resolved_by": resolved_by,
                "resolved_at": self.clock.now().isoformat(),
            },
            "教务核对签到冲突",
        )

    def finalize_absences(self, session_id: str) -> list[str]:
        """为应到而未签到的学员登记缺席并按原因归因；有因缺席不算主动退学。"""
        session = self._session(session_id)
        created = []
        for enrollment in self._expected_enrollments(session):
            learner_id = enrollment.learner_id
            if (session_id, learner_id) in self._record_by_session_learner:
                continue
            attribution = self._attribute_absence(learner_id, session)
            record_id = f"abs-{session_id}-{learner_id}"
            self._emit(
                "ABSENCE_ATTRIBUTED", "learning_record", record_id,
                {
                    "record_id": record_id,
                    "session_id": session_id,
                    "learner_id": learner_id,
                    "enrollment_id": enrollment.enrollment_id,
                    "attribution": attribution.value,
                },
                f"缺席归因：{ATTRIBUTION_LABELS[attribution.value]}",
            )
            created.append(record_id)
        return created

    def _attribute_absence(self, learner_id: str, session: CourseSession) -> AbsenceAttribution:
        if self._is_paused(learner_id, session):
            return AbsenceAttribution.SAFETY_PAUSE
        if session.venue_changed and learner_id in session.affected_learners:
            return AbsenceAttribution.VENUE_CHANGE
        needed = self._needs_at(learner_id, session.interval.start)
        if not needed <= self._session_supports(session):
            return AbsenceAttribution.SUPPORT_UNAVAILABLE
        return AbsenceAttribution.UNEXCUSED

    def record_outcome(self, record_id: str, *, outcome: dict, goal_ids=(), reviewed_by: str = "教务员") -> None:
        """登记学习成果；成果一旦完成不可被后续改班或更正覆盖。"""
        record = self._record(record_id)
        if record.completed:
            raise ValueError("学习成果已完成，不能被覆盖")
        if record.under_review:
            raise ValueError("记录存在待核对的签到冲突，暂不能登记成果")
        if record.status != "attended":
            raise ValueError("只有已到课的学习记录才能登记成果")
        self._emit(
            "OUTCOME_REVIEWED", "learning_record", record_id,
            {
                "record_id": record_id,
                "learner_id": record.learner_id,
                "outcome": outcome,
                "goal_ids": sorted(goal_ids),
                "reviewed_by": reviewed_by,
                "reviewed_at": self.clock.now().isoformat(),
            },
            "登记学习成果",
        )

    def absence_summary(self, learner_id: str, *, dropout_threshold: int = 3) -> AbsenceSummary:
        """缺席统计：只有主动缺席计入退学风险，有因缺席不算主动退学。"""
        records = sorted(
            (r for r in self.records.values() if r.learner_id == learner_id),
            key=lambda r: (r.session_start, r.record_id),
        )
        excused: dict[str, int] = {}
        unexcused = 0
        streak = 0
        total = 0
        for record in records:
            if record.status == "attended":
                streak = 0
                continue
            if record.status != "absent":
                continue
            total += 1
            attribution = record.attribution or AbsenceAttribution.UNEXCUSED.value
            if attribution == AbsenceAttribution.UNEXCUSED.value:
                unexcused += 1
                streak += 1
            else:
                excused[attribution] = excused.get(attribution, 0) + 1
        return AbsenceSummary(
            total_absences=total,
            unexcused=unexcused,
            excused=excused,
            unexcused_streak=streak,
            dropout_risk=streak >= dropout_threshold,
        )

    # ------------------------------------------------------------------
    # 候补：按原申请时间与可满足条件推进
    # ------------------------------------------------------------------

    def join_waitlist(self, entry_id: str, learner_id: str, offering_id: str, *, required_supports=None, applied_at: datetime | None = None) -> None:
        applied_at = applied_at or self.clock.now()
        self._offering(offering_id)
        for entry in self.waitlist_entries.values():
            if entry.learner_id == learner_id and entry.offering_id == offering_id and entry.status in ("waiting", "offered"):
                raise ValueError("该学员已在候补队列中")
        supports = frozenset(required_supports) if required_supports is not None else self._needs_at(learner_id, applied_at)
        self._emit(
            "WAITLIST_JOINED", "waitlist_entry", entry_id,
            {
                "entry_id": entry_id,
                "learner_id": learner_id,
                "offering_id": offering_id,
                "applied_at": applied_at.isoformat(),
                "required_supports": sorted(supports),
            },
            "加入候补队列",
        )

    def advance_waitlist(self, offering_id: str) -> list[str]:
        """有空位时按原申请时间推进，只出价给支持条件可满足的申请。"""
        now = self.clock.now()
        made = []
        while self._seats_available(offering_id, now) >= 1:
            entry = self._next_waitlist_entry(offering_id, now)
            if entry is None:
                break
            offer_id = f"offer-{entry.entry_id}-{len(self.offers) + 1}"
            expires_at = now + self.offer_ttl
            self._emit(
                "WAITLIST_OFFERED", "waitlist_entry", entry.entry_id,
                {
                    "offer_id": offer_id,
                    "entry_id": entry.entry_id,
                    "learner_id": entry.learner_id,
                    "offering_id": offering_id,
                    "offered_at": now.isoformat(),
                    "expires_at": expires_at.isoformat(),
                },
                "候补获得名额，待学员确认",
            )
            made.append(offer_id)
        return made

    def _next_waitlist_entry(self, offering_id: str, moment: datetime) -> WaitlistEntry | None:
        version = self._version_at(offering_id, moment)
        resources = version.support_resources if version else frozenset()
        candidates = [
            e for e in self.waitlist_entries.values()
            if e.offering_id == offering_id
            and e.status == "waiting"
            and self._enrollment_at(e.learner_id, offering_id, moment) is None
        ]
        candidates.sort(key=lambda e: (e.applied_at, e.entry_id))
        for entry in candidates:
            if entry.required_supports <= resources:
                return entry
        return None

    def confirm_waitlist_offer(self, offer_id: str, *, enrollment_id: str, mode: str) -> None:
        offer = self._offer(offer_id)
        if offer.status != "pending":
            raise ValueError(f"候补名额当前状态不可确认：{offer.status}")
        now = self.clock.now()
        if now >= offer.expires_at:
            self._expire_offer(offer, now)
            raise ValueError("确认超时，候补名额已释放")
        entry = self.waitlist_entries[offer.entry_id]
        self.place_enrollment(
            enrollment_id, offer.learner_id, offer.offering_id,
            mode=mode, reason="候补转正", decision_ref=offer_id, ignore_offer_id=offer_id,
        )
        self._emit(
            "WAITLIST_PROMOTED", "waitlist_entry", entry.entry_id,
            {"offer_id": offer_id, "entry_id": entry.entry_id, "enrollment_id": enrollment_id},
            "候补确认，正式报名",
        )

    def expire_waitlist_offers(self) -> list[str]:
        """释放超时未确认的候补名额；时间来自注入的时钟，释放后继续推进。"""
        now = self.clock.now()
        expired = []
        touched = set()
        for offer in list(self.offers.values()):
            if offer.status == "pending" and now >= offer.expires_at:
                self._expire_offer(offer, now)
                expired.append(offer.offer_id)
                touched.add(offer.offering_id)
        for offering_id in touched:
            self.advance_waitlist(offering_id)
        return expired

    def _expire_offer(self, offer: WaitlistOffer, at: datetime) -> None:
        self._emit(
            "WAITLIST_OFFER_EXPIRED", "waitlist_entry", offer.entry_id,
            {"offer_id": offer.offer_id, "entry_id": offer.entry_id, "expired_at": at.isoformat()},
            "候补确认超时，名额释放",
        )

    def waitlist_status(self, offering_id: str) -> list[dict]:
        """候补队列现状：申请时间、状态与暂不可满足的原因。"""
        now = self.clock.now()
        version = self._version_at(offering_id, now)
        resources = version.support_resources if version else frozenset()
        rows = []
        entries = [e for e in self.waitlist_entries.values() if e.offering_id == offering_id]
        entries.sort(key=lambda e: (e.applied_at, e.entry_id))
        for entry in entries:
            missing = entry.required_supports - resources
            rows.append({
                "entry_id": entry.entry_id,
                "learner_id": entry.learner_id,
                "status": entry.status,
                "applied_at": entry.applied_at.isoformat(),
                "satisfiable": not missing,
                "note": "" if not missing else "暂不能满足：" + "、".join(NEED_LABELS.get(m, m) for m in sorted(missing)),
            })
        return rows

    # ------------------------------------------------------------------
    # 阶段回顾与待办
    # ------------------------------------------------------------------

    def due_stage_reviews(self) -> list[str]:
        """自上次阶段回顾以来到课达到 stage_size 的报名。"""
        due = []
        for enrollment in self.enrollments.values():
            since = enrollment.stage_reviewed_at
            count = 0
            for record in self.records.values():
                if record.enrollment_id != enrollment.enrollment_id or record.status != "attended":
                    continue
                if since is not None and record.session_start <= since:
                    continue
                count += 1
            if count >= self.stage_size:
                due.append(enrollment.enrollment_id)
        return sorted(due)

    def record_stage_review(self, review_id: str, enrollment_id: str, *, summary: str, reviewed_by: str = "教务员") -> None:
        enrollment = self._enrollment(enrollment_id)
        self._emit(
            "STAGE_REVIEW_RECORDED", "enrollment", enrollment_id,
            {
                "review_id": review_id,
                "enrollment_id": enrollment_id,
                "learner_id": enrollment.learner_id,
                "summary": summary,
                "reviewed_by": reviewed_by,
                "reviewed_at": self.clock.now().isoformat(),
            },
            "完成阶段回顾",
        )

    def pending_followups(self) -> Followups:
        """重启或例行检查时继续：先释放超时名额，再推进候补，最后汇总待办。"""
        self.release_expired_holds()
        self.expire_waitlist_offers()
        for offering_id in self.offerings:
            self.advance_waitlist(offering_id)
        return Followups(
            transfer_reminders=sorted(p.proposal_id for p in self.proposals.values() if p.status == "pending"),
            waitlist_reminders=sorted(o.offer_id for o in self.offers.values() if o.status == "pending"),
            stage_reviews_due=self.due_stage_reviews(),
        )

    # ------------------------------------------------------------------
    # 教务解释
    # ------------------------------------------------------------------

    def explain_period(self, learner_id: str, start: datetime, end: datetime) -> PeriodExplanation:
        """解释一段时间内：为何安排某种形态、哪些支持已落实、缺席如何归因、目标怎样接续。"""
        profile = self._profile(learner_id)
        segments = []
        for enrollment in self._enrollments_of(learner_id):
            for eff in enrollment.modes.between(start, end):
                checks = None
                ref = eff.value.decision_ref
                if ref and ref in self.proposals:
                    checks = dict(self.proposals[ref].checks)
                segments.append(ModeSegment(
                    offering_id=eff.value.offering_id,
                    mode=eff.value.mode,
                    start=max(eff.interval.start, start),
                    end=min(eff.interval.end, end) if eff.interval.end else None,
                    reason=eff.value.reason,
                    decision_ref=ref,
                    checks=checks,
                ))
        segments.sort(key=lambda s: s.start)
        supports = []
        for session in self._expected_sessions(learner_id, start, end):
            needed = self._needs_at(learner_id, session.interval.start)
            provided = self._session_supports(session)
            supports.append(SupportFulfillment(
                session_id=session.session_id,
                start=session.interval.start,
                needed=sorted(needed),
                provided=sorted(provided),
                missing=sorted(needed - provided),
            ))
        absences = []
        for record in self.records.values():
            if record.learner_id != learner_id or record.status != "absent":
                continue
            if not (start <= record.session_start < end):
                continue
            attribution = record.attribution or AbsenceAttribution.UNEXCUSED.value
            excused = AbsenceAttribution(attribution).excused
            absences.append(AbsenceItem(
                session_id=record.session_id,
                start=record.session_start,
                attribution=attribution,
                excused=excused,
                counts_toward_dropout=not excused,
            ))
        absences.sort(key=lambda a: a.start)
        goals = []
        for goal in profile.goals.values():
            path = [goal.origin_offering_id] + [c.to_offering_id for c in goal.continuations]
            outcomes = []
            for record in self.records.values():
                if record.learner_id != learner_id or not record.completed:
                    continue
                if goal.goal_id not in record.goal_ids:
                    continue
                if not (start <= record.session_start < end):
                    continue
                outcomes.append(_outcome_text(record.outcome))
            goals.append(GoalLine(goal_id=goal.goal_id, title=goal.title, path=path, outcomes=sorted(outcomes)))
        return PeriodExplanation(
            learner_id=learner_id,
            start=start,
            end=end,
            mode_segments=segments,
            supports=supports,
            absences=absences,
            goals=goals,
        )

    # ------------------------------------------------------------------
    # 事件落库（重放时不重复校验）
    # ------------------------------------------------------------------

    def _on_course_published(self, event: dict) -> None:
        p = event["payload"]
        offering = CourseOffering(offering_id=p["offering_id"], title=p["title"], subject=p.get("subject", ""))
        self.offerings[offering.offering_id] = offering
        self._apply_course_version(offering, p["version"])

    def _on_course_versioned(self, event: dict) -> None:
        p = event["payload"]
        self._apply_course_version(self.offerings[p["offering_id"]], p["version"])

    @staticmethod
    def _apply_course_version(offering: CourseOffering, v: dict) -> None:
        offering.versions.append(
            CourseVersion(
                version_no=v["version_no"],
                teacher_id=v["teacher_id"],
                teacher_qualifications=frozenset(v["teacher_qualifications"]),
                required_qualifications=frozenset(v["required_qualifications"]),
                capacity=v["capacity"],
                support_resources=frozenset(v["support_resources"]),
                modes=frozenset(v["modes"]),
            ),
            parse_ts(v["effective_from"]),
            reason=f"课程版本 v{v['version_no']}",
        )

    def _on_learner_registered(self, event: dict) -> None:
        p = event["payload"]
        profile = ContinuousProfile(learner_id=p["learner_id"], name=p.get("name", ""))
        profile.support_needs.append(frozenset(p["support_needs"]), parse_ts(p["effective_from"]), reason=p.get("reason", ""))
        self.profiles[profile.learner_id] = profile

    def _on_support_updated(self, event: dict) -> None:
        p = event["payload"]
        self.profiles[p["learner_id"]].support_needs.append(
            frozenset(p["support_needs"]), parse_ts(p["effective_from"]), reason=p.get("reason", "")
        )

    def _on_safety_applied(self, event: dict) -> None:
        p = event["payload"]
        profile = self.profiles[p["learner_id"]]
        profile.safety_restrictions[p["restriction_id"]] = SafetyRestriction(
            restriction_id=p["restriction_id"],
            modes=frozenset(p["modes"]),
            interval=Interval(parse_ts(p["start"]), parse_ts(p["end"]) if p["end"] else None),
            note=p.get("note", ""),
        )

    def _on_safety_lifted(self, event: dict) -> None:
        p = event["payload"]
        restriction = self.profiles[p["learner_id"]].safety_restrictions[p["restriction_id"]]
        restriction.interval = Interval(restriction.interval.start, parse_ts(p["lifted_at"]))

    def _on_enrollment_placed(self, event: dict) -> None:
        p = event["payload"]
        enrollment = Enrollment(enrollment_id=p["enrollment_id"], learner_id=p["learner_id"], offering_id=p["offering_id"])
        effective_from = parse_ts(p["effective_from"])
        enrollment.modes.append(
            ModeAssignment(mode=p["mode"], offering_id=p["offering_id"], reason=p["reason"], decision_ref=p.get("decision_ref")),
            effective_from,
        )
        enrollment.eligibility.append(ELIGIBILITY_ACTIVE, effective_from, reason="报名生效")
        self.enrollments[enrollment.enrollment_id] = enrollment

    def _on_eligibility_changed(self, event: dict) -> None:
        p = event["payload"]
        self.enrollments[p["enrollment_id"]].eligibility.append(p["status"], parse_ts(p["effective_from"]), reason=p.get("reason", ""))

    def _on_transfer_proposed(self, event: dict) -> None:
        p = event["payload"]
        self.proposals[p["proposal_id"]] = TransferProposal(
            proposal_id=p["proposal_id"],
            enrollment_id=p["enrollment_id"],
            learner_id=p["learner_id"],
            target_offering_id=p["target_offering_id"],
            target_mode=p["target_mode"],
            checks=dict(p["checks"]),
            check_details=dict(p["check_details"]),
            status=p["status"],
            reason=p.get("reason", ""),
            proposed_at=parse_ts(p["proposed_at"]),
            expires_at=parse_ts(p["expires_at"]) if p["expires_at"] else None,
            hold_id=p["hold_id"],
        )
        if p["hold_id"]:
            self.holds[p["hold_id"]] = SeatHold(
                hold_id=p["hold_id"],
                offering_id=p["target_offering_id"],
                learner_id=p["learner_id"],
                proposal_id=p["proposal_id"],
                expires_at=parse_ts(p["expires_at"]),
            )

    def _on_transfer_confirmed(self, event: dict) -> None:
        p = event["payload"]
        proposal = self.proposals[p["proposal_id"]]
        proposal.status = "confirmed"
        proposal.confirmed_at = parse_ts(event["occurred_at"])
        if p["hold_id"] and p["hold_id"] in self.holds:
            self.holds[p["hold_id"]].status = "consumed"
        enrollment = self.enrollments[p["enrollment_id"]]
        enrollment.offering_id = p["to_offering_id"]
        enrollment.modes.append(
            ModeAssignment(mode=p["mode"], offering_id=p["to_offering_id"], reason=p["reason"], decision_ref=p["proposal_id"]),
            parse_ts(p["switched_at"]),
        )

    def _on_transfer_declined(self, event: dict) -> None:
        p = event["payload"]
        proposal = self.proposals[p["proposal_id"]]
        proposal.status = "declined"
        if p["hold_id"] and p["hold_id"] in self.holds:
            self.holds[p["hold_id"]].status = "released"

    def _on_transfer_expired(self, event: dict) -> None:
        p = event["payload"]
        proposal = self.proposals[p["proposal_id"]]
        proposal.status = "expired"
        if p["hold_id"] and p["hold_id"] in self.holds:
            self.holds[p["hold_id"]].status = "released"

    def _on_goal_set(self, event: dict) -> None:
        p = event["payload"]
        self.profiles[p["learner_id"]].goals[p["goal_id"]] = LearningGoal(
            goal_id=p["goal_id"], title=p["title"], origin_offering_id=p["offering_id"]
        )

    def _on_goal_continued(self, event: dict) -> None:
        p = event["payload"]
        self.profiles[p["learner_id"]].goals[p["goal_id"]].continuations.append(
            GoalContinuation(
                from_offering_id=p["from_offering_id"],
                to_offering_id=p["to_offering_id"],
                at=parse_ts(p["at"]),
                note=p.get("note", ""),
            )
        )

    def _on_session_recorded(self, event: dict) -> None:
        p = event["payload"]
        self.sessions[p["session_id"]] = CourseSession(
            session_id=p["session_id"],
            offering_id=p["offering_id"],
            mode=p["mode"],
            interval=Interval(parse_ts(p["start"]), parse_ts(p["end"]) if p["end"] else None),
            venue_changed=p["venue_changed"],
            affected_learners=frozenset(p["affected_learners"]),
            supports_available=frozenset(p["supports_available"]) if p["supports_available"] is not None else None,
        )

    def _on_checkin_uploaded(self, event: dict) -> None:
        p = event["payload"]
        self._upload_index[p["upload_id"]] = p["record_id"]
        if p["duplicate_of"]:
            return
        session = self.sessions[p["session_id"]]
        record = LearningRecord(
            record_id=p["record_id"],
            learner_id=p["learner_id"],
            enrollment_id=p["enrollment_id"],
            session_id=p["session_id"],
            offering_id=session.offering_id,
            mode=session.mode,
            session_start=session.interval.start,
            session_end=session.interval.end,
            status=p["payload"].get("status", "attended"),
            source=p["source"],
            payloads=[p["payload"]],
            recorded_at=parse_ts(event["occurred_at"]),
        )
        self.records[record.record_id] = record
        self._record_by_session_learner[(record.session_id, record.learner_id)] = record.record_id

    def _on_conflict_raised(self, event: dict) -> None:
        p = event["payload"]
        self._upload_index[p["upload_id"]] = p["record_id"]
        record = self.records[p["record_id"]]
        record.payloads.append(p["payload"])
        record.under_review = True
        self.conflicts[p["case_id"]] = ConflictCase(
            case_id=p["case_id"], session_id=p["session_id"], learner_id=p["learner_id"], record_id=p["record_id"]
        )

    def _on_conflict_resolved(self, event: dict) -> None:
        p = event["payload"]
        case = self.conflicts[p["case_id"]]
        case.status = "resolved"
        case.resolution = p["chosen_status"]
        case.note = p.get("note", "")
        case.resolved_by = p["resolved_by"]
        case.resolved_at = parse_ts(p["resolved_at"])
        record = self.records[p["record_id"]]
        record.status = p["chosen_status"]
        record.attribution = p.get("attribution")
        record.under_review = False

    def _on_absence_attributed(self, event: dict) -> None:
        p = event["payload"]
        session = self.sessions[p["session_id"]]
        record = LearningRecord(
            record_id=p["record_id"],
            learner_id=p["learner_id"],
            enrollment_id=p["enrollment_id"],
            session_id=p["session_id"],
            offering_id=session.offering_id,
            mode=session.mode,
            session_start=session.interval.start,
            session_end=session.interval.end,
            status="absent",
            source="教务归因",
            attribution=p["attribution"],
            recorded_at=parse_ts(event["occurred_at"]),
        )
        self.records[record.record_id] = record
        self._record_by_session_learner[(record.session_id, record.learner_id)] = record.record_id

    def _on_outcome_reviewed(self, event: dict) -> None:
        p = event["payload"]
        record = self.records[p["record_id"]]
        record.outcome = p["outcome"]
        record.goal_ids = list(p["goal_ids"])

    def _on_waitlist_joined(self, event: dict) -> None:
        p = event["payload"]
        self.waitlist_entries[p["entry_id"]] = WaitlistEntry(
            entry_id=p["entry_id"],
            learner_id=p["learner_id"],
            offering_id=p["offering_id"],
            applied_at=parse_ts(p["applied_at"]),
            required_supports=frozenset(p["required_supports"]),
        )

    def _on_waitlist_offered(self, event: dict) -> None:
        p = event["payload"]
        self.offers[p["offer_id"]] = WaitlistOffer(
            offer_id=p["offer_id"],
            entry_id=p["entry_id"],
            offering_id=p["offering_id"],
            learner_id=p["learner_id"],
            offered_at=parse_ts(p["offered_at"]),
            expires_at=parse_ts(p["expires_at"]),
        )
        entry = self.waitlist_entries[p["entry_id"]]
        entry.status = "offered"
        entry.current_offer_id = p["offer_id"]

    def _on_waitlist_promoted(self, event: dict) -> None:
        p = event["payload"]
        self.offers[p["offer_id"]].status = "confirmed"
        entry = self.waitlist_entries[p["entry_id"]]
        entry.status = "promoted"
        entry.current_offer_id = None

    def _on_waitlist_offer_expired(self, event: dict) -> None:
        p = event["payload"]
        self.offers[p["offer_id"]].status = "expired"
        entry = self.waitlist_entries[p["entry_id"]]
        entry.status = "waiting"
        entry.current_offer_id = None

    def _on_stage_review(self, event: dict) -> None:
        p = event["payload"]
        self.enrollments[p["enrollment_id"]].stage_reviewed_at = parse_ts(p["reviewed_at"])
