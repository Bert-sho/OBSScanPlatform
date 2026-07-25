# Configuration Defaults and Bucket Enable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every modeled YAML field safe to omit, inherit global endpoints into applications, skip only buckets explicitly configured with `enable: false`, and document the complete configuration in English and Chinese.

**Architecture:** Keep defaults and endpoint normalization in the Pydantic configuration models, expose focused helpers for effective endpoints, required scan fields, threshold resolution, and bucket enablement, then have `Scanner` enforce only the operational checks and filtering. Preserve the existing listbuckets, ownership/shared-bucket, CSV, aggregation, and manifest behavior.

**Tech Stack:** Python 3.11+, Pydantic 2, PyYAML, asyncio/httpx, pytest/pytest-asyncio, Markdown.

## Global Constraints

- Use the YAML key `applications[].buckets.<bucket-name>.enable`; do not add an `enabled` alias.
- An absent bucket entry or absent `enable` means enabled; only explicit `enable: false` skips a bucket.
- Missing thresholds default to 100 GiB directory size, 10 GiB file size, 180 inactive days, and filelist depth 5.
- Preserve an explicit non-empty application endpoint; otherwise inherit a non-empty global endpoint; otherwise use the empty string.
- An enabled application missing effective endpoint, appid, or apptoken fails before any OBS request.
- Explicit `null` remains invalid for non-nullable fields; nullable application endpoint and bucket threshold overrides retain their nullable semantics.
- Do not change CSV schemas, manifest schemas, shared-bucket rules, request concurrency, or aggregation behavior.
- Do not add dependencies, abstractions, or unrelated cleanup.

---

### Task 1: Model-native defaults and endpoint inheritance

**Files:**
- Modify: `src/obs_scan_platform/config.py:8-108`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: YAML mappings loaded by `load_config(path: str | Path) -> AppConfigFile`.
- Produces: `Thresholds()` with business defaults; `BucketOverrides.enable: bool`; default-constructible `ApplicationConfig` and `AppConfigFile`; `AppConfigFile.endpoint_for(application) -> str`; `AppConfigFile.missing_scan_fields(application) -> list[str]`; `AppConfigFile.bucket_enabled(application, bucket_name) -> bool`.

- [ ] **Step 1: Write failing tests for complete missing-field defaults**

Add tests that load an empty YAML file and a partial nested configuration:

```python
def test_empty_config_loads_all_global_defaults(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text("", encoding="utf-8")

    config = load_config(config_file)

    assert config.endpoint == ""
    assert config.applications == []
    assert config.scan.results_dir == "results"
    assert config.scan.objectkeys_concurrency_limit() == 30
    assert config.defaults.large_directory_bytes == 107374182400
    assert config.defaults.large_file_bytes == 10737418240
    assert config.defaults.inactive_directory_days == 180
    assert config.defaults.filelist_depth == 5


def test_partial_application_and_bucket_load_missing_defaults(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
applications:
  - buckets:
      bucket-a: {}
""",
        encoding="utf-8",
    )

    config = load_config(config_file)
    application = config.applications[0]

    assert application.appid == ""
    assert application.name == ""
    assert application.endpoint == "http://obs.global"
    assert application.apptoken == ""
    assert application.enabled is True
    assert application.scan_shared_buckets is False
    assert config.bucket_enabled(application, "bucket-a") is True
    assert config.thresholds_for(application, "bucket-a") == config.defaults
```

Extend the config-test imports to include `AppConfigFile` for the direct helper
test.

