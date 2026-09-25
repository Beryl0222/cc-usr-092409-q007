"""银龄连续学习档案：多形态课程接续的领域实现。"""

from .clock import Clock, ManualClock, SystemClock
from .events import AGGREGATE_TYPES, EVENT_TYPES, Event
from .explain import explain_enrollment
from .model import (
    AbsenceAttribution,
    CheckinChannel,
    DeliveryFormat,
    DomainError,
    EligibilityStatus,
    GoalStatus,
    NotFoundError,
    ProposalStatus,
    StateError,
    SupportNeed,
    WaitlistStatus,
)
from .service import ContinuityService
from .store import JsonlEventStore

__all__ = [
    "AGGREGATE_TYPES",
    "EVENT_TYPES",
    "AbsenceAttribution",
    "CheckinChannel",
    "Clock",
    "ContinuityService",
    "DeliveryFormat",
    "DomainError",
    "EligibilityStatus",
    "Event",
    "GoalStatus",
    "JsonlEventStore",
    "ManualClock",
    "NotFoundError",
    "ProposalStatus",
    "StateError",
    "SupportNeed",
    "SystemClock",
    "WaitlistStatus",
    "explain_enrollment",
]
