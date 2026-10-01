# ClearMeeting Server V2.0.1 部署说明

V2.0.1 只修复服务器重启恢复状态机，不改变固件协议、上传、转写、声纹或模板
纪要行为。设备 API 继续保持 `luoye-device-api/2`。

修复内容：

- `transcript_ready` 与 `done` 一样视为终态，服务器重启后不再重新恢复；
- 幂等任务已是 `done/failed/cancelled`、没有 `queued/running` 工作时，不允许将
  会议写成 `finalizing`；
- 增加终态重启幂等和“无活动任务不回退”测试。

部署前必须备份 SQLite 与 `deploy/.env`。只重建 `server nginx`，不重建 ASR
模型容器。部署后运行：

```bash
EXPECTED_SERVER_VERSION=2.0.1 bash ./verify_api_v2.sh http://127.0.0.1
```

数据库中已经被旧版本误回退的会议必须按设备状态、任务终态和缺口情况逐条修复，
不得批量把全部 `finalizing` 改成完成。
