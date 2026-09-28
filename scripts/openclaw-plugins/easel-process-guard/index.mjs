export const guidance = `Easel 本机进程保护：OpenClaw 网关本身是 Node 进程，内存超过 500 MB 属于可能的正常情况。绝对不要按 node/chrome 名称或内存大小批量清理进程，不要使用 shell Stop-Process/taskkill/kill 等终止宿主进程。需要取消你自己启动的渲染时，使用 process 工具的 kill 操作并传入该次渲染返回的 sessionId；若 session 已不存在，先只读检查命令行和产物，不要把未知 Node 当成渲染残留。退出整个 Easel 由用户桌面退出图标处理。此前反复网关离线的已确认原因是 Agent 执行按内存筛选 Node 的 Stop-Process，杀死网关，不是渲染本身失败。恢复任务时禁止重复该清理。`;

// Accident prevention, not an OS security sandbox. Session-scoped process.kill
// remains available; the desktop launcher runs outside agent tool hooks.
export function guard(event) {
  const params = event.params ?? {};
  const shellTool = /(?:^|[_\.])(exec|exec_command|shell|run_command|powershell|bash)$/.test(event.toolName ?? '')
    || event.toolKind === 'code_mode_exec';
  if (!shellTool) return;
  const input = JSON.stringify(params);
  const termination = /\b(?:stop-process|taskkill|killall|pkill|tskill|TerminateProcess|Win32_Process[^\n]*Terminate)\b|\b(?:os|process|psutil)\s*\.\s*(?:kill|terminate)\s*\(|\bkill\s+(?:-[\w]+\s+)*(?:\d|\$)|\b(?:spps)\b/i;
  const shutdown = /(?:Desktop-Easel|Stop-Easel)\.ps1|openclaw[^\n]{0,200}\bgateway\s+(?:stop|restart)\b/i;
  if (!termination.test(input) && !shutdown.test(input)) return;
  return { block: true, blockReason: guidance };
}

export default {
  id: 'easel-process-guard',
  name: 'Easel Process Guard',
  register(api) {
    api.on('before_tool_call', (event) => {
      const decision = guard(event);
      if (decision) api.logger.warn('[easel-process-guard] blocked host process termination; use owned render session cancellation');
      return decision;
    }, { priority: 1000 });
    api.on('before_prompt_build', () => ({ prependContext: guidance }));
  },
};
