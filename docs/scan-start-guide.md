# OBS 扫描功能启动方式指导

本文档说明如何启动 OBS 扫描功能，包括命令行启动和通过 FastAPI 接口启动两种方式。

## 前置准备

先安装开发环境依赖：

```bash
python -m pip install -e ".[dev]"
```

准备配置文件：

```bash
cp config/apps.example.yaml config/apps.yaml
```

编辑 `config/apps.yaml`，填写真实的全局 OBS API `endpoint`、应用 `appid`、`apptoken`、桶级覆盖参数和扫描并发配置。不要提交真实 token。

推荐只在 YAML 顶层配置一次 OBS API endpoint：

```yaml
endpoint: http://obs.example
```

应用可以显式配置自己的 `endpoint`，其优先级高于顶层值；应用 `endpoint` 缺失、为空字符串或为 `null` 时会继承非空的顶层 `endpoint`。如果两处都没有非空值，最终 endpoint 为空。

配置加载允许应用的运行身份暂时不完整，但扫描启用应用前会先检查最终 `endpoint`、`appid` 和 `apptoken`。任一项为空时，不会创建 OBS 客户端或发送请求；该应用在 manifest 中记录为 `failed`、`buckets: []`，其他启用应用继续扫描。`name` 为空不影响扫描。

每个应用默认不扫描共享桶，如需纳入共享桶可显式开启：

```yaml
applications:
  - appid: com.camera.pergen
    name: Example application
    apptoken: replace-with-real-token
    scan_shared_buckets: false
```

当 `scan_shared_buckets: true` 时，扫描器会纳入字段完整且可尝试扫描的共享桶，要求桶记录具备 bucket id、name、vendor、region。

目录发现使用递归 `filelist` 拆分。默认 `filelist_depth` 为 `5`，每个桶的 filelist 目录任务数由 `scan.filelist_task_limit_per_bucket` 控制，默认约 `100` 个任务。桶可以只覆盖自己的 filelist 深度，不需要重复配置三个阈值：

```yaml
defaults:
  large_directory_bytes: 107374182400
  large_file_bytes: 10737418240
  inactive_directory_days: 180
  filelist_depth: 5

applications:
  - appid: com.camera.pergen
    name: Example application
    apptoken: replace-with-real-token
    buckets:
      bucket-1191:
        enable: true
        filelist_depth: 8
```

桶配置映射不是白名单。未配置的桶、未写 `enable` 的桶以及 `enable: true` 的桶都会按现有资格规则扫描；只有桶名精确匹配且显式设置 `enable: false` 才会跳过：

```yaml
applications:
  - appid: com.camera.pergen
    buckets:
      bucket-to-skip:
        enable: false
```

跳过的桶不会调用桶 endpoint、filelist、metadata 或 objectkeys，不生成总览，也不出现在桶级 manifest 条目中。配置中存在但 `listbuckets` 未返回的桶不产生影响。

全局阈值缺失时的默认值为：大目录 `large_directory_bytes: 107374182400`（100 GiB）、大文件 `large_file_bytes: 10737418240`（10 GiB）、不活跃目录 `inactive_directory_days: 180`、发现深度 `filelist_depth: 5`。桶级对应字段缺失或显式为 `null` 时继承全局阈值。

`scan.filelist_task_limit_per_bucket` 是是否继续递归到更深层级的目标阈值，不会截断当前层级已经发现并纳入队列的目录任务。

推荐并发默认值：

```yaml
scan:
  overview_format: parquet
  aggregation_depth: 4
  bucket_concurrency: 4
  global_request_concurrency: 150
  metadata_concurrency_per_bucket: 8
  objectkeys_concurrency_per_bucket: 30
```

`scan.keepalive_expiry_seconds` 默认值为 `5.0` 秒，用于使连接池中的空闲连接过期；它不会在五秒后终止活动请求，会继续启用连接复用，并与现有重试路径协同工作。

应用没有独立的扫描并发限制。`bucket_concurrency` 是所有应用共享的单次运行全局桶并发限制，覆盖每个桶从开始扫描到临时目录最终处理完成的完整生命周期。各应用的 `listbuckets` 不占用桶并发容量，但与其他所有 HTTP 请求一样占用 `global_request_concurrency` 容量。

`metadata_concurrency_per_bucket` 和 `objectkeys_concurrency_per_bucket` 分别限制单个桶内对应 worker 的并发；`filelist` 和 metadata 请求仍受全局请求并发限制。旧配置项 `per_bucket_prefix_concurrency` 仍兼容，但新配置建议使用 `objectkeys_concurrency_per_bucket`。

每个桶会先完成全部 `filelist` 发现和 metadata 获取，再开始 `objectkeys` 获取。

### 全局聚合/请求阶段

一次扫描运行中，所有应用和桶的聚合并发固定为一。聚合等待时，不会启动新的请求尝试或重试；已经准入的响应会先完成读取、解析和当前页面的同步处理，再进入聚合。所有排队的 CSV 和 Parquet 聚合串行执行完毕后，暂停的请求才会恢复。这个固定行为不是 YAML 选项，也不会协调独立进程或独立扫描运行。

