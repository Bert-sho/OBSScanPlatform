import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ScanPhaseCoordinator:
    def __init__(self, max_concurrent_requests: int) -> None:
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be at least 1")
        self._request_semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._condition = asyncio.Condition()
        self._active_request_attempts = 0
        self._waiting_aggregations = 0
        self._aggregation_active = False

    @asynccontextmanager
    async def request_attempt(self) -> AsyncIterator[None]:
        async with self._request_semaphore:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: not self._aggregation_active
                    and self._waiting_aggregations == 0
                )
                self._active_request_attempts += 1
            try:
                yield
            finally:
                async with self._condition:
                    self._active_request_attempts -= 1
                    if self._active_request_attempts == 0:
                        self._condition.notify_all()

    @asynccontextmanager
    async def aggregation(self) -> AsyncIterator[None]:
        admitted = False
        async with self._condition:
            self._waiting_aggregations += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._aggregation_active
                    and self._active_request_attempts == 0
                )
                self._aggregation_active = True
                admitted = True
            finally:
                self._waiting_aggregations -= 1
                if not admitted:
                    self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._aggregation_active = False
                self._condition.notify_all()
