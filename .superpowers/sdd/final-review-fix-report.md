2026-07-10 19:35:04 +08:00

- Fixed child `filelist` rollback for paginated failures by adding `FilelistDiscoveryScheduler.rollback_failed_task()` and using it in the child failure path.
- The rollback prunes the failed prefix, descendant prefixes, queued descendant tasks/paths, and direct files under the failed subtree before completion.
- Sanitized child `filelist` failure logs with `_sanitize_reason(...)` so exception URLs/tokens are not emitted.
- Added regression coverage for page-1 discovery followed by page-2 child failure, plus filelist failure log sanitization.
- Tests run:
  - `& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_scanner.py::test_discover_root_prunes_partial_child_filelist_results_after_paginated_failure tests/test_scanner.py::test_discover_root_sanitizes_child_filelist_failure_logs -q`
  - `& 'C:\Users\lzh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/test_obs_client.py tests/test_scanner.py tests/test_scan_end_to_end.py -q`
- Results:
  - `2 passed in 0.43s`
  - `69 passed in 0.89s`
