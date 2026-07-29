import json
import os
import re
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from obs_scan_platform.config import load_config
from obs_scan_platform.scanner import run_scan


_PARQUET_PART_PATTERN = re.compile(r"part-\d{5}\.parquet")


def _safe_segment(name: str) -> str:
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(status_code=404, detail="resource not found")
    return name


def _safe_child(root: Path, *segments: str) -> Path:
    resolved_root = root.resolve()
    child = resolved_root.joinpath(*(_safe_segment(segment) for segment in segments)).resolve()
    try:
        child.relative_to(resolved_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="resource not found") from exc
    return child


def _read_manifest(run_dir: Path) -> dict:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="run manifest not found")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _bucket_manifest(manifest: dict, appid: str, bucket_name: str) -> dict | None:
    applications = manifest.get("applications")
    if not isinstance(applications, list):
        return None
    for application in applications:
        if not isinstance(application, dict) or application.get("appid") != appid:
            continue
        buckets = application.get("buckets")
        if not isinstance(buckets, list):
            return None
        for bucket in buckets:
            if isinstance(bucket, dict) and bucket.get("bucket_name") == bucket_name:
                return bucket
    return None


def _resolved_manifest_paths(values: object) -> set[Path]:
    if not isinstance(values, list):
        return set()
    resolved: set[Path] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        path = Path(value)
        if not path.is_absolute():
            path = Path.cwd() / path
        resolved.add(path.resolve())
    return resolved


def create_app(config_path: Path | None = None, results_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="OBS Scan Platform")
    configured_results_dir = results_dir or Path("results")
    app.state.active_scan = False

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/config/apps")
    def config_apps() -> dict:
        if config_path is None:
            raise HTTPException(status_code=404, detail="config path is not configured")
        return load_config(config_path).masked_dict()

    @app.get("/runs")
    def runs() -> list[dict]:
        if not configured_results_dir.exists():
            return []
        resolved_results_dir = configured_results_dir.resolve()
        manifests = []
        for run_dir in sorted(configured_results_dir.iterdir()):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            try:
                run_dir.resolve().relative_to(resolved_results_dir)
            except ValueError:
                continue
            manifest_path = run_dir / "manifest.json"
            if manifest_path.exists() and not manifest_path.is_symlink():
                manifests.append(_read_manifest(run_dir))
        return manifests

    @app.get("/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        return _read_manifest(_safe_child(configured_results_dir, run_id))

    @app.get("/runs/{run_id}/logs")
    def run_logs(run_id: str) -> PlainTextResponse:
        log_path = _safe_child(configured_results_dir, run_id, "scan.log")
        if not log_path.exists():
            raise HTTPException(status_code=404, detail="scan log not found")
        return PlainTextResponse(log_path.read_text(encoding="utf-8"))

    @app.get("/runs/{run_id}/apps/{appid}/buckets/{bucket_name}/csv")
    def bucket_csv(run_id: str, appid: str, bucket_name: str) -> FileResponse:
        csv_path = _safe_child(configured_results_dir, run_id, appid, f"{_safe_segment(bucket_name)}.csv")
        if not csv_path.exists():
            raise HTTPException(status_code=404, detail="bucket csv not found")
        return FileResponse(csv_path, media_type="text/csv", filename=f"{bucket_name}.csv")

    @app.get(
        "/runs/{run_id}/apps/{appid}/buckets/{bucket_name}/parquet/{part_name}"
    )
    def bucket_parquet_part(
        run_id: str,
        appid: str,
        bucket_name: str,
        part_name: str,
    ) -> FileResponse:
        if _PARQUET_PART_PATTERN.fullmatch(part_name) is None:
            raise HTTPException(status_code=404, detail="bucket parquet part not found")

        run_dir = _safe_child(configured_results_dir, run_id)
        manifest_bucket = _bucket_manifest(_read_manifest(run_dir), appid, bucket_name)
        if manifest_bucket is None or manifest_bucket.get("overview_format") != "parquet":
            raise HTTPException(status_code=404, detail="bucket parquet part not found")

        bucket_dir = _safe_child(configured_results_dir, run_id, appid, bucket_name)
        candidate = bucket_dir / _safe_segment(part_name)
        if candidate.is_symlink():
            raise HTTPException(status_code=404, detail="bucket parquet part not found")
        parquet_path = _safe_child(
            configured_results_dir,
            run_id,
            appid,
            bucket_name,
            part_name,
        )
        if (
            parquet_path not in _resolved_manifest_paths(manifest_bucket.get("overview_files"))
            or not parquet_path.is_file()
            or parquet_path.is_symlink()
        ):
            raise HTTPException(status_code=404, detail="bucket parquet part not found")
        return FileResponse(
            parquet_path,
            media_type="application/vnd.apache.parquet",
            filename=part_name,
        )

    async def _run_scan_background() -> None:
        try:
            await run_scan(config_path, show_progress=False)
        finally:
            app.state.active_scan = False

    @app.post("/runs", status_code=202)
    async def trigger_run(background_tasks: BackgroundTasks) -> dict[str, str]:
        if config_path is None:
            raise HTTPException(status_code=404, detail="config path is not configured")
        if app.state.active_scan:
            raise HTTPException(status_code=409, detail="scan is already running")
        app.state.active_scan = True
        background_tasks.add_task(_run_scan_background)
        return {"status": "accepted"}

    return app


_env_config_path = os.getenv("OBS_SCAN_CONFIG")
app = create_app(config_path=Path(_env_config_path) if _env_config_path else None)
