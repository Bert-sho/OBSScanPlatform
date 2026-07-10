import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from obs_scan_platform.aggregation import aggregate_bucket
from obs_scan_platform.config import AppConfigFile, ApplicationConfig, Thresholds, load_config
from obs_scan_platform.csv_store import append_object_rows
from obs_scan_platform.filelist_discovery import FilelistDiscoveryScheduler, FilelistTask
from obs_scan_platform.logging_config import configure_logging
from obs_scan_platform.models import (
    BucketInfo,
    BucketScanResult,
    ObjectRow,
    RootDiscovery,
    ScanStatus,
)
from obs_scan_platform.obs_client import OBSClient, encode_object_key, encode_request_body
from obs_scan_platform.paths import prefix_temp_filename


LOGGER = logging.getLogger(__name__)
JSON_HEADERS = {"Content-Type": "application/json"}


def parse_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_owned_bucket(bucket: BucketInfo) -> bool:
    return bucket.auth == "owner" and bucket.share_from is None


def is_scan_capable_bucket(bucket: BucketInfo) -> bool:
    return bool(bucket.bucket_id and bucket.name and bucket.vendor and bucket.region)


def should_scan_bucket(bucket: BucketInfo, include_shared: bool) -> bool:
    if not is_scan_capable_bucket(bucket):
        return False
    if include_shared:
        return True
    return is_owned_bucket(bucket)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _result_payload(data: dict[str, Any]) -> Any:
    return data.get("result", data)