扫描结果默认写入：

```text
results/<run_id>/
```

默认每个桶的 Parquet 总览写入：

```text
results/<run_id>/<appid>/<bucket>/part-00001.parquet
```

Parquet 使用 Snappy 压缩，单个分片最多 50,000 行。`aggregation_depth: 4` 时，根目录 `/` 是第 0 层；第 4 层以下的对象截断并累计到第 4 层，而较浅目录只统计直属文件。旧配置名 `max_depth` 仅在未配置 `aggregation_depth` 时兼容，两个名称同时出现会导致配置校验失败。`file_type_map` 的完整默认映射见 `config/apps.example.yaml`，自定义扩展名会与默认值合并。

如需原有 CSV，设置 `scan.overview_format: csv`，输出仍为 `results/<run_id>/<appid>/<bucket>.csv`，原字段和向祖先累计规则不变。

## 方式一：命令行启动扫描

### 扫描所有启用应用

```bash
obs-scan scan --config config/apps.yaml
```

扫描完成后，命令行会输出类似：

```text
scan finished: success run_id=20260709T010203Z
```

如果扫描状态不是 `success`，命令会以非 0 退出码结束。

命令行扫描会为每个桶的 `filelist` 目录发现和 `objectkeys` 前缀收集显示 `tqdm` 进度条。进度条不会打印每个成功的 OBS 请求。

正常成功请求的完整 URL 会被抑制。但每次失败尝试都会记录未脱敏的已准备 URL、最多 2048 个响应字符、尝试次数、状态、原因和截断信息。默认对可重试失败在首次请求后最多重试 3 次，即最多 4 次尝试。

`scan.log` 和 `manifest.json` 必须按敏感数据处理：失败 URL 可能包含 token、编码请求体、对象键和分页游标，失败响应也可能暴露服务信息。请限制访问，未审查前不要提交或对外分享。

### 只扫描一个应用

```bash
obs-scan scan --config config/apps.yaml --appid com.camera.pergen
```

`--appid` 必须和 `config/apps.yaml` 中的应用 ID 一致。

### 指定固定 run_id

```bash
obs-scan scan --config config/apps.yaml --run-id manual-run-001
```

指定后结果会写入：

```text
results/manual-run-001/
```

也可以同时指定应用和 run_id：

```bash
obs-scan scan --config config/apps.yaml --appid com.camera.pergen --run-id manual-run-001
```

### 查看命令帮助

```bash
obs-scan --help
obs-scan scan --help
```

## 方式二：通过 FastAPI 接口启动扫描

### 启动 API 服务

通过环境变量指定配置文件：

```bash
OBS_SCAN_CONFIG=config/apps.yaml uvicorn obs_scan_platform.api:app --reload
```

生产或长时间运行时建议使用单 worker：

```bash
OBS_SCAN_CONFIG=config/apps.yaml uvicorn obs_scan_platform.api:app --host 0.0.0.0 --port 8000 --workers 1
```

当前版本的 API 使用进程内 `active_scan` 标记防止重复触发扫描，多 worker 之间不会共享这个标记。

### 健康检查

```bash
curl http://127.0.0.1:8000/health
```

预期返回：

```json
{"status":"ok"}
```

### 查看脱敏后的配置

```bash
curl http://127.0.0.1:8000/config/apps
```

返回内容会隐藏 `apptoken`。

### 触发扫描

```bash
curl -X POST http://127.0.0.1:8000/runs
```

预期返回 HTTP `202 Accepted`：

```json
{"status":"accepted"}
```

如果已有 API 触发的扫描正在运行，会返回 HTTP `409 Conflict`。

注意：当前 FastAPI 启动扫描时会按配置文件扫描所有启用应用；接口暂不支持传入 `appid` 或 `run_id`。需要按应用或固定 run_id 扫描时，请使用命令行方式。

FastAPI 启动扫描时不会显示 `tqdm` 终端进度条，但会把同样的 filelist 和 objectkeys 进度写入 `results/<run_id>/scan.log`。请通过该文件或 `/runs/<run_id>/logs` 查看。

### 查看扫描运行列表

```bash
curl http://127.0.0.1:8000/runs
```

### 查看单次扫描 manifest

```bash
curl http://127.0.0.1:8000/runs/<run_id>
```

示例：

```bash
curl http://127.0.0.1:8000/runs/manual-run-001
```

### 查看扫描日志

```bash
curl http://127.0.0.1:8000/runs/<run_id>/logs
```

### 下载桶扫描结果 CSV

```bash
curl -o bucket.csv \
  http://127.0.0.1:8000/runs/<run_id>/apps/<appid>/buckets/<bucket_name>/csv
```

示例：

```bash
curl -o owned-bucket.csv \
  http://127.0.0.1:8000/runs/manual-run-001/apps/com.camera.pergen/buckets/owned-bucket/csv
```

