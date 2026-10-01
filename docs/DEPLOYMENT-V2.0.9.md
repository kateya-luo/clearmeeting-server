# ClearMeeting Server V2.0.9 部署说明

V2.0.9 修复按模板生成会议纪要失败后无法再次提交的问题，并完善 DeepSeek 空响应处理。

## 版本信息

- `server_version=2.0.9`
- `SERVER_RELEASE=clearmeeting-server-v2.0.9`
- 发布包：`clearmeeting-server-v2.0.9-minutes-retry-r1.zip`
- 新能力：`minutes_failed_retry_v1`

## 行为调整

- 相同转写、人员映射和模板的任务若处于 `failed`，再次点击生成会复用原任务编号，清除旧错误与计量字段，重新进入 `queued`。
- `ready`、`queued` 和 `generating` 任务仍保持幂等，不会因重复点击产生额外模型请求。
- DeepSeek HTTP 正文或模型内容明确为空时自动补试一次。
- 非空但无法解析的响应不自动重试，避免已经计费后再次调用；页面会显示可读错误，并允许用户手动重试。
- 部署不会自动重跑历史失败任务，避免未经用户确认产生模型费用。

## 部署与验证

部署前完整备份当前目录和 SQLite 数据库，保留 `.env` 与 `server/data`。将 `.env` 中的：

```ini
SERVER_RELEASE=clearmeeting-server-v2.0.9
```

写入后执行：

```bash
cd /home/luozhou/clearmeeting/deploy
docker compose build server nginx
docker compose up -d server nginx
EXPECTED_SERVER_VERSION=2.0.9 bash ./verify_api_v2.sh http://127.0.0.1
```

浏览器按 `Ctrl+F5`。对于原来失败的纪要，重新选择模板并点击一次“生成纪要”即可重新排队。

## 回退

停止 V2.0.9 容器，将部署前的 V2.0.8 完整备份恢复到 `/home/luozhou/clearmeeting` 后启动。
本版本没有数据库迁移，回退无需转换数据。
