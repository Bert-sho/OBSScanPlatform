import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from obs_scan_platform import obs_client as obs_client_module
from obs_scan_platform.obs_client import OBSClient, OBSRequestError, encode_object_key, encode_request_body
from obs_scan_platform.scan_coordination import ScanPhaseCoordinator


def make_client(
    handler,
    *,
    max_retries: int = 3,
    retry_base_delay_seconds: float = 0,
    phase_coordinator: ScanPhaseCoordinator | None = None,
) -> OBSClient:
    return OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://obs.example"),
        phase_coordinator=phase_coordinator or ScanPhaseCoordinator(1),
        max_retries=max_retries,
        retry_base_delay_seconds=retry_base_delay_seconds,
        retry_max_delay_seconds=retry_base_delay_seconds,
    )


class RecordingCoordinator(ScanPhaseCoordinator):
    def __init__(self, events: list[str]) -> None:
        super().__init__(1)
        self.events = events

    @asynccontextmanager
    async def request_attempt(self) -> AsyncIterator[None]:
        self.events.append("scope-enter")
        async with super().request_attempt():
            yield
        self.events.append("scope-exit")


@pytest.mark.asyncio
async def test_get_json_builds_request_and_validates_response_inside_request_attempt(
    monkeypatch: pytest.MonkeyPatch,
):
    events: list[str] = []
    original_has_failure_reason = obs_client_module._has_failure_reason
    original_json = httpx.Response.json

    def recording_has_failure_reason(data: dict) -> bool:
        events.append("business-validation")
        return original_has_failure_reason(data)

    def recording_json(response: httpx.Response, **kwargs):
        events.append("json")
        return original_json(response, **kwargs)

    async def handler(request: httpx.Request) -> httpx.Response:
        events.append("send")
        return httpx.Response(
            200,
            json={"success": False, "result": {"objects": {}}},
            request=request,
        )

    monkeypatch.setattr(obs_client_module, "_has_failure_reason", recording_has_failure_reason)
    monkeypatch.setattr(httpx.Response, "json", recording_json)
    client = make_client(handler, phase_coordinator=RecordingCoordinator(events))
    original_build_request = client.http.build_request

    def recording_build_request(*args, **kwargs):
        events.append("build-request")
        return original_build_request(*args, **kwargs)

    monkeypatch.setattr(client.http, "build_request", recording_build_request)
    try:
        assert await client.get_json("/test", params={}, endpoint="filelist") == {
            "success": False,
            "result": {"objects": {}},
        }
    finally:
        await client.close()

    assert events == [
        "scope-enter",
        "build-request",
        "send",
        "json",
        "business-validation",
        "scope-exit",
    ]


@pytest.mark.asyncio
async def test_get_json_retry_waits_for_aggregation():
    coordinator = ScanPhaseCoordinator(1)
    first_attempt_seen = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_attempt_seen.set()
            return httpx.Response(503, json={"success": False, "msg": "busy"})
        return httpx.Response(200, json={"success": True, "value": 1})

    client = make_client(
        handler,
        max_retries=1,
        retry_base_delay_seconds=0.05,
        phase_coordinator=coordinator,
    )
    request_task = asyncio.create_task(client.get_json("/test", params={}))
    try:
        await first_attempt_seen.wait()
        await asyncio.sleep(0)
        async with coordinator.aggregation():
            await asyncio.sleep(0.06)
            assert calls == 1

        data = await request_task
    finally:
        await client.close()

    assert data["value"] == 1
    assert calls == 2


def test_encode_request_body_roundtrips_payload():
    payload = {"id": "bucket-id", "path": "/中文/", "pointer": "", "size": 1000}

    encoded = encode_request_body(payload)
    decoded = json.loads(base64.urlsafe_b64decode(encoded.encode("utf-8")).decode("utf-8"))

    assert decoded == payload


def test_encode_object_key_roundtrips_plain_path():
    path = "/a/中文 file.txt"

    encoded = encode_object_key(path)
    decoded = base64.urlsafe_b64decode(encoded.encode("utf-8")).decode("utf-8")

    assert decoded == path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_calls"),
    [(408, 4), (429, 4), (503, 4), (400, 1), (401, 1), (403, 1), (404, 1)],
)
async def test_get_json_classifies_retryable_http_statuses(status_code: int, expected_calls: int):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text="upstream failure", request=request)

    client = make_client(handler)
    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json("/test", params={"token": "raw-token"}, endpoint="filelist")
    finally:
        await client.close()

    assert calls == expected_calls
    assert exc_info.value.attempts == expected_calls


@pytest.mark.asyncio
async def test_get_json_retries_success_false_and_invalid_json():
    responses = [
        httpx.Response(200, json={"success": False, "msg": "busy"}),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"success": True, "value": 1}),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        response.request = request
        return response

    client = make_client(handler)
    try:
        data = await client.get_json("/test", params={}, endpoint="metadata")
    finally:
        await client.close()

    assert data["value"] == 1
    assert responses == []


