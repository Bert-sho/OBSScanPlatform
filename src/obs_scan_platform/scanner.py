import asyncio
import json
import logging
import shutil
import time
from dataclasses import replace
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
    ObjectkeysProgress,
    PartialErrorSummary,
    RequestFailureDetail,
    RootDiscovery,
    ScanStatus,
    _sanitize_reason,
)
from obs_scan_platform.obs_client import OBSClient, OBSRequestError, encode_object_key, encode_request_body
from obs_scan_platform.parquet_aggregation import aggregate_bucket_parquet
from obs_scan_platform.paths import prefix_temp_filename
from obs_scan_platform.scan_coordination import ScanPhaseCoordinator


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


def _iso_utc(timestamp_ms: int) -> str:
    value = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _gather_cancel_on_error(*coroutines: Any) -> list[Any]:
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


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
        self.phase_coordinator = ScanPhaseCoordinator(config.scan.global_request_concurrency)

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
        bucket_semaphore = asyncio.Semaphore(self.config.scan.bucket_concurrency)

        app_entries = await asyncio.gather(
            *(
                self._scan_application(
                    application,
                    run_id,
                    results_dir,
                    started_ms,
                    bucket_semaphore,
                )
                for application in applications
            )
        )
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
        bucket_semaphore: asyncio.Semaphore,
    ) -> dict[str, Any]:
        LOGGER.info("application start appid=%s", application.appid)
        missing_fields = self.config.missing_scan_fields(application)
        if missing_fields:
            error = f"missing required scan configuration: {', '.join(missing_fields)}"
            LOGGER.error(
                "application failure appid=%s error=missing_required_scan_configuration fields=%s",
                application.appid or "<missing>",
                ",".join(missing_fields),
            )
            return {
                "appid": application.appid,
                "name": application.name,
                "status": ScanStatus.FAILED.value,
                "error": error,
                "buckets": [],
            }
        async with httpx.AsyncClient(
            timeout=self.config.scan.request_timeout_seconds,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=self.config.scan.keepalive_expiry_seconds,
            ),
        ) as http:
            client = OBSClient(
                http=http,
                phase_coordinator=self.phase_coordinator,
                max_retries=self.config.scan.max_retries,
                retry_base_delay_seconds=self.config.scan.retry_base_delay_seconds,
                retry_max_delay_seconds=self.config.scan.retry_max_delay_seconds,
            )
            try:
                buckets = await self._list_buckets(application, client)

                async def scan_bucket_with_limit(bucket: BucketInfo) -> BucketScanResult:
                    async with bucket_semaphore:
                        started_ms = _now_ms()
                        started_monotonic = time.monotonic()
                        try:
                            result = await self._scan_bucket(
                                application,
                                bucket,
                                client,
                                run_id,
                                results_dir,
                                scan_started_ms,
                            )
                        except Exception as exc:
                            ended_ms = _now_ms()
                            elapsed_seconds = time.monotonic() - started_monotonic
                            LOGGER.exception(
                                "bucket failure appid=%s bucket=%s error=unexpected_exception",
                                application.appid,
                                bucket.name,
                            )
                            result = BucketScanResult(
                                appid=application.appid,
                                bucket_name=bucket.name,
                                bucket_id=bucket.bucket_id,
                                status=ScanStatus.FAILED,
                                csv_path=None,
                                thresholds=self.config.thresholds_for(application, bucket.name),
                                overview_format=self.config.scan.overview_format,
                                error=str(exc),
                                started_ms=started_ms,
                                ended_ms=ended_ms,
                                started_at=_iso_utc(started_ms),
                                ended_at=_iso_utc(ended_ms),
                                elapsed_seconds=elapsed_seconds,
                                request_elapsed_seconds=elapsed_seconds,
                                processing_elapsed_seconds=0.0,
                            )

                        temp_dir = results_dir / self.config.scan.temp_subdir / application.appid / bucket.name
                        if not self.config.scan.keep_temp_files and temp_dir.exists():
                            try:
                                shutil.rmtree(temp_dir)
                            except Exception as exc:
                                cleanup_error = f"cleanup failed: {_sanitize_reason(str(exc))}"
                                LOGGER.error(
                                    "bucket cleanup failure appid=%s bucket=%s error=%s",
                                    application.appid,
                                    bucket.name,
                                    cleanup_error,
                                )
                                error = f"{result.error}; {cleanup_error}" if result.error else cleanup_error
                                result = replace(result, status=ScanStatus.FAILED, error=error)
                        return result

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
                if self.config.bucket_enabled(application, bucket.name):
                    buckets.append(bucket)
                else:
                    LOGGER.info(
                        "bucket skipped appid=%s bucket=%s reason=config_disabled",
                        application.appid,
                        bucket.name,
                    )
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
        started_ms = _now_ms()
        bucket_started = time.monotonic()
        phase_boundary: float | None = None

        def phase_elapsed_seconds(bucket_ended: float) -> tuple[float, float]:
            if phase_boundary is None:
                return bucket_ended - bucket_started, 0.0
            return phase_boundary - bucket_started, bucket_ended - phase_boundary

        LOGGER.info("bucket start appid=%s bucket=%s", application.appid, bucket.name)
        thresholds = self.config.thresholds_for(application, bucket.name)
        partial_errors = PartialErrorSummary()
        temp_dir = results_dir / self.config.scan.temp_subdir / application.appid / bucket.name
        overview_format = self.config.scan.overview_format
        csv_output_path = results_dir / application.appid / f"{bucket.name}.csv"
        parquet_output_dir = results_dir / application.appid / bucket.name
        csv_path: Path | None = None
        overview_path: Path | None = None
        overview_files: tuple[Path, ...] = ()
        try:
            endpoint = await self._get_bucket_endpoint(application, bucket, client)
            discovery = await self._discover_root(
                application,
                bucket,
                client,
                thresholds,
                partial_errors=partial_errors,
            )
            await self._collect_metadata_files(
                application,
                bucket,
                endpoint,
                discovery.metadata_files,
                temp_dir,
                client,
                partial_errors,
            )
            await self._collect_prefixes(
                application,
                bucket,
                endpoint,
                discovery.prefixes,
                temp_dir,
                client,
                partial_errors,
            )
            phase_boundary = time.monotonic()
            if overview_format == "csv":
                aggregate_bucket(
                    run_id=run_id,
                    appid=application.appid,
                    bucket_name=bucket.name,
                    bucket_id=bucket.bucket_id,
                    temp_dir=temp_dir,
                    output_path=csv_output_path,
                    thresholds=thresholds,
                    scan_started_ms=scan_started_ms,
                    max_directories_in_memory=self.config.scan.aggregation_max_directories_in_memory,
                    keep_temp_files=self.config.scan.keep_temp_files,
                )
                csv_path = csv_output_path
                overview_path = csv_output_path
                overview_files = (csv_output_path,)
            else:
                overview_files = aggregate_bucket_parquet(
                    appid=application.appid,
                    bucket_name=bucket.name,
                    bucket_id=bucket.bucket_id,
                    temp_dir=temp_dir,
                    output_dir=parquet_output_dir,
                    scan_started_ms=scan_started_ms,
                    max_depth=self.config.scan.max_depth,
                    file_type_map=self.config.scan.file_type_map,
                    max_directories_in_memory=self.config.scan.aggregation_max_directories_in_memory,
                    keep_temp_files=self.config.scan.keep_temp_files,
                )
                overview_path = parquet_output_dir
        except OBSRequestError as exc:
            ended_ms = _now_ms()
            bucket_ended = time.monotonic()
            elapsed_seconds = bucket_ended - bucket_started
            request_elapsed_seconds, processing_elapsed_seconds = phase_elapsed_seconds(bucket_ended)
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
                overview_format=overview_format,
                error=str(exc),
                partial_errors=partial_errors if partial_errors.has_errors() else None,
                errors=[
                    *partial_errors.errors,
                    RequestFailureDetail.from_error(exc, scope="bucket", scope_value=bucket.name),
                ],
                started_ms=started_ms,
                ended_ms=ended_ms,
                started_at=_iso_utc(started_ms),
                ended_at=_iso_utc(ended_ms),
                elapsed_seconds=elapsed_seconds,
                request_elapsed_seconds=request_elapsed_seconds,
                processing_elapsed_seconds=processing_elapsed_seconds,
            )
        except Exception as exc:
            ended_ms = _now_ms()
            bucket_ended = time.monotonic()
            elapsed_seconds = bucket_ended - bucket_started
            request_elapsed_seconds, processing_elapsed_seconds = phase_elapsed_seconds(bucket_ended)
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
                overview_format=overview_format,
                error=str(exc),
                partial_errors=partial_errors if partial_errors.has_errors() else None,
                errors=list(partial_errors.errors),
                started_ms=started_ms,
                ended_ms=ended_ms,
                started_at=_iso_utc(started_ms),
                ended_at=_iso_utc(ended_ms),
                elapsed_seconds=elapsed_seconds,
                request_elapsed_seconds=request_elapsed_seconds,
                processing_elapsed_seconds=processing_elapsed_seconds,
            )

        ended_ms = _now_ms()
        bucket_ended = time.monotonic()
        elapsed_seconds = bucket_ended - bucket_started
        request_elapsed_seconds, processing_elapsed_seconds = phase_elapsed_seconds(bucket_ended)
        status = ScanStatus.PARTIAL_FAILED if partial_errors.has_errors() else ScanStatus.SUCCESS
        LOGGER.info(
            "bucket finish appid=%s bucket=%s status=%s elapsed_seconds=%.3f",
            application.appid,
            bucket.name,
            status.value,
            elapsed_seconds,
        )
        return BucketScanResult(
            appid=application.appid,
            bucket_name=bucket.name,
            bucket_id=bucket.bucket_id,
            status=status,
            csv_path=csv_path,
            thresholds=thresholds,
            overview_format=overview_format,
            overview_path=overview_path,
            overview_files=overview_files,
            error=partial_errors.summary_text(),
            partial_errors=partial_errors if partial_errors.has_errors() else None,
            errors=list(partial_errors.errors),
            started_ms=started_ms,
            ended_ms=ended_ms,
            started_at=_iso_utc(started_ms),
            ended_at=_iso_utc(ended_ms),
            elapsed_seconds=elapsed_seconds,
            request_elapsed_seconds=request_elapsed_seconds,
            processing_elapsed_seconds=processing_elapsed_seconds,
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
        partial_errors: PartialErrorSummary | None = None,
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
                await _gather_cancel_on_error(
                    *(
                        self._process_filelist_task(
                            application,
                            bucket,
                            client,
                            scheduler,
                            task,
                            url,
                            progress_bar,
                            partial_errors,
                        )
                        for task in tasks
                    )
                )
                metadata_task_count = scheduler.metadata_task_count
                metadata_task_limit = max(1, self.config.scan.metadata_task_limit_per_bucket)
                if metadata_task_count > metadata_task_limit:
                    rejected_depth = scheduler.current_level_depth
                    restored_prefix_total = scheduler.rollback_current_level()
                    LOGGER.info(
                        "filelist metadata limit rollback appid=%s bucket=%s depth=%s "
                        "metadata_tasks=%s limit=%s prefixes=%s",
                        application.appid,
                        bucket.name,
                        rejected_depth,
                        metadata_task_count,
                        metadata_task_limit,
                        restored_prefix_total,
                    )
                    break
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
        partial_errors: PartialErrorSummary | None,
    ) -> None:
        path = task.path
        try:
            pointer = ""
            found_items = False
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
                    break

                items = _items_from_payload(payload, "objects", "files", "list", "items")
                found_items = found_items or bool(items)
                for item in items:
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
                        file_key = self._filelist_object_key(path, object_key)
                        if file_key:
                            scheduler.record_file(task, file_key)

                next_pointer = None
                if isinstance(payload, dict):
                    next_pointer = str(payload.get("nextOffset") or "")
                if not next_pointer or next_pointer == pointer:
                    break
                pointer = next_pointer
            if found_items:
                scheduler.record_expanded(task)
            else:
                scheduler.record_empty(task)
        except OBSRequestError as exc:
            scheduler.record_failed(task)
            if partial_errors is not None:
                partial_errors.record("filelist", path, exc, scope="directory")
            LOGGER.warning(
                "filelist directory failure appid=%s bucket=%s path=%s status=%s reason=%s",
                application.appid,
                bucket.name,
                path,
                "unknown" if exc.status_code is None else exc.status_code,
                exc.reason,
            )
        finally:
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

    def _objectkeys_progress_bar(self, application: ApplicationConfig, bucket: BucketInfo, total: int) -> Any | None:
        if not self.show_progress:
            return None
        from tqdm import tqdm

        return tqdm(total=total, desc=f"{application.appid}/{bucket.name} objectkeys", unit="prefix")

    def _filelist_folder_prefix(self, path: str, value: Any) -> str:
        raw_prefix = str(value or "").strip("/")
        if not raw_prefix:
            return ""
        current_prefix = path.strip("/")
        if current_prefix and raw_prefix != current_prefix and not raw_prefix.startswith(f"{current_prefix}/"):
            raw_prefix = f"{current_prefix}/{raw_prefix}"
        if path == "/" and "/" in raw_prefix:
            raw_prefix = raw_prefix.split("/", 1)[0]
        return f"{raw_prefix.rstrip('/')}/"

    def _filelist_object_key(self, path: str, value: Any) -> str:
        raw_key = str(value or "").strip("/")
        if not raw_key:
            return ""
        current_prefix = path.strip("/")
        if current_prefix and not raw_key.startswith(f"{current_prefix}/"):
            raw_key = f"{current_prefix}/{raw_key}"
        return raw_key

    async def _collect_metadata_files(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        endpoint: str,
        object_keys: list[str],
        temp_dir: Path,
        client: OBSClient,
        partial_errors: PartialErrorSummary | None = None,
    ) -> None:
        total = len(object_keys)
        if total == 0:
            LOGGER.info("metadata skipped appid=%s bucket=%s total=0", application.appid, bucket.name)
            return

        completed = 0
        succeeded = 0
        failed = 0
        LOGGER.info("metadata start appid=%s bucket=%s total=%s", application.appid, bucket.name, total)
        rows: list[ObjectRow] = []
        queue: asyncio.Queue[str] = asyncio.Queue()
        for object_key in object_keys:
            queue.put_nowait(object_key)

        async def worker() -> None:
            nonlocal completed, succeeded, failed
            while True:
                try:
                    object_key = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                task_succeeded = False
                try:
                    data = await client.get_json(
                        _endpoint(endpoint, "/rest/boto3/s3/object/metadata"),
                        params={
                            "vendor": bucket.vendor,
                            "region": bucket.region,
                            "bucketid": bucket.name,
                            "apptoken": application.apptoken,
                            "objectkey": encode_object_key("/" + object_key.lstrip("/")),
                            "bucketId": bucket.bucket_id,
                        },
                        headers=JSON_HEADERS,
                        endpoint="metadata",
                    )
                    row = self._metadata_to_object_row(object_key, data)
                    if row is not None:
                        rows.append(row)
                        task_succeeded = True
                    else:
                        invalid_response = "invalid metadata response"
                        if partial_errors is not None:
                            partial_errors.record(
                                "metadata",
                                object_key,
                                invalid_response,
                                scope="object_key",
                            )
                        LOGGER.warning(
                            "metadata object failure appid=%s bucket=%s object_key=%s error=%s",
                            application.appid,
                            bucket.name,
                            object_key,
                            invalid_response,
                        )
                except Exception as exc:
                    if partial_errors is not None:
                        partial_errors.record("metadata", object_key, exc, scope="object_key")
                    sanitized_error = _sanitize_reason(str(exc))
                    LOGGER.warning(
                        "metadata object failure appid=%s bucket=%s object_key=%s error=%s",
                        application.appid,
                        bucket.name,
                        object_key,
                        sanitized_error,
                    )
                finally:
                    if task_succeeded:
                        succeeded += 1
                    else:
                        failed += 1
                    completed += 1
                    LOGGER.info(
                        "metadata progress appid=%s bucket=%s completed=%s total=%s succeeded=%s failed=%s",
                        application.appid,
                        bucket.name,
                        completed,
                        total,
                        succeeded,
                        failed,
                    )
                    queue.task_done()

        worker_count = min(max(1, self.config.scan.metadata_concurrency_per_bucket), len(object_keys))
        if worker_count:
            await _gather_cancel_on_error(*(worker() for _ in range(worker_count)))
        LOGGER.info(
            "metadata finish appid=%s bucket=%s completed=%s total=%s succeeded=%s failed=%s",
            application.appid,
            bucket.name,
            completed,
            total,
            succeeded,
            failed,
        )
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
        partial_errors: PartialErrorSummary | None = None,
    ) -> None:
        total = len(prefixes)
        if total == 0:
            LOGGER.info("objectkeys skipped appid=%s bucket=%s total=0", application.appid, bucket.name)
            return
        progress = ObjectkeysProgress(total=total)
        progress_bar = self._objectkeys_progress_bar(application, bucket, total)
        if progress_bar is not None:
            progress_bar.total = total
        LOGGER.info("objectkeys start appid=%s bucket=%s total=%s", application.appid, bucket.name, total)
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
                    try:
                        await self._collect_prefix(application, bucket, endpoint, prefix, temp_dir, client, progress)
                    except OBSRequestError as exc:
                        if partial_errors is not None:
                            partial_errors.record("objectkeys", prefix, exc, scope="prefix")
                        sanitized_error = _sanitize_reason(str(exc))
                        LOGGER.warning(
                            "objectkeys prefix failure appid=%s bucket=%s prefix=%s error=%s",
                            application.appid,
                            bucket.name,
                            prefix,
                            sanitized_error,
                        )
                        progress.record_failure()
                    else:
                        progress.record_success()
                    LOGGER.info(
                        "objectkeys progress appid=%s bucket=%s completed=%s total=%s "
                        "succeeded=%s failed=%s pages=%s objects=%s",
                        application.appid,
                        bucket.name,
                        progress.completed,
                        progress.total,
                        progress.succeeded,
                        progress.failed,
                        progress.pages,
                        progress.objects,
                    )
                    if progress_bar is not None:
                        progress_bar.set_postfix(
                            {
                                "succeeded": progress.succeeded,
                                "failed": progress.failed,
                                "pages": progress.pages,
                                "objects": progress.objects,
                            },
                            refresh=False,
                        )
                        progress_bar.update(1)
                finally:
                    queue.task_done()

        try:
            worker_count = min(max(1, self.config.scan.objectkeys_concurrency_limit()), len(prefixes))
            if worker_count:
                await _gather_cancel_on_error(*(worker() for _ in range(worker_count)))
        finally:
            if progress_bar is not None:
                progress_bar.close()
        LOGGER.info(
            "objectkeys finish appid=%s bucket=%s completed=%s total=%s "
            "succeeded=%s failed=%s pages=%s objects=%s",
            application.appid,
            bucket.name,
            progress.completed,
            progress.total,
            progress.succeeded,
            progress.failed,
            progress.pages,
            progress.objects,
        )

    async def _collect_prefix(
        self,
        application: ApplicationConfig,
        bucket: BucketInfo,
        endpoint: str,
        prefix: str,
        temp_dir: Path,
        client: OBSClient,
        progress: ObjectkeysProgress | None = None,
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
                    "bucketId": bucket.bucket_id,
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
            if progress is not None:
                progress.record_page(len(rows))

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
        overview_path = result.overview_path
        overview_files = result.overview_files
        if result.overview_format == "csv" and result.csv_path is not None:
            overview_path = overview_path or result.csv_path
            overview_files = overview_files or (result.csv_path,)
        manifest = {
            "bucket_name": result.bucket_name,
            "bucket_id": result.bucket_id,
            "status": result.status.value,
            "csv_path": str(result.csv_path) if result.csv_path is not None else None,
            "overview_format": result.overview_format,
            "overview_path": str(overview_path) if overview_path is not None else None,
            "overview_files": [str(path) for path in overview_files],
            "thresholds": result.thresholds.model_dump(mode="json"),
            "error": result.error,
            "errors": [error.to_manifest() for error in result.errors],
            "started_ms": result.started_ms,
            "ended_ms": result.ended_ms,
            "started_at": result.started_at,
            "ended_at": result.ended_at,
            "elapsed_seconds": result.elapsed_seconds,
            "request_elapsed_seconds": result.request_elapsed_seconds,
            "processing_elapsed_seconds": result.processing_elapsed_seconds,
        }
        if result.partial_errors is not None and result.partial_errors.has_errors():
            manifest["partial_errors"] = result.partial_errors.to_manifest()
        if temp_dir is not None and self.config.scan.keep_temp_files:
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
