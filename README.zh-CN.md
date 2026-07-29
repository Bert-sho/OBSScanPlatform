# OBS Scan Platform

[English README](README.md)

OBS Scan Platform 是一个 Python 后端，用于按受控并发扫描 OBS 桶，并生成可配置的 Parquet 或 CSV 目录总览。本文档可独立用于开发环境准备、配置、启动扫描和判断结果。

## 开发环境

安装项目及开发依赖：

```bash
python -m pip install -e ".[dev]"
```

复制安全示例配置：

```bash
cp config/apps.example.yaml config/apps.yaml
```

`config/apps.yaml` 只应保存在本地。填写实际 endpoint、appid 和 token 后，不要提交该文件或把真实凭据复制到文档、日志与工单中。

## 安全配置

以下最小示例使用占位值，并展示 endpoint 继承、应用启用状态、共享桶策略、桶级阈值覆盖和精确的桶禁用方式：

```yaml
endpoint: http://obs.example

applications:
  - appid: com.camera.pergen
    name: Example application
    apptoken: replace-with-real-token
    enabled: true
    scan_shared_buckets: false
    buckets:
      bucket-with-custom-depth:
        enable: true
        filelist_depth: 8
      bucket-to-skip:
        enable: false
```

桶映射不是白名单。扫描器仍会扫描 `listbuckets` 返回且符合现有扫描资格与 owner/shared 规则的所有桶。只有桶名精确匹配并显式设置 `enable: false` 才会跳过该桶：

- 没有 YAML 桶条目：扫描；
- 有条目但没有 `enable`：扫描；
- `enable: true`：扫描；
- `enable: false`：跳过。

跳过的桶不会调用桶 endpoint、filelist、metadata 或 objectkeys，不会生成总览，也不会出现在桶级 manifest 条目中。配置中存在但 `listbuckets` 未返回的桶没有效果。

每个应用可以配置自己的 `endpoint`。解析优先级固定如下：

1. 非空的应用 `endpoint` 优先并保持不变；
2. 否则继承非空的顶层 `endpoint`；
3. 两者均为空时，有效 endpoint 保持空字符串。

应用 `endpoint: null` 是有效输入，其继承规则与缺失或空字符串相同。配置加载允许应用身份不完整，便于查看有效配置；但每个启用应用开始扫描前，扫描器会检查有效 `endpoint`、`appid` 和 `apptoken`。任一项为空时，不会创建 OBS 客户端或发送任何请求；该应用在 manifest 中记录为 `failed`、包含可操作的 `error` 且 `buckets: []`，其他启用应用继续。缺失 `name` 不会阻止扫描，`enabled: false` 的应用不会参与扫描。

## 完整默认配置契约

所有建模字段均可省略，缺失字段使用下列模型默认值，并可通过脱敏后的 `GET /config/apps` 查看。显式 `null` 对非 nullable 字段仍是配置校验错误；只有字段缺失才会启用这些字段的默认值。

### 顶层默认值

| 字段 | 缺失时默认值 |
| --- | --- |
| `endpoint` | `""` |
| `scan` | 使用全部扫描默认值 |
| `defaults` | 使用全部全局阈值默认值 |
| `applications` | `[]` |

### 扫描默认值

| 字段 | 原始缺失默认值 | 有效行为 |
| --- | --- | --- |
| `results_dir` | `"results"` | 结果根目录 |
| `temp_subdir` | `"_tmp"` | 临时文件子目录 |
| `keep_temp_files` | `false` | 扫描结束时清理桶临时目录 |
| `overview_format` | `"parquet"` | 仅允许 `parquet` 或 `csv` |
| `max_depth` | `4` | 全局 Parquet 路径截断深度，必须非负；根目录 `/` 为第 0 层 |
| `file_type_map` | 内置扩展名映射 | YAML 条目按小写扩展名规范化后合并并覆盖默认值 |
| `page_size` | `1000` | 请求分页大小 |
| `bucket_concurrency` | `4` | 单次运行内所有应用共享的桶生命周期并发 |
| `global_request_concurrency` | `150` | 所有 OBS HTTP 请求共享的并发上限 |
| `per_bucket_prefix_concurrency` | `null` | objectkeys 的旧版并发别名 |
| `objectkeys_concurrency_per_bucket` | `null` | 若设置则优先；否则使用旧别名；两者均缺失或为 `null` 时有效上限为 `30` |
| `metadata_concurrency_per_bucket` | `8` | 单桶 metadata worker 并发 |
| `request_timeout_seconds` | `30` | 请求超时秒数 |
| `keepalive_expiry_seconds` | `5.0` | 使连接池中的空闲连接过期；必须为正数 |
| `max_retries` | `3` | 首次请求失败后最多重试三次 |
| `retry_base_delay_seconds` | `2` | 重试基础延迟秒数 |
| `retry_max_delay_seconds` | `60` | 重试最大延迟秒数 |
| `filelist_task_limit_per_bucket` | `100` | 单桶 filelist 目录任务递归阈值 |
| `metadata_task_limit_per_bucket` | `10000` | 单桶累计 metadata 任务阈值 |
| `aggregation_max_directories_in_memory` | `100000` | 单个聚合块的目录统计数量上限，必须为正数 |

