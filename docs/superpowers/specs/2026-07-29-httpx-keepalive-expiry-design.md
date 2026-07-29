# HTTPX Keep-Alive Expiry Configuration Design

## Background

An OBS `objectkeys` request can fail with `httpx.RemoteProtocolError` when the
remote peer disconnects before returning response headers. The reported
failure occurred on the final task of the final bucket, which is consistent
with an idle pooled connection being closed by the server before the client
tries to reuse it. The available log does not prove the server-side timeout,
so this change is a keep-alive mitigation rather than a claim that every
remote disconnect has the same cause.

The scanner currently creates one shared `httpx.AsyncClient` per application
with a request timeout but without an explicit connection-pool keep-alive
expiry. HTTPX therefore uses its library default.

## Goals

- Add `scan.keepalive_expiry_seconds` to YAML configuration.
- Default the setting to `5.0` seconds when it is omitted.
- Require a positive number so invalid expiry values fail during config load.
- Pass the configured value to HTTPX as `Limits.keepalive_expiry`.
- Preserve the existing shared-client, retry, timeout, and concurrency behavior.
- Document that this value controls idle pooled-connection lifetime, not the
  duration allowed for an HTTP request.

## Non-goals

- Sending `Connection: close` or disabling connection reuse.
- Discovering or changing the server's keep-alive timeout.
- Changing HTTPX's effective maximum connection or maximum keep-alive connection counts; the current AsyncClient defaults are supplied explicitly when customizing expiry.
- Changing request timeouts, retry counts, retry delays, or OBS error handling.
- Guaranteeing that `RemoteProtocolError` cannot occur for other network or
  server-side causes.

## Configuration Model

Add the following field to `ScanSettings`:

```python
keepalive_expiry_seconds: float = Field(default=5.0, gt=0)
```

The example YAML will expose the default alongside the existing request
timeout:

```yaml
scan:
  keepalive_expiry_seconds: 5.0
```

An integer such as `5` or a positive decimal is accepted and represented as a
float by the configuration model. Zero and negative values are rejected.
Existing YAML files remain compatible because omission selects `5.0`.

## Client Construction and Data Flow

When the scanner creates the per-application `httpx.AsyncClient`, it constructs
an `httpx.Limits` value using the loaded setting:

```python
limits = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=self.config.scan.keepalive_expiry_seconds,
)
```

The limits object is passed to `httpx.AsyncClient` together with the existing
request timeout. HTTPX 0.28.1's effective AsyncClient defaults of `100` maximum
connections and `20` maximum keep-alive connections are supplied explicitly,
because constructing `httpx.Limits` with only an expiry would otherwise make
both limits unbounded.

HTTPX may reuse an idle pooled connection for up to the configured expiry.
After the connection has been idle longer than that value, it is discarded
instead of being selected for a later request. Active requests are not
terminated after five seconds; `scan.request_timeout_seconds` continues to
control request timeout behavior.

## Error Handling

The existing `OBSClient` handling remains unchanged. A
`RemoteProtocolError` is still an `httpx.RequestError` and continues through
the configured retry path. Reducing the client idle expiry limits the window
for stale connection reuse, while retries continue to handle races and other
transient disconnects that can still occur.

## Testing

Tests will be written before production changes and will cover:

- `ScanSettings` defaulting `keepalive_expiry_seconds` to `5.0`;
- YAML overriding the value with a positive decimal;
- YAML rejecting zero and negative values;
- scanner client construction passing the configured value through
  `httpx.Limits.keepalive_expiry` while preserving the request timeout and the
  effective `100`/`20` connection caps;
- the existing configuration, scanner, and end-to-end tests remaining green.

The client-construction test will replace only the external HTTP client
boundary and inspect the real `httpx.Limits` value supplied by scanner code.

## Documentation

- Add the setting to `config/apps.example.yaml`.
- Add it to the configuration tables and keep-alive explanation in
  `README.md` and `README.zh-CN.md`.
- Update `docs/scan-start-guide.md` where scan HTTP behavior is described.
- Complete the mandatory `docs/current-task.md` and `docs/handoff.md` records.

## Success Criteria

- Omitting the YAML setting results in a client keep-alive expiry of `5.0`
  seconds.
- A positive configured value is passed unchanged to HTTPX.
- Zero and negative values fail configuration validation.
- Connection reuse remains enabled and no `Connection: close` header is added.
- Task-relevant and full tests pass.
- The implementation and handoff are committed and pushed from the feature
  branch without unrelated changes or secrets.
