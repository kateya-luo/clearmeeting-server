# ClearMeeting Server V2.0.8 部署说明

V2.0.8 完善设置页的服务器监控：会议具有可辨认的时间与短编号，并允许用户闭环处理
失败的离线音频片段。

## 版本信息

- `server_version=2.0.8`
- `SERVER_RELEASE=clearmeeting-server-v2.0.8`
- 发布包：`clearmeeting-server-v2.0.8-offline-failure-actions-r1.zip`
- 新能力：`offline_failure_actions_v1`

## 行为调整

- 离线任务显示会议名称、会议开始时间和稳定的八位短编号。
- 失败任务显示准确音频区间、尝试次数和错误摘要。
- “重新识别”会清零尝试计数，将该片段重新投入持久队列。
- “跳过并继续”会把该片段标记为用户跳过，并在没有其他失败片段时重新放行发布任务。
- 两种操作都会恢复会议处理状态；后台完成发布后即可生成会议纪要。
- 跳过只影响转录覆盖，不物理修改或删除原始录音。
- 操作接口校验会议所有者，账号之间不能查看或处理彼此的任务。

## 部署与验证

部署前备份当前目录和 SQLite 数据库，保留 `.env` 与 `server/data`。将 `.env` 中的：

```ini
SERVER_RELEASE=clearmeeting-server-v2.0.8
```

写入后执行：

```bash
cd /home/luozhou/clearmeeting/deploy
docker compose build server nginx
docker compose up -d server nginx
EXPECTED_SERVER_VERSION=2.0.8 bash ./verify_api_v2.sh http://127.0.0.1
```

浏览器按 `Ctrl+F5` 后进入“设置”，核对失败任务的会议标识与两个操作按钮。

## 回退

停止 V2.0.8 容器，将部署前 V2.0.7 完整备份恢复到
`/home/luozhou/clearmeeting` 后启动。本版本没有数据库迁移，回退无需转换数据。
