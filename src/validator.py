"""校验领域事件信封的基础字段与已知类型。"""
from datetime import datetime

REQUIRED = ("event_id", "event_type", "aggregate_type", "aggregate_id", "occurred_at", "version", "summary")

EVENT_TYPES = frozenset({
    "COURSE_PUBLISHED",
    "COURSE_VERSIONED",
    "LEARNER_REGISTERED",
    "SUPPORT_UPDATED",
    "SAFETY_RESTRICTION_APPLIED",
    "SAFETY_RESTRICTION_LIFTED",
    "ENROLLMENT_PLACED",
    "ELIGIBILITY_CHANGED",
    "TRANSFER_PROPOSED",
    "TRANSFER_CONFIRMED",
    "TRANSFER_DECLINED",
    "TRANSFER_EXPIRED",
    "GOAL_SET",
    "GOAL_CONTINUED",
    "SESSION_RECORDED",
    "CHECKIN_UPLOADED",
    "CHECKIN_CONFLICT_RAISED",
    "CHECKIN_CONFLICT_RESOLVED",
    "ABSENCE_ATTRIBUTED",
    "OUTCOME_REVIEWED",
    "WAITLIST_JOINED",
    "WAITLIST_OFFERED",
    "WAITLIST_PROMOTED",
    "WAITLIST_OFFER_EXPIRED",
    "STAGE_REVIEW_RECORDED",
})

AGGREGATE_TYPES = frozenset({
    "learner_profile",
    "course_offering",
    "enrollment",
    "learning_record",
    "course_session",
    "transfer_proposal",
    "waitlist_entry",
})


def validate_event(record: dict) -> list[str]:
    errors = [f"缺少字段：{name}" for name in REQUIRED if name not in record]
    if "version" in record and (not isinstance(record["version"], int) or record["version"] < 1):
        errors.append("version 必须是正整数")
    event_type = record.get("event_type")
    if event_type is not None and event_type not in EVENT_TYPES:
        errors.append(f"未知事件类型：{event_type}")
    aggregate_type = record.get("aggregate_type")
    if aggregate_type is not None and aggregate_type not in AGGREGATE_TYPES:
        errors.append(f"未知聚合类型：{aggregate_type}")
    occurred_at = record.get("occurred_at")
    if isinstance(occurred_at, str):
        try:
            datetime.fromisoformat(occurred_at)
        except ValueError:
            errors.append("occurred_at 必须是 ISO 8601 时间")
    return errors
