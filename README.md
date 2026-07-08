# OBS Scan Platform

Python backend for scanning OBS bucket usage with controlled concurrency and directory-level CSV aggregation.

Planned first-version entry points:

- CLI scanner: `obs-scan scan --config config/apps.yaml`
- API server: `OBS_SCAN_CONFIG=config/apps.yaml uvicorn obs_scan_platform.api:app --reload`

The module-level FastAPI app reads `OBS_SCAN_CONFIG` for `/config/apps` and manual `POST /runs` scans.
Without `OBS_SCAN_CONFIG`, read-only endpoints such as `/health` still start, but config-dependent endpoints return 404.
The manual scan trigger uses an in-process `active_scan` guard, so run the API with a single worker for the first production version.

Design spec: `docs/superpowers/specs/2026-07-08-obs-scan-platform-design.md`
