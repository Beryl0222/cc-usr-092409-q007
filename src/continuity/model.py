"""领域模型：枚举、生效区间与内存状态。

设计约定：
- 课程版本、报名资格、支持需求、授课形态、学习记录都以 [valid_from, valid_to)
  的生效区间保存；更正只追加后继版本，从不原地改写历史。
- 所有时间均为带时区的 datetime。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class DeliveryFormat(str, Enum):
    OFFLINE = "OFFLINE"  # 线下班
    PHONE = "PHONE"  # 电话辅导
    ONLINE = "ONLINE"  # 线上直播


class SupportNeed(str, Enum):
    HEARING_ASSIST = "HEARING_ASSIST"  # 助听
    SLOW_PACE = "SLOW_PACE"  # 慢速讲解
    CARE_COMPANION = "CARE_COMPANION"  # 照护陪同


class EligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"  # 具备报名资格
    SUSPENDED = "SUSPENDED"  # 资格暂停
    WITHDRAWN = "WITHDRAWN"  # 退学
    COMPLETED = "COMPLETED"  # 结业


class AbsenceAttribution(str, Enum):
    UNATTRIBUTED = "UNATTRIBUTED"  # 待归因（不计入退学评估）
    VOLUNTARY = "VOLUNTARY"  # 主动缺席（计入退学评估）
    VENUE_ADJUSTMENT = "VENUE_ADJUSTMENT"  # 场地调整（不计入）
    SAFETY_PAUSE = "SAFETY_PAUSE"  # 安全限制暂停（不计入）
    SUPPORT_GAP = "SUPPORT_GAP"  # 支持资源未落实（不计入）


class ProposalStatus(str, Enum):
    PENDING = "PENDING"  # 待学员确认
    CONFIRMED = "CONFIRMED"  # 已确认并迁移
    REJECTED = "REJECTED"  # 前置核验未通过
    EXPIRED = "EXPIRED"  # 超时未确认
    CANCELLED = "CANCELLED"  # 已撤销


class WaitlistStatus(str, Enum):
    WAITING = "WAITING"
    OFFERED = "OFFERED"
    ENROLLED = "ENROLLED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class GoalStatus(str, Enum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    CARRIED = "CARRIED"  # 已接续到其他报名


class CheckinChannel(str, Enum):
    PHONE = "PHONE"  # 电话签到
    OFFLINE = "OFFLINE"  # 线下签到


# 中文标签，供教务解释视图使用。
FORMAT_LABELS = {
    DeliveryFormat.OFFLINE: "线下班",
    DeliveryFormat.PHONE: "电话辅导",
    DeliveryFormat.ONLINE: "线上直播",
}
NEED_LABELS = {
    SupportNeed.HEARING_ASSIST: "助听",
    SupportNeed.SLOW_PACE: "慢速讲解",
    SupportNeed.CARE_COMPANION: "照护陪同",
}
ATTRIBUTION_LABELS = {
    AbsenceAttribution.UNATTRIBUTED: "待归因",
    AbsenceAttribution.VOLUNTARY: "主动缺席",
    AbsenceAttribution.VENUE_ADJUSTMENT: "场地调整",
    AbsenceAttribution.SAFETY_PAUSE: "安全限制暂停",
    AbsenceAttribution.SUPPORT_GAP: "支持未落实",
}


class DomainError(Exception):
    """领域规则冲突。"""


class NotFoundError(DomainError):
    """引用的聚合不存在。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


@dataclass
class Versioned:
    """带生效区间的事实基类。"""

    valid_from: datetime
    valid_to: datetime | None = None

    def covers(self, moment: datetime) -> bool:
        return self.valid_from <= moment and (self.valid_to is None or moment < self.valid_to)


def close_open(items: list[Versioned], at: datetime) -> None:
    """在 at 时刻关闭当前生效版本。"""
    for item in items:
        if item.valid_to is None:
            item.valid_to = at


def effective_at(items: list[Versioned], moment: datetime):
    """取 moment 时刻生效的版本；同一时刻多个命中时取最后追加的。"""
    hit = None
    for item in items:
        if item.covers(moment):
            hit = item
    return hit


def overlapping(items: list[Versioned], start: datetime, end: datetime) -> list[Versioned]:
    """取与 [start, end) 相交的版本。"""
    return [
        item
        for item in items
        if item.valid_from < end and (item.valid_to is None or item.valid_to > start)
    ]


@dataclass
class Session:
    session_id: str
    starts_at: datetime
    format: DeliveryFormat
    location: str = ""


@dataclass
class CourseVersion(Versioned):
    """课程版本：教师资历、名额、支持资源与课表随版本一起生效。"""

    title: str = ""
    teacher_id: str = ""
    teacher_formats: set[DeliveryFormat] = field(default_factory=set)
    teacher_supports: set[SupportNeed] = field(default_factory=set)
    capacity: dict[DeliveryFormat, int] = field(default_factory=dict)
    resources: dict[DeliveryFormat, set[SupportNeed]] = field(default_factory=dict)
    sessions: list[Session] = field(default_factory=list)


