"""可注入时钟：业务时间一律来自 Clock，便于测试与超时释放。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """时间来源协议。生产环境用 SystemClock，测试用 ManualClock。"""

    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试用手动时钟，可推进、可定格。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock 需要带时区的起始时间")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("ManualClock 需要带时区的时间")
        self._now = value

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now
