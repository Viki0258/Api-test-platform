from __future__ import annotations

from threading import BoundedSemaphore


class RunCapacityLimiter:
    """Nonblocking, process-local capacity for active test runs."""

    def __init__(self, limit: int) -> None:
        if not 1 <= limit <= 64:
            raise ValueError("run capacity must be between 1 and 64")
        self._slots = BoundedSemaphore(limit)

    def try_acquire(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release(self) -> None:
        self._slots.release()