def _items_from_payload(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for value in payload.values():
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _bucket_items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    items: list[dict[str, Any]] = []
    known_keys = (
        "buckets",
        "bucketList",
        "list",
        "sharedBuckets",
        "shareBuckets",
        "sharedBucketList",
        "shareBucketList",
    )
    for key in known_keys:
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    if items:
        return items
    return _items_from_payload(payload)


def _has_empty_filelist_objects(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("objects"), dict) and not payload["objects"]


def _string_or_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _rollup_status(statuses: list[str]) -> str:
    if not statuses:
        return ScanStatus.SUCCESS.value
    if all(status == ScanStatus.SUCCESS.value for status in statuses):
        return ScanStatus.SUCCESS.value
    if all(status == ScanStatus.FAILED.value for status in statuses):
        return ScanStatus.FAILED.value
    return ScanStatus.PARTIAL_FAILED.value


class Scanner:
    def __init__(self, config: AppConfigFile, *, show_progress: bool = False) -> None:
        self.config = config
        self.show_progress = show_progress
        self.request_semaphore = asyncio.Semaphore(config.scan.global_request_concurrency)

    async def run(self, run_id: str | None = None, appid: str | None = None) -> dict[str, Any]:
        run_id = run_id or _default_run_id()
        results_dir = Path(self.config.scan.results_dir) / run_id
        results_dir.mkdir(parents=True, exist_ok=True)
        configure_logging(results_dir / "scan.log")

        started_ms = _now_ms()
        applications = self.config.enabled_applications()
        if appid is not None:
            applications = [application for application in applications if application.appid == appid]

        LOGGER.info("scan start run_id=%s applications=%s", run_id, len(applications))
        app_semaphore = asyncio.Semaphore(self.config.scan.app_concurrency)

        async def scan_with_limit(application: ApplicationConfig) -> dict[str, Any]:
            async with app_semaphore:
                return await self._scan_application(application, run_id, results_dir, started_ms)

        app_entries = await asyncio.gather(*(scan_with_limit(application) for application in applications))
        ended_ms = _now_ms()
        status = _rollup_status([entry["status"] for entry in app_entries])

        manifest = {
            "run_id": run_id,
            "status": status,
            "started_ms": started_ms,
            "ended_ms": ended_ms,
            "config_path": str(self.config.source_path) if self.config.source_path is not None else None,
            "applications": app_entries,
        }
        (results_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        LOGGER.info("scan finish run_id=%s status=%s", run_id, status)
        return manifest

    async def _scan_application(
        self,
        application: ApplicationConfig,
        run_id: str,
        results_dir: Path,
        scan_started_ms: int,
    ) -> dict[str, Any]:
        LOGGER.info("application start appid=%s", application.appid)
        async with httpx.AsyncClient(timeout=self.config.scan.request_timeout_seconds) as http:
            client = OBSClient(
                http=http,
                request_semaphore=self.request_semaphore,
                max_retries=self.config.scan.max_retries,
                retry_base_delay_seconds=self.config.scan.retry_base_delay_seconds,
                retry_max_delay_seconds=self.config.scan.retry_max_delay_seconds,
            )
            try:
                buckets = await self._list_buckets(application, client)
                bucket_semaphore = asyncio.Semaphore(self.config.scan.bucket_concurrency)

                async def scan_bucket_with_limit(bucket: BucketInfo) -> BucketScanResult:
                    async with bucket_semaphore:
                        try:
                            return await self._scan_bucket(
                                application,
                                bucket,
                                client,
                                run_id,
                                results_dir,
                                scan_started_ms,
                            )
                        except Exception as exc:
                            LOGGER.exception(
                                "bucket failure appid=%s bucket=%s error=unexpected_exception",
                                application.appid,
                                bucket.name,
                            )
                            return BucketScanResult(
                                appid=application.appid,
                                bucket_name=bucket.name,
                                bucket_id=bucket.bucket_id,
                                status=ScanStatus.FAILED,
                                csv_path=None,
                                thresholds=self.config.thresholds_for(application, bucket.name),
                                error=str(exc),
                            )

                bucket_results = await asyncio.gather(*(scan_bucket_with_limit(bucket) for bucket in buckets))
            except Exception as exc:
                LOGGER.exception("application failure appid=%s", application.appid)
                return {
                    "appid": application.appid,
                    "name": application.name,
                    "status": ScanStatus.FAILED.value,
                    "error": str(exc),
                    "buckets": [],
                }

        status = _rollup_status([result.status.value for result in bucket_results])
        LOGGER.info("application finish appid=%s status=%s", application.appid, status)
        return {
            "appid": application.appid,
            "name": application.name,
            "status": status,
            "buckets": [
                self._bucket_result_to_manifest(
                    result,
                    results_dir / self.config.scan.temp_subdir / application.appid / result.bucket_name,
                )
                for result in bucket_results
            ],
        }

    async def _list_buckets(self, application: ApplicationConfig, client: OBSClient) -> list[BucketInfo]:
        data = await client.get_json(
            _endpoint(self.config.endpoint_for(application), "/rest/s3/listbuckets"),
            params={"appid": application.appid},
            headers={**JSON_HEADERS, "csb-token": application.apptoken},
            endpoint="listbuckets",
        )
        buckets: list[BucketInfo] = []
        for item in _bucket_items_from_payload(_result_payload(data)):
            bucket = BucketInfo(
                bucket_id=_string_or_empty(item.get("id")),
                name=_string_or_empty(item.get("name")),
                vendor=_string_or_empty(item.get("vendor")),
                region=_string_or_empty(item.get("region")),
                auth=item.get("auth"),
                share_from=item.get("shareFrom"),
            )
            if should_scan_bucket(bucket, application.scan_shared_buckets):
                buckets.append(bucket)
            elif not is_scan_capable_bucket(bucket):
                LOGGER.warning(
                    "bucket skipped appid=%s bucket=%s reason=missing_required_fields",
                    application.appid,
                    bucket.name or "<missing>",
                )
        return buckets

    async def _list_owned_buckets(self, application: ApplicationConfig, client: OBSClient) -> list[BucketInfo]:
        return await self._list_buckets(application, client)

    async def _scan_bucket(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        client: OBSClient,
        run_id: str,
        results_dir: Path,
        scan_started_ms: int,
    ) -> BucketScanResult:
        bucket_started = time.monotonic()
        LOGGER.info("bucket start appid=%s bucket=%s", application.appid, bucket.name)
        thresholds = self.config.thresholds_for(application, bucket.name)
        temp_dir = results_dir / self.config.scan.temp_subdir / application.appid / bucket.name
        output_path = results_dir / application.appid / f"{bucket.name}.csv"
        try:
            endpoint = await self._get_bucket_endpoint(application, bucket, client)
            discovery = await self._discover_root(application, bucket, client, thresholds)
            await self._collect_metadata_files(application, bucket, endpoint, discovery.metadata_files, temp_dir, client)
            await self._collect_prefixes(application, bucket, endpoint, discovery.prefixes, temp_dir, client)
            aggregate_bucket(
                run_id=run_id,
                appid=application.appid,
                bucket_name=bucket.name,
                bucket_id=bucket.bucket_id,
                temp_dir=temp_dir,
                output_path=output_path,
                thresholds=thresholds,
                scan_started_ms=scan_started_ms,
            )
        except Exception as exc:
            elapsed_seconds = time.monotonic() - bucket_started
            LOGGER.exception(
                "bucket failure appid=%s bucket=%s elapsed_seconds=%.3f",
                application.appid,
                bucket.name,
                elapsed_seconds,
            )
            return BucketScanResult(
                appid=application.appid,
                bucket_name=bucket.name,
                bucket_id=bucket.bucket_id,
                status=ScanStatus.FAILED,
                csv_path=None,
                thresholds=thresholds,
                error=str(exc),
            )

        elapsed_seconds = time.monotonic() - bucket_started
        LOGGER.info(
            "bucket finish appid=%s bucket=%s status=%s elapsed_seconds=%.3f",
            application.appid,
            bucket.name,
            ScanStatus.SUCCESS.value,
            elapsed_seconds,
        )
        return BucketScanResult(
            appid=application.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            status=ScanStatus.SUCCESS,
            csv_path=output_path,
            thresholds=thresholds,
        )

    async def _get_bucket_endpoint(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        client: OBSClient,
    ) -> str:
        data = await client.get_json(
            _endpoint(self.config.endpoint_for(application), "/rest/s3/bucket/endpoint"),
            params={
                "bucketid": bucket.name,
                "token": application.apptoken,
                "vendor": bucket.vendor,
                "region": bucket.region,
                "bucketUid": bucket.bucket_id,
            },
            headers=JSON_HEADERS,
            endpoint="bucket_endpoint",
        )
        result = _result_payload(data)
        if isinstance(result, dict):
            result = result.get("endpoint") or result.get("url") or result.get("value")
        return str(result).rstrip("/")

    async def _discover_root(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        client: OBSClient,
        thresholds: Thresholds | None = None,
    ) -> RootDiscovery:
        thresholds = thresholds or self.config.thresholds_for(application, bucket.name)
        max_depth = max(1, thresholds.filelist_depth)
        task_limit = max(1, self.config.scan.filelist_task_limit_per_bucket)
        url = _endpoint(self.config.endpoint_for(application), "/rest/s3/bucket/filelist")
        scheduler = FilelistDiscoveryScheduler(max_depth=max_depth, task_limit=task_limit)
        progress_bar = (
            self._filelist_progress_bar(application, bucket, scheduler.total_tasks) if self.show_progress else None
        )
        if progress_bar is not None:
            progress_bar.total = scheduler.total_tasks
        try:
            while True:
                tasks = scheduler.current_level()
                if not tasks:
                    break
                await asyncio.gather(
                    *(
                        self._process_filelist_task(
                            application,
                            bucket,
                            client,
                            scheduler,
                            task,
                            url,
                            progress_bar,
                        )
                        for task in tasks
                    )
                )
                if not scheduler.finish_level():
                    break
                if progress_bar is not None:
                    progress_bar.total = scheduler.total_tasks
                    progress_bar.refresh()
        finally:
            if progress_bar is not None:
                progress_bar.close()

        return scheduler.result()

    async def _process_filelist_task(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        client: OBSClient,
        scheduler: FilelistDiscoveryScheduler,
        task: FilelistTask,
        url: str,
        progress_bar: Any | None,
    ) -> None:
        path = task.path
        pointer = ""
        while True:
            request_body = encode_request_body(
                {
                    "id": bucket.bucket_id,
                    "path": path,
                    "pointer": pointer,
                    "size": self.config.scan.page_size,
                }
            )
            data = await client.get_json(
                url,
                params={"appid": application.appid, "requestbody": request_body},
                headers={**JSON_HEADERS, "csb-token": application.apptoken},
                endpoint="filelist",
            )
            payload = _result_payload(data)
            if _has_empty_filelist_objects(payload):
                scheduler.record_empty(task)
                break

            for item in _items_from_payload(payload, "objects", "files", "list", "items"):
                object_type = str(item.get("objectType") or "").lower()
                object_key = item.get("objectKey")
                if object_type == "folder":
                    prefix = self._filelist_folder_prefix(path, object_key or item.get("name"))
                    if prefix:
                        scheduler.record_folder(task, prefix)
                        if progress_bar is not None and progress_bar.total != scheduler.pending_total_tasks:
                            progress_bar.total = scheduler.pending_total_tasks
                            progress_bar.refresh()
                elif object_key:
                    scheduler.record_file(task, str(object_key))

            next_pointer = None
            if isinstance(payload, dict):
                next_pointer = str(payload.get("nextOffset") or "")
            if not next_pointer or next_pointer == pointer:
                break
            pointer = next_pointer

        scheduler.mark_completed(task)
        LOGGER.info(
            "filelist progress appid=%s bucket=%s completed=%s total=%s",
            application.appid,
            bucket.name,
            scheduler.completed_tasks,
            scheduler.pending_total_tasks,
        )
        if progress_bar is not None:
            progress_bar.update(1)

    def _filelist_progress_bar(self, application: ApplicationConfig, bucket: BucketInfo, total: int) -> Any | None:
        if not self.show_progress:
            return None
        from tqdm import tqdm

        return tqdm(total=total, desc=f"{application.appid}/{bucket.name} filelist", unit="dir")

    def _filelist_folder_prefix(self, path: str, value: Any) -> str:
        raw_prefix = str(value or "").strip("/")
        if not raw_prefix:
            return ""
        current_prefix = path.strip("/")
        if current_prefix and not raw_prefix.startswith(f"{current_prefix}/"):
            raw_prefix = f"{current_prefix}/{raw_prefix}"
        if path == "/" and "/" in raw_prefix:
            raw_prefix = raw_prefix.split("/", 1)[0]
        return f"{raw_prefix.rstrip('/')}/"

    async def _collect_metadata_files(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        endpoint: str,
        object_keys: list[str],
        temp_dir: Path,
        client: OBSClient,
    ) -> None:
        rows: list[ObjectRow] = []
        queue: asyncio.Queue[str] = asyncio.Queue()
        for object_key in object_keys:
            queue.put_nowait(object_key)

        async def worker() -> None:
            while True:
                try:
                    object_key = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    data = await client.get_json(
                        _endpoint(endpoint, "/rest/boto3/s3/object/metadata"),
                        params={
                            "vendor": bucket.vendor,
                            "region": bucket.region,
                            "bucketid": bucket.name,
                            "apptoken": application.apptoken,
                            "objectkey": encode_object_key("/" + object_key.lstrip("/")),
                            "bucketld": bucket.bucket_id,
                        },
                        headers=JSON_HEADERS,
                        endpoint="metadata",
                    )
                    row = self._metadata_to_object_row(object_key, data)
                    if row is not None:
                        rows.append(row)
                finally:
                    queue.task_done()

        worker_count = min(max(1, self.config.scan.metadata_concurrency_per_bucket), len(object_keys))
        if worker_count:
            await asyncio.gather(*(worker() for _ in range(worker_count)))
        if rows:
            append_object_rows(temp_dir / "metadata_files.csv", rows)

    async def _collect_prefixes(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        endpoint: str,
        prefixes: list[str],
        temp_dir: Path,
        client: OBSClient,
    ) -> None:
        queue: asyncio.Queue[str] = asyncio.Queue()
        for prefix in prefixes:
            queue.put_nowait(prefix)

        async def worker() -> None:
            while True:
                try:
                    prefix = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await self._collect_prefix(application, bucket, endpoint, prefix, temp_dir, client)
                finally:
                    queue.task_done()

        worker_count = min(max(1, self.config.scan.objectkeys_concurrency_limit()), len(prefixes))
        if worker_count:
            await asyncio.gather(*(worker() for _ in range(worker_count)))

    async def _collect_prefix(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        endpoint: str,
        prefix: str,
        temp_dir: Path,
        client: OBSClient,
    ) -> None:
        next_marker = ""
        url = _endpoint(endpoint, "/rest/boto3/s3/list/bucket/objectkeys")
        while True:
            data = await client.get_json(
                url,
                params={
                    "vendor": bucket.vendor,
                    "region": bucket.region,
                    "bucketid": bucket.name,
                    "apptoken": application.apptoken,
                    "objectkey": encode_object_key("/" + prefix.lstrip("/")),
                    "nextmarker": next_marker,
                    "bucketld": bucket.bucket_id,
                },
                headers=JSON_HEADERS,
                endpoint="objectkeys",
            )
            payload = _result_payload(data)
            rows = [
                row
                for row in (
                    self._object_key_to_row(item)
                    for item in _items_from_payload(payload, "objectkeys", "objectKeys", "list")
                )
                if row is not None
            ]
            if rows:
                append_object_rows(temp_dir / prefix_temp_filename(prefix), rows)

            truncated = payload.get("truncated") if isinstance(payload, dict) else None
            if str(truncated).lower() != "true":
                break
            new_marker = str(payload.get("nextmarker") or payload.get("nextMarker") or "")
            if not new_marker or new_marker == next_marker:
                break
            next_marker = new_marker

    def _metadata_to_object_row(self, object_key: str, data: dict[str, Any]) -> ObjectRow | None:
        payload = _result_payload(data)
        if isinstance(payload, dict) and isinstance(payload.get("objectKey"), dict):
            payload = payload["objectKey"]
        if not isinstance(payload, dict):
            return None
        size = parse_int_or_none(payload.get("size"))
        if size is None:
            return None
        return ObjectRow(
            object_key=str(payload.get("objectKey") or payload.get("key") or object_key),
            size_bytes=size,
            last_modified_ms=parse_int_or_none(payload.get("lastModifyTime")),
        )

    def _object_key_to_row(self, item: dict[str, Any]) -> ObjectRow | None:
        size = parse_int_or_none(item.get("size"))
        object_key = item.get("objectKey") or item.get("key")
        if size is None or not object_key:
            return None
        return ObjectRow(
            object_key=str(object_key),
            size_bytes=size,
            last_modified_ms=parse_int_or_none(item.get("lastModifyTime")),
        )

    def _bucket_result_to_manifest(self, result: BucketScanResult, temp_dir: Path | None = None) -> dict[str, Any]:
        manifest = {
            "bucket_name": result.bucket_name,
            "bucket_id": result.bucket_id,
            "status": result.status.value,
            "csv_path": str(result.csv_path) if result.csv_path is not None else None,
            "thresholds": result.thresholds.model_dump(mode="json"),
            "error": result.error,
        }
        if result.partial_errors is not None and result.partial_errors.has_errors():
            manifest["partial_errors"] = result.partial_errors.to_manifest()
        if temp_dir is None:
            return manifest
        if result.status == ScanStatus.SUCCESS and not self.config.scan.keep_temp_files:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            return manifest
        manifest["temp_dir"] = str(temp_dir)
        return manifest


async def run_scan(
    config_path: str | Path,
    *,
    run_id: str | None = None,
    appid: str | None = None,
    show_progress: bool = False,
) -> dict[str, Any]:
    scanner = Scanner(load_config(config_path), show_progress=show_progress)
    return await scanner.run(run_id=run_id, appid=appid)
