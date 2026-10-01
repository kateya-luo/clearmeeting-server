# ClearMeeting Server V2.0.6 部署说明

V2.0.6 修复短音频或静音会议因 FunASR 没有返回句级时间戳而反复重试的问题。

## 版本信息

- `server_version=2.0.6`
- `SERVER_RELEASE=clearmeeting-server-v2.0.6`
- 发布包：`clearmeeting-server-v2.0.6-no-speech-terminal-r1.zip`
- 新能力：`offline_no_speech_terminal_v1`

## 行为调整

- 没有文字、没有时间戳：返回 `outcome=no_speech`，任务正常完成，不覆盖已有实时字幕。
- 有文字、没有时间戳：按实际音频范围生成确定性的保底时间段，文字不会丢失。
- 参数错误、音频缺失等永久性错误立即失败，不再盲目重试。
- 网络、容器启动和模型繁忙仍按依赖故障退避，不消耗业务重试次数。
- 模型推理异常的业务重试上限由 30 次降低为 5 次。

## 部署与验证

部署前备份当前目录和 SQLite 数据库。保留 `.env` 与 `server/data`，并确保：

```ini
SERVER_RELEASE=clearmeeting-server-v2.0.6
OFFLINE_ASR_MAX_RETRIES=5
```

然后执行：

```bash
cd /home/luozhou/clearmeeting/deploy
docker compose build server nginx speaker funasr-offline-cpu
docker compose up -d server nginx speaker funasr-offline-cpu
EXPECTED_SERVER_VERSION=2.0.6 bash ./verify_api_v2.sh http://127.0.0.1
```

日志出现 `offline_canonical_no_speech` 和 `offline_canonical_job_no_speech` 表示静音任务已正常越过定稿屏障。

## 回退

停止 V2.0.6 容器，将部署前 V2.0.5 完整备份恢复到
`/home/luozhou/clearmeeting` 后启动。此版本没有数据库迁移，回退无需转换数据。
