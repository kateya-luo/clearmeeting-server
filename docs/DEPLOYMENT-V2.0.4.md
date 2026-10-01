# ClearMeeting Server V2.0.4 部署说明

V2.0.4 在 V2.0.3 的真实 60 秒离线转写切片基础上，隔离 CAM++ 声纹模型的
原生进程故障。HTTP 服务作为稳定父进程运行，CAM++ 在独立工作子进程内加载；
子进程段错误、超时或推理异常时，当前声纹片段失败并自动创建新工作进程，不能再
带走整个 Speaker 服务。

## 版本

- `server_version=2.0.4`
- `SERVER_RELEASE=clearmeeting-server-v2.0.4`
- 发布包：`clearmeeting-server-v2.0.4-speaker-isolation-r1.zip`
- 新能力：`speaker_worker_isolation_v1`

## Speaker 安全参数

```ini
SPEAKER_WORKER_MAX_REQUESTS=20
SPEAKER_WORKER_STARTUP_TIMEOUT=180
SPEAKER_WORKER_REQUEST_TIMEOUT=30
```

每处理 20 个成功声纹片段后，工作进程在下一次请求前主动轮换。每个请求记录请求
编号、PCM 字节数、采样数、时长、RMS、峰值和 SHA-256，不记录音频正文。

## 部署

部署前必须完整保留当前程序目录、`.env` 和 SQLite 数据库快照。解压后继承原
`.env`，将 `SERVER_RELEASE` 设置为 `clearmeeting-server-v2.0.4`，然后执行：

```bash
cd /home/luozhou/clearmeeting/deploy
docker compose --profile real-asr --profile offline-asr-cpu build speaker server nginx funasr-offline-cpu
docker compose --profile real-asr up -d speaker server nginx
EXPECTED_SERVER_VERSION=2.0.4 bash ./verify_api_v2.sh http://127.0.0.1
```

验证 Speaker 健康响应中的 `worker.ready=true`、`generation` 和 `served`。受控恢复
离线任务后，观察 `speaker_request`、`speaker_worker_recycle_scheduled`、
`speaker_worker_lost` 和内核段错误日志。

## 回退

停止 V2.0.4 容器，将部署前完整备份目录恢复为
`/home/luozhou/clearmeeting`，恢复对应 `.env` 和数据库快照，再使用原 Compose
启动。V2.0.4 不引入数据库结构迁移，回退无需数据转换。
