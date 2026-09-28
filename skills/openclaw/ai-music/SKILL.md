---
name: ai-music
description: "BGM 选曲 / AI 音乐：给短视频、社媒内容从本地授权曲库优先选背景音乐；曲库不合适时去配置的授权网站查找；只有用户明确要求原创/AI 作曲时，才调用 DashScope 或 Suno 类付费 provider。支持裁剪、归一化和混入视频。与 tts-voiceover 的区别：tts-voiceover 生成人声口播/旁白，本技能处理背景音乐/配乐。"
layer: produce
---

# AI 音乐 / BGM 生成（AI Music）

给短视频、Vlog、社媒内容选择或生成**背景音乐 / 配乐 / 纯音乐**。默认通过共享脚本
`skills/shared/scripts/ai_music.py` 的 `local-library` provider 从 D 盘授权曲库确定性选曲；
只有用户明确说“原创音乐 / AI 作曲 / 生成一首”时，才调用音乐生成 API。
选中的音乐可再交给 `audio_ops.py` / `video_ops.py` 做裁剪、归一化或加到视频。

**边界**：本 SKILL 只做背景音乐 / 配乐。要**人声口播 / 旁白 / 朗读**用 `tts-voiceover`。

## 输入

| 字段 | 必填 | 说明 |
|------|------|------|
| prompt | 是 | 风格 / 情绪 / 乐器描述（如“轻快 lo-fi，钢琴+鼓点，vlog 片头”） |
| provider | 否 | 默认 `local-library`；原创生成才用 `dashscope` 或 `suno-compatible` |
| lyrics | 否 | 歌词（提供后走带演唱模式，不再是纯音乐） |
| duration | 否 | 时长（秒），部分 provider 支持 |
| instrumental | 否 | 纯音乐（无人声 BGM） |
| output | 否 | 默认 `outputs/主题名/{name}.mp3` |

## 输出

- 音乐音频文件（mp3，放入 `outputs/主题名/`）
- 打印实际提交的 provider / model、任务轮询过程、最终输出路径

## 配置（需设的环境变量）

> **配置检查路径铁律**：先 `cd` 到 `AGENTS.md` 末尾给出的 Easel 项目根，确认当前目录有 `.env` 和 `skills/shared/scripts/`，再运行注册表、`check` 或生成命令。不得改用 workspace 的 `./shared/scripts/...`，也不得以 `env` / `printenv` 没显示变量为由判断未配置。

在项目根 `.env` 或环境变量中设置（脚本自动向上查找 `.env`）。用户自己填 key。

**本地曲库（默认）**

```bash
MUSIC_PROVIDER=local-library
BGM_LIBRARY_DIR=D:/Easel/assets/music-library
BGM_SOURCE_SITE=https://mixkit.co/free-stock-music/
```

曲库目录里的 `library.json` 保存来源、授权提示、标签和时长。曲库没有语义合适的音乐时，
去 `BGM_SOURCE_SITE` 查找并核对当次授权；下载后补进曲库和台账。不要把旁白、语音测试或音效当 BGM。

**provider = dashscope**（阿里云 DashScope / 百炼）

```bash
DASHSCOPE_API_KEY=...           # 必填（别名 DASHSCOPE_KEY / ALIYUN_API_KEY）
DASHSCOPE_MUSIC_MODEL=audio-generation  # 可选（兼容旧名 DASHSCOPE_MODEL）
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1   # 可选
```

**provider = suno-compatible**（Suno 类第三方 API 网关）

```bash
MUSIC_API_KEY=...               # 必填
MUSIC_BASE_URL=https://api.example.com/v1   # 必填，你的 API 根地址
MUSIC_MODEL=music-1             # 可选，按服务商模型名调整
```

## 执行步骤

先跑 `python skills/shared/scripts/model_registry.py configured --group music --env-file .env`。普通“加背景音乐”固定优先
`local-library`，不因为还配置了付费 provider 就改走生成；只有用户明确要求原创/生成时，才列出付费 provider 供选择并说明会产生调用。

脚本路径（相对项目根）：`skills/shared/scripts/ai_music.py`。每个子命令支持 `-h`。

### 1. 先自检配置 check（不发请求）

```bash
python skills/shared/scripts/ai_music.py check --provider local-library
```

缺 key 会明确列出缺哪个 env（含别名提示）。配置齐了再往下。

### 2. 本地选曲（默认、无 API 调用）

```bash
python skills/shared/scripts/ai_music.py generate --provider local-library \
  --prompt "轻快、克制、科技感，适合有旁白的知识视频" --instrumental \
  -o outputs/主题名/assets/bgm.mp3
```

脚本会按 `library.json` 标签和文件名确定性选曲并复制到项目输出。选后必须用 `ffprobe` 读取真实时长，
试听开头/中段/结尾，确认情绪与旁白匹配；不匹配时去 Mixkit 补充曲目，而不是硬用第一首。

### 3. 原创生成（仅用户明确要求时）

```bash
# 纯音乐 BGM（dashscope）
python skills/shared/scripts/ai_music.py generate --provider dashscope \
  --prompt "轻快的 lo-fi 嘻哈，钢琴 + 柔和鼓点，适合 vlog 片头" \
  --duration 30 --instrumental \
  -o outputs/主题名/vlog-bgm.mp3

# 带演唱（suno-compatible，给了歌词）
python skills/shared/scripts/ai_music.py generate --provider suno-compatible \
  --prompt "温暖的民谣，吉他弹唱" \
  --lyrics "$(cat lyrics.txt)" \
  -o outputs/主题名/song.mp3
```

`--poll-interval`（轮询间隔，默认 5s）、`--timeout`（超时，默认 300s）可按需调。

### 4. 后处理（可选，复用已有共享脚本，不在本 SKILL 重造）

```bash
# ① 裁剪到片头需要的长度
python skills/shared/scripts/audio_ops.py trim outputs/主题名/vlog-bgm.mp3 \
  -o outputs/主题名/bgm-8s.mp3 --duration 8

# ② 归一化到社媒响度（-14 LUFS）
python skills/shared/scripts/audio_ops.py normalize outputs/主题名/vlog-bgm.mp3 \
  -o outputs/主题名/bgm-norm.mp3

# ③ 把 BGM 加到视频（可调原声/BGM 音量比，自动循环并截断）
python skills/shared/scripts/video_ops.py bgm -i clip.mp4 -o clip_bgm.mp4 \
  --music outputs/主题名/vlog-bgm.mp3 --voice-volume 1.0 --music-volume 0.3
```

## 规则

1. **固定来源优先级** — 本地授权曲库 → Mixkit 授权音乐库 → 用户明确要求时才 AI 生成。不得把“需要 BGM”解释成“需要付费生成”。
2. **先 check 再 generate** — 缺配置时 check 给出清晰中文提示，不浪费一次失败请求。
3. **绝不覆盖原始素材** — 只写新文件到 `outputs/主题名/`。
4. **不重造能力** — 裁剪 / 归一化 / 加视频 BGM 复用 `audio_ops.py` / `video_ops.py`。
5. **混音服从旁白** — 口播片默认无人声音乐，BGM 建议音量 0.08–0.18，并做 0.5–1.5 秒淡入、1.5–3 秒淡出；不在句中突然切歌或生硬循环。
6. **授权台账** — 从网站补充曲目时记录来源页、下载时间和许可证；交付前确认用途允许。
7. **与 tts-voiceover 分工** — 背景音乐/配乐找 ai-music，人声配音找 tts-voiceover。
   两者可组合：ai-music 出 BGM + tts-voiceover 出旁白 → `video_ops.py bgm` 混音。
