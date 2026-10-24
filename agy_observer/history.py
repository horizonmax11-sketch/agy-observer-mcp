"""Read only the configured history directory, rejecting traversal and symlinks."""

from pathlib import Path
import re
import threading
from .runtime import strict_json

MAX_STATUS_BYTES = 5 * 1024 * 1024
MAX_EVENTS_BYTES = 256 * 1024
SUMMARY_KEYS = (
    "run_id",
    "kind",
    "state",
    "model_requested",
    "started_utc",
    "finished_utc",
    "last_event_utc",
    "exit_code",
    "discussion_id",
    "discussion_message_id",
)


class History:
    def __init__(self, state_dir):
        self.root = Path(state_dir).resolve() / "runs"
        self._summaries = {}
        self._lock = threading.Lock()

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
        with self.file(run_id, "status.json").open("rb") as file:
            data = file.read(MAX_STATUS_BYTES + 1)
        if len(data) > MAX_STATUS_BYTES:
            raise ValueError("History status exceeds the read limit")
        try:
            item = strict_json(data.decode("utf-8"))
        except (UnicodeError, RecursionError) as error:
            raise ValueError("Invalid history encoding or nesting") from error
        if not isinstance(item, dict) or item.get("run_id") != run_id:
            raise ValueError("Invalid history status object")
        for key in (
            "kind",
            "state",
            "started_utc",
            "last_event_utc",
            "task",
            "response",
            "warnings",
        ):
            if not isinstance(item.get(key), str):
                raise ValueError("Invalid history status fields")
        return item

    def list(self, limit=50, include_tests=False):
        with self._lock:
            return self._list(limit, include_tests)

    def _list(self, limit, include_tests):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        rows = []
        if self.root.exists():
            # Generated run IDs start with a UTC timestamp; read newest candidates first.
            for folder in sorted(self.root.iterdir(), key=lambda path: path.name, reverse=True):
                if not folder.is_dir() or folder.is_symlink():
                    continue
                try:
                    file = self.file(folder.name, "status.json")
                    stat = file.stat()
                    stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                    cached = self._summaries.get(folder.name)
                    if cached and cached[0] == stamp:
                        item = cached[1]
                    else:
                        full = self.status(folder.name)
                        item = {key: full[key] for key in SUMMARY_KEYS if key in full}
                        item["task_preview"] = full["task"][:120]
                        self._summaries[folder.name] = (stamp, item)
                    if include_tests or not any(
                        label in item.get("kind", "") for label in ("离线", "模拟")
                    ):
                        rows.append(dict(item))
                        if len(rows) == limit:
                            break
                except (OSError, ValueError, TypeError):
                    continue
        visible = {item["run_id"] for item in rows}
        self._summaries = {key: value for key, value in self._summaries.items() if key in visible}
        return rows

    def events(self, run_id):
        with self.file(run_id, "events.jsonl").open("rb") as file:
            size = file.seek(0, 2)
            offset = max(0, size - MAX_EVENTS_BYTES)
            file.seek(offset)
            data = file.read(MAX_EVENTS_BYTES)
        if offset:
            data = data.partition(b"\n")[2]
        lines = data.split(b"\n")[:-1]  # Ignore a concurrent writer's incomplete final line.
        rows = []
        for line in lines[-100:]:
            try:
                row = strict_json(line.decode("utf-8"))
                if isinstance(row, dict) and isinstance(row.get("event"), str):
                    rows.append(row)
            except (ValueError, UnicodeError, RecursionError):
                continue
        return rows
