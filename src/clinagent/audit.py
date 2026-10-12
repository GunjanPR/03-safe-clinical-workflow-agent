"""Append-only, hash-chained audit log with replay and tamper detection.

Every event records the previous event's hash, so editing or deleting any line breaks
the chain from that point on. `verify()` recomputes the chain; `replay()` rebuilds a
session's history from the log alone.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _digest(prev_hash: str, body: dict[str, Any]) -> str:
    payload = prev_hash + json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path | None = None):
        # path=None keeps the log in memory (used by the evaluation harness).
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._mem: list[dict[str, Any]] = []
        self._last = GENESIS
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                for ev in self._read():
                    self._last = ev["hash"]

    def _read(self) -> Iterator[dict[str, Any]]:
        if not self.path:
            yield from self._mem
            return
        if not self.path.exists():
            return
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def append(self, session_id: str, actor: str, action: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            body = {
                "id": str(uuid.uuid4()),
                "ts": time.time(),
                "session_id": session_id,
                "actor": actor,
                "action": action,
                # Serialize now: later mutation of the caller's objects must not alter the log.
                "data": json.loads(json.dumps(data, default=str)),
                "prev": self._last,
            }
            ev = {**body, "hash": _digest(self._last, body)}
            if self.path:
                with self.path.open("a") as f:
                    f.write(json.dumps(ev, default=str) + "\n")
            else:
                self._mem.append(ev)
            self._last = ev["hash"]
            return ev

    def verify(self) -> tuple[bool, int | None]:
        """Return (ok, index_of_first_bad_event)."""
        prev = GENESIS
        for i, ev in enumerate(self._read()):
            body = {k: v for k, v in ev.items() if k != "hash"}
            if ev.get("prev") != prev or _digest(prev, body) != ev.get("hash"):
                return False, i
            prev = ev["hash"]
        return True, None

    def replay(self, session_id: str) -> list[dict[str, Any]]:
        return [ev for ev in self._read() if ev["session_id"] == session_id]

    def events(self) -> list[dict[str, Any]]:
        return list(self._read())
