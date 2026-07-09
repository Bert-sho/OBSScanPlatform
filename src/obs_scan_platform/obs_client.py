import asyncio
import base64
import json
from typing import Any

import httpx


class OBSRequestError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint: str,
        status_code: int | None,
        reason: str,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.reason = reason
        status = "unknown" if status_code is None else str(status_code)
        super().__init__(f"OBS request failed endpoint={endpoint} status={status} reason={reason}")


def _response_reason(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:200] if text else response.reason_phrase
    if isinstance(data, dict):
        value = data.get("msg") or data.get("message") or data.get("error")
        if value:
            return str(value)[:200]
    return response.reason_phrase


def _json_failure_reason(data: dict[str, Any]) -> str:
    return str(data.get("msg") or data.get("message") or data.get("error") or "OBS returned success=false")[:200]


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
        endpoint: str = "unknown",
    ) -> dict[str, Any]:
        last_error: OBSRequestError | httpx.TimeoutException | httpx.ConnectError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self.request_semaphore:
                    response = await self.http.get(url, params=params, headers=headers)
                if response.status_code >= 400:
                    error = OBSRequestError(
                        endpoint=endpoint,
                        status_code=response.status_code,
                        reason=_response_reason(response),
                    )
                    raise error
                data = response.json()
                success = data.get("success")
                if success in (False, "false"):
                    raise OBSRequestError(
                        endpoint=endpoint,
                        status_code=response.status_code,
                        reason=_json_failure_reason(data),
                    )
                return data
            except OBSRequestError as exc:
                last_error = exc
                if exc.status_code is not None and 400 <= exc.status_code < 500:
                    raise
                if attempt >= self.max_retries:
                    break
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
            delay = min(
                self.retry_max_delay_seconds,
                self.retry_base_delay_seconds * (2**attempt),
            )
            if delay > 0:
                await asyncio.sleep(delay)
        if isinstance(last_error, OBSRequestError):
            raise last_error
        reason = last_error.__class__.__name__ if last_error is not None else "unknown"
        raise OBSRequestError(endpoint=endpoint, status_code=None, reason=reason)

    async def close(self) -> None:
        await self.http.aclose()
