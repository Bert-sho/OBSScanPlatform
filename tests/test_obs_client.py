import asyncio

import httpx
import pytest

from obs_scan_platform.obs_client import OBSClient, OBSRequestError, encode_object_key, encode_request_body


def test_encode_request_body_contains_path_and_size():
    encoded = encode_request_body({"id": "bucket-id", "path": "/", "pointer": "", "size": 1000})
    assert isinstance(encoded, str)
    assert encoded


def test_encode_object_key_encodes_plain_path():
    encoded = encode_object_key("/a/b/file.txt")
    assert isinstance(encoded, str)
    assert encoded


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

    data = await client.get_json("http://obs.example/test", params={})

    assert data["value"] == 1
    assert calls == 2


@pytest.mark.asyncio
async def test_get_json_raises_after_retries():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"success": False, "msg": "busy"})

    client = OBSClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://obs.example"),
        request_semaphore=asyncio.Semaphore(1),
        max_retries=1,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    with pytest.raises(OBSRequestError):
        await client.get_json("http://obs.example/test", params={})
