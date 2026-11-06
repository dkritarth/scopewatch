"""Server-Sent Events broadcaster and stream generator."""

import asyncio
from collections import defaultdict
import json
import logging
import threading
from typing import AsyncGenerator, Optional
from scopewatch.schemas import EvidenceEvent

logger = logging.getLogger("scopewatch.events")


class EventBroadcaster:
    """Manages active SSE subscriber queues per run."""

    def __init__(self) -> None:
        self._subscribers: dict[str, dict[asyncio.Queue, asyncio.AbstractEventLoop]] = defaultdict(dict)
        self._lock = threading.Lock()

    async def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers[run_id][queue] = loop
        return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            if run_id in self._subscribers:
                self._subscribers[run_id].pop(queue, None)
                if not self._subscribers[run_id]:
                    del self._subscribers[run_id]

    def publish_sync(self, run_id: str, event: EvidenceEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers.get(run_id, {}).items())

        def deliver(queue: asyncio.Queue) -> None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue full for run %s; dropping event %d", run_id, event.sequence)

        for queue, loop in subscribers:
            try:
                # Queue._loop binds lazily. Capture ownership at subscribe
                # time instead, and always wake its loop from worker threads.
                if loop.is_running():
                    loop.call_soon_threadsafe(deliver, queue)
            except Exception as exc:
                logger.warning("Failed to publish event %d for run %s: %s", event.sequence, run_id, exc)

    async def publish(self, run_id: str, event: EvidenceEvent) -> None:
        self.publish_sync(run_id, event)


broadcaster = EventBroadcaster()


async def format_sse(event: EvidenceEvent) -> str:
    """Format an EvidenceEvent as a standard SSE message block."""
    payload = event.model_dump_json()
    return f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {payload}\n\n"
