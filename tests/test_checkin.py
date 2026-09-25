"""签到上传：电话/线下重复上传只记一次，内容冲突交教务核对。"""

import unittest

from src.continuity import CheckinChannel, DeliveryFormat, StateError
from tests.factories import at, make_service, publish_offering, session


class CheckinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service, self.clock = make_service()
        self.service.register_learner("L-1", "赵奶奶")
        publish_offering(
            self.service,
            sessions=[session("S-1", at(10), DeliveryFormat.OFFLINE)],
        )
        self.payload = {"session": "S-1", "checked_at": "2026-09-01T09:55:00+08:00", "by": "iv"}

    def test_duplicate_upload_recorded_once(self) -> None:
        first = self.service.upload_checkin("S-1", "L-1", CheckinChannel.PHONE, self.payload)
        second = self.service.upload_checkin("S-1", "L-1", CheckinChannel.PHONE, dict(self.payload))
        # 同一内容换个渠道再传，同样只记一次
        third = self.service.upload_checkin("S-1", "L-1", CheckinChannel.OFFLINE, dict(self.payload))
        self.assertEqual(first, "recorded")
        self.assertEqual(second, "duplicate")
        self.assertEqual(third, "duplicate")
        events = [e for e in self.service.events if e.event_type == "CHECKIN_RECORDED"]
        self.assertEqual(len(events), 1)

    def test_conflicting_content_goes_to_review(self) -> None:
        self.service.upload_checkin("S-1", "L-1", CheckinChannel.PHONE, self.payload)
        other = {"session": "S-1", "checked_at": "2026-09-01T10:40:00+08:00", "by": "desk"}
        result = self.service.upload_checkin("S-1", "L-1", CheckinChannel.OFFLINE, other)
        self.assertEqual(result, "conflict")
        # 原记录保持有效，冲突进入教务核对队列
        checkin = self.service.checkins["S-1:L-1"]
        self.assertEqual(checkin.payload, self.payload)
        pending = self.service.pending_conflicts()
        self.assertEqual(len(pending), 1)
        conflict_id = pending[0].conflict_id
        self.service.resolve_checkin_conflict(
            conflict_id, "incoming", resolved_by="教务员小周"
        )
        self.assertEqual(self.service.checkins["S-1:L-1"].payload, other)
        self.assertEqual(self.service.pending_conflicts(), [])
        with self.assertRaises(StateError):
            self.service.resolve_checkin_conflict(conflict_id, "original")

    def test_corrected_resolution(self) -> None:
        self.service.upload_checkin("S-1", "L-1", CheckinChannel.OFFLINE, self.payload)
        self.service.upload_checkin(
            "S-1", "L-1", CheckinChannel.PHONE, {"session": "S-1", "checked_at": "冲突时间"}
        )
        conflict_id = self.service.pending_conflicts()[0].conflict_id
        fixed = {"session": "S-1", "checked_at": "2026-09-01T10:05:00+08:00", "by": "教务订正"}
        self.service.resolve_checkin_conflict(
            conflict_id, "corrected", corrected_payload=fixed, resolved_by="教务员小周"
        )
        self.assertEqual(self.service.checkins["S-1:L-1"].payload, fixed)


if __name__ == "__main__":
    unittest.main()
