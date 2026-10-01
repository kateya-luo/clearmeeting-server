# ClearMeeting Server V2.0.3 部署说明

V2.0.3 在 V2.0.2 的持久化单 worker、公平轮转队列基础上，将每个 5 分钟离线
预览任务在服务器内部真正拆成最多 60 秒的独立 FunASR 调用。固件上传协议、10 MiB
补传范围和数据库中的 5 分钟任务颗粒度均不改变。

## 版本与能力

- `server_version=2.0.3`
- `SERVER_RELEASE=clearmeeting-server-v2.0.3`
- 新能力：`offline_preview_chunked_60s_v1`

## 数据安全

部署前必须同时保留：

1. 当前完整应用目录；
2. SQLite 一致性备份；
3. `deploy/.env`；
4. 当前 `docker-compose.yml`。

发布包不包含数据库、音频、账号密钥和 `.env`。

## 关键配置

```ini
OFFLINE_ASR_WORKERS=1
OFFLINE_CANONICAL_PREVIEW_CHUNK_SECONDS=60
OFFLINE_CANONICAL_BATCH_SIZE_S=60
SERVER_RELEASE=clearmeeting-server-v2.0.3
```

`OFFLINE_CANONICAL_PREVIEW_CHUNK_SECONDS` 控制服务器外层真实 PCM 切分；
`OFFLINE_CANONICAL_BATCH_SIZE_S` 只控制单个子块内部的 FunASR 批次。

离线容器固定在逻辑 CPU 2～7，并把 OpenMP/MKL/OpenBLAS 限制为 2 线程，同时使用
`MALLOC_ARENA_MAX=2`。这些设置用于保护系统 CPU 0，并限制 native 分配器碎片化。

## 验收

```bash
EXPECTED_SERVER_VERSION=2.0.3 bash ./verify_api_v2.sh http://127.0.0.1
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' \
  ai-recorder-system-funasr-offline-cpu-1 | \
  grep OFFLINE_CANONICAL_PREVIEW_CHUNK_SECONDS
```

模型健康接口在工作时应显示 `part_index`、`part_count`、`part_start_ms` 和
`part_end_ms`。同一个持久任务重试时，已完成的 `transcribe-part` 缓存必须被复用。

## 回退

停止 V2.0.3 后，将完整 V2.0.2 备份目录恢复为
`/home/luozhou/clearmeeting`，恢复对应 `.env`，再启动原 compose。V2.0.3 不修改
现有数据库结构，回退不需要迁移或删除数据。
