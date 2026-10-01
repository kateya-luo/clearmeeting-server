# ClearMeeting Server V2.0.2 部署说明

V2.0.2 修复大量离线语音同时进入时，4 个 HTTP 请求挤入单模型、超时后重复
提交并最终停止出结果的问题。固件协议仍为 `luoye-device-api/2`，无需升级固件。

## 本版核心行为

- SQLite 可以持久保存任意数量的离线任务，但模型同时只处理 1 项。
- 同一会议严格按 `chunk_index` 处理，不同会议 round-robin 轮流取一段。
- `bulk/gap -> canonical -> publish` 阶段屏障保持不变。
- 请求携带稳定 `X-Offline-Job-Id`；完成结果持久缓存，重试不重复推理。
- 模型忙、连接失败和读取超时计入 `transient_attempts`，不消耗业务 `attempts`。
- 从 V2.0.1 升级时，未完成任务中由连接故障造成的旧重试次数自动迁移。

## 1. 上传并校验发布包

把以下文件传到 `/home/luozhou/`：

```text
clearmeeting-server-v2.0.2-offline-queue-r1.zip
clearmeeting-server-v2.0.2-offline-queue-r1.zip.sha256
```

服务器执行：

```bash
cd /home/luozhou
sha256sum -c clearmeeting-server-v2.0.2-offline-queue-r1.zip.sha256
```

## 2. 备份数据库与当前配置

```bash
cd /home/luozhou/clearmeeting/deploy
mkdir -p /home/luozhou/clearmeeting-backups

docker compose exec -T server python - <<'PY'
import sqlite3
from datetime import datetime

source = sqlite3.connect("/app/data/clearmeeting.db")
name = "/app/data/clearmeeting-before-v2.0.2-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db"
target = sqlite3.connect(name)
source.backup(target)
target.close()
source.close()
print(name)
PY

cp -p .env /home/luozhou/clearmeeting-backups/env-before-v2.0.2
```

## 3. 解压并继承数据和 `.env`

```bash
cd /home/luozhou
unzip -q clearmeeting-server-v2.0.2-offline-queue-r1.zip \
  -d clearmeeting-v2.0.2

cp -p /home/luozhou/clearmeeting/deploy/.env \
  /home/luozhou/clearmeeting-v2.0.2/deploy/.env

sed -i 's/^OFFLINE_ASR_WORKERS=.*/OFFLINE_ASR_WORKERS=1/' \
  /home/luozhou/clearmeeting-v2.0.2/deploy/.env
grep '^OFFLINE_ASR_WORKERS=' \
  /home/luozhou/clearmeeting-v2.0.2/deploy/.env
```

如果 `.env` 原来没有该项：

```bash
grep -q '^OFFLINE_ASR_WORKERS=' /home/luozhou/clearmeeting-v2.0.2/deploy/.env || \
  echo 'OFFLINE_ASR_WORKERS=1' >> /home/luozhou/clearmeeting-v2.0.2/deploy/.env
```

## 4. 构建并切换

```bash
cd /home/luozhou/clearmeeting-v2.0.2/deploy

docker compose --profile real-asr --profile offline-asr-cpu build \
  funasr-offline-cpu server nginx

cd /home/luozhou/clearmeeting/deploy
docker compose stop server nginx funasr-offline-cpu

# 停止写入后再复制最终数据快照，避免构建期间新上传的数据遗漏。
cp -a /home/luozhou/clearmeeting/server/data/. \
  /home/luozhou/clearmeeting-v2.0.2/server/data/

mv /home/luozhou/clearmeeting /home/luozhou/clearmeeting-v2.0.1-backup
mv /home/luozhou/clearmeeting-v2.0.2 /home/luozhou/clearmeeting

cd /home/luozhou/clearmeeting/deploy
docker compose --profile real-asr --profile offline-asr-cpu up -d \
  funasr funasr-offline-cpu speaker server nginx
```

启动时会自动把旧 `running` 任务恢复为 `queued`，不会删除音频，也不需要设备重新
上传。V2.0.1 中由 `ConnectError`、`ReadTimeout` 等造成的业务重试次数会自动转为
临时故障次数。

## 5. 验证

```bash
cd /home/luozhou/clearmeeting/deploy
EXPECTED_SERVER_VERSION=2.0.2 bash ./verify_api_v2.sh http://127.0.0.1

curl -s http://127.0.0.1/api/v2/build-info | python3 -m json.tool

docker compose exec -T server python - <<'PY'
import sqlite3
db = sqlite3.connect("/app/data/clearmeeting.db")
for row in db.execute("""
SELECT state, reason, COUNT(*)
FROM offline_asr_jobs
GROUP BY state, reason
ORDER BY state, reason
"""):
    print(row)
print("RUNNING:", list(db.execute("""
SELECT id, session_id, chunk_index, attempts, transient_attempts
FROM offline_asr_jobs WHERE state='running'
""")))
PY

docker stats --no-stream --format \
  'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.PIDs}}'
```

验收标准：

- `build-info.server_version` 为 `2.0.2`。
- capabilities 包含 `offline_queue_serial_fair_v1`。
- 离线任务 `running` 同时最多 1 条。
- `done` 数量持续增长，任务在不同 `session_id` 之间轮换。
- 连接故障时 `transient_attempts` 增长，而 `attempts` 不增长。
- 在线 FunASR、网页实时字幕和设备上传保持正常。

## 回退

只有 V2.0.2 启动失败时才回退：

```bash
cd /home/luozhou/clearmeeting/deploy
docker compose down
mv /home/luozhou/clearmeeting /home/luozhou/clearmeeting-v2.0.2-failed
mv /home/luozhou/clearmeeting-v2.0.1-backup /home/luozhou/clearmeeting
cd /home/luozhou/clearmeeting/deploy
docker compose --profile real-asr --profile offline-asr-cpu up -d
```

V2.0.2 只增加兼容字段和调度状态表，回退不会删除会议数据。
