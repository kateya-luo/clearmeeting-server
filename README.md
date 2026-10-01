# ClearMeeting Server · 落叶会议服务器

落叶录音卡的独立服务器项目，包含 Python API、会议网页和 Docker 部署配置。

**[落叶总项目 / 固件与硬件](https://github.com/kateya-luo/luoye)** · **[服务器版本下载](https://github.com/kateya-luo/clearmeeting-server/releases)** · **[部署教程](docs/DEPLOYMENT.md)**

## 当前版本

| 项目 | 标识 |
| --- | --- |
| 服务器 | **V2.1.0 R2** |
| 完整发布标识 | `clearmeeting-server-v2.1.0-daily-message-r2` |
| 设备协议 | `luoye-device-api/2`，保持老固件兼容 |
| 对齐固件 | V2.1.0 省电查阅版（待办只读、唤醒同步、每日寄语） |

本仓库从已部署的服务器版本整理，原项目中的历史服务器版本仍保留。
这是工程版本，不代表所有硬件、网络与长音频组合已经通过量产验收。

## 功能

- 网页/录音卡实时转写，可靠上传、离线补传和会议历史。
- 离线转写队列、多人识别、任务状态及失败处理。
- 会后手动选择模板生成纪要，不在录音中生成滚动纪要。
- 人员校正、会议记忆和 Word / Markdown 等导出。
- 设备配对、SD卡管理、待办同步与按账号缓存的每日短寄语。

寄语接口不会等待模型：先返回缓存或备用短句，后台每天每账号最多尝试生成一次。
请求寄语时返回完整待办快照，即使待办版本没有变化也不会返回误导性的空列表。

## 快速开始

推荐 Linux、Docker Compose、16 GiB 内存；模型首次下载需网络与充足磁盘空间。无需显卡。

```bash
git clone https://github.com/kateya-luo/clearmeeting-server.git
cd clearmeeting-server/deploy
cp .env.example .env
# 先编辑 .env：设置独立随机密钥、初始账号密码、AI Key及自己的网页地址。
bash start.sh
bash verify_api_v2.sh http://127.0.0.1
```

公网使用应自行配置 HTTPS；不要把未经配置的工程实例直接开放到公网。
数据库、音频和密钥不包含在源码或发布附件中。升级现有实例请先读[备份与升级说明](docs/DEPLOYMENT.md)。

## 源码与测试

```bash
python -m pip install -r server/requirements.txt
cd server
python -m unittest discover -s tests
# 另开终端，在仓库根目录：
npm ci
npm --workspace @ai-recorder/web-client test
npm run build:web
```

`server/` 是API和模型服务，`apps/web-client/` 是网页，`deploy/` 是部署配置。
历史 `docs/DEPLOYMENT-V*.md` 仅供追溯，当前安装以本版部署教程为准。

## 隐私与发布边界

源码中的域名/IP均为配置示例；密钥仅放本地 `.env`。
请勿提交数据库、会议内容、音频、设备令牌或模型下载缓存。
本仓库没有授予额外开源许可；第三方依赖和模型遵循各自许可证。
