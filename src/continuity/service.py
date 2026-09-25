"""连续学习档案服务：命令、查询与时钟驱动的恢复。

- 所有状态变化先追加事件再应用，重启后回放事件即可重建状态；
- recover() 在重启后继续到期的提醒、候补推进与阶段回顾；
- 候补名额超时、迁移方案超时均由注入时钟判定。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from .clock import Clock
from .events import AGGREGATE_TYPES, EVENT_TYPES, Event, iso, parse_moment
from .model import (
    FORMAT_LABELS,
    NEED_LABELS,
    AbsenceAttribution,
    Checkin,
    CheckinChannel,
    CheckinConflict,
    CourseOffering,
    CourseVersion,
    DeliveryFormat,
    DomainError,
    Eligibility,
    EligibilityStatus,
    Enrollment,
    Goal,
    GoalStatus,
    LearnerProfile,
    LearningRecord,
    NotFoundError,
    Placement,
    ProposalStatus,
    SafetyWindow,
    Session,
    StateError,
    SupportNeed,
    SupportNeedSet,
    TransferProposal,
    WaitlistEntry,
    WaitlistStatus,
    close_open,
)
from .store import JsonlEventStore

# 缺席归因中，只有主动缺席计入退学评估。
COUNTS_TOWARD_WITHDRAWAL = {AbsenceAttribution.VOLUNTARY}


def _labels(labels: dict, values: Iterable) -> list[str]:
    return [labels[value] for value in values]


class ContinuityService:
    def __init__(
        self,
        clock: Clock,
        *,
        store: JsonlEventStore | None = None,
        reminder_lead: timedelta = timedelta(hours=24),
        review_interval: timedelta = timedelta(days=30),
        offer_ttl: timedelta = timedelta(hours=48),
        proposal_ttl: timedelta = timedelta(hours=72),
    ) -> None:
        self.clock = clock
        self.store = store
        self.reminder_lead = reminder_lead
        self.review_interval = review_interval
        self.offer_ttl = offer_ttl
        self.proposal_ttl = proposal_ttl
        self.learners: dict[str, LearnerProfile] = {}
        self.offerings: dict[str, CourseOffering] = {}
        self.enrollments: dict[str, Enrollment] = {}
        self.records: dict[str, LearningRecord] = {}
        self.proposals: dict[str, TransferProposal] = {}
        self.waitlist: dict[str, WaitlistEntry] = {}
        self.checkins: dict[str, Checkin] = {}
        self.conflicts: dict[str, tuple[str, CheckinConflict]] = {}
        self.events: list[Event] = []
        self._versions: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------
    # 打开与恢复
    # ------------------------------------------------------------------
    @classmethod
    def open(cls, path: str | Path, clock: Clock, **kwargs) -> "ContinuityService":
        """从事件存储回放重建服务，并继续到期任务（提醒/候补/阶段回顾）。"""
        store = JsonlEventStore(path)
        service = cls(clock, store=store, **kwargs)
        for event in store.load():
            service._apply(event)
            service.events.append(event)
        service.recover()
        return service

    def recover(self) -> None:
        """重启恢复：先补发各班级候补，再处理超时、提醒与阶段回顾。幂等。"""
        at = self.clock.now()
        for offering_id in list(self.offerings):
            self._offer_waitlist(offering_id, at)
        self.sweep(at)

    def sweep(self, at: datetime | None = None) -> None:
        """时钟驱动的周期任务：方案超时、候补释放与推进、提醒、阶段回顾。"""
        at = at or self.clock.now()
        for proposal in list(self.proposals.values()):
            if proposal.status == ProposalStatus.PENDING and proposal.expires_at <= at:
                self._emit(
                    "TRANSFER_EXPIRED", "transfer_proposal", proposal.proposal_id,
                    "迁移方案超时未确认", {}, at,
                )
        affected: set[str] = set()
        for entry in list(self.waitlist.values()):
            if entry.status == WaitlistStatus.OFFERED and entry.offer_expires_at <= at:
                self._emit(
                    "WAITLIST_EXPIRED", "waitlist_entry", entry.entry_id,
                    "候补名额超时未确认，释放给下一位", {}, at,
                )
                affected.add(entry.offering_id)
        for offering_id in affected:
            self._offer_waitlist(offering_id, at)
        self._send_due_reminders(at)
        self._generate_due_reviews(at)

    # ------------------------------------------------------------------
    # 事件写入与应用
    # ------------------------------------------------------------------
    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        summary: str,
        payload: dict,
        at: datetime | None = None,
    ) -> Event:
        if event_type not in EVENT_TYPES:
            raise DomainError(f"未知事件类型：{event_type}")
        if aggregate_type not in AGGREGATE_TYPES:
            raise DomainError(f"未知聚合类型：{aggregate_type}")
        at = at or self.clock.now()
        key = (aggregate_type, aggregate_id)
        version = self._versions.get(key, 0) + 1
        event = Event(
            event_id=f"{aggregate_type}:{aggregate_id}:v{version}",
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=at,
            version=version,
            summary=summary,
            payload=payload,
        )
        self._versions[key] = version
        if self.store is not None:
            self.store.append(event)
        self.events.append(event)
        self._apply(event)
        return event

    def _apply(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            handler(event)
        key = (event.aggregate_type, event.aggregate_id)
        self._versions[key] = max(self._versions.get(key, 0), event.version)

    # ------------------------------------------------------------------
    # 学员与课程
    # ------------------------------------------------------------------
    def register_learner(self, learner_id: str, name: str, needs: Iterable[SupportNeed] = ()) -> None:
        if learner_id in self.learners:
            raise StateError(f"学员 {learner_id} 已登记")
        need_set = {SupportNeed(n) for n in needs}
        self._emit(
            "LEARNER_REGISTERED", "learner_profile", learner_id,
            f"登记学员 {name}",
            {
                "name": name,
                "needs": sorted(n.value for n in need_set),
                "effective_from": iso(self.clock.now()),
                "reason": "初始登记",
            },
        )

    def publish_course_version(
        self,
        offering_id: str,
        *,
        title: str,
        teacher_id: str,
        teacher_formats: Iterable[DeliveryFormat],
        teacher_supports: Iterable[SupportNeed] = (),
        capacity: dict[DeliveryFormat, int],
        resources: dict[DeliveryFormat, Iterable[SupportNeed]] | None = None,
        sessions: Iterable[dict] = (),
        effective_from: datetime | None = None,
    ) -> None:
        """发布课程版本；旧版本在 effective_from 关闭，未开始的旧场次随之失效。"""
        at = effective_from or self.clock.now()
        offering = self.offerings.get(offering_id)
        if offering and offering.versions and at <= offering.versions[-1].valid_from:
            raise StateError("课程版本的生效时间必须晚于当前版本")
        capacity_map = {DeliveryFormat(k): int(v) for k, v in capacity.items()}
        if any(v < 0 for v in capacity_map.values()):
            raise StateError("名额不能为负数")
        resource_map = {
            DeliveryFormat(k): {SupportNeed(n) for n in v}
            for k, v in (resources or {}).items()
        }
        session_list: list[Session] = []
        seen: set[str] = set()
        for raw in sessions:
            session = Session(
                session_id=raw["session_id"],
                starts_at=raw["starts_at"],
                format=DeliveryFormat(raw["format"]),
                location=raw.get("location", ""),
            )
            if session.session_id in seen:
                raise StateError(f"场次编号重复：{session.session_id}")
            if session.format not in capacity_map:
                raise StateError(f"场次 {session.session_id} 的形态不在课程名额配置中")
            seen.add(session.session_id)
            session_list.append(session)
        self._emit(
            "COURSE_VERSION_PUBLISHED", "course_offering", offering_id,
            f"发布课程版本《{title}》（{iso(at)} 起生效）",
            {
                "title": title,
                "teacher_id": teacher_id,
                "teacher_formats": sorted(f.value for f in teacher_formats),
                "teacher_supports": sorted(n.value for n in teacher_supports),
                "capacity": {k.value: v for k, v in capacity_map.items()},
                "resources": {
                    k.value: sorted(n.value for n in v) for k, v in resource_map.items()
                },
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "starts_at": iso(s.starts_at),
                        "format": s.format.value,
                        "location": s.location,
                    }
                    for s in session_list
                ],
                "effective_from": iso(at),
            },
        )
        self._offer_waitlist(offering_id, at)

    # ------------------------------------------------------------------
    # 报名与资格
    # ------------------------------------------------------------------
    def place_enrollment(
        self,
        enrollment_id: str,
        learner_id: str,
        offering_id: str,
        format: DeliveryFormat,
        *,
        goals: Iterable[dict] = (),
        effective_from: datetime | None = None,
        reason: str = "初始报名",
    ) -> None:
        at = effective_from or self.clock.now()
        fmt = DeliveryFormat(format)
        if enrollment_id in self.enrollments:
            raise StateError(f"报名 {enrollment_id} 已存在")
        learner = self._learner(learner_id)
        checks = self._placement_checks(learner, offering_id, fmt, at)
        if not checks["ok"]:
            raise StateError("；".join(checks["reasons"]))
        self._emit(
            "ENROLLMENT_PLACED", "enrollment", enrollment_id,
            f"安排 {learner.name} 进入{FORMAT_LABELS[fmt]}",
            {
                "learner_id": learner_id,
                "offering_id": offering_id,
                "format": fmt.value,
                "effective_from": iso(at),
                "reason": reason,
                "proposal_id": None,
                "goals": [dict(g) for g in goals],
            },
        )

    def set_eligibility(
        self,
        enrollment_id: str,
        status: EligibilityStatus,
        *,
        reason: str = "",
        effective_from: datetime | None = None,
    ) -> None:
        at = effective_from or self.clock.now()
        enrollment = self._enrollment(enrollment_id)
        status = EligibilityStatus(status)
        if enrollment.eligibility and at < enrollment.eligibility[-1].valid_from:
            raise StateError("资格变更的生效时间不能早于当前版本")
        placement = enrollment.placement_at(at)
        self._emit(
            "ELIGIBILITY_UPDATED", "enrollment", enrollment_id,
            f"报名资格调整为 {status.value}",
            {"status": status.value, "effective_from": iso(at), "reason": reason},
        )
        if status != EligibilityStatus.ELIGIBLE and placement is not None:
            self._offer_waitlist(placement.offering_id, at)

    # ------------------------------------------------------------------
    # 支持需求与安全限制
    # ------------------------------------------------------------------
    def update_support_needs(
        self,
        learner_id: str,
        needs: Iterable[SupportNeed],
        *,
        effective_from: datetime | None = None,
        reason: str = "",
    ) -> None:
        """调整支持需求；按场次开始时间解析，只影响生效点之后的场次。"""
        at = effective_from or self.clock.now()
        learner = self._learner(learner_id)
        if learner.support_needs and at < learner.support_needs[-1].valid_from:
            raise StateError("支持需求的生效时间不能早于当前版本")
        need_set = {SupportNeed(n) for n in needs}
        self._emit(
            "SUPPORT_UPDATED", "learner_profile", learner_id,
            "更新支持需求：" + ("、".join(_labels(NEED_LABELS, need_set)) or "无"),
            {"needs": sorted(n.value for n in need_set), "effective_from": iso(at), "reason": reason},
        )

    def set_safety_restriction(
        self,
        learner_id: str,
        restricted_formats: Iterable[DeliveryFormat],
        *,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        reason: str = "",
    ) -> None:
        """安全限制：窗口内暂停指定形态的活动，其他形态照常可参加。"""
        start = valid_from or self.clock.now()
        if valid_to is not None and valid_to <= start:
            raise StateError("安全限制的结束时间必须晚于开始时间")
        self._learner(learner_id)
        formats = {DeliveryFormat(f) for f in restricted_formats}
        if not formats:
            raise StateError("安全限制至少指定一种授课形态")
        self._emit(
            "SAFETY_RESTRICTION_SET", "learner_profile", learner_id,
            "安全限制：" + "、".join(_labels(FORMAT_LABELS, formats)),
            {
                "restricted_formats": sorted(f.value for f in formats),
                "valid_from": iso(start),
                "valid_to": iso(valid_to) if valid_to else None,
                "reason": reason,
            },
        )

    # ------------------------------------------------------------------
    # 换班 / 换形态：先核验，学员确认后才迁移
    # ------------------------------------------------------------------
    def propose_transfer(
        self,
        enrollment_id: str,
        target_offering_id: str,
        target_format: DeliveryFormat,
    ) -> TransferProposal:
        """生成迁移建议方案；前置核验教师资历、名额与支持资源。"""
        at = self.clock.now()
        enrollment = self._enrollment(enrollment_id)
        learner = self._learner(enrollment.learner_id)
        fmt = DeliveryFormat(target_format)
        placement = enrollment.placement_at(at)
        if placement is None:
            raise StateError("当前没有生效的授课安排，无法发起迁移")
        checks = self._placement_checks(learner, target_offering_id, fmt, at)
        reasons = list(checks["reasons"])
        if not enrollment.is_active_at(at):
            reasons.append("报名资格当前不可用")
        if placement.offering_id == target_offering_id and placement.format == fmt:
            reasons.append("目标与当前授课安排相同")
        checks["ok"] = not reasons
        checks["reasons"] = reasons
        seq = sum(1 for p in self.proposals.values() if p.enrollment_id == enrollment_id) + 1
        proposal_id = f"TP-{enrollment_id}-{seq}"
        payload = {
            "enrollment_id": enrollment_id,
            "target_offering_id": target_offering_id,
            "target_format": fmt.value,
            "checks": checks,
            "expires_at": iso(at + self.proposal_ttl),
        }
        if checks["ok"]:
            self._emit(
                "TRANSFER_PROPOSED", "transfer_proposal", proposal_id,
                f"建议迁移至{FORMAT_LABELS[fmt]}，待学员确认", payload,
            )
        else:
            self._emit(
                "TRANSFER_REJECTED", "transfer_proposal", proposal_id,
                "迁移方案未通过前置核验：" + "；".join(reasons),
                {**payload, "reasons": reasons},
            )
        return self.proposals[proposal_id]

    def confirm_transfer(self, proposal_id: str) -> TransferProposal:
        """学员确认方案后才迁移；确认时复核名额等条件。"""
        at = self.clock.now()
        proposal = self._proposal(proposal_id)
        if proposal.status == ProposalStatus.PENDING and proposal.expires_at <= at:
            self._emit("TRANSFER_EXPIRED", "transfer_proposal", proposal_id, "迁移方案超时未确认", {})
            return self.proposals[proposal_id]
        if proposal.status != ProposalStatus.PENDING:
            raise StateError(f"迁移方案当前状态为 {proposal.status.value}，无法确认")
        enrollment = self._enrollment(proposal.enrollment_id)
        learner = self._learner(enrollment.learner_id)
        checks = self._placement_checks(learner, proposal.target_offering_id, proposal.target_format, at)
        if not enrollment.is_active_at(at):
            checks["ok"] = False
            checks["reasons"].append("报名资格当前不可用")
        if not checks["ok"]:
            self._emit(
                "TRANSFER_REJECTED", "transfer_proposal", proposal_id,
                "确认时复核未通过：" + "；".join(checks["reasons"]),
                {"reasons": checks["reasons"]},
            )
            return self.proposals[proposal_id]
        old_placement = enrollment.placement_at(at)
        self._emit(
            "TRANSFER_CONFIRMED", "transfer_proposal", proposal_id,
            "学员确认迁移方案", {},
        )
        self._emit(
            "ENROLLMENT_PLACED", "enrollment", enrollment.enrollment_id,
            f"迁移至{FORMAT_LABELS[proposal.target_format]}",
            {
                "learner_id": learner.learner_id,
                "offering_id": proposal.target_offering_id,
                "format": proposal.target_format.value,
                "effective_from": iso(at),
                "reason": f"经学员确认迁移（方案 {proposal_id}）",
                "proposal_id": proposal_id,
                "goals": [],
            },
        )
        if old_placement is not None:
            self._offer_waitlist(old_placement.offering_id, at)
        return self.proposals[proposal_id]

    def cancel_transfer(self, proposal_id: str) -> None:
        proposal = self._proposal(proposal_id)
        if proposal.status != ProposalStatus.PENDING:
            raise StateError(f"迁移方案当前状态为 {proposal.status.value}，无法撤销")
        self._emit("TRANSFER_CANCELLED", "transfer_proposal", proposal_id, "撤销迁移方案", {})

    # ------------------------------------------------------------------
    # 学习记录与缺席归因
    # ------------------------------------------------------------------
    def record_session(
        self,
        enrollment_id: str,
        session_id: str,
        attended: bool,
        *,
        outcomes: Iterable[str] = (),
        attribution: AbsenceAttribution | None = None,
        note: str = "",
    ) -> str:
        """登记一次学习记录；缺席未指明归因时记为待归因，绝不默认主动缺席。"""
        enrollment = self._enrollment(enrollment_id)
        learner = self._learner(enrollment.learner_id)
        offering, session, _ = self._session_for(enrollment, session_id)
        record_id = f"LR-{enrollment_id}-{session_id}"
        if record_id in self.records:
            raise StateError("该场次已有学习记录，如需修改请使用更正")
        outcome_ids = tuple(outcomes)
        for goal_id in outcome_ids:
            goal = enrollment.goals.get(goal_id)
            if goal is None:
                raise NotFoundError(f"学习目标 {goal_id} 不存在")
            if goal.status != GoalStatus.OPEN:
                raise StateError(f"学习目标 {goal_id} 已关闭，不能重复完成")
        if attended:
            attribution = None
        elif attribution is None:
            paused = learner.paused_formats_at(session.starts_at)
            attribution = (
                AbsenceAttribution.SAFETY_PAUSE
                if session.format in paused
                else AbsenceAttribution.UNATTRIBUTED
            )
        else:
            attribution = AbsenceAttribution(attribution)
        self._emit(
            "SESSION_RECORDED", "learning_record", record_id,
            f"记录场次 {session_id}：" + ("出席" if attended else "缺席"),
            {
                "enrollment_id": enrollment_id,
                "session_id": session_id,
                "offering_id": offering.offering_id,
                "session_starts_at": iso(session.starts_at),
                "attended": attended,
                "attribution": attribution.value if attribution else None,
                "outcomes": list(outcome_ids),
                "note": note,
            },
        )
        return record_id

    def correct_record(
        self,
        record_id: str,
        *,
        attended: bool | None = None,
        attribution: AbsenceAttribution | None = None,
        outcomes: Iterable[str] | None = None,
        note: str | None = None,
        reason: str = "",
    ) -> str:
        """更正产生后继记录，原记录保留生效区间；已完成成果不可移除。"""
        old = self._record(record_id)
        if old.valid_to is not None:
            raise StateError("该记录已被后继更正取代，请对最新记录操作")
        new_attended = old.attended if attended is None else attended
        new_outcomes = old.outcomes if outcomes is None else tuple(outcomes)
        missing = set(old.outcomes) - set(new_outcomes)
        if missing:
            raise StateError("已完成的学习成果不可移除：" + "、".join(sorted(missing)))
        if new_attended:
            new_attribution = None
        elif attribution is not None:
            new_attribution = AbsenceAttribution(attribution)
        else:
            new_attribution = old.attribution or AbsenceAttribution.UNATTRIBUTED
        seq = 1
        while f"{old.record_id}~c{seq}" in self.records:
            seq += 1
        new_id = f"{old.record_id}~c{seq}"
        self._emit(
            "RECORD_CORRECTED", "learning_record", new_id,
            f"更正学习记录 {old.record_id}" + (f"：{reason}" if reason else ""),
            {
                "supersedes": old.record_id,
                "enrollment_id": old.enrollment_id,
                "session_id": old.session_id,
                "offering_id": old.offering_id,
                "session_starts_at": iso(old.session_starts_at),
                "attended": new_attended,
                "attribution": new_attribution.value if new_attribution else None,
                "outcomes": list(new_outcomes),
                "note": old.note if note is None else note,
                "reason": reason,
            },
        )
        return new_id

    def attribute_absence(
        self, record_id: str, attribution: AbsenceAttribution, *, reason: str = ""
    ) -> str:
        """教务归因缺席；场地调整等原因不计入退学评估。"""
        record = self._record(record_id)
        if record.attended:
            raise StateError("出席记录无需归因")
        return self.correct_record(record_id, attribution=attribution, reason=reason)

    def withdrawal_risk(self, enrollment_id: str) -> dict:
        """退学评估：仅统计主动缺席，场地调整/安全暂停等不计入。"""
        self._enrollment(enrollment_id)
        by_attribution: dict[str, int] = {}
        for record in self.records.values():
            if record.enrollment_id != enrollment_id or record.valid_to is not None:
                continue
            if record.attended:
                continue
            key = record.attribution.value if record.attribution else "UNATTRIBUTED"
            by_attribution[key] = by_attribution.get(key, 0) + 1
        voluntary = by_attribution.get(AbsenceAttribution.VOLUNTARY.value, 0)
        return {
            "counts_toward_withdrawal": voluntary,
            "by_attribution": by_attribution,
            "note": "仅主动缺席计入退学评估",
        }

    # ------------------------------------------------------------------
    # 签到上传：幂等去重，冲突交教务核对
    # ------------------------------------------------------------------
    def upload_checkin(
        self,
        session_id: str,
        learner_id: str,
        channel: CheckinChannel,
        payload: dict,
    ) -> str:
        """返回 recorded / duplicate / conflict。同一场次同一学员只记一次。"""
        self._learner(learner_id)
        self._find_session(session_id)
        channel = CheckinChannel(channel)
        key = f"{session_id}:{learner_id}"
        existing = self.checkins.get(key)
        if existing is None:
            channel_label = "电话签到" if channel == CheckinChannel.PHONE else "线下签到"
            self._emit(
                "CHECKIN_RECORDED", "checkin", key,
                f"记录{channel_label}",
                {
                    "session_id": session_id,
                    "learner_id": learner_id,
                    "channel": channel.value,
                    "payload": dict(payload),
                },
            )
            return "recorded"
        if existing.payload == payload:
            # 同一内容无论渠道重复上传，只记一次
            return "duplicate"
        seq = len(existing.conflicts) + 1
        conflict_id = f"CF-{session_id}-{learner_id}-{seq}"
        self._emit(
            "CHECKIN_CONFLICT_RAISED", "checkin", key,
            "签到内容冲突，交教务核对",
            {
                "conflict_id": conflict_id,
                "incoming_channel": channel.value,
                "incoming_payload": dict(payload),
            },
        )
        return "conflict"

    def resolve_checkin_conflict(
        self,
        conflict_id: str,
        decision: str,
        *,
        corrected_payload: dict | None = None,
        resolved_by: str = "",
    ) -> None:
        """教务核对冲突：original 保留原记录 / incoming 采用新上传 / corrected 采用订正内容。"""
        found = self.conflicts.get(conflict_id)
        if found is None:
            raise NotFoundError(f"签到冲突 {conflict_id} 不存在")
        key, conflict = found
        if conflict.status != "PENDING":
            raise StateError("该冲突已核对完毕")
        if decision not in ("original", "incoming", "corrected"):
            raise StateError("核对结论只能是 original / incoming / corrected")
        if decision == "corrected" and corrected_payload is None:
            raise StateError("采用订正内容时必须提供 corrected_payload")
        self._emit(
            "CHECKIN_CONFLICT_RESOLVED", "checkin", key,
            f"教务核对签到冲突：{decision}",
            {
                "conflict_id": conflict_id,
                "resolution": decision,
                "resolved_by": resolved_by,
                "corrected_payload": dict(corrected_payload) if corrected_payload else None,
            },
        )

    def pending_conflicts(self) -> list[CheckinConflict]:
        return [conflict for _, conflict in self.conflicts.values() if conflict.status == "PENDING"]

    # ------------------------------------------------------------------
    # 候补：按原申请时间与可满足条件推进，超时由时钟释放
    # ------------------------------------------------------------------
    def apply_waitlist(
        self,
        learner_id: str,
        offering_id: str,
        desired_format: DeliveryFormat,
        *,
        required_supports: Iterable[SupportNeed] | None = None,
    ) -> WaitlistEntry:
        at = self.clock.now()
        learner = self._learner(learner_id)
        offering = self._offering(offering_id)
        fmt = DeliveryFormat(desired_format)
        version = offering.version_at(at)
        if version is None or fmt not in version.capacity:
            raise StateError("目标班级当前不提供该授课形态")
        supports = (
            set(learner.needs_at(at))
            if required_supports is None
            else {SupportNeed(n) for n in required_supports}
        )
        seq = sum(1 for e in self.waitlist.values() if e.learner_id == learner_id) + 1
        entry_id = f"WL-{learner_id}-{seq}"
        self._emit(
            "WAITLIST_APPLIED", "waitlist_entry", entry_id,
            f"申请候补{FORMAT_LABELS[fmt]}",
            {
                "learner_id": learner_id,
                "offering_id": offering_id,
                "desired_format": fmt.value,
                "required_supports": sorted(n.value for n in supports),
                "applied_at": iso(at),
            },
        )
        self._offer_waitlist(offering_id, at)
        return self.waitlist[entry_id]

    def confirm_waitlist_offer(self, entry_id: str) -> str:
        """确认候补名额，返回新报名号；超时名额已被时钟释放。"""
        at = self.clock.now()
        entry = self._entry(entry_id)
        if entry.status == WaitlistStatus.OFFERED and entry.offer_expires_at <= at:
            self._emit("WAITLIST_EXPIRED", "waitlist_entry", entry_id, "候补名额超时未确认，释放", {})
            self._offer_waitlist(entry.offering_id, at)
            raise StateError("候补名额已超时释放")
        if entry.status != WaitlistStatus.OFFERED:
            raise StateError(f"候补当前状态为 {entry.status.value}，无法确认")
        seq = sum(1 for e in self.enrollments.values() if e.learner_id == entry.learner_id) + 1
        enrollment_id = f"EN-{entry.learner_id}-{seq}"
        self._emit(
            "WAITLIST_ENROLLED", "waitlist_entry", entry_id,
            "候补确认录取", {"enrollment_id": enrollment_id},
        )
        self._emit(
            "ENROLLMENT_PLACED", "enrollment", enrollment_id,
            "候补确认录取，安排入班",
            {
                "learner_id": entry.learner_id,
                "offering_id": entry.offering_id,
                "format": entry.desired_format.value,
                "effective_from": iso(at),
                "reason": f"候补确认录取（原申请 {iso(entry.applied_at)}）",
                "proposal_id": None,
                "goals": [],
            },
        )
        return enrollment_id

    def cancel_waitlist(self, entry_id: str) -> None:
        entry = self._entry(entry_id)
        if entry.status not in (WaitlistStatus.WAITING, WaitlistStatus.OFFERED):
            raise StateError(f"候补当前状态为 {entry.status.value}，无法取消")
        was_offered = entry.status == WaitlistStatus.OFFERED
        self._emit("WAITLIST_CANCELLED", "waitlist_entry", entry_id, "取消候补", {})
        if was_offered:
            self._offer_waitlist(entry.offering_id, self.clock.now())

    # ------------------------------------------------------------------
    # 学习目标跨班接续
    # ------------------------------------------------------------------
    def carry_goals(
        self,
        source_enrollment_id: str,
        target_enrollment_id: str,
        goal_ids: Iterable[str] | None = None,
    ) -> list[str]:
        """把未完成的目标接续到另一个报名；已完成成果留在原处，不被覆盖。"""
        source = self._enrollment(source_enrollment_id)
        target = self._enrollment(target_enrollment_id)
        if source is target:
            raise StateError("不能接续到同一个报名")
        if goal_ids is None:
            ids = [g.goal_id for g in source.goals.values() if g.status == GoalStatus.OPEN]
        else:
            ids = list(goal_ids)
        carried: list[str] = []
        for goal_id in ids:
            goal = source.goals.get(goal_id)
            if goal is None:
                raise NotFoundError(f"学习目标 {goal_id} 不存在")
            if goal.status != GoalStatus.OPEN:
                raise StateError(f"学习目标 {goal_id} 已关闭，无需接续")
            seq = sum(1 for g in target.goals.values() if g.carried_from is not None) + 1
            new_goal_id = f"G-{target_enrollment_id}-{seq}"
            self._emit(
                "GOAL_CARRIED", "enrollment", target_enrollment_id,
                f"目标「{goal.title}」自 {source_enrollment_id} 接续",
                {
                    "source_enrollment_id": source_enrollment_id,
                    "source_goal_id": goal_id,
                    "goal_id": new_goal_id,
                    "title": goal.title,
                },
            )
            carried.append(new_goal_id)
        return carried

    # ------------------------------------------------------------------
    # 内部：候补推进、提醒、阶段回顾
    # ------------------------------------------------------------------
    def _offer_waitlist(self, offering_id: str, at: datetime) -> None:
        offering = self.offerings.get(offering_id)
        if offering is None:
            return
        version = offering.version_at(at)
        if version is None:
            return
        for fmt, capacity in version.capacity.items():
            free = capacity - self._active_placement_count(offering_id, fmt, at)
            free -= sum(
                1
                for e in self.waitlist.values()
                if e.offering_id == offering_id
                and e.desired_format == fmt
                and e.status == WaitlistStatus.OFFERED
            )
            if free <= 0:
                continue
            candidates = sorted(
                (
                    e
                    for e in self.waitlist.values()
                    if e.offering_id == offering_id
                    and e.desired_format == fmt
                    and e.status == WaitlistStatus.WAITING
                ),
                key=lambda e: (e.applied_at, e.entry_id),
            )
            for entry in candidates:
                if free <= 0:
                    break
                if not self._waitlist_conditions_met(entry, version):
                    continue
                self._emit(
                    "WAITLIST_OFFERED", "waitlist_entry", entry.entry_id,
                    "候补名额已腾出，发出录取通知",
                    {"offer_expires_at": iso(at + self.offer_ttl)},
                )
                free -= 1

    def _waitlist_conditions_met(self, entry: WaitlistEntry, version: CourseVersion) -> bool:
        if entry.desired_format not in version.teacher_formats:
            return False
        available = version.resources.get(entry.desired_format, set())
        return entry.required_supports <= available

    def _send_due_reminders(self, at: datetime) -> None:
        for enrollment in list(self.enrollments.values()):
            if not enrollment.is_active_at(at):
                continue
            placement = enrollment.placement_at(at)
            if placement is None:
                continue
            offering = self.offerings.get(placement.offering_id)
            if offering is None:
                continue
            learner = self.learners[enrollment.learner_id]
            for session, _version in offering.live_sessions():
                if session.format != placement.format:
                    continue
                if session.starts_at <= at or session.starts_at - self.reminder_lead > at:
                    continue
                if session.format in learner.paused_formats_at(session.starts_at):
                    continue
                key = f"{enrollment.enrollment_id}:{session.session_id}"
                if key in enrollment.reminders_sent:
                    continue
                self._emit(
                    "REMINDER_SENT", "enrollment", enrollment.enrollment_id,
                    f"课程提醒：{session.session_id}",
                    {
                        "reminder_key": key,
                        "session_id": session.session_id,
                        "starts_at": iso(session.starts_at),
                        "format": session.format.value,
                    },
                )

    def _generate_due_reviews(self, at: datetime) -> None:
        for enrollment in list(self.enrollments.values()):
            anchor = enrollment.review_anchor
            if anchor is None:
                continue
            while anchor + (enrollment.reviews_generated + 1) * self.review_interval <= at:
                start = anchor + enrollment.reviews_generated * self.review_interval
                end = start + self.review_interval
                summary = self.period_summary(enrollment.enrollment_id, start, end)
                self._emit(
                    "OUTCOME_REVIEWED", "enrollment", enrollment.enrollment_id,
                    f"阶段回顾（第 {enrollment.reviews_generated + 1} 期）",
                    {"period_start": iso(start), "period_end": iso(end), **summary},
                )

    def explain(self, enrollment_id: str, start=None, end=None) -> dict:
        """教务解释视图：授课形态安排原因、支持落实、缺席归因与目标接续。"""
        from .explain import explain_enrollment

        return explain_enrollment(self, enrollment_id, start, end)

    def period_summary(self, enrollment_id: str, start: datetime, end: datetime) -> dict:
        self._enrollment(enrollment_id)
        records = [
            r
            for r in self.records.values()
            if r.enrollment_id == enrollment_id
            and r.valid_to is None
            and r.session_starts_at is not None
            and start <= r.session_starts_at < end
        ]
        absences: dict[str, int] = {}
        for record in records:
            if record.attended:
                continue
            key = record.attribution.value if record.attribution else "UNATTRIBUTED"
            absences[key] = absences.get(key, 0) + 1
        return {
            "sessions": len(records),
            "attended": sum(1 for r in records if r.attended),
            "absences_by_attribution": absences,
            "outcomes_completed": sorted({g for r in records for g in r.outcomes}),
        }

    # ------------------------------------------------------------------
    # 内部：核验与查找
    # ------------------------------------------------------------------
    def _placement_checks(
        self, learner: LearnerProfile, offering_id: str, fmt: DeliveryFormat, at: datetime
    ) -> dict:
        reasons: list[str] = []
        unmet: set[SupportNeed] = set()
        offering = self.offerings.get(offering_id)
        if offering is None:
            reasons.append(f"班级 {offering_id} 不存在")
            version = None
        else:
            version = offering.version_at(at)
            if version is None:
                reasons.append("目标班级在该时间没有生效的课程版本")
        teacher_ok = capacity_ok = supports_ok = False
        if version is not None and fmt in version.capacity:
            teacher_ok = fmt in version.teacher_formats
            if not teacher_ok:
                reasons.append(f"教师未具备{FORMAT_LABELS[fmt]}的授课资历")
            occupied = self._active_placement_count(offering_id, fmt, at) + sum(
                1
                for e in self.waitlist.values()
                if e.offering_id == offering_id
                and e.desired_format == fmt
                and e.status == WaitlistStatus.OFFERED
            )
            capacity_ok = occupied < version.capacity[fmt]
            if not capacity_ok:
                reasons.append("目标名额已满")
            unmet = learner.needs_at(at) - version.resources.get(fmt, set())
            supports_ok = not unmet
            if unmet:
                reasons.append("支持资源不足：" + "、".join(_labels(NEED_LABELS, unmet)))
        elif version is not None:
            reasons.append(f"目标班级不提供{FORMAT_LABELS[fmt]}")
        return {
            "ok": not reasons,
            "reasons": reasons,
            "teacher_ok": teacher_ok,
            "capacity_ok": capacity_ok,
            "supports_ok": supports_ok,
            "unmet_needs": sorted(n.value for n in unmet),
        }

    def _active_placement_count(self, offering_id: str, fmt: DeliveryFormat, at: datetime) -> int:
        count = 0
        for enrollment in self.enrollments.values():
            if not enrollment.is_active_at(at):
                continue
            placement = enrollment.placement_at(at)
            if placement and placement.offering_id == offering_id and placement.format == fmt:
                count += 1
        return count

    def _session_for(self, enrollment: Enrollment, session_id: str):
        offering, session, version = self._find_session(session_id)
        placement = enrollment.placement_at(session.starts_at)
        if placement is None or placement.offering_id != offering.offering_id:
            raise StateError("该场次时间学员没有对应班级的生效授课安排")
        if placement.format != session.format:
            raise StateError("该场次的授课形态与学员当时的安排不一致")
        return offering, session, version

    def _find_session(self, session_id: str):
        for offering in self.offerings.values():
            for session, version in offering.live_sessions():
                if session.session_id == session_id:
                    return offering, session, version
        raise NotFoundError(f"场次 {session_id} 不存在或已失效")

    def _learner(self, learner_id: str) -> LearnerProfile:
        learner = self.learners.get(learner_id)
        if learner is None:
            raise NotFoundError(f"学员 {learner_id} 不存在")
        return learner

    def _offering(self, offering_id: str) -> CourseOffering:
        offering = self.offerings.get(offering_id)
        if offering is None:
            raise NotFoundError(f"班级 {offering_id} 不存在")
        return offering

    def _enrollment(self, enrollment_id: str) -> Enrollment:
        enrollment = self.enrollments.get(enrollment_id)
        if enrollment is None:
            raise NotFoundError(f"报名 {enrollment_id} 不存在")
        return enrollment

    def _record(self, record_id: str) -> LearningRecord:
        record = self.records.get(record_id)
        if record is None:
            raise NotFoundError(f"学习记录 {record_id} 不存在")
        return record

    def _proposal(self, proposal_id: str) -> TransferProposal:
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise NotFoundError(f"迁移方案 {proposal_id} 不存在")
        return proposal

    def _entry(self, entry_id: str) -> WaitlistEntry:
        entry = self.waitlist.get(entry_id)
        if entry is None:
            raise NotFoundError(f"候补 {entry_id} 不存在")
        return entry

    # ------------------------------------------------------------------
    # 事件应用
    # ------------------------------------------------------------------
    def _on_LEARNER_REGISTERED(self, event: Event) -> None:
        p = event.payload
        learner = LearnerProfile(learner_id=event.aggregate_id, name=p["name"])
        learner.support_needs.append(
            SupportNeedSet(
                valid_from=parse_moment(p["effective_from"]),
                needs={SupportNeed(n) for n in p["needs"]},
                reason=p.get("reason", ""),
            )
        )
        self.learners[learner.learner_id] = learner

    def _on_COURSE_VERSION_PUBLISHED(self, event: Event) -> None:
        p = event.payload
        offering = self.offerings.setdefault(event.aggregate_id, CourseOffering(event.aggregate_id))
        effective_from = parse_moment(p["effective_from"])
        close_open(offering.versions, effective_from)
        offering.versions.append(
            CourseVersion(
                valid_from=effective_from,
                title=p["title"],
                teacher_id=p["teacher_id"],
                teacher_formats={DeliveryFormat(f) for f in p["teacher_formats"]},
                teacher_supports={SupportNeed(n) for n in p["teacher_supports"]},
                capacity={DeliveryFormat(k): v for k, v in p["capacity"].items()},
                resources={
                    DeliveryFormat(k): {SupportNeed(n) for n in v}
                    for k, v in p["resources"].items()
                },
                sessions=[
                    Session(
                        session_id=s["session_id"],
                        starts_at=parse_moment(s["starts_at"]),
                        format=DeliveryFormat(s["format"]),
                        location=s.get("location", ""),
                    )
                    for s in p["sessions"]
                ],
            )
        )

    def _on_ENROLLMENT_PLACED(self, event: Event) -> None:
        p = event.payload
        effective_from = parse_moment(p["effective_from"])
        enrollment = self.enrollments.get(event.aggregate_id)
        if enrollment is None:
            enrollment = Enrollment(
                enrollment_id=event.aggregate_id,
                learner_id=p["learner_id"],
                review_anchor=effective_from,
            )
            enrollment.eligibility.append(
                Eligibility(
                    valid_from=effective_from,
                    status=EligibilityStatus.ELIGIBLE,
                    reason="初始报名",
                )
            )
            for g in p.get("goals", []):
                enrollment.goals[g["goal_id"]] = Goal(g["goal_id"], g["title"])
            self.enrollments[enrollment.enrollment_id] = enrollment
        close_open(enrollment.placements, effective_from)
        enrollment.placements.append(
            Placement(
                valid_from=effective_from,
                offering_id=p["offering_id"],
                format=DeliveryFormat(p["format"]),
                reason=p["reason"],
                proposal_id=p.get("proposal_id"),
            )
        )

    def _on_ELIGIBILITY_UPDATED(self, event: Event) -> None:
        p = event.payload
        enrollment = self.enrollments[event.aggregate_id]
        effective_from = parse_moment(p["effective_from"])
        close_open(enrollment.eligibility, effective_from)
        enrollment.eligibility.append(
            Eligibility(
                valid_from=effective_from,
                status=EligibilityStatus(p["status"]),
                reason=p.get("reason", ""),
            )
        )

    def _on_SUPPORT_UPDATED(self, event: Event) -> None:
        p = event.payload
        learner = self.learners[event.aggregate_id]
        effective_from = parse_moment(p["effective_from"])
        close_open(learner.support_needs, effective_from)
        learner.support_needs.append(
            SupportNeedSet(
                valid_from=effective_from,
                needs={SupportNeed(n) for n in p["needs"]},
                reason=p.get("reason", ""),
            )
        )

    def _on_SAFETY_RESTRICTION_SET(self, event: Event) -> None:
        p = event.payload
        learner = self.learners[event.aggregate_id]
        learner.safety_windows.append(
            SafetyWindow(
                valid_from=parse_moment(p["valid_from"]),
                valid_to=parse_moment(p["valid_to"]) if p.get("valid_to") else None,
                restricted_formats={DeliveryFormat(f) for f in p["restricted_formats"]},
                reason=p.get("reason", ""),
            )
        )

    def _on_TRANSFER_PROPOSED(self, event: Event) -> None:
        p = event.payload
        self.proposals[event.aggregate_id] = TransferProposal(
            proposal_id=event.aggregate_id,
            enrollment_id=p["enrollment_id"],
            target_offering_id=p["target_offering_id"],
            target_format=DeliveryFormat(p["target_format"]),
            status=ProposalStatus.PENDING,
            checks=p["checks"],
            created_at=event.occurred_at,
            expires_at=parse_moment(p["expires_at"]),
        )

    def _on_TRANSFER_REJECTED(self, event: Event) -> None:
        p = event.payload
        proposal = self.proposals.get(event.aggregate_id)
        if proposal is None:
            proposal = TransferProposal(
                proposal_id=event.aggregate_id,
                enrollment_id=p["enrollment_id"],
                target_offering_id=p["target_offering_id"],
                target_format=DeliveryFormat(p["target_format"]),
                status=ProposalStatus.REJECTED,
                checks=p.get("checks", {}),
                created_at=event.occurred_at,
                expires_at=parse_moment(p["expires_at"]) if p.get("expires_at") else event.occurred_at,
            )
            self.proposals[proposal.proposal_id] = proposal
        proposal.status = ProposalStatus.REJECTED
        proposal.reasons = list(p.get("reasons", []))
        proposal.decided_at = event.occurred_at

    def _on_TRANSFER_CONFIRMED(self, event: Event) -> None:
        proposal = self.proposals[event.aggregate_id]
        proposal.status = ProposalStatus.CONFIRMED
        proposal.decided_at = event.occurred_at

    def _on_TRANSFER_EXPIRED(self, event: Event) -> None:
        proposal = self.proposals[event.aggregate_id]
        proposal.status = ProposalStatus.EXPIRED
        proposal.decided_at = event.occurred_at

    def _on_TRANSFER_CANCELLED(self, event: Event) -> None:
        proposal = self.proposals[event.aggregate_id]
        proposal.status = ProposalStatus.CANCELLED
        proposal.decided_at = event.occurred_at

    def _on_SESSION_RECORDED(self, event: Event) -> None:
        p = event.payload
        self.records[event.aggregate_id] = LearningRecord(
            record_id=event.aggregate_id,
            enrollment_id=p["enrollment_id"],
            session_id=p["session_id"],
            attended=p["attended"],
            attribution=AbsenceAttribution(p["attribution"]) if p["attribution"] else None,
            outcomes=tuple(p["outcomes"]),
            note=p.get("note", ""),
            recorded_at=event.occurred_at,
            session_starts_at=parse_moment(p["session_starts_at"]),
            offering_id=p["offering_id"],
        )
        self._complete_goals(p["enrollment_id"], p["outcomes"], event.occurred_at, p["offering_id"])

    def _on_RECORD_CORRECTED(self, event: Event) -> None:
        p = event.payload
        old = self.records[p["supersedes"]]
        old.valid_to = event.occurred_at
        self.records[event.aggregate_id] = LearningRecord(
            record_id=event.aggregate_id,
            enrollment_id=p["enrollment_id"],
            session_id=p["session_id"],
            attended=p["attended"],
            attribution=AbsenceAttribution(p["attribution"]) if p["attribution"] else None,
            outcomes=tuple(p["outcomes"]),
            note=p.get("note", ""),
            recorded_at=event.occurred_at,
            session_starts_at=parse_moment(p["session_starts_at"]),
            offering_id=p["offering_id"],
            supersedes=old.record_id,
        )
        self._complete_goals(p["enrollment_id"], p["outcomes"], event.occurred_at, p["offering_id"])

    def _complete_goals(
        self, enrollment_id: str, goal_ids: Iterable[str], at: datetime, offering_id: str
    ) -> None:
        enrollment = self.enrollments[enrollment_id]
        for goal_id in goal_ids:
            goal = enrollment.goals[goal_id]
            if goal.status == GoalStatus.OPEN:
                goal.status = GoalStatus.COMPLETED
                goal.completed_at = at
                goal.completed_in_offering = offering_id

    def _on_GOAL_CARRIED(self, event: Event) -> None:
        p = event.payload
        target = self.enrollments[event.aggregate_id]
        source = self.enrollments[p["source_enrollment_id"]]
        source.goals[p["source_goal_id"]].status = GoalStatus.CARRIED
        target.goals[p["goal_id"]] = Goal(
            goal_id=p["goal_id"],
            title=p["title"],
            carried_from=p["source_goal_id"],
            source_enrollment_id=p["source_enrollment_id"],
        )

    def _on_CHECKIN_RECORDED(self, event: Event) -> None:
        p = event.payload
        self.checkins[event.aggregate_id] = Checkin(
            session_id=p["session_id"],
            learner_id=p["learner_id"],
            channel=CheckinChannel(p["channel"]),
            payload=dict(p["payload"]),
            uploaded_at=event.occurred_at,
        )

    def _on_CHECKIN_CONFLICT_RAISED(self, event: Event) -> None:
        p = event.payload
        checkin = self.checkins[event.aggregate_id]
        conflict = CheckinConflict(
            conflict_id=p["conflict_id"],
            incoming_channel=CheckinChannel(p["incoming_channel"]),
            incoming_payload=dict(p["incoming_payload"]),
            raised_at=event.occurred_at,
        )
        checkin.conflicts.append(conflict)
        self.conflicts[conflict.conflict_id] = (event.aggregate_id, conflict)

    def _on_CHECKIN_CONFLICT_RESOLVED(self, event: Event) -> None:
        p = event.payload
        key, conflict = self.conflicts[p["conflict_id"]]
        conflict.status = "RESOLVED"
        conflict.resolution = p["resolution"]
        conflict.resolved_by = p.get("resolved_by", "")
        conflict.resolved_at = event.occurred_at
        checkin = self.checkins[key]
        if p["resolution"] == "incoming":
            checkin.channel = conflict.incoming_channel
            checkin.payload = dict(conflict.incoming_payload)
        elif p["resolution"] == "corrected":
            checkin.payload = dict(p["corrected_payload"])

    def _on_WAITLIST_APPLIED(self, event: Event) -> None:
        p = event.payload
        self.waitlist[event.aggregate_id] = WaitlistEntry(
            entry_id=event.aggregate_id,
            learner_id=p["learner_id"],
            offering_id=p["offering_id"],
            desired_format=DeliveryFormat(p["desired_format"]),
            required_supports={SupportNeed(n) for n in p["required_supports"]},
            applied_at=parse_moment(p["applied_at"]),
        )

    def _on_WAITLIST_OFFERED(self, event: Event) -> None:
        entry = self.waitlist[event.aggregate_id]
        entry.status = WaitlistStatus.OFFERED
        entry.offer_expires_at = parse_moment(event.payload["offer_expires_at"])

    def _on_WAITLIST_ENROLLED(self, event: Event) -> None:
        self.waitlist[event.aggregate_id].status = WaitlistStatus.ENROLLED

    def _on_WAITLIST_EXPIRED(self, event: Event) -> None:
        entry = self.waitlist[event.aggregate_id]
        entry.status = WaitlistStatus.EXPIRED
        entry.offer_expires_at = None

    def _on_WAITLIST_CANCELLED(self, event: Event) -> None:
        entry = self.waitlist[event.aggregate_id]
        entry.status = WaitlistStatus.CANCELLED
        entry.offer_expires_at = None

    def _on_REMINDER_SENT(self, event: Event) -> None:
        self.enrollments[event.aggregate_id].reminders_sent.add(event.payload["reminder_key"])

    def _on_OUTCOME_REVIEWED(self, event: Event) -> None:
        enrollment = self.enrollments[event.aggregate_id]
        enrollment.reviews_generated += 1
        enrollment.reviews.append(dict(event.payload))
