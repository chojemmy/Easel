# MiniMax 接入、额度显示与自动创作

设置 → **MiniMax 额度** 显示 Token Plan 当前与每周剩余百分比、重置倒计时。
服务直接调用官方 CLI `mmx quota show --output json` 使用的
`GET /v1/token_plan/remains`，不要求安装 CLI。

参考官方实现：https://github.com/MiniMax-AI/cli/tree/main/src/commands/quota

## 运行配置

Web 服务和 OpenClaw Gateway 都需要继承以下环境变量（密钥由密码管理器注入）：

```dotenv
MINIMAX_API_KEY=<Token Plan key>
MINIMAX_BASE_URL=https://api.minimaxi.com
VOICE_PROVIDER=minimax
MINIMAX_MODEL=speech-2.8-hd
VOICE_NARRATOR_VOICE_ID=Chinese (Mandarin)_Gentleman
```

不要把真实密钥提交到 Git。Windows 可使用当前用户 DPAPI 保存凭证后在启动时解密注入。
网页 `.env` 支持 `${ENV_NAME}` 引用；Gateway 的工作目录不同，因此所有媒体 URL、模型名和密钥都应一起传入进程环境。

`scripts/local_media_bridge.py` 提供 MiniMax 原生生图、视频到 Easel 兼容接口的转换：

```sh
python -m uvicorn scripts.local_media_bridge:app --host 127.0.0.1 --port 7861
```

给适配器、Web 和 Gateway 注入相同的随机 `EASEL_MEDIA_TOKEN`，同时设置：

```dotenv
IMG_API_KEY=${EASEL_MEDIA_TOKEN}
IMG_BASE_URL=http://127.0.0.1:7861/v1
IMG_MODEL=image-01
VIDEO_PROVIDER=openai-compatible
VIDEO_API_KEY=${EASEL_MEDIA_TOKEN}
VIDEO_BASE_URL=http://127.0.0.1:7861/v1
VIDEO_MODEL=MiniMax-Hailuo-2.3
```

Shell 不一定自动展开 dotenv 引用；启动器需完成引用解析。适配器仅监听本机，除了健康检查外均需 Bearer 认证。
文生图支持 `image-01`，不将角色参考生成冒充通用图片编辑。视频支持 Hailuo 2.3 的 6/10 秒、768P，文生视频横屏；竖屏使用图片首帧。

适配器的可选本地向量功能使用 `fastembed` / `BAAI/bge-small-zh-v1.5`，512 维。
先安装 `pip install '.[minimax-memory]'` 并把模型预下载到
`%LOCALAPPDATA%/Easel/models`；实际请求不会联网下载模型。该可选缓存路径目前面向 Windows。

MiniMax 音乐接口存在历史付费账号限制，不能仅凭有 Token Plan 密钥就标为可用；此版本未启用音乐。

## 自动运行

1. 在额度页输入待运行内容，点击“加入待运行队列”。任务应包含主题、受众、输出形式和要求。
2. 保存并启用规则：默认监测 `general`，距重置不足 60 分钟且剩余额度 **大于** 15%。
3. 检查间隔默认 **15 分钟**，可在界面设置为 1～1440 分钟；保存规则后立即生效。间隔大于触发窗口可能错过本轮。
4. 队列按先进先出执行。可选择在队列为空时，按指定方向查询热点并产出草稿。
5. 同一个重置时间最多触发一次，单任务最多 15 分钟，且尽量在本次重置前结束。

额度只有计数、没有明确百分比时不猜测百分比，不触发自动任务；周额度为零也不启动。
关闭开关只阻止后续任务，不撤销已启动的任务。后台关闭时无法监控；电脑睡眠期间也不会运行。
不要以多 worker 模式启动 Web（仅支持一个调度服务）。SQLite 原子领取与重置时间唯一约束防止重复提交。
重启后运行中任务标为中断，不自动重试，避免重复生成或费用。

执行使用现有 OpenClaw profile `easel` 的独立会话。默认要求仅创建本地内容，不发布、发消息、切换付费供应商。
这些行为约束是发给 Agent 的指令，不是额外的操作系统沙箱；仍应只排入自己信任的创作指令。
每次最多执行一个内容包，不承诺消耗完全部剩余额度，也不承诺一定能在窗口内完成。

任务、状态及结果位于 `outputs/minimax-automation/`（Git 忽略）。执行记录显示 Agent 返回结果；
“已结束”表示这轮 Agent 运行结束，是否产出完整内容以结果中的文件和说明为准。

## API

- `GET /api/minimax/quota`：45 秒缓存的额度快照；失败时不伪造零额度。
- `GET/PUT /api/minimax/automation`：配置、任务队列和最近执行记录。
- `POST /api/minimax/tasks`：`{"prompt":"待运行内容"}`。
- `DELETE /api/minimax/tasks/{id}`：取消尚未开始的任务。

自动运行默认关闭，不会因打开额度页面就启动创作。
