"""可注入时钟：超时释放名额、候补确认等业务时间统一由此取得。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """业务时钟接口，测试与演练可注入替代实现。"""

    def now(self) -> datetime: ...


class SystemClock:
    """生产环境使用的系统时钟（UTC）。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """手动时钟：时间只能前进，用于测试超时释放、候补确认等场景。"""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now

    def set(self, moment: datetime) -> None:
        if moment < self._now:
            raise ValueError("时钟不能回拨")
        self._now = moment
