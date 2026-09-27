# 小红书本机登录

账号页的小红书登录使用 Easel 自己的持久化浏览器目录，默认在用户主目录的 `.easel-browser-profiles/XiaohongshuProfile`。这与 OpenClaw 的 Edge existing-session 连接是两套独立机制，连接 Edge 不会自动给这个目录登录。

Windows 上从账号页登录会打开可见浏览器窗口。用小红书 App 扫码，并在需要时完成网站要求的验证。成功后关闭登录窗口，后续程序复用本机登录态。无桌面部署默认仍使用二维码图片，可通过 `EASEL_XHS_HEADED=0/1` 显式设置。

命令行登录请在项目目录使用：

```powershell
.venv\Scripts\python.exe skills/shared/scripts/xhs_publish.py login --headed --no-proxy
.venv\Scripts\python.exe skills/shared/scripts/xhs_publish.py whoami --no-proxy
```

小红书默认直连，不继承用于境外服务的 HTTP_PROXY / HTTPS_PROXY / EASEL_PROXY。只有显式传入 `--proxy` 才使用代理；`--no-proxy` 优先禁用浏览器代理，但无法绕过系统级 VPN/TUN。

诊断依据必须是实际页面：安全限制页不一定表示 IP 问题。程序只在页面明确显示“IP 存在风险”时这样报告，同时附上实际错误码和连接方式，不记录带会话参数的完整 URL。限制页截图保存在二维码输出旁的 `*-error.png`，供本机排查。

实测中出现过同一家庭网络下无界面登录返回 300012，而可见窗口正常显示二维码、扫码成功、后续无界面 whoami 通过的情况。因此不要仅凭 300012 就要求用户购买代理、换热点或复制其他机器的 Cookie。先用本机可见窗口完成正常登录；仍受限时再按网站反馈处理。
