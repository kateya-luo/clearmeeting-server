# V2.1.0 R2 部署、升级与回退

发布标识：`clearmeeting-server-v2.1.0-daily-message-r2`。协议仍为 `luoye-device-api/2`。

## 新安装

1. 安装 Git、Docker Engine 和 Compose 插件，准备至少16 GiB内存及模型/音频磁盘空间。
2. 克隆本仓库，进入 `deploy`，复制 `.env.example` 为 `.env`。
3. 分别生成 AUTH_SECRET 和 DEVICE_API_SECRET（例如各执行一次 `openssl rand -hex 32`），设置足够强的初始账号密码。
4. 填入自己的 DEEPSEEK_API_KEY、CORS_ALLOW_ORIGINS。AI密钥为空时寄语使用备用文案，AI纪要不可用。
5. 检查 FUNASR_MODELS_DIR 模型缓存路径，执行 `bash start.sh`。同时启用实时与CPU离线服务。
6. 执行 `docker compose ps` 与 `bash verify_api_v2.sh http://127.0.0.1`。

第一次下载模型较慢，依赖服务可能尚未就绪；查看对应服务日志，不要反复清理数据或重装。
浏览器远程麦克风通常需要HTTPS；生产反向代理和TLS需另行配置。

## 升级现有服务器

不要直接用空数据目录替代原部署。先核对挂载路径，保留原 `.env`、账号签名密钥、设备密钥、模型缓存和 `server/data`。

1. 在没有录音、上传、转写和纪要任务的维护窗口操作。
2. 记录当前版本、源码提交、镜像标签及所有挂载。给旧镜像增加唯一回退标签，避免被新构建覆盖。
3. 停止写入后备份完整数据目录；运行中备份SQLite必须用其在线backup机制，不能只复制主db而遗漏WAL。
4. 单独备份源码、`.env`、Compose和Nginx配置，不把备份上传GitHub。
5. 将本版源码放在新目录，审阅配置差异后保留原有数据挂载和密钥。`.env` 中设置本版 SERVER_RELEASE。
6. 维护窗口执行 `docker compose --profile real-asr --profile offline-asr-cpu up -d --build`，检查健康及协议。
7. 测试登录、配对、短录音、补传、历史会议和导出，再恢复正常使用。

从旧稳定版升级涉及多个模块，不要只覆盖三个寄语文件。历史R2增量补丁只适合其限定R1基线，本仓库是完整源码。

## 回退

恢复旧源码、配置和已保留镜像，沿用正确的数据挂载。不要直接把旧数据库覆盖新数据库，这会丢失升级后会议。
新增 `device_daily_messages` 缓存表可以保留；若有其他数据库兼容问题，应先停写、备份当前数据再单独处理。

## 固件对齐

```http
GET /api/v2/device/agenda?after_revision=41&window_days=7&include_daily_message=1&display_utc_offset_minutes=480
```

沿用原设备鉴权，请求寄语时200返回完整待办快照，并增加：

```json
{"daily_message":{"date":"2026-10-01","text":"保持热爱，慢慢成长"}}
```

寄语最多12字符/48 UTF-8字节，无emoji；同账号同显示日期共享缓存。
首次可能返回备用句，后台生成后下次唤醒读取；失败当天不反复重试。
`DAILY_MESSAGE_AI_ENABLED=0` 关闭寄语AI。网页/手机提醒不受固件省电策略影响。
