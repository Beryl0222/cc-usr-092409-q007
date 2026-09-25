import unittest
from datetime import timedelta

from src.intervals import Interval, Timeline
from tests.helpers import T0


class IntervalTest(unittest.TestCase):
    def test_contains_left_closed_right_open(self):
        iv = Interval(T0, T0 + 2 * timedelta(hours=1))
        self.assertTrue(iv.contains(T0))
        self.assertFalse(iv.contains(T0 + 2 * timedelta(hours=1)))
        self.assertFalse(iv.contains(T0 - timedelta(seconds=1)))

    def test_open_interval_has_no_end(self):
        iv = Interval(T0)
        self.assertTrue(iv.contains(T0 + timedelta(days=3650)))

    def test_end_must_be_after_start(self):
        with self.assertRaises(ValueError):
            Interval(T0, T0)


class TimelineTest(unittest.TestCase):
    def test_append_closes_current_record(self):
        tl = Timeline()
        tl.append("线下班", T0)
        tl.append("电话辅导", T0 + timedelta(days=10))
        self.assertEqual(tl.at(T0 + timedelta(days=5)).value, "线下班")
        self.assertEqual(tl.at(T0 + timedelta(days=10)).value, "电话辅导")
        self.assertIsNone(tl.at(T0 - timedelta(seconds=1)))

    def test_insert_in_middle_splits_record(self):
        tl = Timeline()
        tl.append("甲", T0)
        tl.append("乙", T0 + timedelta(days=5), end=T0 + timedelta(days=8))
        self.assertEqual(tl.at(T0 + timedelta(days=2)).value, "甲")
        self.assertEqual(tl.at(T0 + timedelta(days=6)).value, "乙")
        self.assertEqual(tl.at(T0 + timedelta(days=9)).value, "甲")

    def test_between_returns_overlapping_segments(self):
        tl = Timeline()
        tl.append("甲", T0)
        tl.append("乙", T0 + timedelta(days=10))
        segs = tl.between(T0 + timedelta(days=5), T0 + timedelta(days=20))
        self.assertEqual([s.value for s in segs], ["甲", "乙"])

    def test_later_write_supersedes_same_start(self):
        tl = Timeline()
        tl.append("甲", T0)
        tl.append("乙", T0)
        self.assertEqual(tl.at(T0).value, "乙")
        self.assertEqual(len(tl), 1)


if __name__ == "__main__":
    unittest.main()
