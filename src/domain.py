"""连续学习档案的领域类型与状态对象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .intervals import Interval, Timeline


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


class DeliveryMode(str, Enum):
    OFFLINE = "offline"  # 线下班
    PHONE = "phone"  # 电话辅导
    ONLINE = "online"  # 线上课堂


MODE_LABELS = {
    DeliveryMode.OFFLINE.value: "线下班",
    DeliveryMode.PHONE.value: "电话辅导",
    DeliveryMode.ONLINE.value: "线上课堂",
}


class SupportNeed(str, Enum):
    HEARING_ASSIST = "hearing_assist"  # 助听
    SLOW_PACED = "slow_paced"  # 慢速讲解
    CAREGIVER_ESCORT = "caregiver_escort"  # 照护陪同
    LARGE_PRINT = "large_print"  # 大字讲义


NEED_LABELS = {
    SupportNeed.HEARING_ASSIST.value: "助听",
    SupportNeed.SLOW_PACED.value: "慢速讲解",
    SupportNeed.CAREGIVER_ESCORT.value: "照护陪同",
    SupportNeed.LARGE_PRINT.value: "大字讲义",
}


class AbsenceAttribution(str, Enum):
    """缺席归因：只有主动缺席计入退学统计，其余均为有因缺席。"""

    UNEXCUSED = "unexcused"  # 主动缺席
    VENUE_CHANGE = "venue_change"  # 场地/时间调整
    SAFETY_PAUSE = "safety_pause"  # 安全限制暂停
    SUPPORT_UNAVAILABLE = "support_unavailable"  # 所需支持未落实
    MEDICAL = "medical"  # 医嘱请假

    @property
    def excused(self) -> bool:
        return self is not AbsenceAttribution.UNEXCUSED


ATTRIBUTION_LABELS = {
    AbsenceAttribution.UNEXCUSED.value: "主动缺席",
    AbsenceAttribution.VENUE_CHANGE.value: "场地调整",
    AbsenceAttribution.SAFETY_PAUSE.value: "安全限制暂停",
    AbsenceAttribution.SUPPORT_UNAVAILABLE.value: "所需支持未落实",
    AbsenceAttribution.MEDICAL.value: "医嘱请假",
}

ELIGIBILITY_ACTIVE = "active"
ELIGIBILITY_SUSPENDED = "suspended"
ELIGIBILITY_ENDED = "ended"

CHECK_LABELS = {
    "eligibility_active": "报名资格",
    "mode_supported": "授课形态支持",
    "teacher_qualified": "教师资历",
    "seat_available": "名额",
    "supports_satisfiable": "支持资源",
}


@dataclass(frozen=True)
class CourseVersion:
    """课程在某个生效区间内的版本：教师、资历、名额、支持资源与可授形态。"""

    version_no: int
    teacher_id: str
    teacher_qualifications: frozenset[str]
    required_qualifications: frozenset[str]
    capacity: int
    support_resources: frozenset[str]
    modes: frozenset[str]


@dataclass
class CourseOffering:
    offering_id: str
    title: str
    subject: str = ""
    versions: Timeline[CourseVersion] = field(default_factory=Timeline)


@dataclass
class SeatHold:
    """迁移建议待确认期间的名额预留，超时由时钟释放。"""

    hold_id: str
    offering_id: str
    learner_id: str
    proposal_id: str
    expires_at: datetime
    status: str = "active"  # active / consumed / released


@dataclass(frozen=True)
class ModeAssignment:
    """某段生效区间内学员被安排的授课形态；decision_ref 指向迁移提案。"""

    mode: str
    offering_id: str
    reason: str
    decision_ref: str | None = None


@dataclass
class Enrollment:
    enrollment_id: str
    learner_id: str
    offering_id: str
    modes: Timeline[ModeAssignment] = field(default_factory=Timeline)
    eligibility: Timeline[str] = field(default_factory=Timeline)
    stage_reviewed_at: datetime | None = None


@dataclass
class SafetyRestriction:
    """安全限制：在生效区间内暂停指定授课形态，其他形态不受影响。"""

    restriction_id: str
    modes: frozenset[str]
    interval: Interval
    note: str = ""


@dataclass
class GoalContinuation:
    from_offering_id: str
    to_offering_id: str
    at: datetime
    note: str = ""


@dataclass
class LearningGoal:
    goal_id: str
    title: str
    origin_offering_id: str
    continuations: list[GoalContinuation] = field(default_factory=list)


@dataclass
class ContinuousProfile:
    """学员连续档案：支持需求时间线、安全限制与学习目标。"""

    learner_id: str
    name: str = ""
    support_needs: Timeline[frozenset[str]] = field(default_factory=Timeline)
    safety_restrictions: dict[str, SafetyRestriction] = field(default_factory=dict)
    goals: dict[str, LearningGoal] = field(default_factory=dict)


@dataclass
class CourseSession:
    session_id: str
    offering_id: str
    mode: str
    interval: Interval
    venue_changed: bool = False
    affected_learners: frozenset[str] = frozenset()
    supports_available: frozenset[str] | None = None  # None 表示按课程版本


@dataclass
class LearningRecord:
    """每次学习记录：保留场次区间与全部上传内容，成果一旦登记不可改写。"""

    record_id: str
    learner_id: str
    enrollment_id: str
    session_id: str
    offering_id: str
    mode: str
    session_start: datetime
    session_end: datetime | None
    status: str  # attended / absent
    source: str
    attribution: str | None = None
    payloads: list[dict[str, Any]] = field(default_factory=list)
    outcome: dict[str, Any] | None = None
    goal_ids: list[str] = field(default_factory=list)
    under_review: bool = False
    recorded_at: datetime | None = None

    @property
    def completed(self) -> bool:
        return self.outcome is not None


@dataclass
class TransferProposal:
    """换班/换形态建议：先评估，学员确认后才迁移，超时未确认自动释放。"""

    proposal_id: str
    enrollment_id: str
    learner_id: str
    target_offering_id: str
    target_mode: str
    checks: dict[str, bool]
    check_details: dict[str, str]
    status: str  # pending / confirmed / declined / rejected / expired
    reason: str
    proposed_at: datetime
    expires_at: datetime | None
    hold_id: str | None = None
    confirmed_at: datetime | None = None


@dataclass
class WaitlistEntry:
    entry_id: str
    learner_id: str
    offering_id: str
    applied_at: datetime
    required_supports: frozenset[str]
    status: str = "waiting"  # waiting / offered / promoted / cancelled
    current_offer_id: str | None = None


@dataclass
class WaitlistOffer:
    offer_id: str
    entry_id: str
    offering_id: str
    learner_id: str
    offered_at: datetime
    expires_at: datetime
    status: str = "pending"  # pending / confirmed / expired


@dataclass
class ConflictCase:
    """签到内容冲突，交教务核对；两路上传都保留在学习记录里。"""

    case_id: str
    session_id: str
    learner_id: str
    record_id: str
    status: str = "pending"  # pending / resolved
    resolution: str | None = None
    note: str = ""
    resolved_by: str | None = None
    resolved_at: datetime | None = None


@dataclass
class CheckInResult:
    record_id: str
    created: bool = False
    duplicate: bool = False
    conflict_case_id: str | None = None


@dataclass
class AbsenceSummary:
    """缺席统计：有因缺席不计入主动退学，也不打断或计入连续主动缺席。"""

    total_absences: int
    unexcused: int
    excused: dict[str, int]
    unexcused_streak: int
    dropout_risk: bool


@dataclass
class Followups:
    """重启或例行检查时仍需继续的事项。"""

    transfer_reminders: list[str]  # 待学员确认的迁移提案
    waitlist_reminders: list[str]  # 待学员确认的候补名额
    stage_reviews_due: list[str]  # 阶段回顾到期的报名


@dataclass
class ModeSegment:
    offering_id: str
    mode: str
    start: datetime
    end: datetime | None
    reason: str
    decision_ref: str | None
    checks: dict[str, bool] | None


@dataclass
class SupportFulfillment:
    session_id: str
    start: datetime
    needed: list[str]
    provided: list[str]
    missing: list[str]

    @property
    def fulfilled(self) -> bool:
        return not self.missing


@dataclass
class AbsenceItem:
    session_id: str
    start: datetime
    attribution: str
    excused: bool
    counts_toward_dropout: bool


@dataclass
class GoalLine:
    goal_id: str
    title: str
    path: list[str]
    outcomes: list[str]


@dataclass
class PeriodExplanation:
    """教务员可据此解释：为何安排某种形态、哪些支持已落实、缺席如何归因、目标怎样接续。"""

    learner_id: str
    start: datetime
    end: datetime
    mode_segments: list[ModeSegment]
    supports: list[SupportFulfillment]
    absences: list[AbsenceItem]
    goals: list[GoalLine]

    def render(self) -> str:
        lines = [
            f"学员 {self.learner_id} 在 {self.start:%Y-%m-%d} 至 {self.end:%Y-%m-%d} 的连续学习档案：",
            "一、授课形态安排",
        ]
        if not self.mode_segments:
            lines.append("- 该时段内没有授课形态记录")
        for seg in self.mode_segments:
            end_text = f"{seg.end:%Y-%m-%d}" if seg.end else "至今"
            mode = MODE_LABELS.get(seg.mode, seg.mode)
            line = f"- {seg.start:%Y-%m-%d} 至 {end_text}：{mode}（{seg.offering_id}）；原因：{seg.reason}"
            if seg.checks:
                passed = "、".join(CHECK_LABELS.get(k, k) for k, v in seg.checks.items() if v)
                line += f"；迁移前评估已通过：{passed}"
            lines.append(line)
        lines.append("二、支持需求落实")
        if not self.supports:
            lines.append("- 该时段内没有应参加场次")
        for item in self.supports:
            needed = "、".join(NEED_LABELS.get(n, n) for n in item.needed) or "无"
            provided = "、".join(NEED_LABELS.get(n, n) for n in item.provided) or "无"
            line = f"- 场次 {item.session_id}（{item.start:%Y-%m-%d}）：需要 {needed}；已落实 {provided}"
            line += "；全部落实" if item.fulfilled else "；未落实 " + "、".join(NEED_LABELS.get(n, n) for n in item.missing)
            lines.append(line)
        lines.append("三、缺席归因")
        if not self.absences:
            lines.append("- 该时段内没有缺席")
        for item in self.absences:
            label = ATTRIBUTION_LABELS.get(item.attribution, item.attribution)
            note = "计入主动退学统计" if item.counts_toward_dropout else "不计入主动退学"
            lines.append(f"- 场次 {item.session_id}（{item.start:%Y-%m-%d}）：{label}（{note}）")
        lines.append("四、学习目标跨班接续")
        if not self.goals:
            lines.append("- 该学员尚未设定学习目标")
        for goal in self.goals:
            path = " → ".join(goal.path)
            outcomes = "、".join(goal.outcomes) if goal.outcomes else "暂无"
            lines.append(f"- 《{goal.title}》：{path}；期间成果：{outcomes}")
        return "\n".join(lines)