@pytest.mark.asyncio
async def test_get_json_timeout_has_full_request_context():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timed out", request=request)

    client = make_client(handler, max_retries=0)
    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json("/test", params={"token": "raw-token"}, endpoint="metadata")
    finally:
        await client.close()

    error = exc_info.value
    assert error.status_code is None
    assert error.url == "https://obs.example/test?token=raw-token"
    assert error.response_body == "<no response>"
    assert error.exception_type == "ReadTimeout"
    assert error.attempts == 1


@pytest.mark.asyncio
async def test_get_json_logs_unredacted_failed_attempt(caplog: pytest.LogCaptureFixture):
    body = "x" * 2050

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=body, request=request)

    client = make_client(handler, max_retries=0)
    try:
        with caplog.at_level(logging.WARNING, logger="obs_scan_platform.obs_client"):
            with pytest.raises(OBSRequestError) as exc_info:
                await client.get_json(
                    "/test",
                    params={"token": "raw-token", "requestbody": "raw-body"},
                    endpoint="objectkeys",
                )
    finally:
        await client.close()

    error = exc_info.value
    assert len(error.response_body) == 2048
    assert error.response_body_truncated is True
    assert error.response_body_original_chars == 2050
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "https://obs.example/test?token=raw-token&requestbody=raw-body" in message
    assert "response_body=" + ("x" * 2048) in message
    assert "response_body_truncated=true" in message
    assert "response_body_original_chars=2050" in message
    assert "attempt=1 max_attempts=1" in message


@pytest.mark.asyncio
async def test_get_json_retries_503_then_succeeds():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"success": False, "msg": "busy"})
        return httpx.Response(200, json={"success": True, "value": 1})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        phase_coordinator=ScanPhaseCoordinator(1),
        max_retries=2,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        data = await client.get_json("http://obs.example/test", params={})
    finally:
        await client.close()

    assert data["value"] == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_get_json_raises_after_retries():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"success": False, "msg": "busy"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        phase_coordinator=ScanPhaseCoordinator(1),
        max_retries=1,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError):
            await client.get_json("http://obs.example/test", params={})
    finally:
        await client.close()

    assert calls == 2


@pytest.mark.asyncio
async def test_get_json_does_not_retry_404():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"success": False, "msg": "missing"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        phase_coordinator=ScanPhaseCoordinator(1),
        max_retries=3,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError):
            await client.get_json("http://obs.example/test", params={}, endpoint="filelist")
    finally:
        await client.close()

    assert calls == 1


@pytest.mark.asyncio
async def test_get_json_returns_plain_json_without_success_field():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": 1})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        phase_coordinator=ScanPhaseCoordinator(1),
        max_retries=1,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        data = await client.get_json("http://obs.example/test", params={})
    finally:
        await client.close()

    assert data == {"value": 1}


@pytest.mark.asyncio
async def test_get_json_retries_success_false_then_succeeds():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"success": False, "msg": "busy"})
        return httpx.Response(200, json={"success": True, "value": 1})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        phase_coordinator=ScanPhaseCoordinator(1),
        max_retries=1,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        data = await client.get_json("http://obs.example/test", params={})
    finally:
        await client.close()

    assert data["value"] == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_get_json_returns_empty_filelist_objects_even_when_success_false():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"success": False, "msg": "", "objects": {}})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        phase_coordinator=ScanPhaseCoordinator(1),
        max_retries=3,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        data = await client.get_json("http://obs.example/test", params={}, endpoint="filelist")
    finally:
        await client.close()

    assert data == {"success": False, "msg": "", "objects": {}}
    assert calls == 1


@pytest.mark.asyncio
async def test_get_json_returns_empty_objectkeys_even_when_success_false():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "success": False,
                "objectKeys": [],
                "truncated": "false",
            },
        )

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        phase_coordinator=ScanPhaseCoordinator(1),
        max_retries=3,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        data = await client.get_json("http://obs.example/test", params={}, endpoint="objectkeys")
    finally:
        await client.close()

    assert data == {"success": False, "objectKeys": [], "truncated": "false"}
    assert calls == 1


@pytest.mark.asyncio
async def test_get_json_empty_objectkeys_still_raises_when_result_has_error_reason():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": False,
                "result": {
                    "objectKeys": [],
                    "truncated": "false",
                    "message": "permission denied",
                },
            },
        )

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        phase_coordinator=ScanPhaseCoordinator(1),
        max_retries=0,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json("http://obs.example/test", params={}, endpoint="objectkeys")
    finally:
        await client.close()

    assert str(exc_info.value) == "OBS request failed endpoint=objectkeys status=200 reason=permission denied"
