import json
import os
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from obs_scan_platform.config import load_config
from obs_scan_platform.scanner import run_scan


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
            if (run_dir / "manifest.json").exists():
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

    async def _run_scan_background() -> None:
        try:
            await run_scan(config_path)
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
