import asyncio
import base64
import json
from typing import Any

import httpx


class OBSRequestError(RuntimeError):
    pass


def encode_request_body(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def encode_object_key(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("utf-8")


class OBSClient:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        request_semaphore: asyncio.Semaphore,
        max_retries: int,
        retry_base_delay_seconds: float,
        retry_max_delay_seconds: float,
    ) -> None:
        self.http = http
        self.request_semaphore = request_semaphore
        self.max_retries = max_retries
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self.request_semaphore:
                    response = await self.http.get(url, params=params, headers=headers)
                if response.status_code >= 500:
                    raise OBSRequestError(f"HTTP {response.status_code}: {response.text}")
                response.raise_for_status()
                data = response.json()
                success = data.get("success")
                if success in (False, "false"):
                    raise OBSRequestError(str(data.get("msg") or data))
                return data
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                OBSRequestError,
                httpx.HTTPStatusError,
            ) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = min(
                    self.retry_max_delay_seconds,
                    self.retry_base_delay_seconds * (2**attempt),
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        raise OBSRequestError(str(last_error))

    async def close(self) -> None:
        await self.http.aclose()
