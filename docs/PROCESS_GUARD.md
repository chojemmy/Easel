# 防止 Agent 误杀网关

一次 Windows 视频任务故障中，Agent 把内存超过 500 MB 的 Node 进程当成残留渲染进程，使用 `Stop-Process` 批量终止，实际杀掉了 OpenClaw 网关。启动恢复随后续接同一任务，再次执行清理，造成反复离线。进程名和内存用量不能证明进程属于某次渲染。

`scripts/openclaw-plugins/easel-process-guard` 是本地 OpenClaw 插件：

- 在 `before_tool_call` 拦截 shell 进程终止命令和网关退出命令。
- 不拦截进程查询、正常渲染，以及 `process` 工具使用其管理的 `sessionId` 取消任务。
- 在恢复任务的提示中说明网关自身是 Node，不得按名称或内存清理。
- 桌面退出入口不经过 Agent 工具钩子，仍可正常关闭后台。

这是已知误操作的防护，不是操作系统安全沙箱。编码命令或脚本文件中的任意行为不属于静态命令匹配能够全面约束的范围。

在 OpenClaw 配置中将插件目录的绝对路径加入 `plugins.load.paths`，并设置：

```json
{
  "plugins": {
    "entries": {
      "easel-process-guard": {
        "enabled": true,
        "hooks": { "allowConversationAccess": true }
      }
    }
  }
}
```

若配置了 `plugins.allow`，也将此 ID 加入。启动或重载后检查插件是否成功注册。提示钩子需要上述本地授权；工具拦截钩子不依赖会话读取权限。本插件不保存会话内容，只记录固定的拦截提示。

离线回归检查：`node --test tests/test_process_guard.mjs`。
