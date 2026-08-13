from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AppendOnlyEventLog:
    """Thread-safe JSONL event log; events are appended, never rewritten."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sequence = self._last_sequence()

    def _last_sequence(self) -> int:
        if not self.path.is_file():
            return 0
        last = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                last = max(last, int(json.loads(line).get("sequence", 0)))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return last

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            event = {
                "event_id": str(uuid.uuid4()),
                "sequence": self._sequence,
                "event_type": event_type,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                **payload,
            }
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()
            return event