`keepalive_expiry_seconds` 控制连接池中空闲连接可复用的时长。默认值 `5.0` 秒会使空闲连接过期；它不会在五秒后终止活动请求，会继续启用连接复用，并与现有重试路径协同工作。

`objectkeys_concurrency_per_bucket` 的模型原始默认值是 `null`，不是 `30`；`30` 是它和 `per_bucket_prefix_concurrency` 都没有值时的运行时有效回退值。新配置建议使用 `objectkeys_concurrency_per_bucket`。

### 全局阈值默认值

| 字段 | 缺失时默认值 |
| --- | --- |
| `large_directory_bytes` | `107374182400`（100 GiB） |
| `large_file_bytes` | `10737418240`（10 GiB） |
| `inactive_directory_days` | `180` |
| `filelist_depth` | `5` |

### 应用默认值

| 字段 | 缺失时默认值 |
| --- | --- |
| `appid` | `""` |
| `name` | `""` |
| `endpoint` | `""`，随后按规则继承非空顶层 endpoint |
| `apptoken` | `""` |
| `enabled` | `true` |
| `scan_shared_buckets` | `false` |
| `buckets` | `{}` |

### 桶默认值

| 字段 | 缺失时默认值/有效行为 |
| --- | --- |
| `enable` | `true` |
| `large_directory_bytes` | `null`；继承全局值（默认 `107374182400`） |
| `large_file_bytes` | `null`；继承全局值（默认 `10737418240`） |
| `inactive_directory_days` | `null`；继承全局值（默认 `180`） |
| `filelist_depth` | `null`；继承全局值（默认 `5`） |

桶级阈值字段允许 `null`，显式 `null` 表示“不覆盖全局值”。这与非 nullable 字段不同：非 nullable 字段的显式 `null` 会导致配置校验失败。

## 扫描流程与并发

应用没有独立的扫描并发上限。`bucket_concurrency` 是单次运行内所有应用共享的桶并发，覆盖一个桶从开始到临时目录最终处理完毕的完整生命周期。`listbuckets` 不占用桶并发许可，但和所有其他 HTTP 请求一样受 `global_request_concurrency` 限制。

每个桶先完成全部 filelist 目录发现和 metadata 获取，然后对剩余的非重叠前缀执行 objectkeys，最后按 `overview_format` 生成 Parquet 或 CSV 总览。`filelist_task_limit_per_bucket` 决定是否继续到更深层级，不会截断当前层已经发现的任务。`metadata_task_limit_per_bucket` 在每个完整 BFS 层结束后检查累计 metadata 任务数；超限时回滚该层，根层超限时改由 objectkeys 从 `/` 扫描。

`aggregation_max_directories_in_memory` 控制每个聚合块中目录统计字典的条目数量，而不是精确内存字节数。Parquet 模式中每个对象只归入一个路径；旧 CSV 模式仍计入所在目录及全部祖先。

### 全局聚合/请求阶段

一次扫描运行内，所有应用和桶的聚合并发固定为一。聚合开始等待后，不会启动新的请求尝试或重试；已经准入的响应会先完成读取、解析以及当前页面的同步处理，之后才开始聚合。所有排队的 CSV 和 Parquet 聚合会串行完成，随后暂停的请求恢复。此行为不是 YAML 选项，也不协调独立进程或独立扫描运行。

## 命令行扫描

扫描全部启用应用：

```bash
obs-scan scan --config config/apps.yaml
```

只扫描指定应用：

```bash
obs-scan scan --config config/apps.yaml --appid com.camera.pergen
```

指定固定运行 ID：

```bash
obs-scan scan --config config/apps.yaml --run-id manual-run-001
```

查看帮助：

```bash
obs-scan --help
obs-scan scan --help
```

CLI 会显示每个桶的 filelist 和 objectkeys `tqdm` 进度条；详细进度同时写入 `results/<run_id>/scan.log`。扫描状态非 `success` 时，命令以非 0 状态退出。

## API 服务

使用配置文件启动 FastAPI：

```bash
OBS_SCAN_CONFIG=config/apps.yaml uvicorn obs_scan_platform.api:app --reload
```

当前版本通过进程内 `active_scan` 标记防止重复触发，因此生产运行使用单 worker：

```bash
OBS_SCAN_CONFIG=config/apps.yaml uvicorn obs_scan_platform.api:app --host 0.0.0.0 --port 8000 --workers 1
```

常用接口：

