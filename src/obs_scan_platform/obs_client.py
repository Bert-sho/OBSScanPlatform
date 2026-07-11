import asyncio
import base64
import json
import logging
from typing import Any

import httpx

LOGGER = logging.getLogger(__name__)
MAX_RESPONSE_BODY_CHARS = 2048


class OBSRequestError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint: str,
        status_code: int | None,
        reason: str,
        url: str = "",
        response_body: str = "<no response>",
        response_body_truncated: bool = False,
        response_body_original_chars: int = 0,
        exception_type: str = "OBSRequestError",
        attempts: int = 1,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.reason = reason
        self.url = url
        self.response_body = response_body
        self.response_body_truncated = response_body_truncated
        self.response_body_original_chars = response_body_original_chars
        self.exception_type = exception_type
        self.attempts = attempts
        status = "unknown" if status_code is None else str(status_code)
        super().__init__(f"OBS request failed endpoint={endpoint} status={status} reason={reason}")


def _bounded_body(text: str) -> tuple[str, bool, int]:
    original_chars = len(text)
    return text[:MAX_RESPONSE_BODY_CHARS], original_chars > MAX_RESPONSE_BODY_CHARS, original_chars


def _is_retryable(error: OBSRequestError) -> bool:
    if error.status_code is None:
        return True
    if error.status_code in (408, 429) or error.status_code >= 500:
        return True
    return error.exception_type in {"OBSBusinessError", "InvalidJSON"}


def _log_failed_attempt(error: OBSRequestError, max_attempts: int) -> None:
    LOGGER.warning(
        "OBS request attempt failed endpoint=%s attempt=%s max_attempts=%s status=%s "
        "reason=%s url=%s response_body=%s response_body_truncated=%s "
        "response_body_original_chars=%s exception_type=%s",
        error.endpoint,
        error.attempts,
        max_attempts,
        "unknown" if error.status_code is None else error.status_code,
        error.reason,
        error.url,
        error.response_body,
        str(error.response_body_truncated).lower(),
        error.response_body_original_chars,
        error.exception_type,
    )


def _response_reason(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.reason_phrase or f"HTTP {response.status_code}"
    if isinstance(data, dict):
        value = data.get("msg") or data.get("message") or data.get("error")
        if value:
            return str(value)[:200]
    return response.reason_phrase


def _json_failure_reason(data: dict[str, Any]) -> str:
    payload = _payload_from_data(data)
    payload_reason = payload.get("msg") or payload.get("message") or payload.get("error")
    return str(data.get("msg") or data.get("message") or data.get("error") or payload_reason or "OBS returned success=false")[
        :200
    ]


def _payload_from_data(data: dict[str, Any]) -> Any:
    payload = data.get("result")
    return payload if isinstance(payload, dict) else data


def _has_failure_reason(data: dict[str, Any]) -> bool:
    payload = _payload_from_data(data)
    payload_reason = payload.get("msg") or payload.get("message") or payload.get("error")
    return bool(data.get("msg") or data.get("message") or data.get("error") or payload_reason)


def _has_empty_filelist_objects(data: dict[str, Any]) -> bool:
    payload = _payload_from_data(data)
    objects = payload.get("objects")
    return isinstance(objects, dict) and not objects


def _has_empty_objectkeys(data: dict[str, Any]) -> bool:
    payload = _payload_from_data(data)
    for key in ("objectkeys", "objectKeys", "list"):
        value = payload.get(key)
        if isinstance(value, list) and not value:
            return True
        if isinstance(value, dict) and not value:
            return True
    return False


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
        max_attempts = self.max_retries + 1
        for attempt_number in range(1, max_attempts + 1):
            request = self.http.build_request("GET", url, params=params, headers=headers)
            try:
                async with self.request_semaphore:
                    response = await self.http.send(request)
                if response.status_code >= 400:
                    body, truncated, original_chars = _bounded_body(response.text)
                    raise OBSRequestError(
                        endpoint=endpoint,
                        status_code=response.status_code,
                        reason=_response_reason(response),
                        url=str(request.url),
                        response_body=body,
                        response_body_truncated=truncated,
                        response_body_original_chars=original_chars,
                        exception_type="HTTPStatusError",
                        attempts=attempt_number,
                    )
                try:
                    data = response.json()
                except ValueError as exc:
                    body, truncated, original_chars = _bounded_body(response.text)
                    raise OBSRequestError(
                        endpoint=endpoint,
                        status_code=response.status_code,
                        reason=f"Invalid JSON: {exc}",
                        url=str(request.url),
                        response_body=body,
                        response_body_truncated=truncated,
                        response_body_original_chars=original_chars,
                        exception_type="InvalidJSON",
                        attempts=attempt_number,
                    ) from exc
                success = data.get("success")
                if success is False or (isinstance(success, str) and success.lower() == "false"):
                    if not _has_failure_reason(data) and endpoint == "filelist" and _has_empty_filelist_objects(data):
                        return data
                    if not _has_failure_reason(data) and endpoint == "objectkeys" and _has_empty_objectkeys(data):
                        return data
                    body, truncated, original_chars = _bounded_body(response.text)
                    raise OBSRequestError(
                        endpoint=endpoint,
                        status_code=response.status_code,
                        reason=_json_failure_reason(data),
                        url=str(request.url),
                        response_body=body,
                        response_body_truncated=truncated,
                        response_body_original_chars=original_chars,
                        exception_type="OBSBusinessError",
                        attempts=attempt_number,
                    )
                return data
            except OBSRequestError as error:
                _log_failed_attempt(error, max_attempts)
                if not _is_retryable(error) or attempt_number == max_attempts:
                    raise
            except httpx.RequestError as exc:
                error = OBSRequestError(
                    endpoint=endpoint,
                    status_code=None,
                    reason=f"{exc.__class__.__name__}: {exc}",
                    url=str(request.url),
                    response_body="<no response>",
                    response_body_truncated=False,
                    response_body_original_chars=0,
                    exception_type=exc.__class__.__name__,
                    attempts=attempt_number,
                )
                _log_failed_attempt(error, max_attempts)
                if attempt_number == max_attempts:
                    raise error from exc
            delay = min(
                self.retry_max_delay_seconds,
                self.retry_base_delay_seconds * (2 ** (attempt_number - 1)),
            )
            if delay > 0:
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    async def close(self) -> None:
        await self.http.aclose()
