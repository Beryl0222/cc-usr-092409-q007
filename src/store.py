"""事件存储：追加写入、完整重放，支撑重启后恢复提醒、候补与阶段回顾。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class EventStore(Protocol):
    def append(self, event: dict) -> None: ...

    def read_all(self) -> list[dict]: ...


class NullEventStore:
    """不持久化：仅内存运行时使用。"""

    def append(self, event: dict) -> None:
        pass

    def read_all(self) -> list[dict]:
        return []


class InMemoryEventStore:
    """内存存储：写入时做 JSON 往返，顺带保证事件可序列化。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def append(self, event: dict) -> None:
        self.events.append(json.loads(json.dumps(event, ensure_ascii=False)))

    def read_all(self) -> list[dict]:
        return [dict(event) for event in self.events]


class JsonlEventStore:
    """JSONL 文件存储：每行一个事件，重启后按顺序重放。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
