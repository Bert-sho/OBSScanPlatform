import asyncio
import base64
import json

import httpx
import pytest

from obs_scan_platform.obs_client import OBSClient, OBSRequestError, encode_object_key, encode_request_body


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
        request_semaphore=asyncio.Semaphore(1),
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
        request_semaphore=asyncio.Semaphore(1),
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
        request_semaphore=asyncio.Semaphore(1),
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
        request_semaphore=asyncio.Semaphore(1),
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
        request_semaphore=asyncio.Semaphore(1),
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
async def test_get_json_404_error_is_sanitized_and_has_endpoint_label():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"success": False, "msg": "missing"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=3,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json(
                "http://obs.example/test?token=secret-token",
                params={"requestbody": "encoded-secret-body"},
                endpoint="filelist",
            )
    finally:
        await client.close()

    message = str(exc_info.value)
    assert "endpoint=filelist" in message
    assert "status=404" in message
    assert "missing" in message
    assert "http://obs.example" not in message
    assert "secret-token" not in message
    assert "encoded-secret-body" not in message


@pytest.mark.asyncio
async def test_get_json_503_error_after_retries_is_sanitized():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"success": False, "msg": "busy"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=1,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json("http://obs.example/test", params={}, endpoint="objectkeys")
    finally:
        await client.close()

    assert calls == 2
    assert str(exc_info.value) == "OBS request failed endpoint=objectkeys status=503 reason=busy"


@pytest.mark.asyncio
async def test_get_json_non_json_error_body_is_sanitized():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            text="upstream failed for https://obs.example/test?token=secret-token requestbody=encoded-secret-body",
        )

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=0,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json(
                "http://obs.example/test?token=secret-token",
                params={"requestbody": "encoded-secret-body"},
                endpoint="objectkeys",
            )
    finally:
        await client.close()

    message = str(exc_info.value)
    assert "endpoint=objectkeys" in message
    assert "status=503" in message
    assert "Service Unavailable" in message
    assert "obs.example" not in message
    assert "secret-token" not in message
    assert "encoded-secret-body" not in message
    assert "requestbody" not in message
    assert "upstream failed for" not in message


@pytest.mark.asyncio
async def test_get_json_connect_error_is_sanitized():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect boom https://obs.example/path?token=secret-token")

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=0,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json(
                "http://obs.example/test?token=secret-token",
                params={"requestbody": "encoded-secret-body"},
                endpoint="metadata",
            )
    finally:
        await client.close()

    message = str(exc_info.value)
    assert "endpoint=metadata" in message
    assert "status=unknown" in message
    assert "reason=ConnectError" in message
    assert "obs.example" not in message
    assert "secret-token" not in message
    assert "encoded-secret-body" not in message
    assert "connect boom" not in message


@pytest.mark.asyncio
async def test_get_json_success_false_error_is_sanitized():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "msg": "permission denied"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=0,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    try:
        with pytest.raises(OBSRequestError) as exc_info:
            await client.get_json("http://obs.example/test", params={"token": "secret"}, endpoint="metadata")
    finally:
        await client.close()

    assert str(exc_info.value) == "OBS request failed endpoint=metadata status=200 reason=permission denied"


@pytest.mark.asyncio
async def test_get_json_returns_empty_filelist_objects_even_when_success_false():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"success": False, "msg": "", "objects": {}})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
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
        request_semaphore=asyncio.Semaphore(1),
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
        request_semaphore=asyncio.Semaphore(1),
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
