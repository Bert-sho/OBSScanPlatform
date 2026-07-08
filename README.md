# OBS Scan Platform

Python backend for scanning OBS bucket usage with controlled concurrency and directory-level CSV aggregation.

First-version entry points:

- CLI scanner: `obs-scan scan --config config/apps.yaml`
- API server: `uvicorn obs_scan_platform.api:app --reload`

Design spec: `docs/superpowers/specs/2026-07-08-obs-scan-platform-design.md`
