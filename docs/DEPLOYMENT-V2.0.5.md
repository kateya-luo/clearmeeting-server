# ClearMeeting Server V2.0.5 部署说明

V2.0.5 在 V2.0.4 的声纹进程隔离基础上，修复超长离线缺口任务无法进入模型服务的问题。

## 版本信息

- `server_version=2.0.5`
- `SERVER_RELEASE=clearmeeting-server-v2.0.5`
- 发布包：`clearmeeting-server-v2.0.5-offline-http-chunking-r1.zip`
- 新能力：`offline_http_chunked_upload_v1`

## 修复原理

服务器向离线模型发送音频前，默认先按 300 秒切成 HTTP 子请求，每个约 9.16 MiB，
低于模型入口的 20 MiB 上限。模型服务收到子请求后仍按 V2.0.3 的 60 秒物理推理块处理。
因此数据库里已有的 20 分钟、30 分钟甚至更长缺口任务可以直接续跑，无需固件或数据库迁移。

可通过 `.env` 的 `OFFLINE_HTTP_CHUNK_SECONDS` 调整第一层切片，允许 30～600 秒，默认 300 秒。

## 部署

先停止离线服务并备份当前目录与 SQLite 数据库，然后解压发布包到新目录。保留旧 `.env`
和 `server/data`，把新目录切换到 `/home/luozhou/clearmeeting` 后执行：

```bash
cd /home/luozhou/clearmeeting/deploy
docker compose build server nginx speaker
docker compose up -d server nginx speaker
EXPECTED_SERVER_VERSION=2.0.5 bash ./verify_api_v2.sh http://127.0.0.1
docker compose up -d funasr-offline-cpu
```

## 验证

日志应出现 `offline_http_chunked_start`、连续的 `offline_http_chunk_done`，最后出现
`offline_http_chunked_done`。超长任务不应再出现请求尚未进入模型就发生的空 `ReadError`。

## 回退

停止 V2.0.5 容器，把部署前的 V2.0.4 完整备份恢复为
`/home/luozhou/clearmeeting` 后重新启动。V2.0.5 不修改数据库结构，回退无需数据转换。
