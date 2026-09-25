"""JSONL 事件存储：只追加，不重写；重启时整段回放恢复状态。"""

from __future__ import annotations

import json
from pathlib import Path

from .events import Event


class JsonlEventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, event: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_record(), ensure_ascii=False) + "\n")

    def load(self) -> list[Event]:
        if not self.path.exists():
            return []
        events: list[Event] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(Event.from_record(json.loads(line)))
        return events
