from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from .models import RuntimeEvent


class EventBus:
    def __init__(self, history_limit: int = 1000):
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._history: list[str] = []
        self._history_limit = history_limit
        self._lock = asyncio.Lock()

    async def publish(self, event: RuntimeEvent) -> None:
        message = json.dumps(event.to_dict(), ensure_ascii=False)
        async with self._lock:
            self._history.append(message)
            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit :]
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, replay: bool = False) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._subscribers.add(queue)
            history = list(self._history) if replay else []
        try:
            for message in history:
                yield message
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