CSV 下载接口仅适用于 `overview_format: csv`。Parquet 模式先从 manifest 的 `overview_files` 获取分片名，再逐个下载：

```bash
curl -o part-00001.parquet \
  http://127.0.0.1:8000/runs/<run_id>/apps/<appid>/buckets/<bucket_name>/parquet/part-00001.parquet
```

## 结果文件说明

每次扫描会生成：

- `results/<run_id>/manifest.json`：本次扫描的应用、桶、状态、总览路径、错误和时间。桶条目包含 `overview_format`、`overview_path` 和 `overview_files`；兼容字段 `csv_path` 仅在 CSV 模式有值。
- `results/<run_id>/scan.log`：扫描日志，包含 filelist 进度、metadata 的 `completed` / `total` / `succeeded` / `failed` 进度，以及 objectkeys 前缀的 `completed` / `total` / `succeeded` / `failed` / `pages` / `objects` 进度。metadata 的 `total` 是 filelist 生成的 metadata 任务数；当 `total=0` 时只写一条 `metadata skipped` 记录。
- `results/<run_id>/<appid>/<bucket>/part-xxxxx.parquet`：默认的 Snappy Parquet 总览分片，每个最多 50,000 行。
- `results/<run_id>/<appid>/<bucket>.csv`：仅在 `overview_format: csv` 时生成的旧目录级汇总。

Parquet 字段依次为 `bucket_id`、`bucket_name`、`appid`、`path`、`object_count`、`total_size`、`max_file_size`、`last_modified`、`max_depth`、`file_types`，全部 non-null。`last_modified` 是最新 UTC 日期 `YYYY-MM-DD`，全部时间缺失时使用扫描开始日；`max_depth` 是该行所有文件原始所在目录的最大层级且不包含文件名，例如 `/a/b/file.txt` 为 2、`/a/b/c/d/e/file.txt` 为 5；`file_types` 是去重排序后的 JSON 数组，未知或无扩展名归为 `其他`。

最终总览不保存完整文件清单。对象级临时 CSV 在扫描过程中写入 `results/<run_id>/_tmp/`。`scan.keep_temp_files` 默认为 `false`：每个桶一得到最终结果就会立即删除对应临时目录，然后才释放共享桶并发许可，无论桶最终为 `success`、`partial_failed` 还是 `failed`；设为 `true` 时则保留所有状态的临时目录。

如果桶为空，或桶内只有空文件夹，扫描仍会成功；Parquet 模式生成一个带完整 schema 的 0 行分片，CSV 模式生成只有表头的文件。

### 请求失败边界

- `listbuckets`：桶集合未知，当前应用失败，其他应用继续。
- `bucket_endpoint`：当前桶失败且不生成总览，其他桶继续。
- `filelist`：停止失败目录（包括根目录 `/`）的剩余分页，保留早先成功页和其他已发现目录，桶为 `partial_failed`。
- `metadata`：只跳过失败对象，其他对象继续，桶为 `partial_failed`。
- `objectkeys`：停止失败前缀的剩余分页，保留早先成功页和其他前缀，桶为 `partial_failed`。

### 如何判读部分总览

使用 Parquet 或 CSV 前必须先检查 manifest 中对应桶的 `status`。`success` 表示聚合完成且没有最终可恢复请求失败；`partial_failed` 表示总览不完整。此时：

- `error` 是简短汇总；
- `partial_errors` 保留兼容计数和有限样本；
- `errors` 保留每个最终请求失败的详细记录。

部分总览会保留已成功收集的行，并缺少只能从失败请求获得的数据。未检查上述状态和错误字段前，不得将其视为完整结果。每个桶还有 `started_ms` / `ended_ms`、UTC `started_at` / `ended_at` 和单调计时的 `elapsed_seconds`。总耗时进一步拆分为 `request_elapsed_seconds`（bucket endpoint、filelist、metadata、objectkeys 的采集阶段，包含并发等待、重试退避和响应解析）与 `processing_elapsed_seconds`（读取临时 CSV、聚合并生成最终总览）。请求阶段失败时处理时间为 `0.0`；处理阶段失败时保留两个阶段已经发生的实际耗时。桶之间并发执行，因此不能把各桶阶段耗时简单相加当作整次扫描的墙钟时间。

## 常见问题

### POST /runs 返回 404

通常是 API 启动时没有设置 `OBS_SCAN_CONFIG`。

请使用：

```bash
OBS_SCAN_CONFIG=config/apps.yaml uvicorn obs_scan_platform.api:app --reload
```

### POST /runs 返回 409

说明当前 API 进程已有扫描任务正在运行。等待当前扫描完成后再重试。

### 结果总览找不到

先查看 manifest：

```bash
curl http://127.0.0.1:8000/runs/<run_id>
```

确认对应 bucket 的 `status`、`overview_format`、`overview_path` 和 `overview_files`。CSV 模式可继续查看 `csv_path`。如果是 `partial_failed`，总览可能存在，但必须结合 `error`、`partial_errors` 和 `errors` 判断缺失范围。
