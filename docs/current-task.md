# Current Task

## Current task title

Task 5: Bucket Phase Order and Objectkeys Worker Limit

## Current branch

`codex/obs-scan-platform`

## Task status

`completed`

## User goal

实现 Task 5：每个桶必须先完成全部 filelist 扫描和 metadata 获取，再开始 objectkeys 获取；单桶 objectkeys 并发应优先使用 `objectkeys_concurrency_per_bucket`，并更新对应测试与 handoff 文档。

## Completed work

- 在 `tests/test_scanner.py` 新增 bucket phase-order 回归测试，显式验证单桶执行顺序为 `bucket_endpoint -> filelist -> metadata -> objectkeys`。
- 将 metadata 收集相关测试从 `_collect_root_files` / `root_files.csv` 改为 `_collect_metadata_files` / `metadata_files.csv`。
- 调整 objectkeys worker 并发测试，覆盖 `objectkeys_concurrency_per_bucket` 优先于 legacy `per_bucket_prefix_concurrency`。
- 更新 `tests/test_scan_end_to_end.py` 中 Task 5 相关配置写法，改为 `objectkeys_concurrency_per_bucket`。
- 在 `src/obs_scan_platform/scanner.py` 中将 `_collect_root_files` 重命名为 `_collect_metadata_files`，并把输出临时文件改为 `metadata_files.csv`。
- 确认 `_scan_bucket()` 继续按串行顺序执行：先 filelist discovery，再 metadata，再 objectkeys。
- 本地写入 `.superpowers/sdd/task-5-report.md` 记录 TDD 红/绿与实现说明，并保持其不进入 git。

## Remaining work

- 无。等待 reviewer 或后续任务继续。

## Key files changed

- `src/obs_scan_platform/scanner.py`
- `tests/test_scanner.py`
- `tests/test_scan_end_to_end.py`
- `docs/current-task.md`
- `docs/handoff.md`

## Validation commands run

- `pytest tests/test_scanner.py::test_scan_bucket_finishes_filelist_and_metadata_before_objectkeys tests/test_scanner.py::test_collect_metadata_files_uses_bucket_name_as_bucketid_and_writes_csv tests/test_scanner.py::test_collect_metadata_files_processes_all_files_with_bounded_workers tests/test_scanner.py::test_collect_prefixes_processes_all_prefixes_with_bounded_workers -v`
- `pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v`
- `/opt/homebrew/bin/git diff --check`
- `/opt/homebrew/bin/git ls-files .superpowers`

## Validation result

- RED then GREEN:
  - 首次聚焦测试结果为 2 failed / 2 passed；失败点是 `Scanner` 还没有 `_collect_metadata_files()`，说明内部 metadata 命名链尚未完成。
  - 同一轮中 phase-order 测试和 objectkeys 并发优先级测试已直接通过，说明这两项行为已由前序实现覆盖，但这次仍保留测试作为回归保护。
  - 完成 `scanner.py` 最小改动后，同组聚焦测试 4 passed。
- GREEN:
  - `pytest tests/test_scanner.py tests/test_scan_end_to_end.py -v` 32 passed。
  - `/opt/homebrew/bin/git diff --check` clean。
  - `/opt/homebrew/bin/git ls-files .superpowers` 无输出。

## Known risks

- 本次只在 Task 5 指定范围内统一了 metadata 临时文件命名和回归测试；没有改动 FastAPI 参数、最终 bucket CSV schema、日志脱敏策略或 Task 4 的按层调度语义。
- phase-order 与 objectkeys 并发优先级的生产逻辑在本轮开始前已存在，当前主要依赖新回归测试防止后续回退。

## Next recommended action

- review 本次 Task 5 提交，重点关注 `metadata_files.csv` 命名替换是否满足后续调试/排障预期。