- [ ] **Step 2: Run the new default tests and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py -k "empty_config or partial_application" -q
```

Expected: failures for required `defaults`/`applications` fields and missing application fields or bucket `enable` support.

- [ ] **Step 3: Add failing endpoint, null, and helper tests**

Add focused cases:

```python
def test_explicit_application_endpoint_wins_over_global(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
applications:
  - endpoint: http://obs.application
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.applications[0].endpoint == "http://obs.application"
    assert config.endpoint_for(config.applications[0]) == "http://obs.application"


def test_null_application_endpoint_inherits_global(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
endpoint: http://obs.global
applications:
  - endpoint: null
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.applications[0].endpoint == "http://obs.global"


def test_null_non_nullable_default_is_rejected(tmp_path: Path):
    config_file = tmp_path / "apps.yaml"
    config_file.write_text(
        """
defaults:
  large_file_bytes: null
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_config(config_file)


def test_missing_scan_fields_reports_only_operational_identity():
    config = AppConfigFile(applications=[{}])
    application = config.applications[0]
    assert config.missing_scan_fields(application) == ["endpoint", "appid", "apptoken"]
```

- [ ] **Step 4: Implement minimal model defaults and helpers**

Update the configuration models using field declarations as the single source of defaults:

```python
class Thresholds(BaseModel):
    large_directory_bytes: int = 107374182400
    large_file_bytes: int = 10737418240
    inactive_directory_days: int = 180
    filelist_depth: int = 5


class BucketOverrides(BaseModel):
    enable: bool = True
    large_directory_bytes: int | None = None
    large_file_bytes: int | None = None
    inactive_directory_days: int | None = None
    filelist_depth: int | None = None


class ApplicationConfig(BaseModel):
    appid: str = ""
    name: str = ""
    endpoint: str | None = ""
    apptoken: str = ""
    enabled: bool = True
    scan_shared_buckets: bool = False
    buckets: dict[str, BucketOverrides] = Field(default_factory=dict)


class AppConfigFile(BaseModel):
    endpoint: str | None = ""
    scan: ScanSettings = Field(default_factory=ScanSettings)
    defaults: Thresholds = Field(default_factory=Thresholds)
    applications: list[ApplicationConfig] = Field(default_factory=list)
    source_path: Path | None = None

    @model_validator(mode="after")
    def inherit_global_endpoint(self) -> "AppConfigFile":
        if (self.endpoint or "").strip():
            for application in self.applications:
                if not (application.endpoint or "").strip():
                    application.endpoint = self.endpoint
        return self

    def endpoint_for(self, application: ApplicationConfig) -> str:
        return application.endpoint or self.endpoint or ""

    def missing_scan_fields(self, application: ApplicationConfig) -> list[str]:
        values = {
            "endpoint": self.endpoint_for(application),
            "appid": application.appid,
            "apptoken": application.apptoken,
        }
        return [name for name, value in values.items() if not value.strip()]

    def bucket_enabled(self, application: ApplicationConfig, bucket_name: str) -> bool:
        override = application.buckets.get(bucket_name)
        return override is None or override.enable
```

Remove the load-time validator that requires an endpoint. Keep
`thresholds_for()` excluding nullable override values so bucket thresholds
inherit from the new `Thresholds` defaults.

- [ ] **Step 5: Run config tests and verify GREEN**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py -q
```

Expected: all config tests pass after replacing the old load-time endpoint-error expectation with default/runtime-helper expectations.

- [ ] **Step 6: Review and commit the configuration unit**

Run `git diff --check`, inspect `git diff -- src/obs_scan_platform/config.py tests/test_config.py`, then commit:

```powershell
git add src/obs_scan_platform/config.py tests/test_config.py
git commit -m "feat: default all scan configuration fields"
```

---

### Task 2: Bucket opt-out filtering

**Files:**
- Modify: `src/obs_scan_platform/scanner.py:304-330`
- Test: `tests/test_scanner.py`

**Interfaces:**
- Consumes: `AppConfigFile.bucket_enabled(application, bucket_name) -> bool` from Task 1 and existing `should_scan_bucket(bucket, include_shared) -> bool`.
- Produces: `_list_buckets()` output that excludes only eligible exact-name buckets with `enable: false` and emits an informational skip log.

- [ ] **Step 1: Write failing scanner tests for bucket enable behavior**

Use a `FakeClient` listbuckets payload containing `bucket-off`, `bucket-on`, and
`bucket-unconfigured`. Configure only the first two:

```python
@pytest.mark.asyncio
async def test_list_buckets_skips_only_explicitly_disabled_exact_bucket():
    scanner, application, _ = make_scanner()
    application.buckets = {
        "bucket-off": BucketOverrides(enable=False),
        "bucket-on": BucketOverrides(),
    }
    client = FakeClient(
        [
            {
                "result": {
                    "buckets": [
                        {
                            "id": f"{name}-id",
                            "name": name,
                            "vendor": "HEC",
                            "region": "cn-east-3",
                            "auth": "owner",
                            "shareFrom": None,
                        }
                        for name in ("bucket-off", "bucket-on", "bucket-unconfigured")
                    ]
                }
            }
        ]
    )

    buckets = await scanner._list_buckets(application, client)

    assert [bucket.name for bucket in buckets] == ["bucket-on", "bucket-unconfigured"]
```

Extend the scanner-test config import to include `BucketOverrides`.

Add a caplog assertion that the disabled bucket log identifies appid,
bucket name, and `reason=config_disabled`, without containing the token.

- [ ] **Step 2: Run the bucket filtering tests and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -k "explicitly_disabled_exact_bucket" -q
```

Expected: `bucket-off` is still present before the scanner uses `enable`.

- [ ] **Step 3: Implement minimal filtering after existing eligibility checks**

Update the successful eligibility branch in `_list_buckets()`:

```python
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
    # Preserve the existing warning.
```

- [ ] **Step 4: Run scanner bucket tests and verify GREEN**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -k "list_buckets or should_scan_bucket or explicitly_disabled" -q
```

Expected: selected scanner tests pass; unconfigured and missing-`enable` buckets remain included.

- [ ] **Step 5: Review and commit the bucket filtering unit**

Run `git diff --check`, inspect the focused diff, then commit:

```powershell
git add src/obs_scan_platform/scanner.py tests/test_scanner.py
git commit -m "feat: allow per-bucket scan opt out"
```

---

### Task 3: Fail incomplete applications before OBS requests

**Files:**
- Modify: `src/obs_scan_platform/scanner.py:204-303`
- Test: `tests/test_scanner.py`
- Test: `tests/test_scan_end_to_end.py`

**Interfaces:**
- Consumes: `AppConfigFile.missing_scan_fields(application) -> list[str]` from Task 1.
- Produces: application manifest entries with `status="failed"`, a non-secret `error`, and `buckets=[]` without constructing an HTTP client when operational identity is incomplete.

- [ ] **Step 1: Write a failing no-request application test**

Add a direct `_scan_application()` test:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["endpoint", "appid", "apptoken"])
async def test_scan_application_rejects_missing_operational_field_before_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
):
    scanner, application, _ = make_scanner()
    if missing_field == "endpoint":
        scanner.config.endpoint = ""
        application.endpoint = ""
    else:
        setattr(application, missing_field, "")

    monkeypatch.setattr(
        "obs_scan_platform.scanner.httpx.AsyncClient",
        lambda *args, **kwargs: pytest.fail("HTTP client must not be constructed"),
    )

    result = await scanner._scan_application(
        application,
        "run-1",
        tmp_path,
        scan_started_ms=1000,
        bucket_semaphore=asyncio.Semaphore(1),
    )

    assert result["status"] == "failed"
    assert result["buckets"] == []
    assert missing_field in result["error"]
    assert "token-1" not in result["error"]
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py -k "missing_operational_field_before_http" -q
```

Expected: the patched `AsyncClient` is reached before validation.

- [ ] **Step 3: Implement the early application failure**

At the start of `_scan_application()`, before `httpx.AsyncClient`, add:

```python
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
```

Do not include values in the error or log. Keep the existing exception isolation
for actual OBS/listbuckets failures.

- [ ] **Step 4: Add and run a focused run-rollup regression**

Add a test with one invalid and one valid application, stub the valid
application listbuckets result to empty, and assert the run becomes
`partial_failed` while the valid application succeeds. Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_scanner.py tests/test_scan_end_to_end.py -k "missing_operational or keeps_other_applications" -q
```

Expected: invalid application fails without a request; valid sibling succeeds.

- [ ] **Step 5: Run all config/scanner/end-to-end tests and commit**

Run:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py -q
```

Expected: all selected tests pass. Review the focused diff, then commit:

```powershell
git add src/obs_scan_platform/scanner.py tests/test_scanner.py tests/test_scan_end_to_end.py
git commit -m "fix: reject incomplete applications before requests"
```

---

### Task 4: Operator documentation and example configuration

**Files:**
- Modify: `config/apps.example.yaml`
- Modify: `README.md`
- Create: `README.zh-CN.md`
- Modify: `docs/scan-start-guide.md`
- Modify: `CLAUDE.md`

**Interfaces:**
- Consumes: final field names, defaults, inheritance, and filtering behavior from Tasks 1-3.
- Produces: copyable English and Chinese configuration guidance with no real credentials.

- [ ] **Step 1: Update the example YAML**

Add `enable: true` to `bucket-1191` while retaining all existing safe example
values. Do not add real endpoints or tokens.

- [ ] **Step 2: Add the complete English configuration contract**

Update `README.md` to:

- link to `README.zh-CN.md` near the title;
- show `enable: false` as the only bucket opt-out;
- explain application endpoint precedence and early operational validation;
- list every top-level, scan, threshold, application, and bucket default from
  the approved design, including raw nullable concurrency defaults and the
  effective objectkeys concurrency fallback of 30.

- [ ] **Step 3: Create the standalone Chinese README**

Create `README.zh-CN.md` with Chinese sections for development setup, safe
configuration, full default tables, CLI, API, results, failure semantics, and
security. Link back to `README.md`; keep commands and field names exact.

- [ ] **Step 4: Update focused operator and architecture notes**

Add the bucket opt-out, default thresholds, endpoint inheritance, and invalid
application behavior to `docs/scan-start-guide.md`. Update the `Config` and
per-bucket pipeline paragraphs in `CLAUDE.md`. Do not rewrite unrelated
historical design or plan files.

- [ ] **Step 5: Verify documentation consistency and commit**

Run:

```powershell
rg -n "enable|107374182400|10737418240|100000|endpoint" README.md README.zh-CN.md docs/scan-start-guide.md CLAUDE.md config/apps.example.yaml
git diff --check
```

Inspect all documentation diffs for secrets and contradictory defaults, then
commit:

```powershell
git add config/apps.example.yaml README.md README.zh-CN.md docs/scan-start-guide.md CLAUDE.md
git commit -m "docs: document config defaults and bucket opt out"
```

---

### Task 5: Handoff, review, verification, and push

**Files:**
- Modify: `docs/current-task.md`
- Modify: `docs/handoff.md`

**Interfaces:**
- Consumes: committed implementation, documentation, review findings, and fresh validation output.
- Produces: a self-contained cross-machine handoff and a pushed feature branch.

- [ ] **Step 1: Run task-relevant and full validation**

Run fresh commands:

```powershell
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest tests/test_config.py tests/test_scanner.py tests/test_scan_end_to_end.py tests/test_api.py tests/test_cli.py -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m pytest -q
& '.superpowers\sdd\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check 8eaf918..HEAD
```

Record exact pass/fail counts. Fix only failures caused by this task; document
the known Windows baseline categories separately.

- [ ] **Step 2: Perform the required code review**

Use `superpowers:requesting-code-review` to inspect the complete feature diff
from `8eaf918` through `HEAD`. Address verified findings with TDD and rerun the
affected tests.

- [ ] **Step 3: Update mandatory task and handoff files**

Replace `docs/current-task.md` with this task's title, branch, status, user goal,
completed/remaining work, files, exact validation, result, risks, and next
action. Replace `docs/handoff.md` with timestamp, environment, branch, starting
commit `50c1144`, all session commits, decisions, rejected paths, test status,
uncommitted state, and exact resume commands.

- [ ] **Step 4: Run the final repository checks**

Run:

```powershell
git status --short --branch
git diff --stat
git diff
git diff --check
```

Review for unrelated changes, secrets, machine-specific paths, and incomplete
handoff data.

- [ ] **Step 5: Commit the final handoff**

```powershell
git add docs/current-task.md docs/handoff.md
git commit -m "docs: record config defaults handoff"
```

- [ ] **Step 6: Use verification-before-completion and push**

Use `superpowers:verification-before-completion`, rerun any evidence it
requires, then push:

```powershell
git push -u origin HEAD
```

Confirm `git status --short --branch` is clean and local HEAD matches
`origin/codex/obs-scan-platform`. If push fails, do not retry blindly; record
the exact command/error in `docs/handoff.md` and give the user the manual
command.
