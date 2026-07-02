"""Append-only JSONL audit trail for tool calls and results."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    """Record events to an append-only JSONL file."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / ".lou-op" / "audit.jsonl"

    def record(self, event: str, data: dict) -> None:
        """Append one JSON line with timestamp, event, and data."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        line = json.dumps({"ts": ts, "event": event, "data": data})
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
