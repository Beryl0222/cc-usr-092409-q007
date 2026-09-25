"""银龄连续学习档案服务。"""
from .clock import ManualClock, SystemClock
from .domain import AbsenceAttribution, DeliveryMode, SupportNeed
from .service import LearningContinuityService
from .store import InMemoryEventStore, JsonlEventStore
from .validator import validate_event

__all__ = [
    "AbsenceAttribution",
    "DeliveryMode",
    "InMemoryEventStore",
    "JsonlEventStore",
    "LearningContinuityService",
    "ManualClock",
    "SupportNeed",
    "SystemClock",
    "validate_event",
]
