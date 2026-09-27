import { useEffect, useState } from 'react';
import './MiniMaxPanel.css';

type Settings = { enabled: boolean; check_interval_minutes: number; minutes_before_reset: number; remaining_threshold: number; quota_model: string; trending_enabled: boolean; trending_prompt: string };
type Row = { model: string; reset_at: number; remaining_percent: number | null; weekly_remaining_percent: number | null };
type Task = { id: string; prompt: string; state: string };
type Run = { id: string; prompt: string; state: string; started: number; result: string };
type State = { settings: Settings; tasks: Task[]; runs: Run[]; error: string };
const labels: Record<string, string> = { queued: '排队中', running: '运行中', completed: '已结束', failed: '失败', interrupted: '已中断', cancelled: '已取消' };

async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api/minimax/' + path, { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败');
  return data;
}

export default function MiniMaxPanel() {
  const [state, setState] = useState<State | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [checked, setChecked] = useState(0);
  const [now, setNow] = useState(Date.now() / 1000);
  const [prompt, setPrompt] = useState('');
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  async function refresh() {
    const [status, quota] = await Promise.allSettled([
      api<State>('automation'), api<{ rows: Row[]; checked_at: number }>('quota'),
    ]);
    if (status.status === 'fulfilled') {
      setState(status.value);
      setSettings(old => old || status.value.settings);
    }
    if (quota.status === 'fulfilled') {
      setRows(quota.value.rows); setChecked(quota.value.checked_at); setError('');
    } else { setError(quota.reason.message); }
    if (status.status === 'rejected') setError(status.reason.message);
  }
  useEffect(() => {
    void refresh();
    const clock = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => { clearInterval(clock); };
  }, []);
  const intervalMinutes = state?.settings.check_interval_minutes ?? 15;
  useEffect(() => {
    const poll = setInterval(() => void refresh(), intervalMinutes * 60000);
    return () => clearInterval(poll);
  }, [intervalMinutes]);
  async function action(fn: () => Promise<void>) {
    setBusy(true); setNote('');
    try { await fn(); setState(await api<State>('automation')); setError(''); } catch (e) { setError(e instanceof Error ? e.message : '操作失败'); }
    finally { setBusy(false); }
  }
  const countdown = (end: number) => {
    const seconds = Math.max(0, Math.floor(end - now));
    return `${Math.floor(seconds / 3600)}小时 ${Math.floor(seconds % 3600 / 60)}分 ${seconds % 60}秒`;
  };
  return <section className="st-sec active minimax-panel" style={{ padding: 24, overflowY: 'auto' }}>
    <h2>MiniMax 额度与自动创作</h2>
    <p>读取官方 Token Plan 额度；使用已有密钥，无需安装 CLI。检查间隔：{intervalMinutes} 分钟。</p>
    {error && <p role="alert" style={{ color: '#c55742' }}>{error}</p>}
    <button disabled={busy} onClick={() => void refresh()}>刷新额度</button>
    {checked > 0 && <small style={{ marginLeft: 12 }}>更新于 {new Date(checked * 1000).toLocaleTimeString()} {now - checked > intervalMinutes * 60 + 60 ? '（数据已过期，等待刷新）' : ''}</small>}
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(210px,1fr))', gap: 12, margin: '16px 0 24px' }}>
      {rows.map(row => <article key={row.model} style={{ padding: 16, border: '1px solid #8885', borderRadius: 12 }}>
        <strong>{row.model === 'general' ? '共享额度 · general' : row.model}</strong>
        <div style={{ fontSize: 30, margin: '8px 0' }}>{row.remaining_percent === null ? '未知' : `${row.remaining_percent}%`} <small style={{ fontSize: 13 }}>剩余</small></div>
        <progress max={100} value={row.remaining_percent ?? 0} style={{ width: '100%' }} />
        <p>距重置 {countdown(row.reset_at)}</p>
        <small>周剩余：{row.weekly_remaining_percent === null ? '未提供' : `${row.weekly_remaining_percent}%`}</small>
      </article>)}
    </div>
    {settings && <div style={{ display: 'grid', gap: 14 }}>
      <h3>自动运行规则</h3>
      <label><input type="checkbox" checked={settings.enabled} onChange={e => setSettings({ ...settings, enabled: e.target.checked })} /> 启用自动创作</label>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        <label>每 <input aria-label="检查间隔分钟数" type="number" min={1} max={1440} style={{ width: 65 }} value={settings.check_interval_minutes} onChange={e => setSettings({ ...settings, check_interval_minutes: Number(e.target.value) })} /> 分钟检查一次</label>
        <label>监测额度 <select value={settings.quota_model} onChange={e => setSettings({ ...settings, quota_model: e.target.value })}>
          {Array.from(new Set([settings.quota_model, ...rows.map(r => r.model)])).map(name => <option key={name}>{name}</option>)}
        </select></label>
        <label>距重置不足 <input aria-label="重置前分钟数" type="number" min={5} max={180} style={{ width: 65 }} value={settings.minutes_before_reset} onChange={e => setSettings({ ...settings, minutes_before_reset: Number(e.target.value) })} /> 分钟</label>
        <label>且剩余超过 <input aria-label="剩余额度阈值" type="number" min={0} max={99} style={{ width: 65 }} value={settings.remaining_threshold} onChange={e => setSettings({ ...settings, remaining_threshold: Number(e.target.value) })} /> %</label>
      </div>
      {settings.check_interval_minutes > settings.minutes_before_reset && <small>检查间隔大于触发窗口，可能错过本轮自动运行。</small>}
      <label><input type="checkbox" checked={settings.trending_enabled} onChange={e => setSettings({ ...settings, trending_enabled: e.target.checked })} /> 队列为空时，按以下方向寻找热点</label>
      <textarea aria-label="热点方向" rows={3} value={settings.trending_prompt} placeholder="例如：关注工程数字化和 BIM，找一个近期热点，写一篇带来源的小红书草稿。" onChange={e => setSettings({ ...settings, trending_prompt: e.target.value })} />
      <small>排队内容优先；每个重置周期最多启动一次。只保存本地草稿，不自动发布。关闭开关停止后续启动；已启动任务有最长 15 分钟的运行时限。</small>
      <button disabled={busy} onClick={() => void action(async () => { await api('automation', 'PUT', settings); setNote('规则已保存'); })}>保存规则</button>
      {note && <p role="status">{note}</p>}
      {state?.error && <p role="alert">后台：{state.error}</p>}
      <h3>预先安排内容</h3>
      <textarea aria-label="待运行内容" rows={4} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="先把要做的事写在这里。明确主题、受众、形式和输出要求，满足额度条件时自动执行。" />
      <button disabled={busy || !prompt.trim()} onClick={() => void action(async () => { await api('tasks', 'POST', { prompt }); setPrompt(''); setNote('已加入队列'); })}>加入待运行队列</button>
      {state?.tasks.filter(task => ['queued', 'running'].includes(task.state)).map(task => <div key={task.id} style={{ borderBottom: '1px solid #8884', padding: 10 }}>
        <b>{labels[task.state]}</b> · {task.prompt}
        {task.state === 'queued' && <button disabled={busy} style={{ marginLeft: 12 }} onClick={() => void action(async () => { await api('tasks/' + task.id, 'DELETE'); })}>取消</button>}
      </div>)}
      <h3>执行记录</h3>
      {!state?.runs.length && <p>尚无执行记录。保存内容并启用规则后，等待额度条件满足。</p>}
      {state?.runs.map(run => <details key={run.id} style={{ border: '1px solid #8884', padding: 12, borderRadius: 8 }}>
        <summary>{labels[run.state] || run.state} · {new Date(run.started * 1000).toLocaleString()} · {run.prompt.slice(0, 65)}</summary>
        <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{run.result || '正在运行…'}</pre>
      </details>)}
    </div>}
  </section>;
}
