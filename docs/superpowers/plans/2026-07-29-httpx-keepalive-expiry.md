# HTTPX Keep-Alive Expiry Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a positive YAML-configurable HTTPX idle keep-alive expiry and apply its `5.0`-second default to every per-application scan client.

**Architecture:** `ScanSettings` remains the source of truth for the value. `Scanner._scan_application` converts the setting into a real `httpx.Limits` object when it constructs the shared per-application `AsyncClient`; existing timeout, retry, concurrency, and connection-count behavior stays unchanged.

**Tech Stack:** Python 3.11+, Pydantic, PyYAML, HTTPX, pytest, pytest-asyncio, Git

## Global Constraints

- Name the YAML field `scan.keepalive_expiry_seconds`.
- Use `5.0` seconds when the YAML field is omitted.
- Accept only values greater than zero; integers and decimals are valid.
- Configure only `httpx.Limits.keepalive_expiry`; retain HTTPX defaults for connection counts.
- Keep connection reuse enabled and do not send `Connection: close`.
- Preserve `scan.request_timeout_seconds`, retries, retry delays, and concurrency behavior.
- Update `docs/current-task.md` and `docs/handoff.md` before the implementation commit.
- Commit and push the current feature branch; do not push directly to `main` or `master`.

---

### Task 1: Add, apply, document, and verify the keep-alive expiry setting

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_scanner.py`
- Modify: `src/obs_scan_platform/config.py`
- Modify: `src/obs_scan_platform/scanner.py`
- Modify: `config/apps.example.yaml`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/scan-start-guide.md`
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`
- Create: `docs/superpowers/plans/2026-07-29-httpx-keepalive-expiry.md`

**Interfaces:**
- Consumes: YAML `scan.keepalive_expiry_seconds: <positive number>`.
- Produces: `ScanSettings.keepalive_expiry_seconds: float` and an `httpx.AsyncClient` whose `limits.keepalive_expiry` equals that value.

- [ ] **Step 1: Write failing configuration tests**

Add the default assertion to `test_scan_settings_new_concurrency_defaults`:

```python
assert config.scan.keepalive_expiry_seconds == 5.0
```

Add a YAML override test:

```python
def test_load_config_sets_keepalive_expiry_seconds(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
scan:
  keepalive_expiry_seconds: 2.5
""",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scan.keepalive_expiry_seconds == 2.5
```

Add invalid-value coverage:

```python
@pytest.mark.parametrize("value", [0, -1])
def test_load_config_rejects_non_positive_keepalive_expiry_seconds(tmp_path: Path, value: int):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        f"scan:\n  keepalive_expiry_seconds: {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_file)
```

- [ ] **Step 2: Run configuration tests and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py -q
```

Expected: FAIL because `ScanSettings` has no `keepalive_expiry_seconds` field and currently ignores that extra YAML key instead of rejecting non-positive values.

- [ ] **Step 3: Add the minimal validated configuration field**

Add next to `request_timeout_seconds` in `ScanSettings`:

```python
keepalive_expiry_seconds: float = Field(default=5.0, gt=0)
```

- [ ] **Step 4: Run configuration tests and verify GREEN**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py -q
```

Expected: all configuration tests pass.

- [ ] **Step 5: Write the failing scanner boundary test**

Add an async test near the existing `_scan_application` tests. Replace only
the external `AsyncClient` boundary, return no buckets, and capture the real
constructor values:

```python
@pytest.mark.asyncio
async def test_scan_application_applies_configured_httpx_keepalive_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    scanner, application, _ = make_scanner()
    scanner.config.scan.request_timeout_seconds = 47
    scanner.config.scan.keepalive_expiry_seconds = 2.5
    captured: dict[str, object] = {}

    class CapturingAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> "CapturingAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    async def fake_list_buckets(application_config, client):
        return []

    monkeypatch.setattr("obs_scan_platform.scanner.httpx.AsyncClient", CapturingAsyncClient)
    monkeypatch.setattr(scanner, "_list_buckets", fake_list_buckets)

    result = await scanner._scan_application(
        application,
        "run-1",
        tmp_path,
        scan_started_ms=1000,
        bucket_semaphore=asyncio.Semaphore(1),
    )

    assert result["status"] == ScanStatus.SUCCESS.value
    assert captured["timeout"] == 47
    limits = captured["limits"]
    assert isinstance(limits, httpx.Limits)
    assert limits.keepalive_expiry == 2.5
```

- [ ] **Step 6: Run the scanner boundary test and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py::test_scan_application_applies_configured_httpx_keepalive_expiry -q
```

Expected: FAIL with `KeyError: 'limits'` because scanner construction currently passes only `timeout`.

- [ ] **Step 7: Apply the minimal HTTPX client change**

Construct the per-application client as follows:

```python
async with httpx.AsyncClient(
    timeout=self.config.scan.request_timeout_seconds,
    limits=httpx.Limits(
        keepalive_expiry=self.config.scan.keepalive_expiry_seconds,
    ),
) as http:
```

- [ ] **Step 8: Run focused scanner and end-to-end tests and verify GREEN**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py::test_scan_application_applies_configured_httpx_keepalive_expiry -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: the focused test and all configuration/scanner/end-to-end tests pass.

- [ ] **Step 9: Document the setting**

Add `keepalive_expiry_seconds: 5.0` after `request_timeout_seconds` in
`config/apps.example.yaml`. Add the field and default to the English and
Chinese README configuration tables. Explain in both READMEs and
`docs/scan-start-guide.md` that the setting expires idle pooled connections,
does not terminate an active request after five seconds, leaves connection
reuse enabled, and works alongside the existing retry path.

- [ ] **Step 10: Update task and handoff records**

Record the task goal, branch, before-session commit `59a45d7`, design commit
`935ca97`, root-cause limits, exact implementation, changed files, validation
commands/results, review outcome, known risks, uncommitted state, and exact
resume instructions in `docs/current-task.md` and `docs/handoff.md`.

- [ ] **Step 11: Run completion verification and inspect the change**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
git status --short --branch
git diff --stat
git diff
```

Expected: the full suite and compilation pass, the diff contains only the
approved setting and required documentation, and no secret or machine-specific
path is introduced.

- [ ] **Step 12: Commit, review, fix findings if needed, and push**

```powershell
git add .
git commit -m "fix: configure HTTPX keepalive expiry"
git push -u origin HEAD
```

Expected: a reviewer finds no Critical or Important issue, commit succeeds on
`codex/obs-scan-platform`, push succeeds, and local HEAD equals
`origin/codex/obs-scan-platform`.
