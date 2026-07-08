import json
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from obs_scan_platform.config import load_config
from obs_scan_platform.scanner import run_scan


def _read_manifest(run_dir: Path) -> dict:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="run manifest not found")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def create_app(*, config_path: Path | None = None, results_dir: Path | None = None) -> FastAPI:
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
        manifests = []
        for run_dir in sorted(configured_results_dir.iterdir()):
            if run_dir.is_dir() and (run_dir / "manifest.json").exists():
                manifests.append(_read_manifest(run_dir))
        return manifests

    @app.get("/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        return _read_manifest(configured_results_dir / run_id)

    @app.get("/runs/{run_id}/logs")
    def run_logs(run_id: str) -> PlainTextResponse:
        log_path = configured_results_dir / run_id / "scan.log"
        if not log_path.exists():
            raise HTTPException(status_code=404, detail="scan log not found")
        return PlainTextResponse(log_path.read_text(encoding="utf-8"))

    @app.get("/runs/{run_id}/apps/{appid}/buckets/{bucket_name}/csv")
    def bucket_csv(run_id: str, appid: str, bucket_name: str) -> FileResponse:
        csv_path = configured_results_dir / run_id / appid / f"{bucket_name}.csv"
        if not csv_path.exists():
            raise HTTPException(status_code=404, detail="bucket csv not found")
        return FileResponse(csv_path, media_type="text/csv", filename=f"{bucket_name}.csv")

    async def _run_scan_background() -> None:
        try:
            await run_scan(config_path)
        finally:
            app.state.active_scan = False

    @app.post("/runs")
    async def trigger_run(background_tasks: BackgroundTasks) -> dict[str, str]:
        if config_path is None:
            raise HTTPException(status_code=404, detail="config path is not configured")
        if app.state.active_scan:
            raise HTTPException(status_code=409, detail="scan is already running")
        app.state.active_scan = True
        background_tasks.add_task(_run_scan_background)
        return {"status": "accepted"}

    return app


app = create_app()
