"""Short, bounded coordination for local atomic snapshots; no network retries."""

import time


def retry_permission(operation):
    for attempt in range(5):
        try:
            return operation()
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.025 * (2 ** attempt))


def wait_for_write(marker):
    """Yield to a pending short snapshot; a stale marker remains inspectable."""
    for attempt in range(4):
        if not marker.exists():
            break
        time.sleep(0.025 * (2 ** attempt))
