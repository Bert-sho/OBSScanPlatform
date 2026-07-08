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
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_json("http://obs.example/test", params={})
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
