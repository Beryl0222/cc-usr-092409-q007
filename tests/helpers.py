"""测试共用的搭建工具。"""
from datetime import datetime, timedelta, timezone

from src.clock import ManualClock
from src.service import LearningContinuityService
from src.store import InMemoryEventStore

CN = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=CN)
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


def build_service(**kwargs):
    clock = ManualClock(T0)
    store = InMemoryEventStore()
    service = LearningContinuityService(
        clock=clock,
        store=store,
        hold_ttl=kwargs.get("hold_ttl", timedelta(hours=24)),
        offer_ttl=kwargs.get("offer_ttl", timedelta(hours=12)),
        stage_size=kwargs.get("stage_size", 2),
    )
    return service, clock, store


def publish_calligraphy(
    service,
    offering_id="course-calligraphy",
    *,
    capacity=2,
    resources=("hearing_assist", "slow_paced"),
    modes=("offline", "phone"),
    quals=("senior_art",),
    required=("senior_art",),
):
    service.publish_course(
        offering_id,
        title="书法班",
        teacher_id="teacher-wang",
        teacher_qualifications=quals,
        required_qualifications=required,
        capacity=capacity,
        support_resources=resources,
        modes=modes,
        effective_from=T0,
    )


def register(service, learner_id, needs=("hearing_assist",)):
    service.register_learner(learner_id, name=learner_id, support_needs=needs)
