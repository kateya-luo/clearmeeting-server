# ClearMeeting Server V2.0.7 部署说明

V2.0.7 将服务器只读监控面板整合到网页“设置”页，替换原有“偏好”卡片。

## 版本信息

- `server_version=2.0.7`
- `SERVER_RELEASE=clearmeeting-server-v2.0.7`
- 发布包：`clearmeeting-server-v2.0.7-web-monitor-r1.zip`
- 新能力：`web_system_monitor_v1`

## 面板内容

- API、在线转录、离线转录和声纹服务可用状态。
- 主机 CPU 负载、可用内存、数据磁盘和运行时间。
- 全局离线队列数量。
- 当前账号最近会议的离线处理进度；不返回字幕正文或其他账号的会议明细。
- 页面打开期间每 10 秒自动刷新，也可手动刷新。

监控接口为登录后只读接口：`GET /api/v1/system/status`。本版本不提供网页重启、
删除任务或修改队列的能力，避免误操作影响正在进行的会议。

## 部署与验证

部署前完整备份当前目录和 SQLite 数据库。保留 `.env` 与 `server/data`，并将：

```ini
SERVER_RELEASE=clearmeeting-server-v2.0.7
```

写入部署目录的 `.env`。随后执行：

```bash
cd /home/luozhou/clearmeeting/deploy
docker compose build server nginx speaker funasr-offline-cpu
docker compose up -d server nginx speaker funasr-offline-cpu
EXPECTED_SERVER_VERSION=2.0.7 bash ./verify_api_v2.sh http://127.0.0.1
```

浏览器按 `Ctrl+F5`，登录后进入“设置”。原“偏好”位置应显示“服务器运行状态”。

## 回退

停止 V2.0.7 容器，将部署前 V2.0.6 完整备份恢复到
`/home/luozhou/clearmeeting` 后启动。本版本没有数据库迁移，回退无需转换数据。
