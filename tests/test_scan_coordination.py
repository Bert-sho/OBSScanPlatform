import asyncio

import pytest

from obs_scan_platform.scan_coordination import ScanPhaseCoordinator


@pytest.mark.asyncio
async def test_aggregation_drains_active_request_and_blocks_late_request():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=2)
    active_entered = asyncio.Event()
    release_active = asyncio.Event()
    aggregation_entered = asyncio.Event()
    release_aggregation = asyncio.Event()
    late_entered = asyncio.Event()
    events: list[str] = []

    async def active_request() -> None:
        async with coordinator.request_attempt():
            events.append("active-request-enter")
            active_entered.set()
            await release_active.wait()
            events.append("active-request-exit")

    async def aggregate() -> None:
        async with coordinator.aggregation():
            events.append("aggregation-enter")
            aggregation_entered.set()
            await release_aggregation.wait()
            events.append("aggregation-exit")

    async def late_request() -> None:
        async with coordinator.request_attempt():
            events.append("late-request-enter")
            late_entered.set()

    active_task = asyncio.create_task(active_request())
    await active_entered.wait()
    aggregation_task = asyncio.create_task(aggregate())
    await asyncio.sleep(0)
    late_task = asyncio.create_task(late_request())
    await asyncio.sleep(0)

    assert not aggregation_entered.is_set()
    assert not late_entered.is_set()

    release_active.set()
    await aggregation_entered.wait()
    assert not late_entered.is_set()

    release_aggregation.set()
    await asyncio.gather(active_task, aggregation_task, late_task)

    assert events == [
        "active-request-enter",
        "active-request-exit",
        "aggregation-enter",
        "aggregation-exit",
        "late-request-enter",
    ]


@pytest.mark.asyncio
async def test_queued_aggregations_run_serially_before_requests_resume():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=3)
    active_request_entered = asyncio.Event()
    release_active_request = asyncio.Event()
    aggregation_entries: asyncio.Queue[str] = asyncio.Queue()
    aggregation_releases = {
        "aggregation-1": asyncio.Event(),
        "aggregation-2": asyncio.Event(),
    }
    late_request_entered = asyncio.Event()
    active_aggregations = 0
    max_active_aggregations = 0

    async def active_request() -> None:
        async with coordinator.request_attempt():
            active_request_entered.set()
            await release_active_request.wait()

    async def aggregate(name: str) -> None:
        nonlocal active_aggregations, max_active_aggregations
        async with coordinator.aggregation():
            active_aggregations += 1
            max_active_aggregations = max(max_active_aggregations, active_aggregations)
            aggregation_entries.put_nowait(name)
            await aggregation_releases[name].wait()
            active_aggregations -= 1

    async def late_request() -> None:
        async with coordinator.request_attempt():
            late_request_entered.set()

    active_task = asyncio.create_task(active_request())
    await active_request_entered.wait()
    aggregation_tasks = [
        asyncio.create_task(aggregate("aggregation-1")),
        asyncio.create_task(aggregate("aggregation-2")),
    ]
    await asyncio.sleep(0)
    late_task = asyncio.create_task(late_request())
    await asyncio.sleep(0)

    release_active_request.set()
    first_name = await aggregation_entries.get()
    assert not late_request_entered.is_set()
    aggregation_releases[first_name].set()
    second_name = await aggregation_entries.get()
    assert {first_name, second_name} == {"aggregation-1", "aggregation-2"}
    assert not late_request_entered.is_set()
    aggregation_releases[second_name].set()

    await asyncio.gather(active_task, *aggregation_tasks, late_task)
    assert max_active_aggregations == 1
    assert late_request_entered.is_set()


@pytest.mark.asyncio
async def test_cancelled_aggregation_waiter_reopens_request_admission():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=1)
    release_request = asyncio.Event()
    request_entered = asyncio.Event()

    async def active_request() -> None:
        async with coordinator.request_attempt():
            request_entered.set()
            await release_request.wait()

    active_task = asyncio.create_task(active_request())
    await request_entered.wait()
    waiting_writer = asyncio.create_task(_enter_aggregation(coordinator))
    await asyncio.sleep(0)
    waiting_writer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_writer
    release_request.set()
    await active_task

    async with coordinator.request_attempt():
        admitted_after_cancel = True

    assert admitted_after_cancel is True


async def _enter_aggregation(coordinator: ScanPhaseCoordinator) -> None:
    async with coordinator.aggregation():
        return


@pytest.mark.asyncio
async def test_aggregation_error_reopens_request_admission():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=1)

    with pytest.raises(RuntimeError, match="aggregation failed"):
        async with coordinator.aggregation():
            raise RuntimeError("aggregation failed")

    async with coordinator.request_attempt():
        admitted_after_error = True

    assert admitted_after_error is True


@pytest.mark.asyncio
async def test_cancelled_active_request_allows_aggregation():
    coordinator = ScanPhaseCoordinator(max_concurrent_requests=1)
    request_entered = asyncio.Event()
    never_release = asyncio.Event()

    async def request() -> None:
        async with coordinator.request_attempt():
            request_entered.set()
            await never_release.wait()

    request_task = asyncio.create_task(request())
    await request_entered.wait()
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    async with coordinator.aggregation():
        aggregation_entered = True

    assert aggregation_entered is True
