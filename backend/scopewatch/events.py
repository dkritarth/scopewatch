"""Server-Sent Events broadcaster and stream generator."""

import asyncio
from collections import defaultdict
import json
import logging
from typing import AsyncGenerator, Optional
from scopewatch.schemas import EvidenceEvent

logger = logging.getLogger("scopewatch.events")


class EventBroadcaster:
    """Manages active SSE subscriber queues per run."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, run_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers[run_id].add(queue)
        return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            if run_id in self._subscribers:
                self._subscribers[run_id].discard(queue)
                if not self._subscribers[run_id]:
                    del self._subscribers[run_id]

    async def publish(self, run_id: str, event: EvidenceEvent) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(run_id, []))
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue full for run %s; dropping event %d", run_id, event.sequence)


broadcaster = EventBroadcaster()


async def format_sse(event: EvidenceEvent) -> str:
    """Format an EvidenceEvent as a standard SSE message block."""
    payload = event.model_dump_json()
    return f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {payload}\n\n"
