"""领域事件信封与事件名称。

事件名称与聚合类型以 contracts/domain.schema.json 为对外契约，
此处常量与契约保持一致，校验器与业务代码共用同一份定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

EVENT_TYPES = (
    # 兼容基线契约
    "COURSE_PUBLISHED",
    "ENROLLMENT_PLACED",
    "SUPPORT_UPDATED",
    "SESSION_RECORDED",
    "OUTCOME_REVIEWED",
    # 连续学习档案
    "LEARNER_REGISTERED",
    "COURSE_VERSION_PUBLISHED",
    "ELIGIBILITY_UPDATED",
    "SAFETY_RESTRICTION_SET",
    "TRANSFER_PROPOSED",
    "TRANSFER_CONFIRMED",
    "TRANSFER_REJECTED",
    "TRANSFER_EXPIRED",
    "TRANSFER_CANCELLED",
    "RECORD_CORRECTED",
    "GOAL_CARRIED",
    "CHECKIN_RECORDED",
    "CHECKIN_CONFLICT_RAISED",
    "CHECKIN_CONFLICT_RESOLVED",
    "WAITLIST_APPLIED",
    "WAITLIST_OFFERED",
    "WAITLIST_ENROLLED",
    "WAITLIST_EXPIRED",
    "WAITLIST_CANCELLED",
    "REMINDER_SENT",
)

AGGREGATE_TYPES = (
    "learner_profile",
    "course_offering",
    "enrollment",
    "learning_record",
    "transfer_proposal",
    "waitlist_entry",
    "checkin",
)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def parse_moment(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"时间缺少时区：{text}")
    return moment


@dataclass
class Event:
    """追加式事件；一旦写入，标识、发生时间与版本不再改写。"""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    version: int
    summary: str
    payload: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "occurred_at": iso(self.occurred_at),
            "version": self.version,
            "summary": self.summary,
            "payload": self.payload,
        }

    @classmethod
    def from_record(cls, record: dict) -> "Event":
        return cls(
            event_id=record["event_id"],
            event_type=record["event_type"],
            aggregate_type=record["aggregate_type"],
            aggregate_id=record["aggregate_id"],
            occurred_at=parse_moment(record["occurred_at"]),
            version=record["version"],
            summary=record["summary"],
            payload=dict(record.get("payload") or {}),
        )
