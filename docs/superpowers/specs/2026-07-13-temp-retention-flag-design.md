# Temporary File Retention Flag Design

## Goal

Make the existing `scan.keep_temp_files` flag control temporary bucket files consistently for every terminal bucket status.

## Configuration Contract

The existing configuration remains unchanged:

```yaml
scan:
  keep_temp_files: false
```

- `false` (default): remove the bucket temporary directory after manifest conversion for `success`, `partial_failed`, and `failed` results.
- `true`: retain the bucket temporary directory for every result status and expose its path as `temp_dir` in the bucket manifest.

No second retention flag or enum is introduced.

## Architecture and Data Flow

`Scanner._bucket_result_to_manifest()` remains the single retention boundary. After constructing the bucket manifest, it receives the bucket `temp_dir` and applies retention solely from `self.config.scan.keep_temp_files`:

1. If no `temp_dir` was supplied, return the manifest unchanged.
2. If `keep_temp_files` is `false`, remove the directory when it exists and return the manifest without `temp_dir`.
3. If `keep_temp_files` is `true`, preserve the directory and include its path in `manifest['temp_dir']` regardless of bucket status.

The scanner pipeline, aggregation, status calculation, error reporting, and final CSV behavior do not change.

## Error Handling

Missing temporary directories are valid and require no action. Existing filesystem deletion errors continue to propagate; this design does not add silent failure handling or retry behavior.

## Testing

Parameterized unit coverage will verify both flag values across `success`, `partial_failed`, and `failed` results:

- `false`: directory removed and `temp_dir` absent from the manifest.
- `true`: directory retained and `temp_dir` present in the manifest.

Existing end-to-end tests continue to verify successful-scan cleanup and retained partial-failure diagnostics. Documentation will state the new all-status semantics.

## Documentation

Update `README.md`, `docs/scan-start-guide.md`, `config/apps.example.yaml` comments if appropriate, `docs/current-task.md`, and `docs/handoff.md`. The default remains `false`, so existing configuration files remain valid while failed-scan retention behavior intentionally changes.

## Non-Goals

- Per-status retention policies.
- Per-application or per-bucket retention overrides.
- Retention duration or scheduled cleanup.
- Changing the `_tmp` directory layout.
