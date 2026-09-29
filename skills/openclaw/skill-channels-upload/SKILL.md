---
name: skill-channels-upload
description: >-
  微信视频号发布与平台草稿：把竖版短视频保存到微信视频号草稿箱，或经用户明确授权后公开发表
  （channels.weixin.qq.com）。当用户说"发视频号""上传视频号""视频号发布""保存视频号草稿"
  "发到微信视频号""视频号投稿"时使用。基于通用浏览器发布框架（Playwright + 登录态持久化），微信扫码登录。
layer: publish
---

# 微信视频号发布

> 基于通用浏览器发布框架 `../../shared/scripts/web_publisher.py`（`--platform weixin-channels`）。

## ⚠️ 环境依赖

- Easel 桌面版自带项目虚拟环境。Windows 固定使用 `<项目根>\.venv\Scripts\python.exe`；macOS/Linux 使用 `<项目根>/.venv/bin/python`。
- **禁止**使用 PATH 上的裸 `python` / `pip` / `py`，禁止向 Hermes venv 或全局 Python 安装依赖。先用项目解释器执行 `web_publisher.py check`；通过即继续，不重复安装 Playwright/Chromium。
- 只有项目解释器本身缺失或其 `check` 明确失败时才报告环境损坏；发布任务中不要临时改装其它 Python 环境。
- 首次 `login` 用**微信扫码**登录视频号助手，登录态持久化复用
  - 远程/headless 环境用 `login-qr --platform weixin-channels`（抠二维码成图轮询），或走 Web「账号」页登录
- **必须有头运行**：2026-09 实测 headless 偶发只显示空白微前端；脚本会为视频号自动启用 headed。
- **就绪判定**：`/post/create` URL 或 `/micro/content/post/create` frame 出现都不代表页面可操作。
  必须等到真实 `input[type=file]`、描述编辑器和目标按钮就绪；子应用未挂载时只允许有限刷新，不能反复上传。

无浏览器环境可用 `platforms` / `plan` / `check`。

## 执行

```powershell
$ROOT = '<运行时项目根>'
$PY = Join-Path $ROOT '.venv\Scripts\python.exe'
$WP = Join-Path $ROOT 'skills\shared\scripts\web_publisher.py'

& $PY $WP check
& $PY $WP whoami --platform weixin-channels
# 仅 whoami 返回 loggedIn=false 时，才让用户在 Web「账号」页扫码；不要重复安装依赖。
& $PY $WP plan --platform weixin-channels --media '<成片绝对路径>' --title '<标题>' --desc '<正文>' --tags '<话题>' --draft

# 推荐先保存到“微信视频号平台草稿箱”（不会公开发表）。
& $PY $WP publish --platform weixin-channels --media '<成片绝对路径>' --title '<标题>' --desc '<正文>' --tags '<话题>' --draft --exec --headed
$rc = $LASTEXITCODE
if ($rc -ne 0) { exit $rc }

# 只有用户明确要求公开发表时，才去掉 --draft。不得把“发到草稿箱”解释成正式发表。
& $PY $WP publish --platform weixin-channels --media '<成片绝对路径>' --title '<标题>' --desc '<正文>' --tags '<话题>' --exec --headed
$rc = $LASTEXITCODE
if ($rc -ne 0) { exit $rc }
```

## Profile 感知

- 有 Profile：标题短（≤22字）贴合人设；竖版 9:16；可带话题与合集。
- 无 Profile：按视频号通用规范（短标题、竖版、正向内容）。

## 规则

1. 视频号竖版 9:16；横版先 video-reframe 转制。
2. 标题要短（视频号标题偏短），正文可展开。
   - “短标题”控件只接受书名号、引号、冒号、加号、问号、百分号、摄氏度等有限符号；实测中文逗号会触发红色校验。
   - 脚本会把不支持的标点（包括逗号）替换为空格；原始标题仍完整保留在“视频描述”第一行。
3. 视频号始终使用 `--headed`；选择器失效即更新配置。
4. 视频号内容审核偏严，发布前过 skill-quality-gate 合规检查。
5. **动作分流**：平台草稿走 `--draft` 并只寻找“保存草稿”；公开发布才寻找“发表”。两个按钮互斥，禁止回退成另一个动作。
6. **单次提交**：一次任务只允许触发一次“保存草稿”或一次“发表”。动作发出后若结果未知，立即停止；禁止自动重跑、禁止凭超时再次点击。
7. **草稿箱回读**：保存后的 toast、URL 跳转只作旁证；必须进入“草稿箱”，按标题找到唯一条目才报告成功。找不到则报告“结果未确认”，不得冒报成功。
8. **保留失败退出码**：PowerShell 包装命令在调用脚本后先保存 `$LASTEXITCODE`，不要在后面追加 `Write-Host` 后把失败伪装成退出码 0。

## 参考来源

视频号助手网页发布流程；复用统一 Playwright 框架。微信登录需扫码。2026-09 真机复核：后台使用
Wujie 微前端，路由已变化不等于控件就绪；平台草稿以“保存草稿”单击一次 + 草稿箱标题回读为闭环。
