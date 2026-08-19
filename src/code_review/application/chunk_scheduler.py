from __future__ import annotations

import asyncio
from collections import deque

from code_review.domain.review_chunks import ReviewChunk


class ReviewQueueFull(RuntimeError):
    pass


class ChunkScheduler:
    def __init__(self, queue_limit: int) -> None:
        self._queue_limit = queue_limit
        self._queues: dict[str, deque[ReviewChunk]] = {}
        self._rotation: deque[str] = deque()
        self._size = 0
        self._condition = asyncio.Condition()

    async def put(self, chunk: ReviewChunk) -> None:
        async with self._condition:
            if self._size >= self._queue_limit:
                raise ReviewQueueFull("审查队列已满，请稍后重试。")
            queue = self._queues.get(chunk.review_id)
            if queue is None:
                queue = deque()
                self._queues[chunk.review_id] = queue
                self._rotation.append(chunk.review_id)
            queue.append(chunk)
            self._size += 1
            self._condition.notify()

    async def get(self) -> ReviewChunk:
        async with self._condition:
            while self._size == 0:
                await self._condition.wait()
            review_id = self._rotation.popleft()
            queue = self._queues[review_id]
            chunk = queue.popleft()
            self._size -= 1
            if queue:
                self._rotation.append(review_id)
            else:
                del self._queues[review_id]
            return chunk

    async def cancel_review(self, review_id: str) -> int:
        async with self._condition:
            queue = self._queues.pop(review_id, None)
            if queue is None:
                return 0
            removed = len(queue)
            self._size -= removed
            self._rotation = deque(item for item in self._rotation if item != review_id)
            self._condition.notify_all()
            return removed

    @property
    def size(self) -> int:
        return self._size