- `GET /health`
- `GET /config/apps`：查看已展开默认值和 endpoint 继承结果的脱敏配置；`apptoken` 显示为 `******`
- `POST /runs`：触发扫描，正常返回 HTTP `202 Accepted`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/logs`
- `GET /runs/{run_id}/apps/{appid}/buckets/{bucket_name}/csv`
- `GET /runs/{run_id}/apps/{appid}/buckets/{bucket_name}/parquet/{part_name}`

API 触发的扫描不显示终端进度条，但会写入相同的 `scan.log`。当前接口按配置扫描全部启用应用；需要指定 `appid` 或 `run_id` 时请使用 CLI。

## 结果说明

默认结果目录：

```text
results/<run_id>/
```

默认 `overview_format: parquet`。每个成功聚合的桶写入 Snappy 压缩的 Parquet 分片：

```text
results/<run_id>/<appid>/<bucket>/part-00001.parquet
results/<run_id>/<appid>/<bucket>/part-00002.parquet
```

单个文件最多 50,000 行。Parquet 的 10 个 non-null 字段依次为 `bucket_id`、`bucket_name`、`appid`、`path`、`object_count`、`total_size`、`max_file_size`、`last_modified`、`max_depth`、`file_types`。`last_modified` 是最新的 UTC `YYYY-MM-DD`；全部修改时间缺失时使用扫描开始日。`file_types` 是按字符串升序去重后的 JSON 数组，未知或无扩展名归为 `其他`。

每个对象只归入一个 Parquet `path`。`max_depth: 4` 时，`a/direct.txt` 只归入 `/a/`，`a/b/c/d/e/deep.jpg` 截断并归入 `/a/b/c/d/`。小于第 4 层的路径只统计直属文件，第 4 层同时统计更深后代；行内 `max_depth` 是当前 `path` 自身深度。

设置 `scan.overview_format: csv` 可继续生成原有文件：

```text
results/<run_id>/<appid>/<bucket>.csv
```

其字段、路径和向所有祖先累计的规则保持不变。每次运行还会写入：

- `results/<run_id>/manifest.json`：运行、应用、桶、状态、错误、总览路径和耗时；
- `results/<run_id>/scan.log`：filelist、metadata、objectkeys 与聚合进度；

桶级 manifest 新增 `overview_format`、`overview_path` 和有序的 `overview_files`。兼容字段 `csv_path` 仅在 CSV 模式有值，Parquet 模式为 `null`。

对象级临时 CSV 位于 `results/<run_id>/_tmp/`。`keep_temp_files: false` 时，每个桶结束后清理其临时目录；设为 `true` 会保留 detail CSV 和聚合中间文件，可能占用大量磁盘。空桶和只有空文件夹的桶仍可成功：Parquet 模式生成一个带完整 schema 的 0 行分片，CSV 模式生成只有表头的文件。

使用总览前先检查 manifest 中对应桶的状态：

- `success`：聚合完成，且没有最终的可恢复请求失败；
- `partial_failed`：Parquet 或 CSV 不完整，需结合 `error`、`partial_errors` 和 `errors` 评估；
- `failed`：桶或应用失败，不能假设存在可用总览。

## 失败语义

配置阶段会拒绝 YAML 语法错误、无效类型，以及非 nullable 字段的显式 `null`。运行阶段按以下边界隔离失败：

- 启用应用缺少有效 `endpoint`、`appid` 或 `apptoken`：该应用在发送任何 OBS 请求前失败，`buckets: []`，其他应用继续；
- `listbuckets`：当前应用失败，因为桶集合未知，其他应用继续；
- `bucket_endpoint`：当前桶失败且无总览，其他桶继续；
- `filelist`：失败目录停止，已成功页面与其他目录保留，桶为 `partial_failed`；
- `metadata`：只跳过失败对象，其他对象继续，桶为 `partial_failed`；
- `objectkeys`：只停止失败前缀的后续页面，其他前缀继续，桶为 `partial_failed`。

`enable: false` 是配置选择而不是失败：该桶不生成失败或跳过状态的 manifest 桶条目。

## 安全注意事项

- 不要提交 `config/apps.yaml`、真实 endpoint、appid/token 组合或其他凭据。
- `GET /config/apps` 会遮蔽 `apptoken`，但仍应限制配置接口访问。
- 正常成功请求不会记录完整 OBS URL；失败尝试可能记录未脱敏的已准备 URL和最多 2048 个响应字符。
- `scan.log` 和 `manifest.json` 可能包含 token、编码请求体、对象键、分页游标或服务响应信息，应作为敏感数据限制访问，审查前不要提交或分享。
- 下载 CSV/Parquet 和读取日志的 API 路径参数必须使用实际运行 ID、应用 ID 和桶名，不要公开暴露 API 服务。Parquet 下载还会校验分片名和 manifest 成员关系。

更聚焦的启动与接口示例见 [扫描启动指南](docs/scan-start-guide.md)。
