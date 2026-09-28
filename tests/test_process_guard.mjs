import test from 'node:test';
import assert from 'node:assert/strict';
import plugin, {guard} from '../scripts/openclaw-plugins/easel-process-guard/index.mjs';

test('blocks actual gateway self-kill incident and common termination forms', () => {
  for (const command of [
    'Get-Process node | Where-Object { $_.WorkingSet -gt 500MB } | ForEach-Object { Stop-Process -Id $_.Id -Force }',
    'Get-Process chrome | Stop-Process -Force',
    'taskkill /F /IM node.exe', 'Stop-Process -Id 29148',
    'kill -9 29148', 'os.kill(29148, 9)',
    'openclaw --profile easel gateway stop',
    'powershell -File Desktop-Easel.ps1 -Action Stop',
  ]) assert.equal(guard({toolName:'exec', params:{command}})?.block, true, command);
});
test('allows inspection, rendering and session-scoped cancellation', () => {
  for (const command of ['Get-Process node | Select Id,WorkingSet', 'node remotion-cli.js render src/index.ts MainVideo final.mp4 --concurrency=2']) {
    assert.equal(guard({toolName:'exec', params:{command}}), undefined);
  }
  assert.equal(guard({toolName:'process', params:{action:'kill',sessionId:'owned-render'}}), undefined);
});
test('registers a blocking hook and recovery instructions', () => {
  const hooks = {};
  plugin.register({on:(name, fn)=>hooks[name]=fn,logger:{warn:()=>{}}});
  assert.equal(hooks.before_tool_call({toolName:'exec', params:{command:'taskkill /F /IM node.exe'}}).block,true);
  assert.match(hooks.before_prompt_build().prependContext,/sessionId/);
});
