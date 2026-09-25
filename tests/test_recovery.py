import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.clock import ManualClock
from src.service import LearningContinuityService
from src.store import JsonlEventStore
from src.validator import validate_event
from tests.helpers import HOUR, T0, publish_calligraphy, register

KWARGS = {
    "hold_ttl": timedelta(hours=24),
    "offer_ttl": timedelta(hours=12),
    "stage_size": 2,
}


class RecoveryTest(unittest.TestCase):
    """重启后从事件日志恢复，继续提醒、候补与阶段回顾。"""

    def test_restart_recovers_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            clock = ManualClock(T0)
            svc = LearningContinuityService(clock=clock, store=JsonlEventStore(path), **KWARGS)
            publish_calligraphy(svc, capacity=1)
            publish_calligraphy(svc, "course-phone", capacity=1, modes=("phone",))
            register(svc, "learner-1")
            svc.set_goal("learner-1", "goal-1", title="楷书入门", offering_id="course-calligraphy")
            svc.place_enrollment("enr-1", "learner-1", "course-calligraphy", mode="offline")
            # 两场到课，阶段回顾到期
            svc.schedule_session("s-1", "course-calligraphy", mode="offline", start=T0 + HOUR, end=T0 + 2 * HOUR)
            svc.schedule_session("s-2", "course-calligraphy", mode="offline", start=T0 + 2 * HOUR, end=T0 + 3 * HOUR)
            svc.upload_checkin("up-1", "s-1", "learner-1", source="offline", payload={"status": "attended"})
            svc.upload_checkin("up-2", "s-2", "learner-1", source="offline", payload={"status": "attended"})
            # 待确认的迁移与排队中的候补
            svc.propose_transfer("prop-1", "enr-1", target_offering_id="course-phone", target_mode="phone")
            register(svc, "learner-2")
            svc.join_waitlist("wl-1", "learner-2", "course-calligraphy")

            # 重启：换一个时钟，从同一事件文件恢复
            clock2 = ManualClock(T0 + 3 * HOUR)
            svc2 = LearningContinuityService.recover(clock=clock2, store=JsonlEventStore(path), **KWARGS)
            self.assertEqual(svc2.proposals["prop-1"].status, "pending")
            self.assertEqual(len(svc2.records), 2)
            followups = svc2.pending_followups()
            self.assertEqual(followups.transfer_reminders, ["prop-1"])
            self.assertEqual(followups.stage_reviews_due, ["enr-1"])
            self.assertEqual(followups.waitlist_reminders, [])

            # 恢复后继续业务：确认迁移 → 腾出名额 → 候补获得出价
            svc2.confirm_transfer("prop-1")
            followups = svc2.pending_followups()
            self.assertEqual(len(followups.waitlist_reminders), 1)
            offer_id = followups.waitlist_reminders[0]
            svc2.confirm_waitlist_offer(offer_id, enrollment_id="enr-2", mode="offline")
            self.assertEqual(svc2.waitlist_entries["wl-1"].status, "promoted")

            # 阶段回顾完成后不再提醒
            svc2.record_stage_review("rev-1", "enr-1", summary="前两场到课稳定，继续楷书练习")
            self.assertEqual(svc2.due_stage_reviews(), [])

            # 幂等键在重启后仍然有效：重复上传不产生新记录
            again = svc2.upload_checkin("up-1", "s-1", "learner-1", source="offline", payload={"status": "attended"})
            self.assertTrue(again.duplicate)
            self.assertEqual(len(svc2.records), 2)

            # 事件日志全部符合契约
            for event in JsonlEventStore(path).read_all():
                self.assertEqual(validate_event(event), [], event["event_id"])


if __name__ == "__main__":
    unittest.main()
