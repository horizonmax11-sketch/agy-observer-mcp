"""Short, bounded coordination for local atomic snapshots; no network retries."""

import time
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import uuid


@contextmanager
def writer_claim(root):
    """OS-owned guard survives as a file, but its lock is released on process exit.

    write.lock remains a short-lived reader hint. Only markers from this protocol
    may be reclaimed while holding the guard; legacy/unknown markers are preserved.
    The guard inode must never be removed while instances may still be running.
    """
    guard, marker = root / "writer.guard", root / "write.lock"
    audit = root / "lock-recoveries.jsonl"
    if any(path.is_symlink() for path in (guard, marker, audit)):
        raise ValueError("Discussion lock symlinks are not supported")
    descriptor = os.open(guard, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError(
                "Discussion store is busy; another writer holds the OS lock"
            ) from error
        if marker.exists():
            try:
                if marker.stat().st_size > 4096:
                    raise ValueError("Oversized lock marker")
                previous = json.loads(marker.read_text(encoding="utf-8"))
                if (
                    not isinstance(previous, dict)
                    or previous.get("protocol") != "os-guard-v1"
                    or type(previous.get("pid")) is not int
                    or not previous.get("token")
                ):
                    raise ValueError("Unknown lock marker")
            except (ValueError, UnicodeError) as error:
                raise ValueError(
                    "Discussion store is busy; legacy write.lock needs operator inspection"
                ) from error
            # Acquiring the kernel guard proves that no current-protocol writer owns it.
            with audit.open("a", encoding="utf-8") as output:
                output.write(
                    json.dumps(
                        dict(
                            previous_owner=previous,
                            recovered_utc=datetime.now(timezone.utc).isoformat(),
                        )
                    )
                    + "\n"
                )
            marker.unlink()
        owner = dict(
            protocol="os-guard-v1",
            pid=os.getpid(),
            token=uuid.uuid4().hex,
            created_utc=datetime.now(timezone.utc).isoformat(),
        )
        temp = root / (".writer-" + owner["token"] + ".tmp")
        try:
            with temp.open("x", encoding="utf-8") as output:
                json.dump(owner, output)
            retry_permission(lambda: os.rename(temp, marker))
        finally:
            temp.unlink(missing_ok=True)
        try:
            yield
        finally:
            marker.unlink()
    finally:
        # Closing the descriptor releases the kernel lock, including on exceptions.
        os.close(descriptor)


def retry_permission(operation):
    for attempt in range(5):
        try:
            return operation()
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.025 * (2**attempt))


def wait_for_write(marker):
    """Yield to a pending short snapshot; a stale marker remains inspectable."""
    for attempt in range(4):
        if not marker.exists():
            break
        time.sleep(0.025 * (2**attempt))