@dataclass
class CourseOffering:
    offering_id: str
    versions: list[CourseVersion] = field(default_factory=list)

    def version_at(self, moment: datetime) -> CourseVersion | None:
        return effective_at(self.versions, moment)

    def live_sessions(self) -> list[tuple[Session, CourseVersion]]:
        """生效课表：场次开始时间落在其所属版本生效区间内的场次。

        课程换版后，旧版本中尚未开始的场次自动失效，无需逐节取消。
        """
        return [
            (session, version)
            for version in self.versions
            for session in version.sessions
            if version.covers(session.starts_at)
        ]


@dataclass
class SupportNeedSet(Versioned):
    needs: set[SupportNeed] = field(default_factory=set)
    reason: str = ""


@dataclass
class SafetyWindow(Versioned):
    """安全限制窗口：窗口内暂停指定授课形态的活动，其余形态不受影响。"""

    restricted_formats: set[DeliveryFormat] = field(default_factory=set)
    reason: str = ""


@dataclass
class LearnerProfile:
    learner_id: str
    name: str
    support_needs: list[SupportNeedSet] = field(default_factory=list)
    safety_windows: list[SafetyWindow] = field(default_factory=list)

    def needs_at(self, moment: datetime) -> set[SupportNeed]:
        current = effective_at(self.support_needs, moment)
        return set(current.needs) if current else set()

    def paused_formats_at(self, moment: datetime) -> set[DeliveryFormat]:
        paused: set[DeliveryFormat] = set()
        for window in self.safety_windows:
            if window.covers(moment):
                paused |= window.restricted_formats
        return paused


@dataclass
class Eligibility(Versioned):
    status: EligibilityStatus = EligibilityStatus.ELIGIBLE
    reason: str = ""


@dataclass
class Placement(Versioned):
    """授课形态安排：某段时间学员在哪个班级、以哪种形态学习，以及为什么。"""

    offering_id: str = ""
    format: DeliveryFormat = DeliveryFormat.OFFLINE
    reason: str = ""
    proposal_id: str | None = None


@dataclass
class Goal:
    goal_id: str
    title: str
    status: GoalStatus = GoalStatus.OPEN
    carried_from: str | None = None  # 接续来源目标（跨班/跨报名链）
    source_enrollment_id: str | None = None
    completed_at: datetime | None = None
    completed_in_offering: str | None = None


@dataclass
class Enrollment:
    enrollment_id: str
    learner_id: str
    placements: list[Placement] = field(default_factory=list)
    eligibility: list[Eligibility] = field(default_factory=list)
    goals: dict[str, Goal] = field(default_factory=dict)
    review_anchor: datetime | None = None
    reviews_generated: int = 0
    reviews: list[dict] = field(default_factory=list)
    reminders_sent: set[str] = field(default_factory=set)

    def placement_at(self, moment: datetime) -> Placement | None:
        return effective_at(self.placements, moment)

    def eligibility_at(self, moment: datetime) -> Eligibility | None:
        return effective_at(self.eligibility, moment)

    def is_active_at(self, moment: datetime) -> bool:
        current = self.eligibility_at(moment)
        return current is not None and current.status == EligibilityStatus.ELIGIBLE


@dataclass
class LearningRecord:
    """每次学习记录；被更正时 valid_to 关闭，后继记录通过 supersedes 链接。"""

    record_id: str
    enrollment_id: str
    session_id: str
    attended: bool
    attribution: AbsenceAttribution | None
    outcomes: tuple[str, ...]
    note: str
    recorded_at: datetime
    session_starts_at: datetime | None = None
    offering_id: str = ""
    valid_to: datetime | None = None
    supersedes: str | None = None


@dataclass
class TransferProposal:
    proposal_id: str
    enrollment_id: str
    target_offering_id: str
    target_format: DeliveryFormat
    status: ProposalStatus
    checks: dict
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass
class WaitlistEntry:
    entry_id: str
    learner_id: str
    offering_id: str
    desired_format: DeliveryFormat
    required_supports: set[SupportNeed]
    applied_at: datetime  # 原申请时间，候补推进的排序依据
    status: WaitlistStatus = WaitlistStatus.WAITING
    offer_expires_at: datetime | None = None


@dataclass
class CheckinConflict:
    conflict_id: str
    incoming_channel: CheckinChannel
    incoming_payload: dict
    raised_at: datetime
    status: str = "PENDING"  # PENDING / RESOLVED
    resolution: str | None = None
    resolved_by: str = ""
    resolved_at: datetime | None = None


@dataclass
class Checkin:
    session_id: str
    learner_id: str
    channel: CheckinChannel
    payload: dict
    uploaded_at: datetime
    conflicts: list[CheckinConflict] = field(default_factory=list)
