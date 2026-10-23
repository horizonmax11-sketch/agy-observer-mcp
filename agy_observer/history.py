"""Read only the configured history directory, rejecting traversal and symlinks."""

import json
from pathlib import Path
import re


class History:
    def __init__(self, state_dir):
        self.root = Path(state_dir).resolve() / "runs"

    def file(self, run_id, name):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id) or name not in {
            "status.json",
            "events.jsonl",
            "transcript.md",
        }:
            raise ValueError("Invalid history identifier")
        folder = self.root / run_id
        target = folder / name
        if (
            folder.is_symlink()
            or target.is_symlink()
            or not target.resolve().is_relative_to(self.root)
        ):
            raise ValueError("History links outside the artifact directory are not served")
        return target

    def status(self, run_id):
        return json.loads(self.file(run_id, "status.json").read_text(encoding="utf-8"))

    def list(self, limit=50, include_tests=False):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        rows = []
        if self.root.exists():
            for folder in self.root.iterdir():
                if not folder.is_dir() or folder.is_symlink():
                    continue
                try:
                    item = self.status(folder.name)
                    if include_tests or not any(
                        label in item.get("kind", "") for label in ("离线", "模拟")
                    ):
                        rows.append(item)
                except (OSError, ValueError, TypeError):
                    continue
        rows.sort(key=lambda row: row.get("started_utc", ""), reverse=True)
        return rows[:limit]

    def events(self, run_id):
        from collections import deque

        with self.file(run_id, "events.jsonl").open(encoding="utf-8") as f:
            return [json.loads(line) for line in deque(f, maxlen=100)]
