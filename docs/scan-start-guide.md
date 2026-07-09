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

编辑 `config/apps.yaml`，填写真实的应用 `appid`、OBS API `endpoint`、`apptoken`、桶级阈值和扫描并发配置。不要提交真实 token。

扫描结果默认写入：

```text
results/<run_id>/
```

每个桶的目录汇总 CSV 写入：

```text
results/<run_id>/<appid>/<bucket>.csv
```

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

## 结果文件说明

每次扫描会生成：

- `results/<run_id>/manifest.json`：本次扫描的应用、桶、状态和 CSV 路径。
- `results/<run_id>/scan.log`：扫描日志。
- `results/<run_id>/<appid>/<bucket>.csv`：每个桶一个目录级汇总 CSV。

最终桶 CSV 只保存目录汇总信息，不保存完整文件清单。对象级临时 CSV 在扫描过程中写入 `results/<run_id>/_tmp/`，当 `scan.keep_temp_files` 为 `false` 且桶扫描成功时会自动清理。

## 常见问题

### POST /runs 返回 404

通常是 API 启动时没有设置 `OBS_SCAN_CONFIG`。

请使用：

```bash
OBS_SCAN_CONFIG=config/apps.yaml uvicorn obs_scan_platform.api:app --reload
```

### POST /runs 返回 409

说明当前 API 进程已有扫描任务正在运行。等待当前扫描完成后再重试。

### 结果 CSV 找不到

先查看 manifest：

```bash
curl http://127.0.0.1:8000/runs/<run_id>
```

确认对应 bucket 的 `status` 是否为 `success`，以及 `csv_path` 是否存在。
