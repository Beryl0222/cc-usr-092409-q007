"""测试共享构造：手动时钟 + 常用课程/学员。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.continuity import (
    ContinuityService,
    DeliveryFormat,
    ManualClock,
    SupportNeed,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=TZ)
ALL_NEEDS = {SupportNeed.HEARING_ASSIST, SupportNeed.SLOW_PACE, SupportNeed.CARE_COMPANION}


def make_service(**kwargs):
    clock = ManualClock(T0)
    service = ContinuityService(clock, **kwargs)
    return service, clock


def at(hours: float = 0, days: float = 0) -> datetime:
    return T0 + timedelta(hours=hours, days=days)


def publish_offering(
    service: ContinuityService,
    offering_id: str = "OFF-1",
    *,
    fmt: DeliveryFormat = DeliveryFormat.OFFLINE,
    capacity: int = 2,
    resources=None,
    teacher_formats=None,
    sessions=(),
    effective_from=None,
    title: str = "书法初级",
):
    """发布一个单形态课程版本，默认教师资历与支持资源齐全。"""
    return service.publish_course_version(
        offering_id,
        title=title,
        teacher_id="T-1",
        teacher_formats=teacher_formats if teacher_formats is not None else {fmt},
        capacity={fmt: capacity},
        resources=resources if resources is not None else {fmt: set(ALL_NEEDS)},
        sessions=sessions,
        effective_from=effective_from,
    )


def session(session_id: str, starts_at: datetime, fmt: DeliveryFormat, location: str = "一号教室"):
    return {
        "session_id": session_id,
        "starts_at": starts_at,
        "format": fmt,
        "location": location,
    }
