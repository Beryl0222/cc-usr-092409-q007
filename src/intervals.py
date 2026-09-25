"""生效区间工具：为课程版本、报名资格、支持需求、授课形态等事实保留时间维度。

同一事实的连续记录构成时间线：任意时刻至多一条生效，后写入者优先，
被覆盖的部分保留在历史区间中，业务更正因此产生后继记录而不是原地改写。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Iterator, TypeVar

T = TypeVar("T")

_FAR = datetime.max  # 开放区间的远端


@dataclass(frozen=True)
class Interval:
    """左闭右开的生效区间；end 为 None 表示持续有效。"""

    start: datetime
    end: datetime | None = None

    def __post_init__(self) -> None:
        if self.end is not None and self.end <= self.start:
            raise ValueError("生效区间结束必须晚于开始")

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment and (self.end is None or moment < self.end)

    def overlaps(self, other: "Interval") -> bool:
        self_end = self.end or _FAR.replace(tzinfo=self.start.tzinfo)
        other_end = other.end or _FAR.replace(tzinfo=other.start.tzinfo)
        return self.start < other_end and other.start < self_end


@dataclass
class Effective(Generic[T]):
    """一条带生效区间的事实记录；reason 记录写入原因，供事后解释。"""

    value: T
    interval: Interval
    reason: str = ""


def _clip(record: Effective[T], new_interval: Interval) -> list[Effective[T]]:
    """从既有记录中扣除新区间覆盖的部分，可能拆成前后两段。"""
    interval = record.interval
    if not interval.overlaps(new_interval):
        return [record]
    pieces: list[Effective[T]] = []
    if interval.start < new_interval.start:
        pieces.append(Effective(record.value, Interval(interval.start, new_interval.start), record.reason))
    if new_interval.end is not None and (interval.end is None or new_interval.end < interval.end):
        pieces.append(Effective(record.value, Interval(new_interval.end, interval.end), record.reason))
    return pieces


class Timeline(Generic[T]):
    """同一事实的连续生效记录：任意时刻至多一条生效，后写入的记录优先。"""

    def __init__(self) -> None:
        self._records: list[Effective[T]] = []

    def __iter__(self) -> Iterator[Effective[T]]:
        return iter(self.records())

    def __len__(self) -> int:
        return len(self._records)

    def records(self) -> list[Effective[T]]:
        return sorted(self._records, key=lambda r: r.interval.start)

    def latest(self) -> Effective[T] | None:
        return max(self._records, key=lambda r: r.interval.start, default=None)

    def at(self, moment: datetime) -> Effective[T] | None:
        for record in self._records:
            if record.interval.contains(moment):
                return record
        return None

    def between(self, start: datetime, end: datetime) -> list[Effective[T]]:
        window = Interval(start, end)
        return [r for r in self.records() if r.interval.overlaps(window)]

    def append(
        self,
        value: T,
        start: datetime,
        *,
        end: datetime | None = None,
        reason: str = "",
    ) -> Effective[T]:
        """写入一条自 start 生效的记录；与既有记录重叠的部分以新记录为准。"""
        new_interval = Interval(start, end)
        clipped: list[Effective[T]] = []
        for record in self._records:
            clipped.extend(_clip(record, new_interval))
        entry = Effective(value, new_interval, reason)
        clipped.append(entry)
        clipped.sort(key=lambda r: r.interval.start)
        self._records = clipped
        return entry
